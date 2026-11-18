"""Core Financial Logic and Double-Entry Ledger Engine.

Strictly enforces:
- Exact Decimal representation (no floating point arithmetic)
- Net worth calculations separating assets and liabilities
- Monthly cash flow (income, expenses, surplus/deficit) with transfer exclusion
- Chronological running balance rollups
- Split transaction validation and balancing
- Budget envelope tracking and percentage utilization
- Automatic transfer pair detection heuristics
"""

from collections.abc import Collection, Iterable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import NamedTuple
from uuid import UUID

from kash.domain.models import (
    Account,
    AccountCashFlow,
    AccountType,
    Budget,
    Category,
    CategoryBudgetProgress,
    CategoryGroup,
    Transaction,
    TransactionSplit,
)

FOUR_PLACES = Decimal("0.0001")
TWO_PLACES = Decimal("0.01")


class CashFlowSummary(NamedTuple):
    """Cash flow for a period.

    ``income`` = ``earned`` + ``other_income``. ``expenses`` is net spending:
    money spent minus ``refunds`` (money back that was put in a spending
    category, e.g. a store refund or a friend paying you back for groceries).
    """

    income: Decimal
    expenses: Decimal
    surplus: Decimal  # Positive is surplus, negative is deficit
    earned: Decimal = Decimal("0.0000")  # credits in Income-group categories (salary etc.)
    other_income: Decimal = Decimal("0.0000")  # uncategorized credits
    refunds: Decimal = Decimal("0.0000")  # credits in spending categories


class CashFlowKind(StrEnum):
    """How a transaction counts toward cash flow."""

    EARNED = "earned"
    OTHER_INCOME = "other_income"
    REFUND = "refund"
    EXPENSE = "expense"


class RunningBalanceEntry(NamedTuple):
    """Transaction paired with its calculated running balance."""

    transaction: Transaction
    running_balance: Decimal


def quantize_currency(amount: Decimal, places: Decimal = FOUR_PLACES) -> Decimal:
    """Quantize decimal to fixed decimal places using standard ROUND_HALF_UP."""
    return amount.quantize(places, rounding=ROUND_HALF_UP)


def calculate_net_worth(accounts: Sequence[Account]) -> Decimal:
    """Calculate total net worth across all active accounts flagged for inclusion.

    Asset accounts (checking, savings, cash, investment):
        Positive balance adds to net worth, negative balance subtracts.
    Liability accounts (credit_card, loan):
        A positive balance represents debt owed and subtracts from net worth.
        A negative balance also decreases net worth.
    """
    total = Decimal("0.0000")
    for acc in accounts:
        if not acc.is_active or not acc.include_in_net_worth:
            continue

        balance = acc.current_balance
        if acc.account_type in (AccountType.CREDIT_CARD, AccountType.LOAN):
            # For liabilities: if balance is positive (standard debt representation e.g. $1,420),
            # subtract it. If negative (e.g. -$1,420), add it (which also subtracts).
            if balance > 0:
                total -= balance
            else:
                total += balance
        else:
            # Standard assets
            total += balance

    return quantize_currency(total)


def pending_effect(account: Account, pending: Decimal) -> Decimal:
    """How much the account's pending transactions (summed: ``pending``) change the
    balance Kash shows for it.

    A bank reports the posted balance: a charge that is still pending is already
    listed, but isn't in that balance until it posts (often a few days). Kash shows
    the balance after pending, so it moves as soon as a purchase shows up. Manual
    accounts keep their own balance, so nothing changes there. A debt the bank
    reports as a positive amount owed grows with a charge (a negative amount).
    """
    if account.connection_id is None:
        return Decimal("0")
    if (
        account.account_type in (AccountType.CREDIT_CARD, AccountType.LOAN)
        and account.current_balance > 0
    ):
        return -pending
    return pending


def balances_after_pending(
    accounts: Iterable[Account], pending: Mapping[UUID, Decimal]
) -> dict[UUID, Decimal]:
    """Each account's balance including its pending transactions (``pending_effect``)."""
    return {
        a.id: a.current_balance + pending_effect(a, pending.get(a.id, Decimal("0")))
        for a in accounts
    }


def counts_as_cash_flow(
    tx: Transaction, transfer_category_ids: Collection[UUID] = frozenset()
) -> bool:
    """Whether a transaction is real income or spending.

    Money moving between your own accounts is not income or spending. A
    transaction is left out when it is ignored, marked as a transfer (or paired
    with the other leg of a transfer), or sits in a category flagged
    "treat as transfer" (e.g. Transfers, Credit Card Payment).
    """
    if tx.is_transfer or tx.is_ignored or tx.matched_transfer_id is not None:
        return False
    return tx.category_id is None or tx.category_id not in transfer_category_ids


def needs_category(tx: Transaction, transfer_category_ids: Collection[UUID] = frozenset()) -> bool:
    """Whether this is income or spending with no category yet. Pending ones wait: they
    can still change, and many get a rule's category when they post. Pair it with
    ``as_counted``: accounts that don't count never need one."""
    return (
        tx.category_id is None
        and not tx.is_pending
        and counts_as_cash_flow(tx, transfer_category_ids)
    )


def as_counted(
    tx: Transaction, account: Account | None, income_category_ids: Collection[UUID]
) -> Transaction | None:
    """The transaction if its account's "Income & spending" setting lets it count
    toward income/spending totals, else None. Categories, transfers and rules then
    decide how it counts.

    - Count everything: every transaction.
    - Count income only: money in with an Income-group category (e.g. dividends a
      rule files under "Investments & Dividends"); buys, sells and transfers never.
    - Don't count, or an inactive/unknown account: nothing.
    """
    if account is None or not account.is_active:
        return None
    if account.cash_flow == AccountCashFlow.ALL:
        return tx
    if account.cash_flow == AccountCashFlow.INCOME:
        is_income = tx.amount > 0 and tx.category_id in income_category_ids
        return tx if is_income else None
    return None


def cash_flow_transactions(
    transactions: Iterable[Transaction],
    accounts: Iterable[Account],
    income_category_ids: Collection[UUID],
) -> list[Transaction]:
    """The transactions income/spending totals use (see ``as_counted``)."""
    by_id = {a.id: a for a in accounts}
    counted = (as_counted(t, by_id.get(t.account_id), income_category_ids) for t in transactions)
    return [t for t in counted if t is not None]


def cash_flow_kind(
    tx: Transaction,
    transfer_category_ids: Collection[UUID] = frozenset(),
    income_category_ids: Collection[UUID] = frozenset(),
) -> CashFlowKind | None:
    """Classify a transaction for income/spending totals (None = not counted).

    - Transfers, ignored transactions and transfer categories: not counted.
    - Anything in an Income-group category: earned (a debit there, like a
      reversed paycheck, reduces earned).
    - Other money in: uncategorized is "other income"; in a spending category it
      is a refund that reduces that category's spending.
    - Other money out: spending.
    """
    if not counts_as_cash_flow(tx, transfer_category_ids) or tx.amount == 0:
        return None
    if tx.category_id is not None and tx.category_id in income_category_ids:
        return CashFlowKind.EARNED
    if tx.amount > 0:
        return CashFlowKind.OTHER_INCOME if tx.category_id is None else CashFlowKind.REFUND
    return CashFlowKind.EXPENSE


def calculate_cashflow(
    transactions: Sequence[Transaction],
    month_prefix: str | None = None,
    include_pending: bool = False,
    transfer_category_ids: Collection[UUID] = frozenset(),
    income_category_ids: Collection[UUID] | None = None,
) -> CashFlowSummary:
    """Compute income (earned + other), net spending, and surplus/deficit.

    - Pending transactions are skipped unless ``include_pending=True``.
    - If ``month_prefix`` ('YYYY-MM') is given, only that month is counted.
    - Without ``income_category_ids``, every credit counts as income and every
      debit as spending (no refund netting).
    """
    earned = other = refunds = spent = Decimal("0.0000")

    for tx in transactions:
        if tx.is_pending and not include_pending:
            continue
        if month_prefix is not None and tx.date.strftime("%Y-%m") != month_prefix:
            continue
        if income_category_ids is None:
            if not counts_as_cash_flow(tx, transfer_category_ids):
                continue
            if tx.amount > 0:
                other += tx.amount
            else:
                spent += abs(tx.amount)
            continue

        kind = cash_flow_kind(tx, transfer_category_ids, income_category_ids)
        if kind is CashFlowKind.EARNED:
            earned += tx.amount
        elif kind is CashFlowKind.OTHER_INCOME:
            other += tx.amount
        elif kind is CashFlowKind.REFUND:
            refunds += tx.amount
        elif kind is CashFlowKind.EXPENSE:
            spent += abs(tx.amount)

    income = quantize_currency(earned + other)
    expenses = quantize_currency(spent - refunds)
    return CashFlowSummary(
        income=income,
        expenses=expenses,
        surplus=quantize_currency(income - expenses),
        earned=quantize_currency(earned),
        other_income=quantize_currency(other),
        refunds=quantize_currency(refunds),
    )


def spending_by_category(
    transactions: Iterable[Transaction],
    transfer_category_ids: Collection[UUID] = frozenset(),
    income_category_ids: Collection[UUID] = frozenset(),
) -> dict[UUID | None, Decimal]:
    """Net spending per category (money out minus refunds). ``None`` = uncategorized."""
    totals: dict[UUID | None, Decimal] = {}
    for tx in transactions:
        kind = cash_flow_kind(tx, transfer_category_ids, income_category_ids)
        if kind is CashFlowKind.EXPENSE:
            totals[tx.category_id] = totals.get(tx.category_id, Decimal("0")) + abs(tx.amount)
        elif kind is CashFlowKind.REFUND:
            totals[tx.category_id] = totals.get(tx.category_id, Decimal("0")) - tx.amount
    return totals


def income_by_category(
    transactions: Iterable[Transaction],
    transfer_category_ids: Collection[UUID] = frozenset(),
    income_category_ids: Collection[UUID] = frozenset(),
) -> dict[UUID | None, Decimal]:
    """Income per category (Salary, Dividends, ...); ``None`` = uncategorized money in.

    Sums to ``calculate_cashflow(...).income``; a debit in an Income category
    (a reversed paycheck) reduces its category.
    """
    totals: dict[UUID | None, Decimal] = {}
    for tx in transactions:
        kind = cash_flow_kind(tx, transfer_category_ids, income_category_ids)
        if kind is CashFlowKind.EARNED:
            totals[tx.category_id] = totals.get(tx.category_id, Decimal("0")) + tx.amount
        elif kind is CashFlowKind.OTHER_INCOME:
            totals[None] = totals.get(None, Decimal("0")) + tx.amount
    return totals


def income_category_ids(
    groups: Sequence[CategoryGroup], categories: Sequence[Category]
) -> set[UUID]:
    """Categories in a group named "Income" (salary, freelance, dividends, ...)."""
    income_groups = {g.id for g in groups if g.name.strip().lower() == "income"}
    return {c.id for c in categories if c.group_id in income_groups}


def calculate_running_balances(
    starting_balance: Decimal,
    transactions: Sequence[Transaction],
    sort_chronological: bool = True,
) -> list[RunningBalanceEntry]:
    """Compute running balances for an account across a series of transactions.

    If `sort_chronological` is True, transactions are sorted by date ascending.
    Running balance at step i = running_balance_{i-1} + tx.amount.
    """
    tx_list = list(transactions)
    if sort_chronological:
        tx_list.sort(key=lambda t: (t.date, t.created_at))

    entries: list[RunningBalanceEntry] = []
    current = starting_balance
    for tx in tx_list:
        current += tx.amount
        entries.append(
            RunningBalanceEntry(transaction=tx, running_balance=quantize_currency(current))
        )

    return entries


def validate_transaction_splits(parent_amount: Decimal, splits: Sequence[TransactionSplit]) -> bool:
    """Validate that the sum of split amounts equals the parent transaction amount.

    Returns True if valid. Raises ValueError if splits do not match.
    """
    if not splits:
        return True

    total_split = sum((s.amount for s in splits), Decimal("0.0000"))
    diff = abs(parent_amount - total_split)

    if diff > FOUR_PLACES:
        raise ValueError(
            f"Split amounts total {total_split}, which does not match "
            f"parent transaction amount {parent_amount} (discrepancy: {diff})"
        )
    return True


def calculate_budget_progress(
    budgets: Sequence[Budget],
    transactions: Sequence[Transaction],
    categories: Sequence[Category],
    period: str,
) -> list[CategoryBudgetProgress]:
    """Calculate budget progress for each budgeted category in a given period ('YYYY-MM').

    - Net spending per category (debits minus refunds), ignoring transfers.
    - Budgets of 0 are treated as "no budget" and skipped.
    - Rollover balances are incorporated if `enable_rollover` is True.
    - Returns list of CategoryBudgetProgress objects sorted by spent descending.
    """
    cat_map = {c.id: c for c in categories}

    # Net spending per category for the period (refunds reduce spending)
    expenses_by_cat = spending_by_category(
        t for t in transactions if t.date.strftime("%Y-%m") == period and not t.is_pending
    )

    results: list[CategoryBudgetProgress] = []
    for b in budgets:
        if b.period != period or b.budgeted_amount <= 0:
            continue
        cat = cat_map.get(b.category_id)
        if not cat:
            continue

        budgeted = b.budgeted_amount
        if b.enable_rollover:
            budgeted += b.rollover_balance
        budgeted = quantize_currency(budgeted)

        spent = quantize_currency(
            max(expenses_by_cat.get(b.category_id, Decimal("0")), Decimal("0"))
        )
        remaining = quantize_currency(budgeted - spent)

        if budgeted > 0:
            pct_raw = (spent / budgeted) * Decimal("100")
            pct_used = quantize_currency(pct_raw, TWO_PLACES)
        else:
            pct_used = Decimal("100.00") if spent > 0 else Decimal("0.00")

        results.append(
            CategoryBudgetProgress(
                category_id=cat.id,
                category_name=cat.name,
                category_icon=cat.icon,
                category_color=cat.color,
                budgeted=budgeted,
                spent=spent,
                remaining=remaining,
                pct_used=pct_used,
            )
        )

    # Sort highest expenditure first
    results.sort(key=lambda r: r.spent, reverse=True)
    return results


def detect_transfers(
    transactions: Sequence[Transaction],
    max_days_difference: int = 3,
) -> list[tuple[Transaction, Transaction]]:
    """Heuristic transfer detection between accounts.

    Pairs a debit from Account A with a credit to Account B when:
    - Different accounts
    - Offsetting amounts: `abs(tx_debit.amount + tx_credit.amount) < 0.01`
    - Date within `max_days_difference`
    - Neither transaction is already flagged as a transfer or matched
    """
    unmatched_debits = [
        tx
        for tx in transactions
        if tx.amount < 0 and not tx.is_transfer and not tx.matched_transfer_id
    ]
    unmatched_credits = [
        tx
        for tx in transactions
        if tx.amount > 0 and not tx.is_transfer and not tx.matched_transfer_id
    ]

    matched_pairs: list[tuple[Transaction, Transaction]] = []
    used_credits: set[UUID] = set()

    for debit in unmatched_debits:
        target_amount = abs(debit.amount)
        for credit in unmatched_credits:
            if credit.id in used_credits:
                continue
            if credit.account_id == debit.account_id:
                # Same account is not an inter-account transfer
                continue

            # Check matching amount
            if abs(credit.amount - target_amount) <= FOUR_PLACES:
                # Check date proximity
                day_delta = abs((debit.date - credit.date).days)
                if day_delta <= max_days_difference:
                    matched_pairs.append((debit, credit))
                    used_credits.add(credit.id)
                    break

    return matched_pairs
