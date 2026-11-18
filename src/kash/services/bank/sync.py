"""Bank synchronization orchestration and deduplication service.

One sync of a connection, whatever started it (the Sync button, the schedule, or the
first sync after connecting), runs the same steps:

1. one provider request for accounts, balances and transactions together;
2. match accounts (by provider id, else recognize a re-added one: ``identity.py``);
3. save transactions (deduplicated), drop pending ones the bank stopped sending;
4. pair transfers, bill matching;
5. only then save the new balances and mark the connection synced.

So a sync that fails part way leaves balances, "balance as of" and "last synced" where
they were, and the next sync fetches the same window again. Each run logs one line
per step (``bank_sync run=... event=...``) without credentials, account names,
numbers or amounts.
"""

import asyncio
import hashlib
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from kash.domain.models import (
    NO_DESCRIPTION,
    Account,
    AccountType,
    BankConnection,
    BankConnectionStatus,
    BankSyncResult,
    ExternalAccount,
    ExternalAccountSet,
    ExternalTransaction,
    PendingAccount,
    Transaction,
    TransactionSource,
    TransactionType,
    default_cash_flow,
)
from kash.domain.protocols import BankProvider, DataProvider
from kash.services.bank import identity
from kash.services.bank.exceptions import (
    BankAuthenticationError,
    BankProviderError,
    BankRateLimitError,
)
from kash.services.bank.simplefin import SimpleFINProvider
from kash.services.rules.engine import RuleEngine
from kash.services.security import decrypt_credentials
from kash.services.transfers import link_transfers, mark_debt_payments

logger = logging.getLogger(__name__)

# A pending transaction the bank hasn't reported for this long is gone
STALE_PENDING_DAYS = 14
# Each sync re-reads this far back before the last one: late-posting charges, and
# transactions whose text the bank's feed only fills in days later
OVERLAP_DAYS = 7
# History fetched for an account the first time Kash sees it
HISTORY_DAYS = 90

ALREADY_RUNNING = "A sync of this connection is already running; try again in a minute."

# One sync per connection at a time in this process (the schedule and the Sync button
# could otherwise import the same transactions twice).
_SYNC_LOCKS: dict[UUID, asyncio.Lock] = {}


def any_sync_running() -> bool:
    return any(lock.locked() for lock in _SYNC_LOCKS.values())


def sync_lock(connection_id: UUID) -> asyncio.Lock:
    """The lock a sync of this connection holds (also taken by account merges)."""
    return _SYNC_LOCKS.setdefault(connection_id, asyncio.Lock())


def generate_transaction_fingerprint(
    account_id: UUID,
    tx_date: date,
    amount: Decimal,
    description: str,
) -> str:
    """Generate deterministic SHA-256 fingerprint for secondary deduplication."""
    norm_desc = description.strip().lower()[:60]
    payload = f"{account_id}:{tx_date.isoformat()}:{amount:.4f}:{norm_desc}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


class SyncLog:
    """Structured log lines for one sync run, e.g.
    ``bank_sync run=1a2b3c4d trigger=scheduled connection=d62da02f event=fetched accounts=6``.
    Values are counts, dates, ids and short reasons only."""

    def __init__(self, trigger: str, connection_id: UUID):
        self.run = uuid4().hex[:8]
        self._prefix = (
            f"bank_sync run={self.run} trigger={trigger} connection={connection_id.hex[:8]}"
        )

    def _line(self, event: str, fields: dict[str, Any]) -> str:
        parts = [f"{self._prefix} event={event}"]
        for key, value in fields.items():
            text = str(value)
            parts.append(f'{key}="{text}"' if " " in text else f"{key}={text}")
        return " ".join(parts)

    def info(self, event: str, **fields: Any) -> None:
        logger.info(self._line(event, fields))

    def warning(self, event: str, **fields: Any) -> None:
        logger.warning(self._line(event, fields))

    def error(self, event: str, **fields: Any) -> None:
        logger.error(self._line(event, fields))


@dataclass
class _Accounts:
    """Outcome of matching the provider's accounts to Kash's."""

    # provider account id -> Kash account, for accounts whose transactions are imported
    ids: dict[str, UUID] = field(default_factory=dict)
    # existing accounts to update with the provider's data once transactions are saved
    updates: list[tuple[Account, ExternalAccount]] = field(default_factory=list)
    # provider ids of accounts new to Kash (created or relinked): they need history
    needs_history: list[str] = field(default_factory=list)
    # reported accounts that may be existing ones: nothing imported until confirmed
    pending: list[PendingAccount] = field(default_factory=list)


class BankSyncService:
    """Orchestrates bank sync, account reconciliation, deduplication, and rule application."""

    def __init__(
        self,
        data_provider: DataProvider,
        providers: dict[str, BankProvider] | None = None,
        on_transactions_imported: (
            Callable[[UUID, list[Transaction]], Awaitable[None]] | None
        ) = None,
    ):
        self.data_provider = data_provider
        self.providers: dict[str, BankProvider] = providers or {
            "simplefin": SimpleFINProvider(),
        }
        self.on_transactions_imported = on_transactions_imported

    def register_provider(self, name: str, provider: BankProvider) -> None:
        """Register a custom bank sync provider."""
        self.providers[name] = provider

    # --- accounts -------------------------------------------------------------------

    async def _reconcile_accounts(
        self,
        user_id: UUID,
        connection: BankConnection,
        answer: ExternalAccountSet,
        since: datetime,
        now: datetime,
        result: BankSyncResult,
        log: SyncLog,
    ) -> _Accounts:
        """Match the provider's accounts to Kash accounts (``identity.py``).

        Known ids are matched; retired ones (replaced cards) are ignored; an account
        under a new id is relinked when that is certain; new accounts the user is asked
        about are held; the rest are created. Existing accounts are only looked up
        here: their balances are saved after the transactions (``_save_balances``).
        Accounts the user excluded from sync are matched (so they are not created again)
        but get no transactions.
        """
        accounts = self.data_provider.accounts
        out = _Accounts()
        unknown: list[ExternalAccount] = []
        retired = set(connection.retired_account_ids)
        for ext_acc in answer.accounts:
            existing = await accounts.get_by_external_id(connection.id, ext_acc.external_id)
            if existing:
                out.updates.append((existing, ext_acc))
                if existing.is_active:
                    out.ids[ext_acc.external_id] = existing.id
            elif ext_acc.external_id in retired:
                log.info("account_retired_skipped", provider_id=ext_acc.external_id)
            else:
                unknown.append(ext_acc)
        if not unknown:
            return out

        first_sync = connection.last_synced_at is None
        matches = await self._match_unknown(
            user_id, connection, answer, unknown, since, ask=not first_sync
        )
        for relink in matches.relinks:
            account = await self._relink(connection, relink.account, relink.external, log)
            log.info("account_relinked", account=account.id.hex[:8], reason=relink.reason)
            out.updates.append((account, relink.external))
            out.needs_history.append(relink.external.external_id)
            if account.is_active:
                out.ids[relink.external.external_id] = account.id
            result.relinked_accounts_count += 1
        relinked = {r.external.external_id for r in matches.relinks}
        for ext_acc in unknown:
            if ext_acc.external_id in relinked:
                continue
            suggested = matches.ask.get(ext_acc.external_id)
            if suggested:
                out.pending.append(
                    PendingAccount(account=ext_acc, suggested_ids=[a.id for a in suggested])
                )
                log.info(
                    "account_held",
                    provider_id=ext_acc.external_id,
                    suggested=",".join(a.id.hex[:8] for a in suggested),
                )
                result.warnings.append(
                    f"New account “{ext_acc.name}”: is it new, or replacing one you have? "
                    "Answer on the Accounts page; nothing is imported for it until then."
                )
                continue
            created = await accounts.create_account(_new_account(user_id, connection, ext_acc, now))
            log.info("account_created", account=created.id.hex[:8])
            out.ids[ext_acc.external_id] = created.id
            out.needs_history.append(ext_acc.external_id)
            result.new_accounts_count += 1
        return out

    async def _match_unknown(
        self,
        user_id: UUID,
        connection: BankConnection,
        answer: ExternalAccountSet,
        unknown: list[ExternalAccount],
        since: datetime,
        ask: bool,
    ) -> identity.Matches:
        """Relink / ask / new for accounts with ids Kash doesn't know (``identity.py``)."""
        reported = {a.external_id for a in answer.accounts}
        accounts = await self.data_provider.accounts.list_accounts(user_id, include_inactive=True)
        history: dict[UUID, Counter[identity.TxKey]] = {}
        start = since.date() - timedelta(days=7)
        for acc in accounts:
            # shared transactions only matter for accounts the provider isn't reporting
            reported_now = acc.connection_id == connection.id and acc.external_id in reported
            if not reported_now and any(identity.lookalike(ext, acc) for ext in unknown):
                history[acc.id] = identity.history_keys(
                    await self.data_provider.transactions.list_transactions(
                        user_id, account_id=acc.id, start_date=start, limit=100_000
                    )
                )
        return identity.find_matches(
            unknown,
            accounts,
            connection.id,
            reported,
            identity.incoming_keys(answer.transactions),
            history,
            ask=ask,
        )

    async def _relink(
        self, connection: BankConnection, account: Account, ext_acc: ExternalAccount, log: SyncLog
    ) -> Account:
        """Point an existing account (and its history) at the provider's new id."""
        relinked = await self.data_provider.accounts.relink_account(
            account.id, connection.id, ext_acc.external_id
        )
        log.info(
            "account_id_changed",
            account=account.id.hex[:8],
            old_id=account.external_id or "-",
            new_id=ext_acc.external_id,
        )
        return relinked

    async def _save_balances(self, reconciled: _Accounts, now: datetime) -> None:
        """Save balances and bank identity of the existing accounts this answer covered."""
        for existing, ext_acc in reconciled.updates:
            # The bank renamed it or changed its number (a new card on the same account):
            # follow it, keeping a name the user chose
            if ext_acc.mask and ext_acc.mask != existing.mask:
                existing.mask = ext_acc.mask
            if existing.provider_account_name and existing.name == existing.provider_account_name:
                existing.name = ext_acc.name
            existing.provider_account_name = ext_acc.name
            existing.institution_id = ext_acc.institution_id or existing.institution_id
            if existing.is_active:
                if (
                    ext_acc.account_type == AccountType.INVESTMENT
                    and existing.account_type != AccountType.INVESTMENT
                ):
                    existing.account_type = AccountType.INVESTMENT
                    existing.cash_flow = default_cash_flow(AccountType.INVESTMENT)
                existing.current_balance = ext_acc.current_balance
                if ext_acc.available_balance is not None:
                    existing.available_balance = ext_acc.available_balance
                existing.last_reconciled_at = now
                existing.last_reconciled_balance = ext_acc.current_balance
                existing.balance_as_of = ext_acc.balance_as_of or existing.balance_as_of
            await self.data_provider.accounts.update_account(existing)

    # --- transactions ---------------------------------------------------------------

    async def _import_transactions(
        self,
        user_id: UUID,
        account_ids: dict[str, UUID],
        ext_transactions: list[ExternalTransaction],
        result: BankSyncResult,
        claimed: set[UUID],
    ) -> list[Transaction]:
        """Save new transactions; update pending ones that posted. Returns the new ones.

        A transaction is the same as a saved one when it has its provider id, or else
        when date, amount and description match a saved one of the same account whose
        id the provider no longer sends (a pending charge posted under a new id, or a
        bank re-added in SimpleFIN): ``_saved_under_old_id``. That saved row is
        updated, not duplicated, and each saved row matches at most one incoming
        transaction (``claimed``), so two identical purchases on one day both stay.
        """
        transactions = self.data_provider.transactions
        all_rules = await self.data_provider.rules.list_rules(user_id)
        active_rules = [r for r in all_rules if r.is_active]
        sent: dict[UUID, set[str]] = {}
        for ext_tx in ext_transactions:
            local = account_ids.get(ext_tx.account_external_id)
            if local:
                sent.setdefault(local, set()).add(ext_tx.external_id)

        imported: list[Transaction] = []
        for ext_tx in ext_transactions:
            local_account_id = account_ids.get(ext_tx.account_external_id)
            if not local_account_id:
                result.skipped_transactions_count += 1  # account excluded from sync
                continue

            tx_date = ext_tx.posted_at.date()
            tx_type = TransactionType.DEBIT if ext_tx.amount < 0 else TransactionType.CREDIT
            fingerprint = generate_transaction_fingerprint(
                local_account_id, tx_date, ext_tx.amount, ext_tx.description
            )

            existing = await transactions.get_by_external_id(local_account_id, ext_tx.external_id)
            if existing is None:
                existing = await self._saved_under_old_id(
                    user_id, local_account_id, ext_tx, fingerprint, sent[local_account_id], claimed
                )

            if existing is not None:
                claimed.add(existing.id)
                rekeyed = existing.external_id != ext_tx.external_id
                named = _fill_missing_description(existing, ext_tx)
                if named and active_rules:
                    existing, applied = RuleEngine.evaluate_and_apply(existing, active_rules)
                    result.applied_rules_count += len(applied)
                changed = _apply_bank_update(existing, ext_tx, tx_date, tx_type) or named
                # Follow the bank's current id and wording, so the next time its id
                # changes (a re-added bank) this row is still recognized
                current = generate_transaction_fingerprint(
                    local_account_id, existing.date, existing.amount, ext_tx.description
                )
                if rekeyed or changed or existing.fingerprint != current:
                    existing.external_id = ext_tx.external_id
                    existing.fingerprint = current
                    await transactions.update_transaction(existing)
                if changed:
                    result.updated_transactions_count += 1
                else:
                    result.duplicate_transactions_count += 1
                continue

            new_tx = Transaction(
                id=uuid4(),
                user_id=user_id,
                account_id=local_account_id,
                date=tx_date,
                amount=ext_tx.amount,
                type=tx_type,
                original_description=ext_tx.description,
                clean_description=ext_tx.description,
                merchant_name=ext_tx.payee,
                is_pending=ext_tx.is_pending,
                source=TransactionSource.SYNC,
                external_id=ext_tx.external_id,
                fingerprint=fingerprint,
            )

            # Automatically evaluate rules on newly imported transactions
            if active_rules:
                categorized_tx, applied = RuleEngine.evaluate_and_apply(new_tx, active_rules)
                new_tx = categorized_tx
                if applied:
                    result.applied_rules_count += len(applied)

            created_tx = await transactions.create_transaction(new_tx)
            claimed.add(created_tx.id)
            imported.append(created_tx)
            result.new_transactions_count += 1
        return imported

    async def _saved_under_old_id(
        self,
        user_id: UUID,
        account_id: UUID,
        ext_tx: ExternalTransaction,
        fingerprint: str,
        sent: set[str],
        claimed: set[UUID],
    ) -> Transaction | None:
        """The saved transaction an incoming one is, though it came under another id:
        same account, date, amount and description, and the provider no longer sends
        the saved one's id. The description may differ in how a reference number is
        masked (``identity.normalize_description``)."""
        transactions = self.data_provider.transactions

        def free(t: Transaction) -> bool:
            return t.id not in claimed and t.external_id not in sent

        exact = await transactions.list_by_fingerprint(account_id, fingerprint)
        found = next((t for t in exact if free(t)), None)
        if found is not None:
            return found
        tx_date = ext_tx.posted_at.date()
        wanted = identity.normalize_description(ext_tx.description)
        same_day = await transactions.list_transactions(
            user_id, account_id=account_id, start_date=tx_date, end_date=tx_date, limit=100_000
        )
        return next(
            (
                t
                for t in sorted(same_day, key=lambda t: t.created_at)
                if t.source == TransactionSource.SYNC
                and t.amount == ext_tx.amount
                and free(t)
                and identity.normalize_description(t.original_description) == wanted
            ),
            None,
        )

    async def _remove_stale_pending(
        self,
        user_id: UUID,
        account_map: dict[str, UUID],
        ext_transactions: list[ExternalTransaction],
        since: datetime,
        imported: list[Transaction],
        claimed: set[UUID],
    ) -> int:
        """Delete synced pending transactions that this fetch no longer returns.

        Only pending ones dated inside the fetch window (the bank would have sent
        them) or older than ``STALE_PENDING_DAYS`` (a hold doesn't last that long)
        are removed; never one this sync matched to an incoming transaction. A
        category the user gave a removed one carries over to the posted transaction
        that replaced it (same account and amount, close date).
        """
        if not ext_transactions:
            return 0  # nothing came back at all: don't trust it to mean "dropped"
        returned = {
            (account_map.get(t.account_external_id), t.external_id) for t in ext_transactions
        }
        window_start = since.date()
        too_old = datetime.now(UTC).date() - timedelta(days=STALE_PENDING_DAYS)
        transactions = self.data_provider.transactions

        removed = 0
        for account_id in set(account_map.values()):
            for tx in await transactions.list_transactions(
                user_id, account_id=account_id, limit=100_000
            ):
                if (
                    not tx.is_pending
                    or tx.source != TransactionSource.SYNC
                    or tx.id in claimed
                    or (account_id, tx.external_id) in returned
                    or too_old <= tx.date < window_start
                ):
                    continue
                if tx.category_id:
                    replacement = next(
                        (
                            t
                            for t in imported
                            if t.account_id == account_id
                            and not t.is_pending
                            and t.amount == tx.amount
                            and t.category_id is None
                            and abs((t.date - tx.date).days) <= STALE_PENDING_DAYS
                        ),
                        None,
                    )
                    if replacement:
                        replacement.category_id = tx.category_id
                        await transactions.update_transaction(replacement)
                await transactions.delete_transaction(tx.id)
                removed += 1
        return removed

    async def _after_import(
        self, user_id: UUID, imported: list[Transaction], result: BankSyncResult, log: SyncLog
    ) -> None:
        """Pair transfers and match bills. The transactions are saved already, so a
        failure here is reported as a warning instead of failing the sync."""
        if not imported:
            return
        earliest = min(t.date for t in imported)
        steps: list[tuple[str, Callable[[], Awaitable[Any]]]] = []

        async def transfers() -> None:
            # Pair transfer legs (e.g. checking -> savings) so they are not income/spending
            await link_transfers(self.data_provider, user_id, since=earliest - timedelta(days=7))
            # Payments into cards/loans whose other leg wasn't found (e.g. paid
            # from a bank that isn't connected) are still not income.
            new_ids = {t.id for t in imported}
            for account_id in {t.account_id for t in imported}:
                await mark_debt_payments(
                    self.data_provider, user_id, account_id, since=earliest, only=new_ids
                )

        steps.append(("pairing transfers", transfers))
        if self.on_transactions_imported:
            hook = self.on_transactions_imported
            steps.append(("matching bills", lambda: hook(user_id, imported)))
        for name, step in steps:
            try:
                await step()
            except Exception as exc:
                logger.exception("Bank sync: %s failed", name)
                log.error(
                    "after_import_failed", step=name.replace(" ", "_"), error=type(exc).__name__
                )
                result.warnings.append(f"Transactions were saved, but {name} failed: {exc}")

    # --- held accounts: the user's answer ---------------------------------------------

    async def link_pending_account(
        self, connection_id: UUID, user_id: UUID, external_id: str, account_id: UUID
    ) -> BankSyncResult:
        """The user says a new account replaces ``account_id`` (e.g. a new card number):
        that account, with its history, continues with the new one. The provider id it
        had is retired, so if the bank still reports the old card it doesn't come back
        as a new account. Then a sync fetches the new account's history."""

        async def link(connection: BankConnection, held: PendingAccount, log: SyncLog) -> None:
            account = await self.data_provider.accounts.get_account(account_id)
            if account is None or account.user_id != user_id:
                raise ValueError("That account no longer exists.")
            if account.currency.upper() != held.account.currency.upper():
                raise ValueError("Choose an account in the same currency.")
            old_connection_id, old_id = account.connection_id, account.external_id
            await self._relink(connection, account, held.account, log)
            log.info("account_relinked", account=account.id.hex[:8], reason="confirmed_by_user")
            if old_id and old_connection_id == connection.id:
                connection.retired_account_ids.append(old_id)
            elif old_id and old_connection_id:
                other = await self.data_provider.bank_connections.get_connection(old_connection_id)
                if other is not None:
                    other.retired_account_ids.append(old_id)
                    await self.data_provider.bank_connections.update_connection(other)

        return await self._answer_held(connection_id, user_id, external_id, link)

    async def add_pending_account(
        self, connection_id: UUID, user_id: UUID, external_id: str
    ) -> BankSyncResult:
        """The user says a held account is a new one: add it, then sync (with history)."""

        async def add(connection: BankConnection, held: PendingAccount, log: SyncLog) -> None:
            created = await self.data_provider.accounts.create_account(
                _new_account(user_id, connection, held.account, datetime.now(UTC))
            )
            log.info("account_created", account=created.id.hex[:8], reason="confirmed_by_user")

        return await self._answer_held(connection_id, user_id, external_id, add)

    async def _answer_held(
        self,
        connection_id: UUID,
        user_id: UUID,
        external_id: str,
        apply: Callable[[BankConnection, PendingAccount, SyncLog], Awaitable[None]],
    ) -> BankSyncResult:
        lock = sync_lock(connection_id)
        if lock.locked():
            raise ValueError(ALREADY_RUNNING)
        async with lock:
            connection = await self.data_provider.bank_connections.get_connection(connection_id)
            if not connection or connection.user_id != user_id:
                raise ValueError(f"Bank connection {connection_id} not found for user {user_id}")
            held = next(
                (p for p in connection.pending_accounts if p.account.external_id == external_id),
                None,
            )
            if held is None:
                raise ValueError("That account isn't waiting for confirmation anymore.")
            await apply(connection, held, SyncLog("manual", connection_id))
            connection.pending_accounts = [
                p for p in connection.pending_accounts if p.account.external_id != external_id
            ]
            await self.data_provider.bank_connections.update_connection(connection)
        # Its transactions weren't imported while it was held: fetch the full history
        return await self.sync_connection(
            connection_id, user_id, force_window_days=HISTORY_DAYS, trigger="manual"
        )

    # --- the sync ---------------------------------------------------------------------

    async def sync_connection(
        self,
        connection_id: UUID,
        user_id: UUID,
        force_window_days: int | None = None,
        trigger: str = "manual",
    ) -> BankSyncResult:
        """Synchronize accounts and transactions for a given BankConnection.

        ``trigger`` (manual, scheduled, initial) only labels the logs and the result;
        every trigger runs the same steps.
        """
        connection = await self.data_provider.bank_connections.get_connection(connection_id)
        if not connection or connection.user_id != user_id:
            raise ValueError(f"Bank connection {connection_id} not found for user {user_id}")

        result = BankSyncResult(connection_id=connection_id, trigger=trigger)
        log = SyncLog(trigger, connection_id)
        lock = sync_lock(connection_id)
        if lock.locked():
            log.warning("skipped", reason="already_running")
            result.errors.append(ALREADY_RUNNING)
            return result
        async with lock:
            started = time.monotonic()
            log.info("started", provider=connection.provider_name, status=connection.status.value)
            await self._sync(connection, user_id, force_window_days, result, log)
            log_finish = log.info if result.outcome == "ok" else log.warning
            log_finish(
                "finished",
                outcome=result.outcome,
                seconds=f"{time.monotonic() - started:.1f}",
                accounts=result.synced_accounts_count,
                new_accounts=result.new_accounts_count,
                relinked_accounts=result.relinked_accounts_count,
                received=result.received_transactions_count,
                new=result.new_transactions_count,
                updated=result.updated_transactions_count,
                duplicate=result.duplicate_transactions_count,
                skipped=result.skipped_transactions_count,
                rejected=result.rejected_transactions_count,
                removed_pending=result.removed_pending_count,
                warnings=len(result.warnings),
                errors=len(result.errors),
            )
        return result

    async def _sync(
        self,
        connection: BankConnection,
        user_id: UUID,
        force_window_days: int | None,
        result: BankSyncResult,
        log: SyncLog,
    ) -> None:
        now = datetime.now(UTC)
        connections = self.data_provider.bank_connections
        provider = self.providers.get(connection.provider_name)
        if not provider:
            err_msg = f"Unknown bank provider: '{connection.provider_name}'"
            log.error("failed", stage="provider", error="unknown_provider")
            result.errors.append(err_msg)
            connection.status = BankConnectionStatus.ERROR
            connection.last_error_message = err_msg
            await connections.update_connection(connection)
            return

        stage = "credentials"
        try:
            credentials = decrypt_credentials(connection.encrypted_credentials)
            since = _window_start(connection, now, force_window_days)

            # 1. One request: accounts, balances and transactions together
            stage = "fetch"
            log.info("fetch", since=since.date().isoformat())
            answer = await provider.fetch_account_set(credentials, since=since)
            result.synced_accounts_count = len(answer.accounts)
            result.rejected_transactions_count = answer.rejected_transactions
            result.warnings.extend(f"{_provider_label(connection)}: {w}" for w in answer.warnings)
            log.info(
                "fetched",
                accounts=len(answer.accounts),
                transactions=len(answer.transactions),
                pending=sum(t.is_pending for t in answer.transactions),
                rejected=answer.rejected_transactions,
                provider_warnings=len(answer.warnings),
            )
            for warning in answer.warnings:
                log.warning("provider_warning", message=warning[:200])
            if answer.rejected_transactions:
                result.warnings.append(
                    f"{answer.rejected_transactions} transaction(s) from the bank were unreadable "
                    "and left out."
                )

            # 2. Match accounts
            stage = "accounts"
            reconciled = await self._reconcile_accounts(
                user_id, connection, answer, since, now, result, log
            )
            ext_transactions = answer.transactions

            # Accounts Kash hasn't seen before get their history, not just the window
            history_since = now - timedelta(days=HISTORY_DAYS)
            if reconciled.needs_history and since > history_since + timedelta(days=1):
                stage = "history"
                ext_transactions = await self._with_history(
                    provider, credentials, history_since, reconciled, ext_transactions, result, log
                )
            result.received_transactions_count = len(ext_transactions)
            for ext_id, local_id in reconciled.ids.items():
                count = sum(t.account_external_id == ext_id for t in ext_transactions)
                log.info("account", account=local_id.hex[:8], transactions=count)

            # 3. Transactions
            stage = "transactions"
            claimed: set[UUID] = set()
            imported_txs = await self._import_transactions(
                user_id, reconciled.ids, ext_transactions, result, claimed
            )
            log.info(
                "transactions_saved",
                new=result.new_transactions_count,
                updated=result.updated_transactions_count,
                duplicate=result.duplicate_transactions_count,
                skipped=result.skipped_transactions_count,
            )

            # Pending transactions the bank stopped reporting were dropped, or
            # posted under a new id (imported above); remove them so they don't double up.
            stage = "pending"
            result.removed_pending_count = await self._remove_stale_pending(
                user_id, reconciled.ids, ext_transactions, since, imported_txs, claimed
            )

            # 4. Transfers and bills (failures here are warnings: the data is saved)
            stage = "after_import"
            await self._after_import(user_id, imported_txs, result, log)

            # 5. Balances last, so they never run ahead of the transactions
            stage = "balances"
            await self._save_balances(reconciled, now)

            stage = "connection"
            connection.pending_accounts = reconciled.pending
            connection.last_synced_at = now
            connection.status = BankConnectionStatus.ACTIVE
            connection.last_error_message = (
                ("Synced with warnings: " + "; ".join(result.warnings))[:1000]
                if result.warnings
                else None
            )
            await connections.update_connection(connection)

        except BankAuthenticationError as e:
            log.error("failed", stage=stage, error=type(e).__name__)
            connection.status = BankConnectionStatus.REAUTH_REQUIRED
            connection.last_error_message = str(e)
            await connections.update_connection(connection)
            result.errors.append(str(e))
        except BankRateLimitError as e:
            log.error("failed", stage=stage, error=type(e).__name__)
            connection.last_error_message = str(e)
            await connections.update_connection(connection)
            result.errors.append(str(e))
        except BankProviderError as e:
            log.error("failed", stage=stage, error=type(e).__name__, reason=str(e)[:200])
            connection.status = BankConnectionStatus.ERROR
            connection.last_error_message = str(e)
            await connections.update_connection(connection)
            result.errors.append(str(e))
        except Exception as e:
            logger.exception("Bank sync failed at %s", stage)
            log.error("failed", stage=stage, error=type(e).__name__)
            err_msg = f"Unexpected sync error ({stage}): {e}"
            connection.status = BankConnectionStatus.ERROR
            connection.last_error_message = err_msg
            await connections.update_connection(connection)
            result.errors.append(err_msg)

    async def _with_history(
        self,
        provider: BankProvider,
        credentials: str,
        history_since: datetime,
        reconciled: _Accounts,
        ext_transactions: list[ExternalTransaction],
        result: BankSyncResult,
        log: SyncLog,
    ) -> list[ExternalTransaction]:
        """Add ``HISTORY_DAYS`` of transactions for accounts new to Kash (one more request,
        for those accounts only). If it fails, the sync goes on with what it has."""
        try:
            extra = await provider.fetch_account_set(
                credentials, since=history_since, account_ids=reconciled.needs_history
            )
        except BankProviderError as e:
            log.warning(
                "history_failed", accounts=len(reconciled.needs_history), error=type(e).__name__
            )
            result.warnings.append(f"Couldn't fetch older history for new accounts: {e}")
            return ext_transactions
        have = {(t.account_external_id, t.external_id) for t in ext_transactions}
        added = [
            t
            for t in extra.transactions
            if t.account_external_id in reconciled.needs_history
            and (t.account_external_id, t.external_id) not in have
        ]
        log.info("history_fetched", accounts=len(reconciled.needs_history), transactions=len(added))
        return ext_transactions + added


def _window_start(
    connection: BankConnection, now: datetime, force_window_days: int | None
) -> datetime:
    """Transactions posted on/after this are requested."""
    if force_window_days:
        return now - timedelta(days=force_window_days)
    if connection.last_synced_at:
        last_synced = connection.last_synced_at
        if last_synced.tzinfo is None:
            last_synced = last_synced.replace(tzinfo=UTC)
        # Overlap buffer to capture retroactively posted charges
        return last_synced - timedelta(days=OVERLAP_DAYS)
    return now - timedelta(days=HISTORY_DAYS)  # first sync


def _fill_missing_description(tx: Transaction, ext_tx: ExternalTransaction) -> bool:
    """A row saved without text takes the bank's once it has some; True if it did.
    Rows renamed by the user or a rule no longer say NO_DESCRIPTION and stay as they are."""
    if tx.clean_description != NO_DESCRIPTION or ext_tx.description == NO_DESCRIPTION:
        return False
    tx.original_description = ext_tx.description
    tx.clean_description = ext_tx.description
    tx.merchant_name = ext_tx.payee
    return True


def _apply_bank_update(
    tx: Transaction, ext_tx: ExternalTransaction, tx_date: date, tx_type: TransactionType
) -> bool:
    """Bring a saved transaction up to date with the bank; True if anything changed."""
    if tx.is_pending and not ext_tx.is_pending:
        # Posted: the posted amount can differ from the hold (tips, fuel)
        tx.is_pending = False
        tx.date = tx_date
        tx.amount = ext_tx.amount
        tx.type = tx_type
        return True
    if tx.is_pending and tx.date != tx_date:
        # Still pending: keep its date current (also repairs pending rows saved as
        # 1970-01-01 before posted=0 was handled)
        tx.date = tx_date
        return True
    return False


def _new_account(
    user_id: UUID, connection: BankConnection, ext_acc: ExternalAccount, now: datetime
) -> Account:
    return Account(
        id=uuid4(),
        user_id=user_id,
        connection_id=connection.id,
        name=ext_acc.name,
        account_type=ext_acc.account_type,
        currency=ext_acc.currency,
        current_balance=ext_acc.current_balance,
        available_balance=ext_acc.available_balance,
        mask=ext_acc.mask,
        is_active=True,
        include_in_net_worth=True,
        external_id=ext_acc.external_id,
        last_reconciled_at=now,
        last_reconciled_balance=ext_acc.current_balance,
        balance_as_of=ext_acc.balance_as_of,
        provider_account_name=ext_acc.name,
        institution_id=ext_acc.institution_id,
    )


def _provider_label(connection: BankConnection) -> str:
    return "SimpleFIN" if connection.provider_name == "simplefin" else connection.provider_name
