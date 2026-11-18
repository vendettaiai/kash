"""Pair the two legs of transfers between your own accounts.

A transfer from checking to savings shows up as -$500 in checking and +$500 in
savings. Neither leg is income or spending. Active "transfer" matching rules
decide which debit/credit pairs belong together; linked pairs get
``is_transfer=True`` and point at each other through ``matched_transfer_id``.
"""

from collections import defaultdict
from datetime import date
from uuid import UUID

from kash.domain.models import AccountType, Rule, Transaction
from kash.domain.protocols import DataProvider
from kash.services.rules.matching import evaluate_transfer_rules

MAX_SCAN = 100_000
# Money into these accounts is almost always a payment from another account.
DEBT_ACCOUNT_TYPES = (AccountType.CREDIT_CARD, AccountType.LOAN)


def _only_exact_rules(rules: list[Rule]) -> bool:
    return all((r.config or {}).get("amount_match_mode", "exact") == "exact" for r in rules)


async def link_transfers(provider: DataProvider, user_id: UUID, since: date | None = None) -> int:
    """Link unpaired transfer legs dated on/after ``since``. Returns the number of pairs linked.

    Only rules in "links" mode change data; "suggests" rules are left for review.
    """
    rules = [
        r
        for r in await provider.rules.list_rules(user_id, rule_type="transfer")
        if r.is_active and r.action_mode == "links"
    ]
    if not rules:
        return 0

    accounts = {a.id: a for a in await provider.accounts.list_accounts(user_id)}
    candidates = [
        t
        for t in await provider.transactions.list_transactions(
            user_id, start_date=since, limit=MAX_SCAN
        )
        if t.matched_transfer_id is None
        and not t.is_pending
        and not t.is_ignored
        and t.account_id in accounts
    ]

    # With exact-amount rules, a pair must share the same absolute amount, so
    # matching within each amount bucket gives the same result much faster.
    groups: list[list[Transaction]]
    if _only_exact_rules(rules):
        buckets: dict[object, list[Transaction]] = defaultdict(list)
        for tx in candidates:
            buckets[abs(tx.amount)].append(tx)
        groups = [g for g in buckets.values() if len(g) > 1]
    else:
        groups = [candidates]

    linked = 0
    for group in groups:
        for match in evaluate_transfer_rules(rules, group, accounts):
            out_tx, in_tx = match.tx_out, match.tx_in
            out_tx.is_transfer = True
            out_tx.matched_transfer_id = in_tx.id
            in_tx.is_transfer = True
            in_tx.matched_transfer_id = out_tx.id
            await provider.transactions.update_transaction(out_tx)
            await provider.transactions.update_transaction(in_tx)
            linked += 1
    return linked


async def unlink_transfer(provider: DataProvider, tx: Transaction) -> None:
    """Undo a transfer marking on ``tx`` and on its paired leg, if any."""
    partner_id = tx.matched_transfer_id
    tx.is_transfer = False
    tx.matched_transfer_id = None
    await provider.transactions.update_transaction(tx)
    if partner_id:
        partner = await provider.transactions.get_transaction(partner_id)
        if partner and partner.matched_transfer_id == tx.id:
            partner.is_transfer = False
            partner.matched_transfer_id = None
            await provider.transactions.update_transaction(partner)


def is_debt_payment(tx: Transaction) -> bool:
    """Uncategorized money in on a credit card or loan: a payment, not income.
    (Card refunds usually get a category from a rule or by hand and are kept.)"""
    return (
        tx.amount > 0
        and tx.category_id is None
        and not tx.is_transfer
        and not tx.is_ignored
        and tx.matched_transfer_id is None
    )


async def mark_debt_payments(
    provider: DataProvider,
    user_id: UUID,
    account_id: UUID,
    since: date | None = None,
    only: set[UUID] | None = None,
) -> int:
    """Mark payments into a credit card / loan account as transfers (not income).

    Called when an account becomes a credit card or loan (all its history) and
    after a sync (``only`` the new transactions, so ones you un-marked stay that
    way). Returns how many were marked."""
    account = await provider.accounts.get_account(account_id)
    if account is None or account.account_type not in DEBT_ACCOUNT_TYPES:
        return 0
    marked = 0
    for tx in await provider.transactions.list_transactions(
        user_id, account_id=account_id, start_date=since, limit=MAX_SCAN
    ):
        if (only is None or tx.id in only) and is_debt_payment(tx):
            tx.is_transfer = True
            await provider.transactions.update_transaction(tx)
            marked += 1
    return marked
