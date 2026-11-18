"""Transaction rules engine.

Rules are plain data: a list of conditions (optionally nested in and/or groups)
plus a list of actions. This module evaluates them against ``Transaction``
objects in memory and never touches storage, so it is cheap to test and safe to
use for previews.

Matching is accent- and case-insensitive ("Café" matches "CAFE"). A rule that
does not match a transaction's cleaned-up description is retried against the
original bank description, so renaming a transaction never breaks the rules
that categorized it.
"""

from __future__ import annotations

import operator
import re
import unicodedata
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any
from uuid import UUID

from kash.domain.models import Rule, Transaction

MAX_PATTERN_LENGTH = 500

# A quantified group that is itself quantified, e.g. (a+)+ or (\w*)*: the classic
# catastrophic-backtracking shape. Python's `re` has no timeout, so reject it up front.
_NESTED_QUANTIFIER = re.compile(r"\((?:[^()\\]|\\.)*[+*}](?:[^()\\]|\\.)*\)\s*(?:[+*]|\{\d)")


# ---------------------------------------------------------------------------
# Text normalization
# ---------------------------------------------------------------------------


def strip_accents(text: str) -> str:
    """Remove diacritics while keeping the original letter case ("Café" -> "Cafe")."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def normalize_text(text: str) -> str:
    """Canonical form used for all text comparisons: accent-free upper case."""
    return strip_accents(text).upper()


# ---------------------------------------------------------------------------
# Regex safety
# ---------------------------------------------------------------------------


def compile_rule_regex(pattern: str) -> re.Pattern[str]:
    """Validate and compile a user-supplied regex (case-insensitive).

    Raises ValueError for empty, oversized, invalid, backtracking-prone, or
    match-everything patterns.
    """
    source = str(pattern or "").strip()
    if not source:
        raise ValueError("Regular expression cannot be empty")
    if len(source) > MAX_PATTERN_LENGTH:
        raise ValueError(
            f"Regular expression pattern is too long (max {MAX_PATTERN_LENGTH} characters)"
        )
    if _NESTED_QUANTIFIER.search(source):
        raise ValueError(
            "Regular expression uses nested quantifiers like (a+)+, which can hang the server"
        )
    return _compile_cached(strip_accents(source))


@lru_cache(maxsize=512)
def _compile_cached(source: str) -> re.Pattern[str]:
    try:
        compiled = re.compile(source, re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"Invalid regular expression syntax: {exc}") from exc
    if compiled.search("") is not None:
        raise ValueError("Regular expression must not match an empty string")
    return compiled


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


def _description(tx: Transaction) -> str:
    return tx.clean_description or tx.original_description or ""


# Field name (including accepted aliases) -> value extractor.
_FIELDS: dict[str, Callable[[Transaction], Any]] = {
    "description": _description,
    "clean_description": _description,
    "original_description": lambda tx: tx.original_description or "",
    "merchant": lambda tx: tx.merchant_name or "",
    "merchant_name": lambda tx: tx.merchant_name or "",
    "notes": lambda tx: tx.notes or "",
    "type": lambda tx: str(tx.type.value if hasattr(tx.type, "value") else tx.type),
    "account_id": lambda tx: str(tx.account_id),
    "date": lambda tx: tx.date,
    "amount": lambda tx: tx.amount,
    "status": lambda tx: "pending" if tx.is_pending else "posted",
    "is_pending": lambda tx: "pending" if tx.is_pending else "posted",
}

_TEXT_OPS: dict[str, Callable[[str, str], bool]] = {
    "contains": lambda text, needle: needle in text,
    "not_contains": lambda text, needle: needle not in text,
    "starts_with": lambda text, needle: text.startswith(needle),
    "ends_with": lambda text, needle: text.endswith(needle),
    "equals": operator.eq,
    "not_equals": operator.ne,
}

_COMPARE_OPS: dict[str, Callable[[Any, Any], bool]] = {
    "gt": operator.gt,
    "gte": operator.ge,
    "lt": operator.lt,
    "lte": operator.le,
}


def _as_decimal(value: object) -> Decimal | None:
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError):
        return None


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def _is_blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def match_condition(condition: Mapping[str, Any], tx: Transaction) -> bool:
    """Evaluate one leaf condition ``{"field", "op", "value"}`` against a transaction.

    Blank values never match, so a half-filled rule cannot categorize everything.
    Unknown fields or operators never match.
    """
    field = str(condition.get("field", ""))
    op = str(condition.get("op", ""))
    expected = condition.get("value")
    if _is_blank(expected):
        return False

    extractor = _FIELDS.get(field)
    actual = extractor(tx) if extractor else getattr(tx, field, None)

    if op in _TEXT_OPS:
        return _TEXT_OPS[op](normalize_text(str(actual or "")), normalize_text(str(expected)))

    if op == "regex":
        try:
            pattern = compile_rule_regex(str(expected))
        except ValueError:
            return False
        return pattern.search(normalize_text(str(actual or ""))) is not None

    if op in _COMPARE_OPS:
        coerce = _as_date if field == "date" else _as_decimal
        left, right = coerce(actual), coerce(expected)
        if left is None or right is None:
            return False
        return _COMPARE_OPS[op](left, right)

    return False


def _as_dict(node: Any) -> dict[str, Any]:
    if isinstance(node, dict):
        return node
    return dict(node.model_dump())


def _match_node(node: Mapping[str, Any], tx: Transaction) -> bool:
    """A node is either a leaf condition or a group ``{"op": "and"|"or", "conditions": [...]}``."""
    children = node.get("conditions")
    if isinstance(children, list):
        return _combine(str(node.get("op", "and")), children, tx)
    return match_condition(node, tx)


def _combine(joiner: str, nodes: Iterable[Any], tx: Transaction) -> bool:
    results = [_match_node(_as_dict(n), tx) for n in nodes]
    if not results:
        return False
    return any(results) if joiner.lower() == "or" else all(results)


def evaluate_conditions(conditions_op: str, conditions: Sequence[Any], tx: Transaction) -> bool:
    """True if the transaction satisfies the condition list joined by ``conditions_op``.

    Groups may be nested to any depth. An empty list never matches.
    """
    return _combine(conditions_op, conditions, tx)


def conditions_match(conditions_op: str, conditions: Sequence[Any], tx: Transaction) -> bool:
    """Like ``evaluate_conditions`` but also tries the original bank description.

    Lets a rule written against the raw bank text keep working after the
    transaction's display description has been cleaned up.
    """
    if evaluate_conditions(conditions_op, conditions, tx):
        return True
    original = tx.original_description
    if not original or original == tx.clean_description:
        return False
    as_original = tx.model_copy(update={"clean_description": original})
    return evaluate_conditions(conditions_op, conditions, as_original)


def add_alternative_condition(rule: Rule, condition: Mapping[str, Any]) -> bool:
    """Make ``rule`` also match ``condition`` (as an extra "or" alternative).

    - "or" rules and single-condition rules just get the condition appended.
    - An "and" rule with several conditions keeps them together as one group,
      so the rule becomes: (all the old conditions) OR (the new one).

    Returns False (and changes nothing) if the rule already has that exact condition.
    """
    new = {"field": condition["field"], "op": condition["op"], "value": condition["value"]}
    existing = [_as_dict(c) for c in rule.conditions]
    if rule.conditions_op == "or" or len(existing) <= 1:
        if any(c.get("conditions") is None and c == new for c in existing):
            return False
        rule.conditions = [*existing, new]
    else:
        rule.conditions = [{"op": "and", "conditions": existing}, new]
    rule.conditions_op = "or"
    return True


def rule_category_id(rule: Rule) -> str | None:
    """The category a categorization rule assigns (its first set_category action)."""
    for action in rule.actions:
        act = _as_dict(action)
        if act.get("op") == "set_category" and act.get("value"):
            return str(act["value"])
    return None


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def merge_notes(existing: str | None, incoming: str | None) -> str:
    """Append words from ``incoming`` that ``existing`` does not already contain."""
    current = (existing or "").split()
    additions = [word for word in (incoming or "").split() if word not in current]
    return " ".join(current + additions)


def _truthy(value: object) -> bool:
    return str(value).strip().lower() in {"true", "1", "yes"}


def apply_rule_actions(
    actions: Sequence[Any],
    tx: Transaction,
    category_already_set: bool,
    *,
    hidden_category_ids: Collection[UUID] | None = None,
    overwrite_existing_category: bool = False,
) -> bool:
    """Apply actions to ``tx`` in place and return the updated "category is set" flag.

    ``set_category`` is skipped when a category is already set (unless
    ``overwrite_existing_category``) or when the target category is hidden.
    """
    for raw in actions:
        action = _as_dict(raw)
        op = action.get("op")
        value = action.get("value")
        text = str(value or "").strip()

        if op == "set_category":
            if category_already_set and not overwrite_existing_category:
                continue
            try:
                category_id = UUID(text)
            except ValueError:
                continue
            if hidden_category_ids and category_id in hidden_category_ids:
                continue
            tx.category_id = category_id
            category_already_set = True
        elif op in ("set_description", "set_clean_description") and text:
            if not tx.original_description:
                tx.original_description = tx.clean_description
            tx.clean_description = text
        elif op in ("set_merchant", "set_payee") and text:
            tx.merchant_name = text
        elif op == "append_notes" and text:
            tx.notes = merge_notes(tx.notes, text)
        elif op == "ignore":
            tx.is_ignored = True
        elif op in ("mark_transfer", "set_transfer"):
            tx.is_transfer = _truthy(value)

    return category_already_set


def _rule_dicts(items: Sequence[Any]) -> list[dict[str, Any]]:
    return [_as_dict(item) for item in items]


def apply_rules_to_transaction(
    rules: Sequence[Rule],
    tx: Transaction,
    *,
    skip_category_rules: bool = False,
    hidden_category_ids: Collection[UUID] | None = None,
    overwrite_existing_category: bool = False,
) -> list[Rule]:
    """Run active rules in priority order against ``tx`` (mutated in place).

    The first matching rule that sets a category wins; later rules can still
    rename, tag, or ignore the transaction. Returns the rules that matched.
    """
    category_set = skip_category_rules or (
        tx.category_id is not None and not overwrite_existing_category
    )
    matched: list[Rule] = []

    for rule in sorted((r for r in rules if r.is_active), key=lambda r: (r.priority, str(r.id))):
        if not conditions_match(rule.conditions_op, _rule_dicts(rule.conditions), tx):
            continue
        matched.append(rule)
        category_set = apply_rule_actions(
            _rule_dicts(rule.actions),
            tx,
            category_set,
            hidden_category_ids=hidden_category_ids,
            overwrite_existing_category=overwrite_existing_category,
        )
    return matched


class RuleEngine:
    """Small facade used by the bank sync service."""

    @staticmethod
    def evaluate_and_apply(
        tx: Transaction,
        rules: Sequence[Rule],
        *,
        hidden_category_ids: Collection[UUID] | None = None,
        overwrite_existing_category: bool = False,
    ) -> tuple[Transaction, list[Rule]]:
        """Apply rules to ``tx`` and return it together with the rules that matched."""
        matched = apply_rules_to_transaction(
            rules,
            tx,
            hidden_category_ids=hidden_category_ids,
            overwrite_existing_category=overwrite_existing_category,
        )
        return tx, matched
