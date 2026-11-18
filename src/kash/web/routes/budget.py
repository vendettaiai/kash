"""Monthly category budgets."""

from decimal import Decimal, InvalidOperation
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from kash.domain.models import CategoryBudgetProgress
from kash.services.ledger import cash_flow_transactions, spending_by_category
from kash.web.deps import (
    OWNER_USER_ID,
    Provider,
    get_templates,
    income_category_ids,
    month_window,
    redirect_to,
    transfer_category_ids,
)

router = APIRouter()


@router.get("/budget", response_class=HTMLResponse)
async def budget(request: Request, provider: Provider, month: str | None = None) -> HTMLResponse:
    """Budget vs. actual for a month.

    Categories with a budget show progress against it. Spending in categories
    without a budget is listed too (with a zero budget) so nothing is hidden.
    """
    user_id = OWNER_USER_ID
    window = month_window(month)

    progress = list(await provider.budgets.get_budget_progress(user_id, window.period))
    budgeted_ids = {p.category_id for p in progress}

    groups = await provider.categories.list_groups(user_id)
    all_categories = await provider.categories.list_categories(user_id)
    income_ids = income_category_ids(groups, all_categories)
    transfer_ids = transfer_category_ids(all_categories)
    non_spending = income_ids | transfer_ids
    accounts = await provider.accounts.list_accounts(user_id)

    month_txs = await provider.transactions.list_transactions(
        user_id, start_date=window.start, end_date=window.end, limit=100_000
    )
    net_spending = spending_by_category(
        cash_flow_transactions((t for t in month_txs if not t.is_pending), accounts, income_ids),
        transfer_ids,
        income_ids,
    )
    unbudgeted = {
        cat_id: total
        for cat_id, total in net_spending.items()
        if cat_id is not None and cat_id not in budgeted_ids and total > 0
    }

    for cat in all_categories:
        spent = unbudgeted.get(cat.id)
        if spent:
            progress.append(
                CategoryBudgetProgress(
                    category_id=cat.id,
                    category_name=cat.name,
                    category_icon=cat.icon,
                    category_color=cat.color,
                    budgeted=Decimal("0.00"),
                    spent=spent,
                    remaining=-spent,
                    pct_used=Decimal("100.0"),
                )
            )

    # The ring compares budgeted categories with their budgets; spending in
    # categories without a budget is shown on its own so it can't inflate the %.
    total_spent = sum((p.spent for p in progress if p.budgeted > 0), Decimal("0.00"))
    total_budgeted = sum((p.budgeted for p in progress), Decimal("0.00"))
    unbudgeted_spent = sum(unbudgeted.values(), Decimal("0.00"))
    if total_budgeted > 0:
        pct_used = int((total_spent / total_budgeted * 100).quantize(Decimal("1")))
    else:
        pct_used = 100 if total_spent > 0 else 0

    return get_templates(request).TemplateResponse(
        request=request,
        name="pages/budget.html",
        context={
            "active_page": "budget",
            "period": window.period,
            "period_label": window.label,
            "prev_month": window.prev_period,
            "next_month": window.next_period,
            "category_progress": progress,
            "total_budgeted": total_budgeted,
            "total_spent": total_spent,
            "unbudgeted_spent": unbudgeted_spent,
            "available_funds": max(Decimal("0.00"), total_budgeted - total_spent),
            "budget_pct_used": pct_used,
            "categories": [c for c in all_categories if c.id not in non_spending],
        },
    )


@router.post("/budget/set")
async def set_budget(
    provider: Provider,
    category_id: Annotated[UUID, Form()],
    amount: Annotated[str, Form()] = "",
    period: Annotated[str, Form()] = "",
    remove: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Set, change, or remove (amount 0) a category's budget from this month on."""
    window = month_window(period.strip() or None)
    back = {"month": window.period} if period.strip() else {}
    if remove:
        value = Decimal("0")
    else:
        try:
            value = abs(Decimal(amount.strip().replace(",", "")))
        except (InvalidOperation, ValueError):
            return redirect_to("/budget", **back, error="Enter a budget amount.")
    await provider.budgets.set_category_budget(
        user_id=OWNER_USER_ID,
        category_id=category_id,
        period=window.period,
        amount=value,
    )
    return redirect_to("/budget", month=window.period) if period.strip() else redirect_to("/budget")
