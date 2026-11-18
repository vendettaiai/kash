"""Recognizing existing accounts when the bank provider sends an account id Kash
doesn't know.

SimpleFIN account ids are unique only within one SimpleFIN connection to a bank. When a
bank is removed and added again in SimpleFIN Bridge, its accounts come back with new
ids; a replaced card may also come back as a new account (sometimes while the bank
still reports the old card for a while). Each account with an unknown id gets one of
three outcomes:

- **relink** (automatic): it is the same account under a new id. The existing account
  must be one the provider isn't reporting now (its connection's answer left it out,
  or its connection was removed in Kash), only it may fit, and the evidence is strong:
  same currency, same bank when both know it, and the same last 4 digits plus the same
  name or shared transactions, or (without digits) the same name plus shared
  transactions. Also: the same provider id on an account whose connection was removed.
- **ask**: a new account at a bank where the user already has a bank account of the
  same kind (card and card, checking and checking) in that currency, or one that looks
  like an existing account without being sure: it might replace one (a new card
  number), or be another one. Nothing is created or imported until the
  user answers on the Accounts page: new account, or replacing which one.
- **new**: anything else (the first card at a bank, a bank newly added in SimpleFIN,
  a savings account next to a checking account). A connection's first sync never asks.
"""

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from uuid import UUID

from kash.domain.models import Account, ExternalAccount, ExternalTransaction, Transaction

TxKey = tuple[date, Decimal, str]

# A reference number, masked or not ("xxxx1358", "88881358", "****1358", "#0324")
_REFERENCE = re.compile(r"[0-9x*#]{4,}")


def normalize_name(name: str | None) -> str:
    """Case- and spacing-insensitive form of an account name."""
    return " ".join((name or "").lower().split())


def normalize_description(description: str) -> str:
    """Form of a transaction description that two records of one bank transaction
    share: case- and spacing-insensitive, and reference numbers count by their last 4
    characters only, because banks mask them differently over time (the same Zelle
    transfer is "TRNSFR88881358" in one answer and "TRNSFRXXXX1358" in another)."""
    return _REFERENCE.sub(lambda m: m.group()[-4:], normalize_name(description))[:60]


def transaction_key(tx_date: date, amount: Decimal, description: str) -> TxKey:
    """What makes two records the same bank transaction, ignoring provider ids."""
    return (tx_date, Decimal(amount).quantize(Decimal("0.01")), normalize_description(description))


def history_keys(transactions: Iterable[Transaction]) -> Counter[TxKey]:
    return Counter(transaction_key(t.date, t.amount, t.original_description) for t in transactions)


def incoming_keys(transactions: Iterable[ExternalTransaction]) -> dict[str, Counter[TxKey]]:
    """Keys of incoming transactions per provider account id."""
    keys: dict[str, Counter[TxKey]] = {}
    for t in transactions:
        keys.setdefault(t.account_external_id, Counter())[
            transaction_key(t.posted_at.date(), t.amount, t.description)
        ] += 1
    return keys


def overlap(a: Counter[TxKey], b: Counter[TxKey]) -> int:
    """How many transactions two histories have in common (each counted once)."""
    return sum((a & b).values())


def _same_name(ext: ExternalAccount, acc: Account) -> bool:
    names = {normalize_name(acc.provider_account_name), normalize_name(acc.name)} - {""}
    return normalize_name(ext.name) in names


def _same_bank(ext: ExternalAccount, acc: Account, connection_id: UUID) -> bool:
    """Same bank by its id; when either doesn't know it, the same SimpleFIN connection."""
    if ext.institution_id and acc.institution_id:
        return ext.institution_id == acc.institution_id
    return acc.connection_id in (connection_id, None)


def lookalike(ext: ExternalAccount, acc: Account) -> bool:
    """Might be the same account: same currency, nothing contradicting (another bank,
    other last 4 digits), and the same last 4 digits or the same name."""
    if ext.currency.upper() != acc.currency.upper():
        return False
    if ext.institution_id and acc.institution_id and ext.institution_id != acc.institution_id:
        return False
    if ext.mask and acc.mask and ext.mask != acc.mask:
        return False
    return bool(ext.mask and ext.mask == acc.mask) or _same_name(ext, acc)


def same_kind_at_bank(ext: ExternalAccount, acc: Account, connection_id: UUID) -> bool:
    """An existing bank account the new one could be replacing (worth asking about)."""
    return (
        acc.external_id is not None  # came from a bank, not added by hand
        and acc.account_type == ext.account_type
        and acc.currency.upper() == ext.currency.upper()
        and _same_bank(ext, acc, connection_id)
    )


def _evidence(ext: ExternalAccount, acc: Account, shared: int) -> str | None:
    """Why a lookalike is surely the same account, or None if that isn't sure."""
    same_mask = bool(ext.mask) and ext.mask == acc.mask
    same_name = _same_name(ext, acc)
    noun = "transaction" if shared == 1 else "transactions"
    if same_mask and same_name:
        return "same account name and last 4 digits"
    if same_mask and shared:
        return f"same last 4 digits and {shared} matching {noun}"
    if same_name and shared:
        return f"same account name and {shared} matching {noun}"
    return None


@dataclass(frozen=True)
class Relink:
    """Reuse ``account`` for the provider's ``external`` account."""

    account: Account
    external: ExternalAccount
    reason: str


@dataclass
class Matches:
    relinks: list[Relink] = field(default_factory=list)
    # provider account id -> existing accounts it most likely replaces, best first
    ask: dict[str, list[Account]] = field(default_factory=dict)


def find_matches(
    unknown: list[ExternalAccount],
    accounts: list[Account],
    connection_id: UUID,
    reported: set[str],
    incoming: dict[str, Counter[TxKey]],
    history: dict[UUID, Counter[TxKey]],
    ask: bool = True,
) -> Matches:
    """Decide relink / ask / new for accounts whose id no existing account has.

    ``accounts`` are all the user's accounts; ``reported`` the provider ids in this
    answer; ``incoming``/``history`` transaction keys per incoming account id and per
    existing account. ``ask=False`` (a connection's first sync) never asks.
    Accounts in neither result are new.
    """
    out = Matches()
    # Only accounts the provider isn't reporting can be the same account under a new id
    candidates = [
        a
        for a in accounts
        if a.external_id
        and (
            (a.connection_id == connection_id and a.external_id not in reported)
            or a.connection_id is None
        )
    ]
    taken: set[UUID] = set()
    remaining: list[ExternalAccount] = []

    # The same provider id on an account whose connection was removed in Kash (the
    # access token was added again): ids are random per account, so this is the account.
    detached: dict[str, list[Account]] = {}
    for acc in candidates:
        if acc.connection_id is None:
            detached.setdefault(acc.external_id or "", []).append(acc)
    for ext in unknown:
        same_id = detached.get(ext.external_id, [])
        if len(same_id) == 1 and ext.currency.upper() == same_id[0].currency.upper():
            out.relinks.append(Relink(same_id[0], ext, "same provider account id"))
            taken.add(same_id[0].id)
        else:
            remaining.append(ext)

    pool = [acc for acc in candidates if acc.id not in taken]
    looks = {ext.external_id: [acc for acc in pool if lookalike(ext, acc)] for ext in remaining}
    wanted_by = Counter(acc.id for options in looks.values() for acc in options)
    unsure: list[ExternalAccount] = []
    for ext in remaining:
        lookalikes = looks[ext.external_id]
        # Sure, and nothing else competes: neither another lookalike for this account
        # nor another incoming account that looks like the same existing one
        if len(lookalikes) == 1 and wanted_by[lookalikes[0].id] == 1:
            acc = lookalikes[0]
            shared = overlap(
                incoming.get(ext.external_id, Counter()), history.get(acc.id, Counter())
            )
            reason = _evidence(ext, acc, shared)
            if reason:
                out.relinks.append(Relink(acc, ext, reason))
                taken.add(acc.id)
                continue
        unsure.append(ext)

    if not ask:
        return out
    for ext in unsure:
        alike = {a.id for a in looks[ext.external_id]}
        options = [a for a in accounts if a.id in alike or same_kind_at_bank(ext, a, connection_id)]
        if options:
            out.ask[ext.external_id] = sorted(
                options, key=lambda a: _likelihood(ext, a, connection_id, reported)
            )
    return out


def _likelihood(
    ext: ExternalAccount, acc: Account, connection_id: UUID, reported: set[str]
) -> tuple[bool, bool, bool, bool, str]:
    """Sort key: the account most likely replaced first. One the bank stopped reporting
    beats one it still reports; then same kind, same last 4 digits, same name."""
    still_reported = acc.connection_id == connection_id and acc.external_id in reported
    return (
        still_reported,
        acc.account_type != ext.account_type,
        not (ext.mask and ext.mask == acc.mask),
        not _same_name(ext, acc),
        acc.name.lower(),
    )
