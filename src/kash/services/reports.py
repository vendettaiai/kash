"""Reports across months: income vs. expenses by month and net worth over time.

Net worth history has no stored ledger of balances to read, so each account's
balance on a past day is worked out backwards: start from the nearest later
saved snapshot (or today's balance) and undo the transactions in between.
Snapshots are saved once a day per account (``record_snapshots``), which also
captures changes transactions don't explain, like investment gains.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from uuid import UUID

from kash.domain.models import Account, BalanceSnapshot, Transaction
from kash.domain.protocols import DataProvider
from kash.services.dates import shift_months
from kash.services.ledger import calculate_cashflow, calculate_net_worth, pending_effect

WEEKLY_POINTS_UP_TO_DAYS = 92  # shorter ranges get a point per week, longer per month


@dataclass
class MonthFlow:
    """Income and spending in one calendar month (clipped to the report range)."""

    month: date  # first of the month
    income: Decimal
    expenses: Decimal

    @property
    def net(self) -> Decimal:
        return self.income - self.expenses


def _month_starts(start: date, end: date) -> list[date]:
    months, month = [], start.replace(day=1)
    while month <= end:
        months.append(month)
        month = shift_months(month, 1)
    return months


def monthly_flows(
    txs: Sequence[Transaction],
    start: date,
    end: date,
    transfer_category_ids: set[UUID],
    income_category_ids: set[UUID],
) -> list[MonthFlow]:
    """Income vs. expenses for each month between ``start`` and ``end``."""
    buckets: dict[date, list[Transaction]] = {m: [] for m in _month_starts(start, end)}
    for tx in txs:
        if start <= tx.date <= end and not tx.is_pending:
            buckets[tx.date.replace(day=1)].append(tx)
    flows = []
    for month, month_txs in buckets.items():
        flow = calculate_cashflow(
            month_txs,
            transfer_category_ids=transfer_category_ids,
            income_category_ids=income_category_ids,
        )
        flows.append(MonthFlow(month, flow.income, flow.expenses))
    return flows


def series_dates(start: date, end: date, today: date) -> list[date]:
    """Days to plot: weekly for short ranges, month ends for long ones, and the last day."""
    end = min(end, today)
    if end < start:
        return []
    if (end - start).days <= WEEKLY_POINTS_UP_TO_DAYS:
        days = [start + timedelta(days=7 * i) for i in range((end - start).days // 7 + 1)]
    else:
        days = [shift_months(m, 1) - timedelta(days=1) for m in _month_starts(start, end)]
        days = [start, *(d for d in days if start < d < end)]
    if days[-1] != end:
        days.append(end)
    return days


def balance_on(
    account: Account,
    day: date,
    txs: Sequence[Transaction],
    snapshots: Sequence[BalanceSnapshot],
    today: date,
) -> Decimal:
    """The account's balance at the end of ``day``.

    ``txs`` and ``snapshots`` are this account's, oldest first.
    """
    anchor_day, balance = today, account.current_balance
    for snap in snapshots:
        if snap.date >= day:
            anchor_day, balance = snap.date, snap.balance
            break
    undo = sum(
        (t.amount for t in txs if day < t.date <= anchor_day and not t.is_pending), Decimal("0")
    )
    return balance - undo


def _by_account(
    accounts: Sequence[Account],
    txs: Sequence[Transaction],
    snapshots: Sequence[BalanceSnapshot],
) -> tuple[dict[UUID, list[Transaction]], dict[UUID, list[BalanceSnapshot]]]:
    """Each account's transactions and snapshots, oldest first."""
    by_txs: dict[UUID, list[Transaction]] = {a.id: [] for a in accounts}
    for tx in sorted(txs, key=lambda t: t.date):
        if tx.account_id in by_txs:
            by_txs[tx.account_id].append(tx)
    by_snaps: dict[UUID, list[BalanceSnapshot]] = {a.id: [] for a in accounts}
    for snap in sorted(snapshots, key=lambda s: s.date):
        if snap.account_id in by_snaps:
            by_snaps[snap.account_id].append(snap)
    return by_txs, by_snaps


def net_worth_series(
    accounts: Sequence[Account],
    txs: Sequence[Transaction],
    snapshots: Sequence[BalanceSnapshot],
    days: Sequence[date],
    today: date,
) -> list[tuple[date, Decimal]]:
    """Net worth (assets − debts, same rules as today's figure) on each of ``days``."""
    included = [a for a in accounts if a.is_active and a.include_in_net_worth]
    by_txs, by_snaps = _by_account(included, txs, snapshots)
    series = []
    for day in days:
        on_day = [
            a.model_copy(
                update={"current_balance": balance_on(a, day, by_txs[a.id], by_snaps[a.id], today)}
            )
            for a in included
        ]
        series.append((day, calculate_net_worth(on_day)))
    return series


def balance_series(
    accounts: Sequence[Account],
    txs: Sequence[Transaction],
    snapshots: Sequence[BalanceSnapshot],
    days: Sequence[date],
    today: date,
    pending: Mapping[UUID, Decimal] | None = None,
) -> list[tuple[date, Decimal]]:
    """The summed balance of ``accounts`` at the end of each of ``days``
    (e.g. the dashboard's banking balance). Today's includes ``pending``, each
    account's pending total (``pending_effect``), like the balances on Accounts."""
    by_txs, by_snaps = _by_account(accounts, txs, snapshots)
    pending = pending or {}

    def on(account: Account, day: date) -> Decimal:
        balance = balance_on(account, day, by_txs[account.id], by_snaps[account.id], today)
        if day == today:
            balance += pending_effect(account, pending.get(account.id, Decimal("0")))
        return balance

    return [(day, sum((on(a, day) for a in accounts), Decimal("0"))) for day in days]


async def record_snapshots(
    provider: DataProvider, user_id: UUID, today: date | None = None
) -> None:
    """Save today's balance of every active account (one row per account per day)."""
    today = today or date.today()
    for account in await provider.accounts.list_accounts(user_id):
        await provider.accounts.save_balance_snapshot(
            BalanceSnapshot(account_id=account.id, date=today, balance=account.current_balance)
        )


@dataclass
class LineChart:
    """An SVG line in a 100 × 40 box (stretched to fit), plus its value range."""

    path: str
    area: str
    low: Decimal
    high: Decimal
    points: list[tuple[float, float, date, Decimal]]  # x, y, day, value


def line_chart(series: Sequence[tuple[date, Decimal]], height: float = 40) -> LineChart | None:
    """Scale a series into chart coordinates (None with fewer than two points)."""
    if len(series) < 2:
        return None
    values = [v for _, v in series]
    low, high = min(values), max(values)
    span = (high - low) or abs(high) or Decimal("1")
    pad = height * 0.08
    points = []
    for i, (day, value) in enumerate(series):
        x = 100 * i / (len(series) - 1)
        y = pad + (height - 2 * pad) * float((high - value) / span)
        points.append((round(x, 2), round(y, 2), day, value))
    path = "M" + " L".join(f"{x},{y}" for x, y, _, _ in points)
    area = f"{path} L100,{height} L0,{height} Z"
    return LineChart(path=path, area=area, low=low, high=high, points=points)
