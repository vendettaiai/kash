"""Rule engine and preview service package."""

from kash.services.rules.default_rules import (
    DEFAULT_CATEGORY_GROUPS,
    UNIVERSAL_DEFAULT_RULES,
)
from kash.services.rules.engine import (
    apply_rule_actions,
    apply_rules_to_transaction,
    compile_rule_regex,
    conditions_match,
    evaluate_conditions,
    match_condition,
    normalize_text,
    strip_accents,
)
from kash.services.rules.matching import (
    RecurringMatchResult,
    TransferMatchResult,
    evaluate_recurring_rules,
    evaluate_transfer_rules,
    generate_rule_criteria_summary,
)
from kash.services.rules.preview import (
    RulePreviewItem,
    RulePreviewResult,
    preview_rule,
)

__all__ = [
    "DEFAULT_CATEGORY_GROUPS",
    "UNIVERSAL_DEFAULT_RULES",
    "RecurringMatchResult",
    "RulePreviewItem",
    "RulePreviewResult",
    "TransferMatchResult",
    "apply_rule_actions",
    "apply_rules_to_transaction",
    "compile_rule_regex",
    "conditions_match",
    "evaluate_conditions",
    "evaluate_recurring_rules",
    "evaluate_transfer_rules",
    "generate_rule_criteria_summary",
    "match_condition",
    "normalize_text",
    "preview_rule",
    "strip_accents",
]
