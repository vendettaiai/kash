"""AI categorization pipeline (docs/AI_PLAN.md).

1. Eligible: no category, posted, not ignored, not a transfer (paired or flagged),
   amount ≠ 0, active account, never sent before. Rules and transfer linking have
   already run, so anything they could categorize never gets here.
2. What the AI should skip or leave alone is up to the prompt (Settings → AI →
   Advanced); nothing is filtered by hardcoded patterns.
3. Everything eligible is grouped by merchant + money in/out; one representative per group
   is sent, in batches (Settings → AI → Advanced → batch size).
4. Every transaction gets a suggestion row. At or above the confidence threshold
   the category is applied ("auto_applied", can be undone). Below it nothing
   changes ("pending": the AI's guess is kept for learning but not shown as a to-do);
   the transaction stays in the normal Uncategorized list. No answer: "unknown".
5. Runs after each bank sync and from the AI page, never on page load and never
   while AI is off. There is no quota; provider errors stop a run and the rest is
   tried next time.
"""

import asyncio
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from uuid import UUID

from kash.domain.models import (
    AIRun,
    AISuggestion,
    Category,
    SuggestionStatus,
    Transaction,
    UserSettings,
)
from kash.domain.protocols import DataProvider
from kash.services.ai import options as ai_options
from kash.services.ai.prompt import Group, build_request, parse_reply, system_prompt
from kash.services.ai.providers import AIClient, AIProviderError, Connection
from kash.services.bills.merchant import normalize_merchant, transaction_merchant
from kash.services.ledger import income_category_ids
from kash.services.rules.engine import normalize_text
from kash.services.security import decrypt_credentials

MAX_EXAMPLES = 30
SCAN_LIMIT = 100_000

# One run at a time (after-sync and the button could overlap and send twice).
_running = asyncio.Lock()

logger = logging.getLogger(__name__)


def saved_key(saved: UserSettings) -> tuple[str, bool]:
    """(decrypted key or "", whether a stored key can't be read anymore)."""
    if not saved.ai_api_key_encrypted:
        return "", False
    try:
        return decrypt_credentials(saved.ai_api_key_encrypted), False
    except ValueError:
        return "", True  # DATA_ENCRYPTION_KEY / SECRET_KEY changed since it was saved


def connection_for(saved: UserSettings) -> Connection | None:
    """The saved connection when AI is on and complete, else None."""
    if not saved.ai_enabled or not saved.ai_provider or not saved.ai_model:
        return None
    try:
        return Connection.build(
            saved.ai_provider, saved.ai_base_url or "", saved.ai_model, saved_key(saved)[0]
        )
    except AIProviderError:
        return None


def eligible(
    tx: Transaction, active_accounts: set[UUID], transfer_ids: set[UUID], sent: set[UUID]
) -> bool:
    """May this transaction be considered for AI categorization at all?"""
    return (
        tx.category_id is None
        and not tx.is_pending
        and not tx.is_ignored
        and not tx.is_transfer
        and tx.matched_transfer_id is None
        and tx.amount != 0
        and tx.account_id in active_accounts
        and tx.id not in sent
    )


def group_key(tx: Transaction) -> str:
    merchant = (
        normalize_merchant(transaction_merchant(tx))
        or normalize_text(tx.clean_description or tx.original_description)[:40]
    )
    return f"{merchant}|{'in' if tx.amount > 0 else 'out'}"


@dataclass
class Candidates:
    groups: list[Group]
    categories: list[Category]  # what the AI may choose from
    income_ids: set[UUID]
    account_types: dict[UUID, str]
    examples: list[tuple[str, UUID]]

    @property
    def transaction_count(self) -> int:
        return sum(len(g.transactions) for g in self.groups)


def _examples(
    txs: list[Transaction], allowed: set[UUID], transfer_ids: set[UUID]
) -> list[tuple[str, UUID]]:
    """The user's most frequent "merchant → category" pairs (decision 1 in AI_PLAN.md)."""
    pairs: Counter[tuple[str, UUID]] = Counter()
    for tx in txs:
        if tx.category_id in allowed and not tx.is_transfer and tx.category_id not in transfer_ids:
            merchant = normalize_merchant(transaction_merchant(tx))
            if merchant:
                pairs[(merchant, tx.category_id)] += 1  # type: ignore[index]
    best: dict[str, tuple[int, UUID]] = {}
    for (merchant, cat_id), n in pairs.most_common():
        best.setdefault(merchant, (n, cat_id))
    ranked = sorted(best.items(), key=lambda kv: -kv[1][0])[:MAX_EXAMPLES]
    return [(merchant, cat_id) for merchant, (_, cat_id) in ranked]


async def find_candidates(
    provider: DataProvider,
    user_id: UUID,
    since: date | None = None,
    only: set[UUID] | None = None,
) -> Candidates:
    """Everything the AI could be asked about right now (nothing is sent here).
    With ``since``, only transactions on or after that date; with ``only``, only those."""
    accounts = await provider.accounts.list_accounts(user_id)
    groups_list = await provider.categories.list_groups(user_id)
    all_categories = await provider.categories.list_categories(user_id, include_hidden=True)
    transfer_ids = {c.id for c in all_categories if c.is_transfer}
    allowed = [c for c in all_categories if not c.is_hidden and not c.is_transfer]
    txs = await provider.transactions.list_transactions(user_id, limit=SCAN_LIMIT)
    sent = await provider.ai.sent_transaction_ids(user_id)
    active = {a.id for a in accounts if a.is_active}

    by_key: dict[str, list[Transaction]] = {}
    for tx in txs:
        if not eligible(tx, active, transfer_ids, sent) or (since and tx.date < since):
            continue
        if only is not None and tx.id not in only:
            continue
        by_key.setdefault(group_key(tx), []).append(tx)

    groups = [Group(key, members) for key, members in by_key.items()]
    groups.sort(key=lambda g: (-len(g.transactions), -g.sample.date.toordinal()))
    return Candidates(
        groups=groups,
        categories=allowed,
        income_ids=income_category_ids(groups_list, all_categories),
        account_types={a.id: a.account_type.value for a in accounts},
        examples=_examples(txs, {c.id for c in allowed}, transfer_ids),
    )


@dataclass
class RunSummary:
    groups_sent: int = 0
    applied: int = 0  # transactions categorized automatically
    left: int = 0  # the AI had a guess below the threshold: left uncategorized
    unknown: int = 0  # the AI didn't know
    error: str | None = None
    busy: bool = False  # another run was already going
    run: AIRun | None = None
    notes: list[str] = field(default_factory=list)


def _suggestions_for(
    group: Group,
    category: Category | None,
    confidence: float,
    reason: str | None,
    user_id: UUID,
    conn: Connection,
    threshold: float,
) -> list[AISuggestion]:
    if category is None:
        status = SuggestionStatus.UNKNOWN
    elif confidence >= threshold:
        status = SuggestionStatus.AUTO_APPLIED
    else:
        status = SuggestionStatus.PENDING
    now = datetime.now(UTC)
    return [
        AISuggestion(
            user_id=user_id,
            transaction_id=tx.id,
            category_id=category.id if category else None,
            confidence=round(confidence, 3),
            reason=reason,
            status=status,
            provider=conn.provider,
            model=conn.model,
            resolved_at=now if status == SuggestionStatus.AUTO_APPLIED else None,
        )
        for tx in group.transactions
    ]


def is_running() -> bool:
    return _running.locked()


async def run(
    provider: DataProvider,
    user_id: UUID,
    client: AIClient,
    trigger: str = "manual",
    since: date | None = None,
    only: set[UUID] | None = None,
) -> RunSummary:
    """Ask the AI about every eligible merchant group (``only``: just those
    transactions), apply the confident answers and leave the rest uncategorized."""
    if _running.locked():
        return RunSummary(busy=True)
    async with _running:
        return await _run(provider, user_id, client, trigger, since, only)


async def _run(
    provider: DataProvider,
    user_id: UUID,
    client: AIClient,
    trigger: str,
    since: date | None,
    only: set[UUID] | None = None,
) -> RunSummary:
    saved = await provider.settings.get_settings(user_id)
    opts = ai_options.from_stored(saved.ai_options)
    threshold = ai_options.threshold(saved.ai_auto_apply_threshold)
    summary = RunSummary()
    candidates = await find_candidates(provider, user_id, since, only)
    groups = candidates.groups
    if not groups:
        return summary

    conn = client.conn
    system = system_prompt(opts.system_prompt)
    examples = candidates.examples if opts.send_examples else []
    ai_run = AIRun(user_id=user_id, trigger=trigger, provider=conn.provider, model=conn.model)
    for start in range(0, len(groups), opts.batch_size):
        batch = groups[start : start + opts.batch_size]
        request = build_request(
            batch,
            candidates.categories,
            candidates.income_ids,
            candidates.account_types,
            examples,
            system,
        )
        try:
            reply = await client.chat_json(
                request.system, request.user, opts.output_tokens(len(batch)), opts.temperature
            )
        except AIProviderError as exc:
            ai_run.status = "partial" if ai_run.requests else "error"
            ai_run.error = str(exc)[:255]
            summary.error = str(exc)
            break
        ai_run.requests += 1
        ai_run.items_sent += len(batch)
        ai_run.transactions_covered += sum(len(g.transactions) for g in batch)
        ai_run.prompt_tokens += reply.prompt_tokens
        ai_run.completion_tokens += reply.completion_tokens
        new: list[AISuggestion] = []
        for r in parse_reply(request, reply.data):
            rows = _suggestions_for(
                r.group, r.category, r.confidence, r.reason, user_id, conn, threshold
            )
            if rows and rows[0].status == SuggestionStatus.AUTO_APPLIED:
                summary.applied += await _apply(provider, r.group.transactions, rows[0].category_id)
            new += rows
        await provider.ai.save_suggestions(new)
        summary.groups_sent += len(batch)
        summary.left += sum(1 for s in new if s.status == SuggestionStatus.PENDING)
        summary.unknown += sum(1 for s in new if s.status == SuggestionStatus.UNKNOWN)

    if ai_run.requests or ai_run.error:
        summary.run = await provider.ai.save_run(ai_run)
    return summary


async def _apply(provider: DataProvider, txs: list[Transaction], category_id: UUID | None) -> int:
    """Give still-uncategorized ``txs`` the category (never overwrites one)."""
    applied = 0
    for tx in txs:
        current = await provider.transactions.get_transaction(tx.id)
        if current is not None and current.category_id is None and category_id is not None:
            current.category_id = category_id
            await provider.transactions.update_transaction(current)
            applied += 1
    return applied


async def run_after_sync(provider: DataProvider, user_id: UUID) -> RunSummary | None:
    """After a bank sync: categorize what the rules missed, if the user turned that
    on. Never raises (a sync must not fail because of the AI)."""
    try:
        saved = await provider.settings.get_settings(user_id)
        conn = connection_for(saved)
        if conn is None or not saved.ai_run_after_sync:
            return None
        summary = await run(provider, user_id, AIClient(conn), trigger="sync")
        if summary.error:
            logger.warning("AI categorization after sync: %s", summary.error)
        return summary
    except Exception:
        logger.exception("AI categorization after sync failed")
        return None


async def retryable(
    provider: DataProvider, user_id: UUID, suggestion_ids: list[UUID] | None = None
) -> list[AISuggestion]:
    """ "No answer" suggestions whose transaction is still uncategorized (``suggestion_ids``:
    only among those; None: all), i.e. the ones asking the AI again could still help."""
    if suggestion_ids is None:
        rows = await provider.ai.list_suggestions(user_id, SuggestionStatus.UNKNOWN.value)
    else:
        rows = await provider.ai.get_suggestions(suggestion_ids)
    found = []
    for s in rows:
        if s.user_id != user_id or s.status != SuggestionStatus.UNKNOWN:
            continue
        tx = await provider.transactions.get_transaction(s.transaction_id)
        if tx is not None and tx.category_id is None:
            found.append(s)
    return found


async def ask_again(
    provider: DataProvider,
    user_id: UUID,
    client: AIClient,
    suggestion_ids: list[UUID] | None = None,
) -> RunSummary:
    """Forget the "No answer" results (all, or ``suggestion_ids``) and ask the AI about
    those transactions again, using the current prompt and settings."""
    if _running.locked():
        return RunSummary(busy=True)
    rows = await retryable(provider, user_id, suggestion_ids)
    if not rows:
        return RunSummary()
    await provider.ai.delete_suggestions([s.id for s in rows])
    return await run(provider, user_id, client, only={s.transaction_id for s in rows})


@dataclass
class Guess:
    """The AI's below-threshold guess for one still-uncategorized transaction."""

    suggestion: AISuggestion
    category: Category

    @property
    def percent(self) -> int:
        return round(self.suggestion.confidence * 100)


async def guesses_for(
    provider: DataProvider, user_id: UUID, transaction_ids: list[UUID]
) -> dict[UUID, Guess]:
    """Usable guesses (kept as ``pending``, category still available) for these transactions."""
    categories = {
        c.id: c
        for c in await provider.categories.list_categories(user_id)
        if not c.is_hidden and not c.is_transfer
    }
    found: dict[UUID, Guess] = {}
    for start in range(0, len(transaction_ids), 400):  # stay under SQL variable limits
        for s in await provider.ai.suggestions_for_transactions(
            transaction_ids[start : start + 400]
        ):
            if (
                s.user_id == user_id
                and s.status == SuggestionStatus.PENDING
                and s.category_id in categories
            ):
                found[s.transaction_id] = Guess(s, categories[s.category_id])  # type: ignore[index]
    return found


async def accept_guesses(
    provider: DataProvider, user_id: UUID, suggestion_ids: list[UUID]
) -> list[UUID]:
    """Use the AI's below-threshold guesses (``pending``): the transactions get the guessed
    category and the guess counts as accepted (so rule proposals can learn from it).
    Ones that were categorized since are left alone. Returns the transactions categorized."""
    used: list[UUID] = []
    changed: list[AISuggestion] = []
    for s in await provider.ai.get_suggestions(suggestion_ids):
        if s.user_id != user_id or s.status != SuggestionStatus.PENDING or s.category_id is None:
            continue
        tx = await provider.transactions.get_transaction(s.transaction_id)
        if tx is None or tx.category_id is not None:
            continue
        tx.category_id = s.category_id
        await provider.transactions.update_transaction(tx)
        s.status, s.resolved_at = SuggestionStatus.ACCEPTED, datetime.now(UTC)
        changed.append(s)
        used.append(tx.id)
    await provider.ai.save_suggestions(changed)
    return used


async def undo(provider: DataProvider, user_id: UUID, suggestion_ids: list[UUID]) -> int:
    """Take back automatically applied categories: the transactions become
    uncategorized again (and are not sent again). Ones the user re-categorized
    since are left alone."""
    undone = 0
    changed: list[AISuggestion] = []
    for s in await provider.ai.get_suggestions(suggestion_ids):
        if s.user_id != user_id or s.status != SuggestionStatus.AUTO_APPLIED:
            continue
        tx = await provider.transactions.get_transaction(s.transaction_id)
        if tx is not None and tx.category_id == s.category_id:
            tx.category_id = None
            await provider.transactions.update_transaction(tx)
            undone += 1
            s.status, s.resolved_at = SuggestionStatus.REJECTED, datetime.now(UTC)
        else:
            s.status = SuggestionStatus.CHANGED
        changed.append(s)
    await provider.ai.save_suggestions(changed)
    return undone


async def ai_applied_ids(provider: DataProvider, user_id: UUID) -> set[UUID]:
    """Transactions whose current category the AI applied (for the AI badge)."""
    rows = await provider.ai.list_suggestions(user_id, SuggestionStatus.AUTO_APPLIED.value)
    return {s.transaction_id for s in rows}


async def record_user_category(
    provider: DataProvider, user_id: UUID, transaction_id: UUID, category_id: UUID | None
) -> None:
    """The user set a transaction's category by hand: note how that compares with
    the AI's answer (phase E learns from "changed"). No-op without a suggestion."""
    found = await provider.ai.suggestions_for_transactions([transaction_id])
    s = next((s for s in found if s.user_id == user_id), None)
    if s is None or s.category_id is None:
        return
    if s.status == SuggestionStatus.PENDING and category_id is not None:
        s.status = (
            SuggestionStatus.ACCEPTED if category_id == s.category_id else SuggestionStatus.CHANGED
        )
    elif s.status in (SuggestionStatus.AUTO_APPLIED, SuggestionStatus.ACCEPTED):
        if category_id == s.category_id:
            return
        s.status = SuggestionStatus.CHANGED
    else:
        return
    s.resolved_at = datetime.now(UTC)
    await provider.ai.save_suggestions([s])
