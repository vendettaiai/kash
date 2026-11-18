"""Income and spending reports for a month or any date range, with trends and net worth."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from kash.domain.models import AccountCashFlow, Category, Transaction
from kash.services import formatting
from kash.services.ledger import (
    CashFlowKind,
    calculate_cashflow,
    cash_flow_kind,
    cash_flow_transactions,
    income_category_ids,
    spending_by_category,
)
from kash.services.reports import (
    line_chart,
    monthly_flows,
    net_worth_series,
    record_snapshots,
    series_dates,
)
from kash.web.deps import (
    OWNER_USER_ID,
    MonthWindow,
    Provider,
    get_templates,
    month_window,
    shift_months,
    transfer_category_ids,
)

router = APIRouter()

HISTORY_LIMIT = 20_000
RANGE_LABELS = {
    "this_month": "This month",
    "last_month": "Last month",
    "3m": "Last 3 months",
    "6m": "Last 6 months",
    "12m": "Last 12 months",
    "ytd": "Year to date",
    "all": "All time",
    "custom": "Custom range",
}


@dataclass(frozen=True)
class ReportRange:
    """The dates a report covers. ``month`` is set when it is one calendar month."""

    key: str
    start: date
    end: date
    label: str
    month: MonthWindow | None = None


def _parse(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def report_range(
    key: str | None,
    period: str | None,
    start: str | None,
    end: str | None,
    today: date,
    earliest: date,
) -> ReportRange:
    """Resolve ``?range=`` (a preset), ``?period=YYYY-MM`` or ``?start=&end=`` to dates."""
    key = key if key in RANGE_LABELS else None
    custom_start, custom_end = _parse(start), _parse(end)
    if key == "custom" or (key is None and (custom_start or custom_end)):
        lo, hi = custom_start or earliest, custom_end or today
        lo, hi = min(lo, hi), max(lo, hi)
        label = f"{formatting.fulldate(lo)} – {formatting.fulldate(hi)}"
        return ReportRange("custom", lo, hi, label)
    if key in ("3m", "6m", "12m"):
        months = int(key[:-1])
        first = shift_months(today.replace(day=1), -(months - 1))
        return ReportRange(key, first, today, RANGE_LABELS[key])
    if key == "ytd":
        return ReportRange(key, today.replace(month=1, day=1), today, RANGE_LABELS[key])
    if key == "all":
        return ReportRange(key, min(earliest, today), today, RANGE_LABELS[key])
    if key == "last_month":
        period = shift_months(today.replace(day=1), -1).strftime("%Y-%m")
    window = month_window(period, today)
    this_month = window.start <= today <= window.end
    return ReportRange(
        "this_month" if this_month else ("last_month" if key == "last_month" else "month"),
        window.start,
        window.end,
        window.label,
        window,
    )


def _group_by_category(
    txs: list[Transaction],
    categories: dict[Any, Category],
    fallback: tuple[str, str, str],
) -> list[dict[str, Any]]:
    """Sum absolute amounts per category, largest first, with percentage of total."""
    groups: dict[str, dict[str, Any]] = {}
    for tx in txs:
        cat = categories.get(tx.category_id) if tx.category_id else None
        name, color, icon = (cat.name, cat.color, cat.icon) if cat else fallback
        slot = groups.setdefault(
            name,
            {
                "id": str(cat.id) if cat else "",
                "name": name,
                "color": color,
                "icon": icon,
                "total": Decimal("0"),
            },
        )
        slot["total"] += abs(tx.amount)

    grand_total = sum((g["total"] for g in groups.values()), Decimal("0"))
    rows = sorted(groups.values(), key=lambda g: g["total"], reverse=True)
    for row in rows:
        row["pct"] = (
            int((row["total"] / grand_total * 100).quantize(Decimal("1"))) if grand_total else 0
        )
    return rows


def _spending_rows(
    totals: dict[Any, Decimal], categories: dict[Any, Category]
) -> list[dict[str, Any]]:
    """Net spending per category (after refunds), largest first, with % of total."""
    rows = []
    for cat_id, total in totals.items():
        if total <= 0:
            continue
        cat = categories.get(cat_id) if cat_id else None
        rows.append(
            {
                "id": str(cat.id) if cat else "",
                "name": cat.name if cat else "Uncategorized",
                "color": cat.color if cat else "#94a3b8",
                "icon": cat.icon if cat else "tag",
                "total": total,
            }
        )
    grand_total = sum((r["total"] for r in rows), Decimal("0"))
    rows.sort(key=lambda r: r["total"], reverse=True)
    for row in rows:
        row["pct"] = (
            int((row["total"] / grand_total * 100).quantize(Decimal("1"))) if grand_total else 0
        )
    return rows


@router.get("/reports", response_class=HTMLResponse)
async def reports(
    request: Request,
    provider: Provider,
    period: str | None = None,
    range: str | None = None,  # noqa: A002 - query parameter name
    start: str | None = None,
    end: str | None = None,
) -> HTMLResponse:
    """Income, expenses, savings rate, category breakdowns, monthly trend and net worth
    for one month or a date range."""
    user_id = OWNER_USER_ID
    today = date.today()
    await record_snapshots(provider, user_id, today)

    accounts = await provider.accounts.list_accounts(user_id)
    # Accounts whose "Income & spending" setting lets anything count (month list)
    spending_accounts = {
        a.id for a in accounts if a.is_active and a.cash_flow != AccountCashFlow.NONE
    }
    category_list = await provider.categories.list_categories(user_id, include_hidden=True)
    categories = {c.id: c for c in category_list}
    transfer_cats = transfer_category_ids(category_list)
    income_cats = income_category_ids(await provider.categories.list_groups(user_id), category_list)

    # Every transaction, oldest first: for the month list, "all time", and net worth.
    history = await provider.transactions.list_transactions(user_id, limit=HISTORY_LIMIT)
    earliest = min((t.date for t in history), default=today)
    rng = report_range(range, period, start, end, today, earliest)

    # Investment accounts add only their income (dividends, interest)
    month_txs = cash_flow_transactions(
        (t for t in history if rng.start <= t.date <= rng.end and not t.is_pending),
        accounts,
        income_cats,
    )
    flow = calculate_cashflow(
        month_txs, transfer_category_ids=transfer_cats, income_category_ids=income_cats
    )
    income_kinds = {CashFlowKind.EARNED, CashFlowKind.OTHER_INCOME}
    income_txs = [
        t for t in month_txs if cash_flow_kind(t, transfer_cats, income_cats) in income_kinds
    ]
    net_spending = spending_by_category(month_txs, transfer_cats, income_cats)
    total_income = flow.income
    total_expenses = flow.expenses
    savings_rate = (
        int(((total_income - total_expenses) / total_income * 100).quantize(Decimal("1")))
        if total_income > 0
        else 0
    )

    # Months that have any data, newest first (for the month picker)
    periods = sorted(
        {t.date.strftime("%Y-%m") for t in history if t.account_id in spending_accounts},
        reverse=True,
    ) or [today.strftime("%Y-%m")]

    flows = monthly_flows(month_txs, rng.start, rng.end, transfer_cats, income_cats)
    trend_max = max((max(f.income, f.expenses) for f in flows), default=Decimal("0"))

    all_accounts = await provider.accounts.list_accounts(user_id, include_inactive=True)
    since = min(rng.start, today) - timedelta(days=1)
    worth = net_worth_series(
        all_accounts,
        [t for t in history if t.date > since],
        await provider.accounts.list_balance_snapshots(user_id),
        series_dates(max(rng.start, earliest), rng.end, today),
        today,
    )

    month = rng.month
    return get_templates(request).TemplateResponse(
        request=request,
        name="pages/reports.html",
        context={
            "active_page": "reports",
            "range": rng,
            "range_labels": RANGE_LABELS,
            "range_query": f"start={rng.start.isoformat()}&end={rng.end.isoformat()}",
            "period": month.period if month else "",
            "period_label": rng.label,
            "prev_month": month.prev_period if month else "",
            "next_month": month.next_period if month else "",
            "available_periods": periods,
            "flows": flows,
            "trend_max": trend_max,
            "net_worth": worth,
            "net_worth_chart": line_chart(worth),
            "category_spending": _spending_rows(net_spending, categories),
            "category_income": _group_by_category(
                income_txs, categories, ("Other Income", "#10b981", "wallet")
            ),
            "total_income": total_income,
            "total_earned": flow.earned,
            "total_other_income": flow.other_income,
            "total_refunds": flow.refunds,
            "total_expenses": total_expenses,
            "net_surplus": total_income - total_expenses,
            "savings_rate": savings_rate,
        },
    )
