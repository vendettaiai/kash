"""What each alert watches for, and the words it uses.

Each rule looks at the household's data as it is now and lists everything that
qualifies; the checker works out what's new for each person. A rule's key decides when it
can fire again, so a rule never needs to remember anything. Text names a bill or account
but never an amount: notifications can appear on a locked phone (ADR-021).
"""

import re
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from kash.domain.models import (
    NO_DESCRIPTION,
    Account,
    AccountType,
    BankConnection,
    BankConnectionStatus,
    Bill,
    BillOccurrence,
    BillOccurrenceStatus,
    BillStatus,
    Transaction,
    TransactionSource,
)
from kash.domain.protocols import DataProvider
from kash.owner import OWNER_USER_ID
from kash.services.ledger import (
    as_counted,
    balances_after_pending,
    counts_as_cash_flow,
    income_category_ids,
    needs_category,
)
from kash.services.notifications.prefs import NotificationPrefs
from kash.services.notifications.push import Message
from kash.services.scheduler import Schedule

BILL_LOOKBACK_DAYS = 60  # older missed bills stay quiet, so turning this on doesn't flood
CATEGORY_LOOKBACK_DAYS = 30
STALL_DAYS = 3  # a bank's data this old means it stopped updating (see stalled_banks)
LARGE_TX_DAYS = 7  # matches how far back each sync re-reads, so late postings still count
FIRST_IMPORT = timedelta(minutes=10)  # what a new account brings in is history, not news
STALE_BALANCE_DAYS = 5
BANKING_TYPES = (AccountType.CHECKING, AccountType.SAVINGS, AccountType.CASH)
MIN_SYNC_GRACE = timedelta(hours=6)
UNSCHEDULED_SYNC_GRACE = timedelta(hours=24)
MAX_TRANSACTIONS = 100_000
MAX_NAMES = 3
TTL_DAILY = 24 * 3600
TTL_SYNC = 6 * 3600


@dataclass(frozen=True)
class Alert:
    key: str  # told once per key (see Rule)
    name: str  # what a grouped message lists
    text: str  # the message when this is the only one


class Facts:
    """The household's data during one check, read once however many people want it."""

    def __init__(self, provider: DataProvider, schedule: Schedule, now: datetime):
        self.provider = provider
        self.schedule = schedule
        self.now = now
        self.today = now.date()
        self._loaded: dict[str, Any] = {}

    async def _once(self, name: str, load: Callable[[], Awaitable[Any]]) -> Any:
        if name not in self._loaded:
            self._loaded[name] = await load()
        return self._loaded[name]

    async def bills(self) -> list[tuple[Bill, list[BillOccurrence]]]:
        async def load() -> list[tuple[Bill, list[BillOccurrence]]]:
            bills = await self.provider.bills.list_bills(OWNER_USER_ID, status=BillStatus.ACTIVE)
            return [(b, await self.provider.bills.list_occurrences(b.id)) for b in bills]

        result: list[tuple[Bill, list[BillOccurrence]]] = await self._once("bills", load)
        return result

    async def accounts(self) -> list[Account]:
        result: list[Account] = await self._once(
            "accounts", lambda: self.provider.accounts.list_accounts(OWNER_USER_ID)
        )
        return result

    async def connections(self) -> list[BankConnection]:
        result: list[BankConnection] = await self._once(
            "connections",
            lambda: self.provider.bank_connections.list_connections(OWNER_USER_ID),
        )
        return result

    async def _category_ids(self) -> tuple[set[UUID], set[UUID]]:
        async def load() -> tuple[set[UUID], set[UUID]]:
            categories = await self.provider.categories.list_categories(
                OWNER_USER_ID, include_hidden=True
            )
            groups = await self.provider.categories.list_groups(OWNER_USER_ID)
            return income_category_ids(groups, categories), {
                c.id for c in categories if c.is_transfer
            }

        result: tuple[set[UUID], set[UUID]] = await self._once("category_ids", load)
        return result

    async def counted_transactions(self) -> tuple[list[Transaction], set[UUID]]:
        """Recent transactions that count toward income or spending, and the transfer
        categories; every rule that looks at transactions looks at these."""

        async def load() -> list[Transaction]:
            income, _ = await self._category_ids()
            accounts = {a.id: a for a in await self.accounts()}
            found = await self.provider.transactions.list_transactions(
                OWNER_USER_ID,
                start_date=self.today - timedelta(days=CATEGORY_LOOKBACK_DAYS),
                limit=MAX_TRANSACTIONS,
            )
            return [t for t in found if as_counted(t, accounts.get(t.account_id), income)]

        transactions: list[Transaction] = await self._once("transactions", load)
        return transactions, (await self._category_ids())[1]

    async def balances(self) -> dict[UUID, Decimal]:
        """Each account's balance as the app shows it, pending transactions included."""

        async def load() -> dict[UUID, Decimal]:
            pending = await self.provider.transactions.pending_totals(OWNER_USER_ID)
            return balances_after_pending(await self.accounts(), pending)

        result: dict[UUID, Decimal] = await self._once("balances", load)
        return result


def _aware(value: datetime) -> datetime:
    """SQLite gives back UTC times without a zone."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _days(count: int) -> str:
    return "1 day" if count == 1 else f"{count} days"


# --- Bills ---------------------------------------------------------------------------


def _reported_on(account: Account, connection: BankConnection | None) -> datetime | None:
    """When the bank last spoke for this account: its own date, else our last sync."""
    return account.balance_as_of or (connection.last_synced_at if connection else None)


async def _bank_data_reaches(facts: Facts, bill: Bill, day: date) -> bool:
    """Whether a payment made by ``day`` could have been seen. If the bank stopped
    updating, "no payment" means nothing, so the alert waits rather than cry wolf.
    Banks report once a day at a time of their choosing, so yesterday's data will do."""
    linked = [a for a in await facts.accounts() if a.connection_id is not None]
    if bill.account_id is not None:
        account = next((a for a in await facts.accounts() if a.id == bill.account_id), None)
        if account is None or account.connection_id is None:
            return True  # kept by hand, so there's nothing to wait for
        linked = [account]
    connections = {c.id: c for c in await facts.connections()}
    for account in linked:
        reported = _reported_on(account, connections.get(account.connection_id))  # type: ignore[arg-type]
        if reported is None or _aware(reported).date() < day - timedelta(days=1):
            return False
    return True


async def bills_overdue(facts: Facts, prefs: NotificationPrefs) -> list[Alert]:
    alerts = []
    for bill, occurrences in await facts.bills():
        for occ in occurrences:
            if occ.status not in (BillOccurrenceStatus.UPCOMING, BillOccurrenceStatus.MISSED):
                continue
            late = (facts.today - occ.due_date).days
            if not prefs.overdue_days <= late <= BILL_LOOKBACK_DAYS:
                continue
            if await _bank_data_reaches(facts, bill, occ.due_date + timedelta(prefs.overdue_days)):
                text = f"{bill.name} is {_days(late)} overdue"
                alerts.append((occ.due_date, Alert(f"{bill.id}:{occ.due_date}", bill.name, text)))
    # Newest first: a bill overdue twice is described by its latest due date.
    return [alert for _, alert in sorted(alerts, key=lambda a: a[0], reverse=True)]


async def bills_due(facts: Facts, prefs: NotificationPrefs) -> list[Alert]:
    alerts = []
    for bill, occurrences in await facts.bills():
        for occ in occurrences:
            left = (occ.due_date - facts.today).days
            if occ.status != BillOccurrenceStatus.UPCOMING or not 0 <= left <= prefs.due_days:
                continue
            when = {0: "today", 1: "tomorrow"}.get(left, f"in {left} days")
            alerts.append(
                Alert(f"{bill.id}:{occ.due_date}", bill.name, f"{bill.name} is due {when}")
            )
    return alerts


# --- Sync ----------------------------------------------------------------------------


def _sync_grace(schedule: Schedule) -> timedelta:
    """How long a failure may last before it's worth a buzz: long enough for scheduled
    syncs to fix a passing fault, since a failed run is retried."""
    if schedule.runs and schedule.mode != "cron" and schedule.interval:
        return max(MIN_SYNC_GRACE, 2 * timedelta(hours=schedule.interval))
    return UNSCHEDULED_SYNC_GRACE


async def stalled_banks(facts: Facts) -> list[Alert]:
    """Banks whose data stopped moving while syncs still succeed.

    SimpleFIN can stop refreshing one bank and report no error at all, so the connection
    stays healthy; only the date the bank last reported gives it away. A bank counts when
    all its accounts have gone quiet, so one dormant account at a working bank doesn't.
    """
    healthy = {
        c.id: c for c in await facts.connections() if c.status == BankConnectionStatus.ACTIVE
    }
    banks: dict[str, list[Account]] = defaultdict(list)
    for account in await facts.accounts():
        connection = healthy.get(account.connection_id) if account.connection_id else None
        if connection is None or account.balance_as_of is None:
            continue
        if account.external_id in connection.retired_account_ids:
            continue
        banks[account.institution_id or str(connection.id)].append(account)

    alerts = []
    for bank, accounts in banks.items():
        reported = max(_aware(a.balance_as_of) for a in accounts if a.balance_as_of)
        quiet = (facts.now - reported).days
        if quiet < STALL_DAYS:
            continue
        name = accounts[0].name
        subject = f"{name} and {len(accounts) - 1} more" if len(accounts) > 1 else name
        verb = "haven't" if len(accounts) > 1 else "hasn't"
        key = f"stall:{bank}:{reported:%Y%m%d%H%M}"[:120]
        alerts.append(Alert(key, name, f"{subject} {verb} updated in {_days(quiet)}"))
    return alerts


async def sync_problems(facts: Facts, prefs: NotificationPrefs) -> list[Alert]:
    """Never reads ``last_error_message``: a healthy sync with warnings has one too. Each
    key holds the last good sync, so the next failure after a recovery alerts again."""
    grace = _sync_grace(facts.schedule)
    alerts = []
    for connection in await facts.connections():
        status = connection.status
        last_good = connection.last_synced_at or connection.created_at
        age = facts.now - _aware(last_good)
        text = None
        if status == BankConnectionStatus.REAUTH_REQUIRED:
            text = f"{connection.institution_name} needs you to reconnect"
        elif status == BankConnectionStatus.ERROR and age >= grace:
            text = f"{connection.institution_name} can't sync"
        elif status == BankConnectionStatus.ACTIVE and facts.schedule.runs and age >= 2 * grace:
            text = f"{connection.institution_name} can't sync"  # e.g. stuck at a rate limit
        if text:
            key = f"{connection.id}:{status.value}:{last_good:%Y%m%d%H%M}"
            alerts.append(Alert(key, connection.institution_name, text))
    return alerts + await stalled_banks(facts)


# --- Categories and budgets ----------------------------------------------------------


async def transactions_needing_a_category(facts: Facts, prefs: NotificationPrefs) -> list[Alert]:
    """One key per transaction, so this only comes back when there's something new."""
    transactions, transfers = await facts.counted_transactions()
    waiting = [t for t in transactions if needs_category(t, transfers)]
    count = len(waiting)
    text = (
        "1 transaction needs a category" if count == 1 else f"{count} transactions need a category"
    )
    return [Alert(str(t.id), "transactions", text) for t in waiting]


async def budgets_exceeded(facts: Facts, prefs: NotificationPrefs) -> list[Alert]:
    """Once a month per category, and again if one that got close then goes over."""
    period = f"{facts.today:%Y-%m}"
    alerts = []
    for line in await facts.provider.budgets.get_budget_progress(OWNER_USER_ID, period):
        over = line.pct_used > Decimal(100)
        if not over and (prefs.budget_percent >= 100 or line.pct_used < prefs.budget_percent):
            continue
        name = line.category_name
        text = (
            f"{name} is over budget"
            if over
            else f"{name} is past {prefs.budget_percent}% of its budget"
        )
        state = "over" if over else "near"
        alerts.append(Alert(f"{line.category_id}:{period}:{state}", name, text))
    return alerts


# --- Large transactions and low balances ----------------------------------------------


def _payee(transaction: Transaction) -> str:
    """Who it was to, without the digits banks mix into their text: they can be an amount."""
    if transaction.merchant_name:
        return transaction.merchant_name
    if transaction.clean_description == NO_DESCRIPTION:
        return ""
    words = [w for w in transaction.clean_description.split() if not re.search(r"\d", w)]
    return " ".join(words)[:40].strip()


async def large_transactions(facts: Facts, prefs: NotificationPrefs) -> list[Alert]:
    """Money out, once posted: a hold can post under a new id at a new amount, so alerting
    on pending ones could say it twice. Only what a sync brought in after an account's
    first import, which is history rather than news."""
    accounts = {a.id: a for a in await facts.accounts()}
    transactions, transfers = await facts.counted_transactions()
    start = facts.today - timedelta(days=LARGE_TX_DAYS)
    alerts = []
    for tx in transactions:
        account = accounts.get(tx.account_id)
        if (
            account is None
            or tx.date < start
            or tx.is_pending
            or tx.amount >= 0
            or abs(tx.amount) < prefs.large_amount
            or tx.source != TransactionSource.SYNC
            or not counts_as_cash_flow(tx, transfers)
            or _aware(tx.created_at) <= _aware(account.created_at) + FIRST_IMPORT
        ):
            continue
        payee = _payee(tx)
        text = f"Large transaction at {payee}" if payee else f"Large transaction on {account.name}"
        alerts.append(Alert(str(tx.id), payee or account.name, text))
    return alerts


async def _balances_to_watch(
    facts: Facts, prefs: NotificationPrefs
) -> list[tuple[Account, Decimal, Decimal]]:
    """(account, balance, limit) for accounts with a limit whose balance can be trusted.
    Cards and loans never: banks report what's owed with either sign. A stale balance
    says nothing either way, so it neither alerts nor counts as recovered."""
    connections = {c.id: c for c in await facts.connections()}
    balances = await facts.balances()
    watched = []
    for account in await facts.accounts():
        limit = prefs.balance_limits.get(str(account.id))
        if limit is None or account.account_type not in BANKING_TYPES:
            continue
        if account.connection_id is not None:
            connection = connections.get(account.connection_id)
            if connection is None or connection.status != BankConnectionStatus.ACTIVE:
                continue
            reported = account.balance_as_of
            if reported is None or (facts.now - _aware(reported)).days > STALE_BALANCE_DAYS:
                continue
        watched.append((account, balances[account.id], limit))
    return watched


async def low_balances(facts: Facts, prefs: NotificationPrefs) -> list[Alert]:
    return [
        Alert(str(account.id), account.name, f"{account.name} is below your limit")
        for account, balance, limit in await _balances_to_watch(facts, prefs)
        if balance < limit
    ]


async def recovered_balances(facts: Facts, prefs: NotificationPrefs) -> list[str]:
    """Back above the limit: the next drop alerts again."""
    return [
        str(account.id)
        for account, balance, limit in await _balances_to_watch(facts, prefs)
        if balance >= limit
    ]


# --- Putting a message together ------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    kind: str
    find: Callable[[Facts, NotificationPrefs], Awaitable[list[Alert]]]
    enabled: Callable[[NotificationPrefs], bool]
    title: str  # for one thing
    grouped_title: str  # for several
    url: str  # {start}, {recent}: the first day the rule looks at
    daily: bool = True  # else: every hour, with the check
    ttl: int = TTL_DAILY
    holds_overnight: bool = False  # an hourly alert that can wait for the morning
    recovered: Callable[[Facts, NotificationPrefs], Awaitable[list[str]]] | None = None


RULES = (
    Rule(
        "bill_overdue",
        bills_overdue,
        lambda p: p.bill_overdue,
        "Bill overdue",
        "{count} bills overdue",
        "/bills",
    ),
    Rule(
        "bill_due",
        bills_due,
        lambda p: p.bill_due,
        "Bill due soon",
        "{count} bills due soon",
        "/bills",
    ),
    Rule(
        "needs_category",
        transactions_needing_a_category,
        lambda p: p.needs_category,
        "Transactions to review",
        "Transactions to review",
        "/transactions?category_id=none&start={start}",
    ),
    Rule(
        "budget",
        budgets_exceeded,
        lambda p: p.budget,
        "Budget alert",
        "{count} budget alerts",
        "/budget",
    ),
    Rule(
        "sync",
        sync_problems,
        lambda p: p.sync,
        "Bank sync problem",
        "{count} bank sync problems",
        "/settings?section=accounts",
        daily=False,
        ttl=TTL_SYNC,
    ),
    Rule(
        "large_tx",
        large_transactions,
        lambda p: p.large_tx,
        "Large transaction",
        "{count} large transactions",
        "/transactions?start={recent}",
        daily=False,
        holds_overnight=True,
    ),
    Rule(
        "low_balance",
        low_balances,
        lambda p: p.low_balance,
        "Low balance",
        "{count} low balances",
        "/accounts",
        daily=False,
        holds_overnight=True,
        recovered=recovered_balances,
    ),
)


def _list_names(names: list[str]) -> str:
    """``Netflix, Rent and 1 more``."""
    shown = names[:MAX_NAMES]
    if len(names) > MAX_NAMES:
        return f"{', '.join(shown)} and {len(names) - MAX_NAMES} more"
    return f"{', '.join(shown[:-1])} and {shown[-1]}"


def build_message(rule: Rule, alerts: Iterable[Alert], today: date) -> Message:
    """One notification per kind: specific for one thing, a count and names for several."""
    new = list(alerts)
    names = list(dict.fromkeys(a.name for a in new))
    if len(names) == 1:
        title, body = rule.title, new[0].text
    else:
        title, body = rule.grouped_title.format(count=len(names)), _list_names(names)
    return Message(
        title=title,
        body=body,
        url=rule.url.format(
            start=today - timedelta(days=CATEGORY_LOOKBACK_DAYS),
            recent=today - timedelta(days=LARGE_TX_DAYS),
        ),
        tag=f"{rule.kind}-{today}",
        ttl=rule.ttl,
    )
