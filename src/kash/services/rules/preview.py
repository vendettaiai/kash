"""Rule testing and live preview engine for evaluating unsaved or existing rules."""

from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from kash.domain.models import Category, Transaction
from kash.services.rules.engine import apply_rule_actions, conditions_match


@dataclass
class RulePreviewItem:
    """Individual transaction preview entry comparing before and after rule evaluation."""

    transaction_id: UUID
    date: str
    amount: Decimal
    current_description: str
    new_description: str
    current_category_id: UUID | None
    current_category_name: str | None
    current_category_color: str | None
    new_category_id: UUID | None
    new_category_name: str | None
    new_category_color: str | None
    will_change: bool


@dataclass
class RulePreviewResult:
    """Aggregated preview response with metrics and sample items."""

    matched_count: int
    changed_count: int
    items: list[RulePreviewItem]


def preview_rule(
    transactions: Sequence[Transaction],
    categories: Sequence[Category],
    conditions_op: str,
    conditions: list[dict],
    actions: list[dict],
    *,
    is_active: bool = True,
    apply_to_existing: bool = True,
    overwrite_existing_categories: bool = False,
    limit: int = 25,
) -> RulePreviewResult:
    """Evaluate candidate conditions and actions against an existing set of transactions."""
    cat_map = {c.id: c for c in categories}

    matched_count = 0
    changed_count = 0
    preview_items: list[RulePreviewItem] = []

    for tx in transactions:
        if not conditions_match(conditions_op, conditions, tx):
            continue

        matched_count += 1

        # Simulate actions on draft copy
        draft = deepcopy(tx)
        before_state = (
            draft.category_id,
            draft.clean_description,
            draft.merchant_name,
            draft.notes,
            draft.is_ignored,
        )

        category_already_set = draft.category_id is not None and not overwrite_existing_categories
        apply_rule_actions(
            actions,
            draft,
            category_already_set,
            overwrite_existing_category=overwrite_existing_categories,
        )

        after_state = (
            draft.category_id,
            draft.clean_description,
            draft.merchant_name,
            draft.notes,
            draft.is_ignored,
        )

        will_change = is_active and apply_to_existing and (before_state != after_state)
        if will_change:
            changed_count += 1

        if len(preview_items) < limit:
            cur_cat = cat_map.get(tx.category_id) if tx.category_id else None
            new_cat = cat_map.get(draft.category_id) if draft.category_id else None

            preview_items.append(
                RulePreviewItem(
                    transaction_id=tx.id,
                    date=tx.date.strftime("%Y-%m-%d"),
                    amount=tx.amount,
                    current_description=tx.clean_description,
                    new_description=draft.clean_description,
                    current_category_id=tx.category_id,
                    current_category_name=cur_cat.name if cur_cat else None,
                    current_category_color=cur_cat.color if cur_cat else None,
                    new_category_id=draft.category_id,
                    new_category_name=new_cat.name if new_cat else None,
                    new_category_color=new_cat.color if new_cat else None,
                    will_change=will_change,
                )
            )

    return RulePreviewResult(
        matched_count=matched_count,
        changed_count=changed_count,
        items=preview_items,
    )
