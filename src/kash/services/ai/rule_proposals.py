"""Rule proposals learned from the categories on your transactions.

From categorized transactions that no rule handles yet (set by hand, an accepted AI
guess, or applied by the AI), Kash proposes:

- ``add_condition``: add a merchant condition to the user's existing rule for
  that category (preferred), or
- ``create_rule``: a new rule for the category when there is none.

A proposal exists when there are at least ``MIN_EVIDENCE`` transactions from the
merchant in that category, and the condition doesn't match any transaction that has a
different category (so one filed elsewhere blocks it). Words like Zelle or Venmo never
make a rule by themselves.

Right after the user categorizes a transaction (``auto_apply``), the proposals it is
evidence for are applied automatically when a person chose or accepted at least one of
the categories involved; the rest wait for approval on the AI page. Everything the AI
adds is listed there and can be removed.

Conditions target the merchant (the bank's payee) with ``contains``, using the
words every piece of evidence shares, so store numbers and cities don't matter.
The raw description is used only when a transaction has no merchant.

Conditions the AI added are listed in the rule's ``config["ai_conditions"]``;
only those can be removed from the AI page. User-written conditions are never
edited or removed here.
"""

import hashlib
from dataclasses import dataclass, field, replace
from typing import Any
from uuid import UUID

from kash.domain.models import (
    Category,
    Rule,
    RuleAction,
    RuleCondition,
    SuggestionStatus,
    Transaction,
)
from kash.domain.protocols import DataProvider
from kash.services.ai import options as ai_options
from kash.services.bills.merchant import common_prefix, normalize_merchant, transaction_merchant
from kash.services.rules.engine import (
    add_alternative_condition,
    conditions_match,
    match_condition,
    normalize_text,
    rule_category_id,
)

MIN_EVIDENCE = 2  # the one just categorized plus an earlier one
MIN_PATTERN = 3
SCAN_LIMIT = 100_000
AI_CONDITIONS = "ai_conditions"  # rule.config key listing conditions the AI added
# Words that say nothing about who was paid; a pattern of only these is useless.
GENERIC = {
    "POS", "POINT", "OF", "SALE", "WITHDRAWAL", "DEBIT", "CREDIT", "CARD", "PURCHASE",
    "PAYMENT", "PAYMENTS", "DEPOSIT", "TRANSFER", "ONLINE", "MOBILE", "EXTERNAL", "ACH",
    "CHECK", "ATM", "FEE", "THE", "INC", "LLC", "CO", "WWW", "COM", "STORE", "RECURRING",
    # Payments to or from people can be anything, so they never make a rule by themselves.
    "ZELLE", "VENMO", "CASHAPP", "PAYPAL", "XOOM", "REMITLY", "MONEYGRAM", "WESTERN", "UNION",
}  # fmt: skip
EVIDENCE = (SuggestionStatus.AUTO_APPLIED, SuggestionStatus.ACCEPTED, SuggestionStatus.CHANGED)


@dataclass
class Proposal:
    key: str
    kind: str  # "add_condition" | "create_rule"
    category: Category
    condition: dict[str, str]
    evidence: list[Transaction]
    rule: Rule | None = None  # the rule to extend (add_condition)
    will_categorize: int = 0  # uncategorized transactions it would categorize now
    merchant: str = ""
    user_confirmed: bool = False  # a person chose or accepted at least one of the categories

    @property
    def reason(self) -> str:
        n = len(self.evidence)
        return (
            f"{n} transaction{'s' if n != 1 else ''} from this merchant "
            f"{'were' if n != 1 else 'was'} all categorized as {self.category.name}, "
            "and no other transaction it matches has a different category."
        )

    def as_dict(self) -> dict[str, Any]:
        """The structured proposal (e.g. for the page or an API)."""
        data: dict[str, Any] = {
            "type": self.kind,
            "condition": self.condition,
            "reason": self.reason,
            "category_id": str(self.category.id),
        }
        if self.rule is not None:
            data["rule_id"] = str(self.rule.id)
        return data


def _key(kind: str, target: str, condition: dict[str, str]) -> str:
    raw = f"{kind}|{target}|{condition['field']}|{condition['op']}|{condition['value']}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _words(text: str) -> list[str]:
    return normalize_text(text).split()


def _stable_prefix(texts: list[str]) -> str:
    """The leading words all texts share, without trailing numbers/store codes."""
    words = common_prefix([_words(t) for t in texts])
    while words and (not any(ch.isalpha() for ch in words[-1]) or words[-1].startswith("#")):
        words.pop()
    return " ".join(words)


def condition_for(txs: list[Transaction]) -> dict[str, str] | None:
    """A ``contains`` condition that matches every transaction, merchant first."""
    candidates: list[tuple[str, str]] = []
    if all(t.merchant_name for t in txs):
        candidates.append(("merchant", _stable_prefix([t.merchant_name or "" for t in txs])))
    # Fallback: the normalized merchant key (store numbers, POS wording removed),
    # checked against the description.
    keys = [normalize_merchant(t.clean_description or t.original_description) for t in txs]
    candidates.append(("description", _stable_prefix(keys)))
    for fld, value in candidates:
        meaningful = [w for w in value.split() if w not in GENERIC]
        if len(value) < MIN_PATTERN or not meaningful:
            continue
        condition = {"field": fld, "op": "contains", "value": value}
        if all(match_condition(condition, t) for t in txs):
            return condition
    return None


def _category_rules(rules: list[Rule]) -> dict[str, list[Rule]]:
    by_category: dict[str, list[Rule]] = {}
    for rule in sorted(rules, key=lambda r: (r.priority, str(r.id))):
        cat = rule_category_id(rule)
        if cat:
            by_category.setdefault(cat, []).append(rule)
    return by_category


async def find_proposals(provider: DataProvider, user_id: UUID) -> list[Proposal]:
    """Current proposals (dismissed ones left out), most evidence first."""
    saved = await provider.settings.get_settings(user_id)
    dismissed = set(ai_options.from_stored(saved.ai_options).dismissed_proposals)
    categories = {
        c.id: c
        for c in await provider.categories.list_categories(user_id, include_hidden=True)
        if not c.is_hidden and not c.is_transfer
    }
    txs = await provider.transactions.list_transactions(user_id, limit=SCAN_LIMIT)
    rules = [
        r
        for r in await provider.rules.list_rules(user_id, rule_type="categorization")
        if r.is_active
    ]
    ai_status = {s.transaction_id: s.status for s in await provider.ai.list_suggestions(user_id)}

    # Evidence: categorized transactions no rule handles yet, however they got their
    # category (by hand, accepted AI guess, or applied by the AI). The current category
    # is the truth. Grouped by the merchant's first word and the category, so the same
    # chain at different stores ("Zippy Fuel #12 Milford", "Zippy Fuel New Haven") is one
    # group; the words they share become the pattern.
    groups: dict[tuple[str, UUID], list[Transaction]] = {}
    confirmed: set[UUID] = set()  # transactions whose category a person chose or accepted
    for tx in txs:
        if tx.category_id not in categories or tx.is_transfer or tx.is_ignored or tx.is_pending:
            continue
        if any(conditions_match(r.conditions_op, r.conditions_data, tx) for r in rules):
            continue  # a rule already covers it
        words = normalize_merchant(transaction_merchant(tx)).split()
        if not words:
            continue
        groups.setdefault((words[0], tx.category_id), []).append(tx)  # type: ignore[arg-type]
        if ai_status.get(tx.id) != SuggestionStatus.AUTO_APPLIED:
            confirmed.add(tx.id)

    by_category = _category_rules(rules)
    proposals: list[Proposal] = []
    for (_, category_id), evidence in groups.items():
        if len(evidence) < MIN_EVIDENCE:
            continue
        category = categories[category_id]
        # Already handled by a rule (e.g. one the user added since)?
        condition = condition_for(evidence)
        if condition is None:
            continue
        matches = [t for t in txs if match_condition(condition, t)]
        if any(t.category_id not in (None, category.id) for t in matches if not t.is_transfer):
            continue  # it would also match transactions filed elsewhere
        target_rules = by_category.get(str(category.id), [])
        rule = target_rules[0] if target_rules else None
        kind = "add_condition" if rule else "create_rule"
        key = _key(kind, str(rule.id if rule else category.id), condition)
        if key in dismissed:
            continue
        proposals.append(
            Proposal(
                key=key,
                kind=kind,
                category=category,
                condition=condition,
                evidence=sorted(evidence, key=lambda t: t.date, reverse=True),
                rule=rule,
                will_categorize=sum(
                    1
                    for t in matches
                    if t.category_id is None and not t.is_transfer and not t.is_ignored
                ),
                merchant=evidence[0].merchant_name or condition["value"],
                user_confirmed=any(t.id in confirmed for t in evidence),
            )
        )
    proposals.sort(key=lambda p: -len(p.evidence))
    return proposals


async def apply_proposal(provider: DataProvider, user_id: UUID, key: str) -> Proposal | None:
    """Approve a proposal: extend/create the rule, then categorize uncategorized
    transactions it matches. Returns None if the proposal no longer applies."""
    proposal = next((p for p in await find_proposals(provider, user_id) if p.key == key), None)
    if proposal is None:
        return None
    condition = proposal.condition
    if proposal.rule is not None:
        rule = proposal.rule
        if add_alternative_condition(rule, condition):
            rule.config = {**rule.config, AI_CONDITIONS: [*_ai_conditions(rule), condition]}
            await provider.rules.update_rule(rule)
    else:
        existing = await provider.rules.list_rules(user_id, rule_type="categorization")
        await provider.rules.create_rule(
            Rule(
                user_id=user_id,
                name=proposal.category.name,
                # After every existing rule, so the user's rules always win.
                priority=max((r.priority for r in existing), default=0) + 10,
                conditions_op="or",
                conditions=[RuleCondition(**condition)],
                actions=[RuleAction(op="set_category", value=str(proposal.category.id))],
                config={"origin": "ai_suggested", AI_CONDITIONS: [condition]},
            )
        )
    for tx in await provider.transactions.list_transactions(user_id, limit=SCAN_LIMIT):
        if (
            tx.category_id is None
            and not tx.is_transfer
            and not tx.is_ignored
            and match_condition(condition, tx)
        ):
            tx.category_id = proposal.category.id
            await provider.transactions.update_transaction(tx)
    return proposal


async def auto_apply(provider: DataProvider, user_id: UUID, touched: set[UUID]) -> list[Proposal]:
    """Right after the user categorized ``touched`` transactions: apply the proposals
    those transactions are evidence for, when the evidence includes a category a person
    chose or accepted (not just the AI's own answers). The rest stay proposals that wait
    for approval on the AI page. Returns what was applied."""
    applied = []
    for proposal in await find_proposals(provider, user_id):
        if proposal.user_confirmed and any(t.id in touched for t in proposal.evidence):
            if await apply_proposal(provider, user_id, proposal.key) is not None:
                applied.append(proposal)
    return applied


def added_notice(applied: list[Proposal]) -> str:
    """One line for the page after rules were added automatically."""
    parts = [f"{p.condition['value']} → {p.category.name}" for p in applied]
    noun = "rule" if len(parts) == 1 else "rules"
    return f"Added {noun}: {'; '.join(parts)}. Review or remove it on the AI activity page."


async def dismiss_proposal(provider: DataProvider, user_id: UUID, key: str) -> None:
    saved = await provider.settings.get_settings(user_id)
    opts = ai_options.from_stored(saved.ai_options)
    if key in opts.dismissed_proposals:
        return
    updated = ai_options.to_stored(
        replace(opts, dismissed_proposals=(*opts.dismissed_proposals, key))
    )
    await provider.settings.save_settings(saved.model_copy(update={"ai_options": updated}))


def _ai_conditions(rule: Rule) -> list[dict[str, Any]]:
    items = (rule.config or {}).get(AI_CONDITIONS)
    return [c for c in items if isinstance(c, dict)] if isinstance(items, list) else []


@dataclass
class AICondition:
    rule: Rule
    index: int
    condition: dict[str, Any]
    active: bool = field(default=True)  # still in the rule (the user may have edited it)


async def ai_added_conditions(provider: DataProvider, user_id: UUID) -> list[AICondition]:
    """Conditions the AI added to rules (after the user approved them)."""
    found = []
    for rule in await provider.rules.list_rules(user_id, rule_type="categorization"):
        present = rule.conditions_data
        for i, cond in enumerate(_ai_conditions(rule)):
            found.append(AICondition(rule, i, cond, cond in present))
    return found


async def remove_ai_condition(
    provider: DataProvider, user_id: UUID, rule_id: UUID, index: int
) -> bool:
    """Remove a condition the AI added (only those). A rule the AI created is
    deleted once it has no conditions left."""
    rule = await provider.rules.get_rule(rule_id)
    if rule is None or rule.user_id != user_id:
        return False
    ai_conds = _ai_conditions(rule)
    if not 0 <= index < len(ai_conds):
        return False
    cond = ai_conds.pop(index)
    remaining = [c for c in rule.conditions_data if c != cond]
    if not remaining and (rule.config or {}).get("origin") == "ai_suggested":
        await provider.rules.delete_rule(rule.id)
        return True
    rule.conditions = remaining  # type: ignore[assignment]
    rule.config = {**rule.config, AI_CONDITIONS: ai_conds}
    await provider.rules.update_rule(rule)
    return True
