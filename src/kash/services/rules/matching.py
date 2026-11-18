"""Transfer and Recurring matching rules engine for Kash.

Evaluates multi-leg transactions (transfer link / suggestion) and
recurring bill detection matches based on user-defined matching criteria.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any
from uuid import UUID

from kash.domain.models import Account, Bill, Rule, Transaction
from kash.services import formatting


@dataclass
class TransferMatchResult:
    """Result of evaluating a transfer matching rule between two transactions."""

    rule_id: UUID
    rule_name: str
    action_mode: str  # 'links' or 'suggests'
    tx_out: Transaction
    tx_in: Transaction
    reasons: list[str] = field(default_factory=list)
    confidence: float = 1.0


@dataclass
class RecurringMatchResult:
    """Result of evaluating a recurring matching rule against a transaction and bill."""

    rule_id: UUID
    rule_name: str
    action_mode: str  # 'links' or 'suggests'
    transaction: Transaction
    bill: Bill
    reasons: list[str] = field(default_factory=list)
    confidence: float = 1.0


FIELD_LABELS = {
    "description": "Description",
    "original_description": "Original description",
    "merchant": "Merchant",
    "amount": "Amount",
    "account_id": "Account",
    "date": "Date",
    "type": "Type",
}
OP_LABELS = {
    "contains": "contains",
    "not_contains": "doesn't contain",
    "starts_with": "starts with",
    "ends_with": "ends with",
    "equals": "is",
    "not_equals": "isn't",
    "regex": "matches",
    "gt": "is over",
    "gte": "is at least",
    "lt": "is under",
    "lte": "is at most",
}


@dataclass(frozen=True)
class ConditionSummary:
    """One line of a categorization rule's conditions on the Rules page."""

    label: str  # "Description contains any of"
    values: list[str]  # shown as chips
    plain: bool = False  # sentences (a group of conditions), not patterns


def regex_alternatives(pattern: str) -> list[str]:
    """The top-level alternatives of a regex: ``a|b(c|d)`` -> ``["a", "b(c|d)"]``.

    A ``|`` inside parentheses or a [...] class, or escaped, doesn't split.
    """
    parts: list[str] = []
    current, depth, in_class, i = "", 0, False, 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\" and i + 1 < len(pattern):
            current += pattern[i : i + 2]
            i += 2
            continue
        if in_class:
            in_class = ch != "]"
        elif ch == "[":
            in_class = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "|" and depth == 0:
            parts.append(current)
            current, i = "", i + 1
            continue
        current += ch
        i += 1
    parts.append(current)
    return [part.strip() for part in parts if part.strip()]


def _condition_sentence(condition: dict[str, Any]) -> str:
    if "conditions" in condition:
        joiner = f" {condition.get('op', 'and')} "
        return "(" + joiner.join(_condition_sentence(c) for c in condition["conditions"]) + ")"
    field_name = FIELD_LABELS.get(condition.get("field", ""), condition.get("field", ""))
    op = OP_LABELS.get(condition.get("op", ""), condition.get("op", ""))
    return f"{field_name} {op} “{condition.get('value', '')}”"


def describe_conditions(rule: Rule) -> list[ConditionSummary]:
    """A categorization rule's conditions, short enough for a phone.

    One line per field and test: in an "or" rule, tests of the same field and kind are
    merged ("Description contains any of A, B"), and a regex is split into its
    alternatives. A group of conditions (from "add to existing rule") is one line of
    sentences. Lines are joined by the rule's and/or.
    """
    lines: list[tuple[str, str, list[str]]] = []  # (field, op, values) or ("", label, sentences)
    for condition in rule.conditions_data:
        if "conditions" in condition:
            label = "All of" if condition.get("op", "and") == "and" else "Any of"
            lines.append(("", label, [_condition_sentence(c) for c in condition["conditions"]]))
            continue
        field_name = str(condition.get("field") or "description")
        op = str(condition.get("op") or "contains")
        value = str(condition.get("value", ""))
        values = (regex_alternatives(value) or [value]) if op == "regex" else [value]
        same = [line for line in lines if line[0] == field_name and line[1] == op]
        if rule.conditions_op == "or" and same:
            same[0][2].extend(values)
        else:
            lines.append((field_name, op, values))

    summaries = []
    for field_name, op, values in lines:
        if not field_name:
            summaries.append(ConditionSummary(op, values, plain=True))
            continue
        label = f"{FIELD_LABELS.get(field_name, field_name)} {OP_LABELS.get(op, op)}"
        summaries.append(ConditionSummary(label + (" any of" if len(values) > 1 else ""), values))
    return summaries


def generate_rule_criteria_summary(rule: Rule) -> list[str]:
    """Generate human-readable criteria summary bullets for UI display."""
    bullets: list[str] = []
    config = rule.config or {}

    # If the user stored explicit criteria_summary in config, return it
    if "criteria_summary" in config and isinstance(config["criteria_summary"], list):
        return [str(s) for s in config["criteria_summary"] if s]

    if rule.rule_type == "transfer":
        # 1. Accounts
        if config.get("different_accounts", True):
            bullets.append("two different accounts")
        elif config.get("same_account", False):
            bullets.append("the same account")

        # 2. Description naming
        if config.get("description_names_other_account", False):
            bullets.append("one description names the other account")

        # 3. Amount closeness
        amount_mode = config.get("amount_match_mode", "exact")
        if amount_mode == "exact":
            bullets.append("the amount matches exactly")
        elif amount_mode in ("within_percent", "percent"):
            pct = config.get("amount_tolerance_percent", 1.0)
            bullets.append(f"the amount is within {pct:.2f}%")
        elif amount_mode == "within_amount":
            tol = config.get("amount_tolerance_amount", 1.0)
            bullets.append(f"the amount is within {formatting.money(tol)}")

        # 4. Dates
        early = config.get("days_early", 2)
        late = config.get("days_late", 2)
        bullets.append(f"up to {early} days early, {late} days late")

        # 5. Closest date
        if config.get("closest_date_wins", True):
            bullets.append("the closest date wins")

        # 6. Only one candidate
        if config.get("unique_candidate", True):
            bullets.append("only one candidate fits")

        # 7. Description similarity
        sim = config.get("description_similarity")
        if sim and str(sim).lower() != "off":
            bullets.append(f"the description is at least {float(sim):.2f} similar")

    elif rule.rule_type == "recurring":
        bullets.append("when money comes in or goes out")
        if config.get("same_account", True):
            bullets.append("the same account")

        amount_mode = config.get("amount_match_mode", "exact")
        if amount_mode == "exact":
            bullets.append("the amount matches exactly")
        elif amount_mode in ("within_percent", "percent"):
            pct = config.get("amount_tolerance_percent", 1.0)
            bullets.append(f"the amount is within {pct:.2f}%")

        early = config.get("days_early", 3)
        late = config.get("days_late", 5)
        bullets.append(f"up to {early} days early, {late} days late")

        sim = config.get("description_similarity", 0.6)
        if sim and str(sim).lower() != "off":
            bullets.append(f"the description is at least {float(sim):.2f} similar")

        if config.get("closest_date_wins", True):
            bullets.append("the closest date wins")

    return bullets


def evaluate_transfer_rules(
    rules: list[Rule],
    transactions: list[Transaction],
    accounts_by_id: dict[UUID, Account] | None = None,
) -> list[TransferMatchResult]:
    """Evaluate transfer rules across unpaired transactions to link or suggest transfer pairs."""
    transfer_rules = [r for r in rules if r.is_active and r.rule_type == "transfer"]
    if not transfer_rules or not transactions:
        return []

    # Sort transfer rules by priority
    transfer_rules.sort(key=lambda r: (r.priority, r.id))
    accounts_map = accounts_by_id or {}

    # Separate unlinked debits (money out) and unlinked credits (money in)
    unpaired_debits = [
        tx
        for tx in transactions
        if not tx.is_ignored and tx.amount < Decimal("0") and not tx.matched_transfer_id
    ]
    unpaired_credits = [
        tx
        for tx in transactions
        if not tx.is_ignored and tx.amount > Decimal("0") and not tx.matched_transfer_id
    ]

    matched_pairs: list[TransferMatchResult] = []
    paired_tx_ids: set[UUID] = set()

    for rule in transfer_rules:
        cfg = rule.config or {}
        days_early = int(cfg.get("days_early", 2))
        days_late = int(cfg.get("days_late", 2))
        amount_mode = cfg.get("amount_match_mode", "exact")
        tolerance_pct = float(cfg.get("amount_tolerance_percent", 1.0))
        require_diff_accounts = cfg.get("different_accounts", True)
        desc_names_account = cfg.get("description_names_other_account", False)
        unique_candidate = cfg.get("unique_candidate", True)
        allowed_acc_types = cfg.get("account_types")  # list of str e.g. ['checking', 'savings']

        for tx_out in unpaired_debits:
            if tx_out.id in paired_tx_ids:
                continue

            candidates: list[tuple[Transaction, int, list[str]]] = []

            for tx_in in unpaired_credits:
                if tx_in.id in paired_tx_ids:
                    continue

                # 1. Accounts check
                if require_diff_accounts and tx_out.account_id == tx_in.account_id:
                    continue

                acc_out = accounts_map.get(tx_out.account_id)
                acc_in = accounts_map.get(tx_in.account_id)

                if allowed_acc_types and acc_out and acc_in:
                    out_type = getattr(acc_out.account_type, "value", str(acc_out.account_type))
                    in_type = getattr(acc_in.account_type, "value", str(acc_in.account_type))
                    if out_type not in allowed_acc_types and in_type not in allowed_acc_types:
                        continue

                # 2. Date check
                # Date difference: days between out and in
                date_diff = (tx_in.date - tx_out.date).days
                if date_diff < -days_early or date_diff > days_late:
                    continue

                # 3. Amount closeness check
                amt_out = abs(tx_out.amount)
                amt_in = abs(tx_in.amount)
                reasons: list[str] = []

                if amount_mode == "exact":
                    if amt_out != amt_in:
                        continue
                    reasons.append("Exact amount match")
                elif amount_mode in ("within_percent", "percent"):
                    max_amt = max(amt_out, amt_in)
                    if max_amt > 0:
                        diff_pct = float(abs(amt_out - amt_in) / max_amt) * 100.0
                        if diff_pct > tolerance_pct:
                            continue
                        reasons.append(f"Amount within {diff_pct:.2f}%")
                else:
                    if amt_out != amt_in:
                        continue

                # 4. Description names other account
                if desc_names_account and acc_out and acc_in:
                    names_match = (
                        acc_out.name.lower() in tx_in.clean_description.lower()
                        or acc_in.name.lower() in tx_out.clean_description.lower()
                    )
                    if not names_match:
                        continue
                    reasons.append("Description names other account")

                candidates.append((tx_in, abs(date_diff), reasons))

            if not candidates:
                continue

            # If unique candidate required and more than 1 fits on the same date
            if unique_candidate and len(candidates) > 1:
                # Check if multiple candidates share the exact same minimum date difference
                candidates.sort(key=lambda c: c[1])
                if candidates[0][1] == candidates[1][1]:
                    # Ambiguous, cannot safely auto-pair under unique_candidate constraint
                    continue

            # Closest date wins
            candidates.sort(key=lambda c: c[1])
            best_tx_in, _, reasons = candidates[0]

            matched_pairs.append(
                TransferMatchResult(
                    rule_id=rule.id,
                    rule_name=rule.name,
                    action_mode=rule.action_mode,
                    tx_out=tx_out,
                    tx_in=best_tx_in,
                    reasons=reasons,
                    confidence=1.0 if rule.action_mode == "links" else 0.85,
                )
            )
            paired_tx_ids.add(tx_out.id)
            paired_tx_ids.add(best_tx_in.id)

    return matched_pairs


def evaluate_recurring_rules(
    rules: list[Rule],
    transactions: list[Transaction],
    bills: list[Bill],
) -> list[RecurringMatchResult]:
    """Evaluate recurring matching rules against transactions and scheduled bills."""
    recurring_rules = [r for r in rules if r.is_active and r.rule_type == "recurring"]
    if not recurring_rules or not transactions or not bills:
        return []

    recurring_rules.sort(key=lambda r: (r.priority, r.id))
    matches: list[RecurringMatchResult] = []

    for rule in recurring_rules:
        cfg = rule.config or {}
        days_early = int(cfg.get("days_early", 3))
        days_late = int(cfg.get("days_late", 5))
        amount_mode = cfg.get("amount_match_mode", "exact")
        sim_threshold = float(cfg.get("description_similarity", 0.60))
        require_same_account = cfg.get("same_account", True)

        for bill in bills:
            for tx in transactions:
                if tx.is_ignored:
                    continue

                if require_same_account and bill.account_id and tx.account_id != bill.account_id:
                    continue

                # Amount check
                amt_tx = abs(tx.amount)
                amt_bill = abs(bill.expected_amount)
                if amount_mode == "exact":
                    if amt_tx != amt_bill:
                        continue
                elif amount_mode in ("within_percent", "percent"):
                    tolerance = float(cfg.get("amount_tolerance_percent", 1.0))
                    if amt_bill > 0:
                        diff_pct = float(abs(amt_tx - amt_bill) / amt_bill) * 100.0
                        if diff_pct > tolerance:
                            continue

                # Date proximity to next_due_date
                date_diff = (tx.date - bill.next_due_date).days
                if date_diff < -days_early or date_diff > days_late:
                    continue

                # Description similarity
                if sim_threshold > 0:
                    sim = SequenceMatcher(
                        None,
                        tx.clean_description.lower(),
                        bill.merchant_name.lower(),
                    ).ratio()
                    if (
                        sim < sim_threshold
                        and bill.merchant_name.lower() not in tx.clean_description.lower()
                    ):
                        continue

                matches.append(
                    RecurringMatchResult(
                        rule_id=rule.id,
                        rule_name=rule.name,
                        action_mode=rule.action_mode,
                        transaction=tx,
                        bill=bill,
                        reasons=[
                            "Same account" if require_same_account else "Account match",
                            "Expected amount match",
                            f"Date within {days_early} early / {days_late} late",
                        ],
                    )
                )

    return matches
