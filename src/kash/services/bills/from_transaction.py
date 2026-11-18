"""Make a bill from one transaction (TASK-P7-07; the transaction pop-up).

The draft is filled in from the transaction: the merchant's name, the amount,
the category, a next due date one period after the payment, and the merchant
key that matches later payments (the same key bill detection uses). Creating
the bill records this transaction as its first, paid due date, so the bill
starts with its history and the next charge is matched as the next payment.

If the merchant already has a bill, the draft points to it instead (no
duplicates). A dismissed bill suggestion for the merchant is cleared, and a
pending one is marked accepted.
"""

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from decimal import Decimal
from uuid import UUID, uuid4

from kash.domain.models import (
    Bill,
    BillFrequency,
    BillOccurrence,
    BillOccurrenceStatus,
    BillStatus,
    CandidateStatus,
    Transaction,
)
from kash.domain.protocols import DataProvider
from kash.services.bills.merchant import (
    display_name,
    normalize_merchant,
    same_merchant,
    transaction_merchant,
)
from kash.services.dates import next_due


@dataclass
class BillDraft:
    transaction: Transaction | None
    problem: str | None = None  # why this transaction can't become a bill
    existing: Bill | None = None  # the merchant already has this bill
    already_paid: bool = False  # this transaction is already a payment of ``existing``
    name: str = ""
    merchant: str = ""
    amount: Decimal = Decimal("0")
    category_id: UUID | None = None
    next_dues: dict[str, date] = field(default_factory=dict)  # frequency -> next due date


async def _paid_for(provider: DataProvider, user_id: UUID, tx_id: UUID) -> Bill | None:
    """The bill this transaction was already recorded as a payment for."""
    for bill in await provider.bills.list_bills(user_id):
        for occ in await provider.bills.list_occurrences(bill.id):
            if occ.matched_transaction_id == tx_id:
                return bill
    return None


async def draft(provider: DataProvider, user_id: UUID, transaction_id: UUID) -> BillDraft:
    tx = await provider.transactions.get_transaction(transaction_id)
    if tx is None or tx.user_id != user_id:
        return BillDraft(None, problem="This transaction no longer exists.")
    if tx.amount >= 0:
        return BillDraft(tx, problem="Only money going out can be a bill.")
    if tx.is_transfer or tx.matched_transfer_id or tx.is_ignored:
        return BillDraft(tx, problem="Transfers and ignored transactions can't be bills.")
    if tx.is_pending:
        return BillDraft(tx, problem="Wait until this transaction has posted.")
    merchant = normalize_merchant(transaction_merchant(tx))
    if not merchant:
        return BillDraft(tx, problem="This transaction has no merchant name to match bills by.")

    paid_for = await _paid_for(provider, user_id, tx.id)
    if paid_for is not None:
        return BillDraft(tx, existing=paid_for, already_paid=True, merchant=merchant)
    existing = next(
        (
            b
            for b in await provider.bills.list_bills(user_id)
            if b.status != BillStatus.ARCHIVED and same_merchant(b.merchant_name, merchant)
        ),
        None,
    )
    return BillDraft(
        tx,
        existing=existing,
        name=(tx.merchant_name or display_name(merchant)).strip()[:80],
        merchant=merchant,
        amount=abs(tx.amount),
        category_id=tx.category_id,
        next_dues={f.value: next_due(tx.date, f, tx.date.day) for f in BillFrequency},
    )


async def create(
    provider: DataProvider,
    user_id: UUID,
    transaction_id: UUID,
    *,
    name: str,
    amount: Decimal,
    frequency: BillFrequency,
    due: date,
    merchant: str,
    category_id: UUID | None,
    variable: bool,
) -> Bill | str:
    """Create the bill (or return why not). The transaction becomes its first payment."""
    d = await draft(provider, user_id, transaction_id)
    if d.problem or d.transaction is None:
        return d.problem or "This transaction no longer exists."
    if d.existing is not None:
        return f"{d.existing.name} is already a bill."
    tx = d.transaction
    if due <= tx.date:
        return "The next due date must be after this payment."
    key = normalize_merchant(merchant) or d.merchant

    # create_bill also adds the first upcoming due date (``due``).
    bill = await provider.bills.create_bill(
        Bill(
            id=uuid4(),
            user_id=user_id,
            account_id=tx.account_id,
            category_id=category_id,
            name=name.strip() or d.name,
            merchant_name=key,
            expected_amount=abs(amount),
            is_variable_amount=variable,
            frequency=frequency,
            due_day=due.day,
            next_due_date=due,
        )
    )
    await provider.bills.save_occurrence(
        BillOccurrence(
            id=uuid4(),
            bill_id=bill.id,
            due_date=tx.date,
            expected_amount=abs(tx.amount),
            status=BillOccurrenceStatus.PAID,
            matched_transaction_id=tx.id,
            paid_amount=abs(tx.amount),
            paid_at=datetime.combine(tx.date, time(12), tzinfo=UTC),
        )
    )

    # It's a bill now: forget "not a bill" for this merchant, settle open suggestions.
    for entry in await provider.bills.list_dismissed_merchants(user_id):
        if same_merchant(entry.partition("|")[0], key):
            await provider.bills.remove_dismissal(user_id, entry)
    for candidate in await provider.bills.list_detection_candidates(user_id):
        if same_merchant(candidate.normalized_merchant, key):
            await provider.bills.update_candidate_status(candidate.id, CandidateStatus.ACCEPTED)
    return bill
