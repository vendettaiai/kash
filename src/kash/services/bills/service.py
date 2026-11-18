"""Bill workflows: scan history for bills, review candidates, match payments, roll forward.

- ``scan_for_bills``: run detection and keep the review list up to date.
- ``accept_candidate`` / ``dismiss_candidate``: the review workflow.
- ``refresh_bills``: link payments to due dates (paid / missed / amount changed)
  and keep one upcoming due date per active bill.
- ``after_import_hook``: both of the above, run after every bank sync.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from kash.domain.models import (
    Bill,
    BillDetectionCandidate,
    BillFrequency,
    BillOccurrence,
    BillOccurrenceStatus,
    BillStatus,
    CandidateStatus,
    Transaction,
)
from kash.domain.protocols import DataProvider
from kash.services.bills.detection import AMOUNT_BAND, DetectedBill, detect_recurring
from kash.services.bills.merchant import (
    description_words,
    merchant_tokens,
    normalize_merchant,
    same_merchant,
    transaction_merchant,
)
from kash.services.dates import next_due, previous_due
from kash.services.ledger import counts_as_cash_flow

HISTORY_DAYS = 400
MAX_SCAN = 100_000
PRICE_CHANGE = Decimal("0.05")  # a fixed bill paid >5% off its expected amount "changed"
LATE_PAYMENT_DAYS = 60  # how long a missed due date can still be paid by a late-arriving payment

# Default payment windows (days early, days late) around a due date
_WINDOWS = {
    BillFrequency.WEEKLY: (2, 3),
    BillFrequency.BIWEEKLY: (3, 4),
    BillFrequency.SEMI_MONTHLY: (3, 4),
    BillFrequency.MONTHLY: (5, 7),
    BillFrequency.QUARTERLY: (10, 14),
    BillFrequency.ANNUAL: (14, 21),
}
DEFAULT_AMOUNT_TOLERANCE = 25.0  # percent, for fixed-amount bills


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _similar_amount(a: Decimal, b: Decimal, tolerance: float = AMOUNT_BAND) -> bool:
    a, b = abs(a), abs(b)
    if not a or not b:
        return False
    return abs(a - b) / max(a, b) <= Decimal(str(tolerance))


def same_bill(
    key: str, amount: Decimal, other_key: str, other_amount: Decimal, variable: bool
) -> bool:
    """Same merchant and (for fixed amounts) a similar price."""
    return same_merchant(key, other_key) and (variable or _similar_amount(amount, other_amount))


def dismissal_key(merchant_key: str, amount: Decimal) -> str:
    return f"{merchant_key}|{abs(amount):.2f}"


def _is_dismissed(detected: DetectedBill, dismissals: set[str]) -> bool:
    for entry in dismissals:
        key, _, amount = entry.partition("|")
        if not same_merchant(key, detected.merchant_key):
            continue
        if (
            not amount
            or detected.is_variable
            or _similar_amount(Decimal(amount), detected.average_amount)
        ):
            return True
    return False


async def _spending_history(
    provider: DataProvider, user_id: UUID, today: date
) -> list[Transaction]:
    categories = await provider.categories.list_categories(user_id, include_hidden=True)
    transfer_ids = {c.id for c in categories if c.is_transfer}
    txs = await provider.transactions.list_transactions(
        user_id, start_date=today - timedelta(days=HISTORY_DAYS), limit=MAX_SCAN
    )
    return [
        t for t in txs if t.amount < 0 and not t.is_pending and counts_as_cash_flow(t, transfer_ids)
    ]


# ---------------------------------------------------------------------------
# Detection & review
# ---------------------------------------------------------------------------


@dataclass
class ScanResult:
    new: int = 0
    updated: int = 0
    removed: int = 0


async def scan_for_bills(
    provider: DataProvider, user_id: UUID, today: date | None = None
) -> ScanResult:
    """Detect recurring charges and refresh the review list.

    Skips merchants that are already bills, were accepted before, or were
    dismissed. Pending candidates that no longer qualify are removed.
    """
    today = today or date.today()
    result = ScanResult()
    detected = detect_recurring(await _spending_history(provider, user_id, today), today)

    bills = [b for b in await provider.bills.list_bills(user_id) if b.status != BillStatus.ARCHIVED]
    dismissals = await provider.bills.list_dismissed_merchants(user_id)
    pending = await provider.bills.list_detection_candidates(
        user_id, CandidateStatus.PENDING_REVIEW
    )
    accepted = await provider.bills.list_detection_candidates(user_id, CandidateStatus.ACCEPTED)

    kept: set[UUID] = set()
    for d in detected:
        if _is_dismissed(d, dismissals):
            continue
        if any(
            same_bill(
                normalize_merchant(b.merchant_name),
                b.expected_amount,
                d.merchant_key,
                d.average_amount,
                b.is_variable_amount or d.is_variable,
            )
            for b in bills
        ):
            continue
        if any(
            same_bill(
                c.normalized_merchant,
                c.average_amount,
                d.merchant_key,
                d.average_amount,
                c.is_variable or d.is_variable,
            )
            for c in accepted
        ):
            continue

        existing = next(
            (
                c
                for c in pending
                if c.id not in kept
                and same_bill(
                    c.normalized_merchant,
                    c.average_amount,
                    d.merchant_key,
                    d.average_amount,
                    c.is_variable or d.is_variable,
                )
            ),
            None,
        )
        candidate = existing or BillDetectionCandidate(
            id=uuid4(),
            user_id=user_id,
            merchant_name=d.name,
            normalized_merchant=d.merchant_key,
            detected_frequency=d.frequency,
            average_amount=d.average_amount,
            last_amount=d.last_amount,
            confidence_score=Decimal(d.confidence),
            next_estimated_date=d.next_date,
        )
        candidate.merchant_name = d.name
        candidate.normalized_merchant = d.merchant_key
        candidate.detected_frequency = d.frequency
        candidate.average_amount = d.average_amount
        candidate.last_amount = d.last_amount
        candidate.is_variable = d.is_variable
        candidate.confidence_score = Decimal(d.confidence)
        candidate.evidence_transaction_ids = d.transaction_ids
        candidate.suggested_category_id = d.category_id
        candidate.suggested_account_id = d.account_id
        candidate.next_estimated_date = d.next_date
        await provider.bills.save_candidate(candidate)
        kept.add(candidate.id)
        if existing:
            result.updated += 1
        else:
            result.new += 1

    for stale in pending:
        if stale.id not in kept:
            await provider.bills.delete_candidate(stale.id)
            result.removed += 1
    return result


async def candidate_reasons(provider: DataProvider, candidate: BillDetectionCandidate) -> list[str]:
    """Plain-English "why we think this is a bill", rebuilt from the evidence."""
    txs = [
        tx
        for tx_id in candidate.evidence_transaction_ids
        if (tx := await provider.transactions.get_transaction(tx_id)) is not None
    ]
    detected = detect_recurring(txs) if txs else []
    if detected:
        return max(detected, key=lambda d: len(d.transaction_ids)).reasons
    return [
        f"{len(candidate.evidence_transaction_ids)} similar charges from {candidate.merchant_name}"
    ]


async def accept_candidate(
    provider: DataProvider,
    candidate_id: UUID,
    name: str | None = None,
    amount: Decimal | None = None,
) -> Bill | None:
    """Turn a detected candidate into a bill with its first upcoming due date."""
    candidate = await provider.bills.get_candidate(candidate_id)
    if candidate is None or candidate.status != CandidateStatus.PENDING_REVIEW:
        return None
    expected = (
        amount
        if amount is not None
        else (candidate.average_amount if candidate.is_variable else candidate.last_amount)
    )
    bill = await provider.bills.create_bill(
        Bill(
            id=uuid4(),
            user_id=candidate.user_id,
            account_id=candidate.suggested_account_id,
            category_id=candidate.suggested_category_id,
            name=(name or "").strip() or candidate.merchant_name,
            merchant_name=candidate.normalized_merchant,
            expected_amount=abs(expected),
            is_variable_amount=candidate.is_variable,
            frequency=candidate.detected_frequency,
            due_day=candidate.next_estimated_date.day,
            next_due_date=candidate.next_estimated_date,
            detected_automatically=True,
        )
    )
    await provider.bills.update_candidate_status(candidate.id, CandidateStatus.ACCEPTED)
    return bill


async def dismiss_candidate(provider: DataProvider, candidate_id: UUID) -> bool:
    """Hide a candidate and don't suggest this merchant (at this price) again."""
    candidate = await provider.bills.get_candidate(candidate_id)
    if candidate is None:
        return False
    await provider.bills.add_dismissal(
        candidate.user_id, dismissal_key(candidate.normalized_merchant, candidate.average_amount)
    )
    await provider.bills.update_candidate_status(candidate.id, CandidateStatus.DISMISSED)
    return True


# ---------------------------------------------------------------------------
# Payments & rolling due dates
# ---------------------------------------------------------------------------


@dataclass
class MatchSettings:
    """From the active "recurring" matching rule, or defaults when there is none."""

    enabled: bool = True
    days_early: int | None = None
    days_late: int | None = None
    same_account: bool = True
    exact_amount: bool = False
    tolerance_pct: float = DEFAULT_AMOUNT_TOLERANCE

    def window(self, frequency: BillFrequency) -> tuple[int, int]:
        early, late = _WINDOWS[frequency]
        return (
            early if self.days_early is None else self.days_early,
            late if self.days_late is None else self.days_late,
        )


async def match_settings(provider: DataProvider, user_id: UUID) -> MatchSettings:
    rules = await provider.rules.list_rules(user_id, rule_type="recurring")
    if not rules:
        return MatchSettings()
    active = sorted((r for r in rules if r.is_active), key=lambda r: (r.priority, str(r.id)))
    if not active:
        return MatchSettings(enabled=False)
    cfg: dict[str, Any] = active[0].config or {}
    mode = cfg.get("amount_match_mode")
    return MatchSettings(
        days_early=int(cfg["days_early"]) if "days_early" in cfg else None,
        days_late=int(cfg["days_late"]) if "days_late" in cfg else None,
        same_account=bool(cfg.get("same_account", True)),
        exact_amount=mode == "exact",
        tolerance_pct=float(cfg.get("amount_tolerance_percent", DEFAULT_AMOUNT_TOLERANCE))
        if mode in ("within_percent", "percent")
        else DEFAULT_AMOUNT_TOLERANCE,
    )


def _pays_bill(tx: Transaction, bill: Bill, key: list[str], settings: MatchSettings) -> bool:
    if not key:
        return False
    # The bill's words lead the payee (the usual case), or all appear anywhere in the
    # payee or bank text in any order ("ZELLE MARTIN" pays "MARTIN CARTER ... - ZELLE").
    tokens = merchant_tokens(transaction_merchant(tx))
    if tokens[: len(key)] != key and not set(key) <= description_words(
        tx.merchant_name, tx.clean_description, tx.original_description
    ):
        return False
    if settings.same_account and bill.account_id and tx.account_id != bill.account_id:
        return False
    if bill.is_variable_amount:
        return True
    paid, expected = abs(tx.amount), abs(bill.expected_amount)
    if settings.exact_amount:
        return paid == expected
    return expected > 0 and float(abs(paid - expected) / expected) * 100 <= settings.tolerance_pct


def _find_payment(
    history: list[Transaction],
    used: set[UUID],
    bill: Bill,
    key: list[str],
    settings: MatchSettings,
    due: date,
) -> Transaction | None:
    """The unused payment for this bill closest to ``due``, inside its window."""
    if not settings.enabled:
        return None
    early, late = settings.window(bill.frequency)
    lo, hi = due - timedelta(days=early), due + timedelta(days=late)
    hits = [
        t
        for t in history
        if lo <= t.date <= hi and t.id not in used and _pays_bill(t, bill, key, settings)
    ]
    return min(hits, key=lambda t: abs((t.date - due).days), default=None)


def _record_payment(occ: BillOccurrence, payment: Transaction) -> None:
    occ.status = BillOccurrenceStatus.PAID
    occ.matched_transaction_id = payment.id
    occ.paid_amount = abs(payment.amount)
    occ.paid_at = datetime.combine(payment.date, time(12), tzinfo=UTC)


@dataclass
class RefreshResult:
    paid: int = 0
    missed: int = 0
    created: int = 0


async def refresh_bills(
    provider: DataProvider, user_id: UUID, today: date | None = None
) -> RefreshResult:
    """Match payments to due dates and keep one upcoming due date per active bill.

    For each open due date: a matching payment inside the window marks it paid
    (and updates a fixed bill's price if it changed); a closed window with no
    payment marks it missed. Then the next due date is created from the bill's
    frequency, repeating until the bill has an open due date again. A recently
    missed due date is paid after all if its payment shows up late, as it does
    when the bank stopped syncing while the window closed.
    """
    today = today or date.today()
    result = RefreshResult()
    bills = await provider.bills.list_bills(user_id, status=BillStatus.ACTIVE)
    if not bills:
        return result
    settings = await match_settings(provider, user_id)
    history = sorted(await _spending_history(provider, user_id, today), key=lambda t: t.date)

    occurrences = {b.id: await provider.bills.list_occurrences(b.id) for b in bills}
    used = {
        o.matched_transaction_id
        for occs in occurrences.values()
        for o in occs
        if o.matched_transaction_id
    }

    for bill in bills:
        before = (bill.expected_amount, bill.next_due_date)
        occs = sorted(occurrences[bill.id], key=lambda o: o.due_date)
        key = merchant_tokens(bill.merchant_name)
        anchor = bill.due_day
        for _ in range(120):  # safety bound (e.g. a weekly bill untouched for 2 years)
            upcoming = [o for o in occs if o.status == BillOccurrenceStatus.UPCOMING]
            if not upcoming:
                last_due = occs[-1].due_date if occs else None
                due = next_due(last_due, bill.frequency, anchor) if last_due else bill.next_due_date
                occ = BillOccurrence(
                    id=uuid4(), bill_id=bill.id, due_date=due, expected_amount=bill.expected_amount
                )
                await provider.bills.save_occurrence(occ)
                occs.append(occ)
                result.created += 1
                continue

            occ = upcoming[0]
            hi = occ.due_date + timedelta(days=settings.window(bill.frequency)[1])
            payment = _find_payment(history, used, bill, key, settings, occ.due_date)

            if payment is not None:
                _record_payment(occ, payment)
                used.add(payment.id)
                await provider.bills.save_occurrence(occ)
                result.paid += 1
                paid_amount = abs(payment.amount)
                if not bill.is_variable_amount and not _similar_amount(
                    paid_amount, bill.expected_amount, float(PRICE_CHANGE)
                ):
                    bill.expected_amount = paid_amount  # price changed: expect the new one
            elif today > hi:
                occ.status = BillOccurrenceStatus.MISSED
                await provider.bills.save_occurrence(occ)
                result.missed += 1
            else:
                break

        for occ in occs:
            if occ.status != BillOccurrenceStatus.MISSED:
                continue
            if occ.due_date < today - timedelta(days=LATE_PAYMENT_DAYS):
                continue
            payment = _find_payment(history, used, bill, key, settings, occ.due_date)
            if payment is not None:
                _record_payment(occ, payment)
                used.add(payment.id)
                await provider.bills.save_occurrence(occ)
                result.paid += 1

        # A new bill starts at its next due date; record the payment just before it
        # (e.g. this month's charge on a bill added from detection) so it shows as paid.
        if all(o.status == BillOccurrenceStatus.UPCOMING for o in occs):
            prev = previous_due(occs[0].due_date, bill.frequency, anchor)
            payment = _find_payment(history, used, bill, key, settings, prev)
            if payment is not None:
                occ = BillOccurrence(
                    id=uuid4(), bill_id=bill.id, due_date=prev, expected_amount=bill.expected_amount
                )
                _record_payment(occ, payment)
                used.add(payment.id)
                await provider.bills.save_occurrence(occ)
                occs.insert(0, occ)
                result.paid += 1

        open_occ = next(o for o in occs if o.status == BillOccurrenceStatus.UPCOMING)
        if bill.is_variable_amount:
            paid = [o.paid_amount for o in occs if o.paid_amount][-3:]
            if paid:
                bill.expected_amount = (sum(paid, Decimal("0")) / len(paid)).quantize(
                    Decimal("0.01")
                )
        if open_occ.expected_amount != bill.expected_amount:
            open_occ.expected_amount = bill.expected_amount
            await provider.bills.save_occurrence(open_occ)
        bill.next_due_date = open_occ.due_date
        if (bill.expected_amount, bill.next_due_date) != before:
            await provider.bills.update_bill(bill)
    return result


@dataclass
class MonthBill:
    """One due date in the month: paid, still to pay, or overdue."""

    bill: Bill
    due_date: date
    amount: Decimal
    status: str  # "paid" | "upcoming" | "overdue"
    occurrence_id: UUID | None = None  # None for projected later due dates this month


@dataclass
class MonthProgress:
    """Bills due in one calendar month: how much is paid and how much is left."""

    month_start: date
    items: list[MonthBill]

    @property
    def paid(self) -> Decimal:
        return sum((i.amount for i in self.items if i.status == "paid"), Decimal("0"))

    @property
    def remaining(self) -> Decimal:
        return sum((i.amount for i in self.items if i.status != "paid"), Decimal("0"))

    @property
    def overdue(self) -> Decimal:
        return sum((i.amount for i in self.items if i.status == "overdue"), Decimal("0"))

    @property
    def total(self) -> Decimal:
        return self.paid + self.remaining

    @property
    def paid_count(self) -> int:
        return sum(1 for i in self.items if i.status == "paid")

    @property
    def pct_paid(self) -> int:
        return int(self.paid / self.total * 100) if self.total else 0


async def month_progress(
    provider: DataProvider, user_id: UUID, today: date | None = None
) -> MonthProgress:
    """Every due date of active bills in the current month.

    Paid: payments recorded for due dates this month (actual amount paid).
    Left: the open due date if it falls this month (overdue if missed or past
    due) plus later due dates this month projected from the bill's frequency
    (e.g. the remaining weeks of a weekly bill).
    """
    today = today or date.today()
    start = today.replace(day=1)
    end = next_due(start, BillFrequency.MONTHLY) - timedelta(days=1)
    items: list[MonthBill] = []

    for bill in await provider.bills.list_bills(user_id, status=BillStatus.ACTIVE):
        occs = sorted(await provider.bills.list_occurrences(bill.id), key=lambda o: o.due_date)
        for occ in occs:
            if not start <= occ.due_date <= end:
                continue
            if occ.status == BillOccurrenceStatus.PAID:
                amount = occ.paid_amount or occ.expected_amount
                items.append(MonthBill(bill, occ.due_date, amount, "paid", occ.id))
            elif occ.status == BillOccurrenceStatus.MISSED or (
                occ.status == BillOccurrenceStatus.UPCOMING and occ.due_date < today
            ):
                items.append(MonthBill(bill, occ.due_date, occ.expected_amount, "overdue", occ.id))
            elif occ.status == BillOccurrenceStatus.UPCOMING:
                items.append(MonthBill(bill, occ.due_date, occ.expected_amount, "upcoming", occ.id))

        # Due dates later this month that don't exist yet (weekly / every-2-weeks bills)
        last = max((o.due_date for o in occs), default=None)
        if last is None:
            continue
        due = next_due(last, bill.frequency, bill.due_day)
        while due <= end:
            if due >= start:
                items.append(MonthBill(bill, due, bill.expected_amount, "upcoming"))
            due = next_due(due, bill.frequency, bill.due_day)

    items.sort(key=lambda i: (i.due_date, i.bill.name))
    return MonthProgress(month_start=start, items=items)


async def mark_occurrence_paid(provider: DataProvider, occurrence_id: UUID) -> bool:
    """Manually mark a due date as paid (e.g. paid from an account Kash can't see,
    or paid late after it was marked missed)."""
    occ = await provider.bills.get_occurrence(occurrence_id)
    if occ is None or occ.status not in (
        BillOccurrenceStatus.UPCOMING,
        BillOccurrenceStatus.MISSED,
    ):
        return False
    occ.status = BillOccurrenceStatus.PAID
    occ.paid_amount = occ.expected_amount
    occ.paid_at = datetime.now(UTC)
    await provider.bills.save_occurrence(occ)
    return True


def after_import_hook(
    provider: DataProvider,
) -> Callable[[UUID, list[Transaction]], Awaitable[None]]:
    """Bank-sync callback: record bill payments, roll due dates, look for new bills."""

    async def _hook(user_id: UUID, _imported: list[Transaction]) -> None:
        await refresh_bills(provider, user_id)
        await scan_for_bills(provider, user_id)

    return _hook
