"""Transactions ledger: search, filters, manual entry, and quick rules."""

import csv
import io
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from kash.domain.models import (
    Account,
    AccountType,
    Rule,
    RuleAction,
    RuleCondition,
    Transaction,
    TransactionType,
)
from kash.domain.protocols import DataProvider
from kash.services import formatting
from kash.services.ai.categorize import ai_applied_ids, guesses_for, record_user_category
from kash.services.ai.rule_proposals import added_notice, auto_apply
from kash.services.ledger import (
    CashFlowKind,
    as_counted,
    cash_flow_kind,
    income_category_ids,
    needs_category,
)
from kash.services.rules.engine import (
    add_alternative_condition,
    apply_rules_to_transaction,
    rule_category_id,
)
from kash.services.transfers import unlink_transfer
from kash.web.deps import (
    OWNER_USER_ID,
    MonthWindow,
    Provider,
    get_templates,
    month_window,
    redirect_to,
    rule_choices,
    safe_local_path,
    transfer_category_ids,
)

router = APIRouter()

BANKING_TYPES = (AccountType.CHECKING, AccountType.SAVINGS, AccountType.CASH)
LEDGER_LIMIT = 500
HISTORY_LIMIT = 20_000  # transactions read to list months for the month picker
UNCATEGORIZED = "none"  # ?category_id=none lists transactions that need a category

# ?type= filters, matching how the dashboard counts income and spending
TYPE_FILTERS: dict[str, tuple[set[CashFlowKind], str]] = {
    "income": ({CashFlowKind.EARNED, CashFlowKind.OTHER_INCOME}, "Income"),
    "earned": ({CashFlowKind.EARNED}, "Salary & earned income"),
    "other_income": ({CashFlowKind.OTHER_INCOME}, "Uncategorized money in"),
    "expense": ({CashFlowKind.EXPENSE, CashFlowKind.REFUND}, "Expenses & refunds"),
}


def _uuid_or_none(value: str | None) -> UUID | None:
    try:
        return UUID(value) if value else None
    except ValueError:
        return None


def _date_or_none(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


async def _month_options(
    provider: DataProvider,
    user_id: UUID,
    accounts: dict[UUID, Account],
    selected: MonthWindow | None,
) -> list[tuple[str, str]]:
    """Months with transactions, newest first, always including this month and the
    selected one: ``[("2026-10", "Oct 2026"), ...]`` for the month picker."""
    history = await provider.transactions.list_transactions(user_id, limit=HISTORY_LIMIT)
    months = {t.date.replace(day=1) for t in history if t.account_id in accounts}
    months.add(date.today().replace(day=1))
    if selected:
        months.add(selected.start)
    return [(m.strftime("%Y-%m"), m.strftime("%b %Y")) for m in sorted(months, reverse=True)]


@router.get("/transactions", response_class=HTMLResponse)
async def transactions(
    request: Request,
    provider: Provider,
    search: str | None = None,
    account_id: str | None = None,
    category_id: str | None = None,
    type: str | None = None,
    month: str | None = None,
    account_type: str | None = None,
    start: str | None = None,
    end: str | None = None,
) -> HTMLResponse:
    """Search and filter transactions. The filter form's live updates (HTMX, aimed at
    ``#tx-table-container``) get only the list; everything else gets the whole page.

    ``start``/``end`` (YYYY-MM-DD) take precedence over ``month``.
    """
    user_id = OWNER_USER_ID
    templates = get_templates(request)
    uncategorized = category_id == UNCATEGORIZED

    start_date: date | None = None
    end_date: date | None = None
    selected_month: MonthWindow | None = None
    if month == "current":
        start_date = date.today().replace(day=1)
        selected_month = month_window(None)
    elif month and len(month) == 7:
        selected_month = month_window(month)
        start_date, end_date = selected_month.start, selected_month.end
    range_start, range_end = _date_or_none(start), _date_or_none(end)
    if range_start or range_end:
        start_date, end_date = range_start, range_end
        # A range that is exactly one month (dashboard links) shows in the month picker
        whole = month_window(range_start.strftime("%Y-%m")) if range_start else None
        selected_month = (
            whole if whole and (whole.start, whole.end) == (range_start, range_end) else None
        )

    tx_list = await provider.transactions.list_transactions(
        user_id,
        account_id=_uuid_or_none(account_id),
        category_id=None if uncategorized else _uuid_or_none(category_id),
        start_date=start_date,
        end_date=end_date,
        search=search,
        limit=LEDGER_LIMIT,
    )
    all_accounts = await provider.accounts.list_accounts(user_id, include_inactive=True)
    all_categories = await provider.categories.list_categories(user_id)

    active_accounts = {acc.id: acc for acc in all_accounts if acc.is_active}
    tx_list = [tx for tx in tx_list if tx.account_id in active_accounts]

    # Whether each transaction can count toward income/spending, by its account's
    # "Income & spending" setting (the type/needs-a-category filters use it)
    groups = await provider.categories.list_groups(user_id)
    income_cats = income_category_ids(groups, all_categories)
    counted = {
        tx.id: as_counted(tx, active_accounts[tx.account_id], income_cats) is not None
        for tx in tx_list
    }

    if account_type == "banking":
        tx_list = [
            tx for tx in tx_list if active_accounts[tx.account_id].account_type in BANKING_TYPES
        ]

    if selected_month:
        period_text = selected_month.label
    elif range_start or range_end:
        lo = formatting.day(range_start) if range_start else "…"
        hi = formatting.fulldate(range_end) if range_end else "today"
        period_text = f"{lo} – {hi}"
    else:
        period_text = ""

    transfer_cats = transfer_category_ids(all_categories)
    if uncategorized:
        # Only transactions that count toward income/spending need a category.
        tx_list = [tx for tx in tx_list if counted[tx.id] and needs_category(tx, transfer_cats)]

    filter_label = None
    if type in TYPE_FILTERS:
        kinds, noun = TYPE_FILTERS[type]
        tx_list = [
            tx
            for tx in tx_list
            if counted[tx.id] and cash_flow_kind(tx, transfer_cats, income_cats) in kinds
        ]
        prefix = f"{period_text}: " if period_text else ""
        filter_label = f"{prefix}{noun} ({len(tx_list)} transactions)"
    elif uncategorized:
        prefix = f"{period_text}: " if period_text else ""
        filter_label = f"{prefix}Needs a category ({len(tx_list)} transactions)"
    elif period_text and not selected_month:  # a single month shows in the month picker
        filter_label = f"{period_text} ({len(tx_list)} transactions)"

    context = {
        "transactions": tx_list,
        "accounts": all_accounts,
        "categories": all_categories,
        "filter_label": filter_label,
        "ai_applied": await ai_applied_ids(provider, user_id),
        # On the needs-a-category list, the AI's suggestions can be accepted right there.
        "ai_guesses": await guesses_for(provider, user_id, [tx.id for tx in tx_list])
        if uncategorized
        else {},
    }
    if request.headers.get("hx-target") == "tx-table-container":
        return templates.TemplateResponse(
            request=request, name="components/tx_table.html", context=context
        )

    return templates.TemplateResponse(
        request=request,
        name="pages/transactions.html",
        context={
            **context,
            "active_page": "transactions",
            "rule_choices": await rule_choices(provider, all_categories),
            "search_query": search,
            "selected_account_id": account_id,
            "selected_category_id": category_id,
            "current_type": type,
            "current_month": month,
            "current_account_type": account_type,
            "today": date.today().isoformat(),
            "current_start": start or "",
            "current_end": end or "",
            "selected_month": selected_month,
            "month_options": await _month_options(
                provider, user_id, active_accounts, selected_month
            ),
        },
    )


@router.post("/transactions/{transaction_id}/category")
async def update_transaction_category(
    transaction_id: UUID,
    provider: Provider,
    category_id: Annotated[str, Form()] = "",
    is_transfer: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/transactions",
) -> RedirectResponse:
    """Set a transaction's category and whether it is a transfer (not income/spending)."""
    target = safe_local_path(next, "/transactions")
    tx = await provider.transactions.get_transaction(transaction_id)
    if tx is None:
        return redirect_to(target)

    tx.category_id = _uuid_or_none(category_id)
    await record_user_category(provider, OWNER_USER_ID, tx.id, tx.category_id)
    wants_transfer = is_transfer in ("true", "1", "on")
    if wants_transfer:
        tx.is_transfer = True
        await provider.transactions.update_transaction(tx)
    elif tx.is_transfer or tx.matched_transfer_id:
        await unlink_transfer(provider, tx)  # also saves the category change
    else:
        await provider.transactions.update_transaction(tx)
    if tx.category_id is not None and not wants_transfer:
        added = await auto_apply(provider, OWNER_USER_ID, {tx.id})
        if added:
            return redirect_to(target, notice=added_notice(added))
    return redirect_to(target)


@router.post("/transactions/{transaction_id}/create-rule")
async def create_rule_from_transaction(
    transaction_id: UUID,
    provider: Provider,
    name: Annotated[str, Form()],
    category_id: Annotated[UUID, Form()],
    field: Annotated[str, Form()] = "description",
    op: Annotated[str, Form()] = "contains",
    value: Annotated[str, Form()] = "",
    apply_to_existing: Annotated[str, Form()] = "false",
    next: Annotated[str, Form()] = "/transactions",
) -> RedirectResponse:
    """Create a categorization rule from a transaction.

    The transaction itself always gets the rule's category. With
    ``apply_to_existing``, every existing match is re-categorized too, even if it
    already had a category (that is what the user is asking for here).
    """
    user_id = OWNER_USER_ID
    target = safe_local_path(next, "/transactions")
    if not value.strip():
        return redirect_to(target, error="A rule needs something to match.")
    rule = await provider.rules.create_rule(
        Rule(
            id=uuid4(),
            user_id=user_id,
            name=name.strip() or f"Rule for {value.strip()}",
            priority=20,
            is_active=True,
            rule_type="categorization",
            conditions_op="and",
            conditions=[RuleCondition(field=field, op=op, value=value.strip())],
            actions=[RuleAction(op="set_category", value=str(category_id))],
        )
    )

    await _categorize_and_apply(
        provider, transaction_id, rule, category_id, apply_to_existing in ("true", "1", "on")
    )
    return redirect_to(target)


@router.post("/transactions/{transaction_id}/add-to-rule")
async def add_transaction_to_rule(
    transaction_id: UUID,
    provider: Provider,
    rule_id: Annotated[UUID, Form()],
    field: Annotated[str, Form()] = "description",
    op: Annotated[str, Form()] = "contains",
    value: Annotated[str, Form()] = "",
    apply_to_existing: Annotated[str, Form()] = "false",
    next: Annotated[str, Form()] = "/transactions",
) -> RedirectResponse:
    """Extend an existing categorization rule so it also matches this transaction.

    The match text becomes an extra "or" alternative on the rule. The
    transaction gets the rule's category; with ``apply_to_existing``, every
    other transaction matching the new text is re-categorized too.
    """
    target = safe_local_path(next, "/transactions")
    rule = await provider.rules.get_rule(rule_id)
    if rule is None or rule.rule_type != "categorization":
        return redirect_to(target, error="That rule no longer exists.")
    category = rule_category_id(rule)
    if category is None:
        return redirect_to(target, error=f"Rule '{rule.name}' doesn't set a category.")
    if not value.strip():
        return redirect_to(target, error="A rule needs something to match.")

    condition = {"field": field, "op": op, "value": value.strip()}
    if add_alternative_condition(rule, condition):
        await provider.rules.update_rule(rule)

    # Only the new alternative is applied to history, so older matches of the
    # rule that you re-categorized by hand are left alone.
    just_new = Rule(
        user_id=OWNER_USER_ID,
        name=rule.name,
        conditions=[RuleCondition(**condition)],
        actions=[RuleAction(op="set_category", value=category)],
    )
    await _categorize_and_apply(
        provider, transaction_id, just_new, UUID(category), apply_to_existing in ("true", "1", "on")
    )
    return redirect_to(target)


async def _categorize_and_apply(
    provider: Provider,
    transaction_id: UUID,
    rule: Rule,
    category_id: UUID,
    apply_to_existing: bool,
) -> None:
    """Give the transaction ``category_id``; optionally run ``rule`` over all history."""
    tx = await provider.transactions.get_transaction(transaction_id)
    if tx is not None and tx.category_id != category_id:
        tx.category_id = category_id
        await provider.transactions.update_transaction(tx)
        await record_user_category(provider, OWNER_USER_ID, tx.id, category_id)

    if apply_to_existing:
        for other in await provider.transactions.list_transactions(OWNER_USER_ID, limit=1_000_000):
            before = other.category_id
            apply_rules_to_transaction([rule], other, overwrite_existing_category=True)
            if other.category_id != before:
                await provider.transactions.update_transaction(other)


@router.post("/transactions/new", response_class=HTMLResponse)
async def create_transaction(
    request: Request,
    provider: Provider,
    date: Annotated[str, Form()],
    description: Annotated[str, Form()],
    amount: Annotated[Decimal, Form()],
    account_id: Annotated[UUID, Form()],
    category_id: Annotated[UUID | None, Form()] = None,
) -> Response:
    """Add a manual transaction (negative amount = money out)."""
    user_id = OWNER_USER_ID
    try:
        parsed_date = datetime.strptime(date, "%Y-%m-%d").date()
    except ValueError:
        return redirect_to("/transactions", error="Invalid date.")

    await provider.transactions.create_transaction(
        Transaction(
            id=uuid4(),
            user_id=user_id,
            account_id=account_id,
            category_id=category_id,
            date=parsed_date,
            amount=amount,
            type=TransactionType.DEBIT if amount < 0 else TransactionType.CREDIT,
            clean_description=description.strip(),
            original_description=description.strip(),
        )
    )

    if request.headers.get("hx-request") and not request.headers.get("hx-boosted"):
        return get_templates(request).TemplateResponse(
            request=request,
            name="components/tx_table.html",
            context={
                "transactions": await provider.transactions.list_transactions(
                    user_id, limit=LEDGER_LIMIT
                ),
                "accounts": await provider.accounts.list_accounts(user_id),
                "categories": await provider.categories.list_categories(user_id),
            },
        )
    return redirect_to("/transactions")


@router.get("/transactions/export.csv")
async def export_transactions_csv(provider: Provider) -> Response:
    """Download every transaction as CSV."""
    user_id = OWNER_USER_ID
    accounts = {
        a.id: a.name for a in await provider.accounts.list_accounts(user_id, include_inactive=True)
    }
    categories = {
        c.id: c.name
        for c in await provider.categories.list_categories(user_id, include_hidden=True)
    }
    rows = await provider.transactions.list_transactions(user_id, limit=1_000_000)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "date",
            "description",
            "original_description",
            "merchant",
            "amount",
            "account",
            "category",
            "notes",
            "pending",
            "transfer",
            "ignored",
        ]
    )
    for tx in rows:
        writer.writerow(
            [
                tx.date.isoformat(),
                tx.clean_description,
                tx.original_description,
                tx.merchant_name or "",
                f"{tx.amount:.2f}",
                accounts.get(tx.account_id, ""),
                categories.get(tx.category_id, "") if tx.category_id else "",
                tx.notes or "",
                tx.is_pending,
                tx.is_transfer,
                tx.is_ignored,
            ]
        )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="kash_transactions_{date.today()}.csv"'
        },
    )
