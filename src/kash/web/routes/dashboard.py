"""Dashboard screen."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from kash.domain.models import AccountCashFlow, AccountType, Category, Transaction
from kash.services import dashboard_layout, sankey
from kash.services.ai.categorize import guesses_for
from kash.services.bills.service import refresh_bills
from kash.services.dashboard_layout import DEVICES
from kash.services.ledger import (
    CashFlowKind,
    cash_flow_kind,
    cash_flow_transactions,
    income_by_category,
    income_category_ids,
    needs_category,
    spending_by_category,
)
from kash.services.reports import balance_series, line_chart, record_snapshots
from kash.services.sankey import Sankey
from kash.web.deps import (
    OWNER_USER_ID,
    DateRange,
    Provider,
    dashboard_range,
    get_templates,
    rule_choices,
    transfer_category_ids,
)
from kash.web.routes.bills import UPCOMING_DAYS
from kash.web.routes.transactions import UNCATEGORIZED

router = APIRouter()

DONUT_CIRCUMFERENCE = Decimal("238.76")  # 2 * pi * r for the r=38 SVG ring
DONUT_MAX_SLICES = 6
RECENT_COUNT = 5
BANKING_TYPES = (AccountType.CHECKING, AccountType.SAVINGS, AccountType.CASH)
HISTORY_LIMIT = 20_000


def greeting(now: datetime | None = None) -> str:
    hour = (now or datetime.now()).hour
    if hour < 12:
        return "Good morning"
    if hour < 18:
        return "Good afternoon"
    return "Good evening"


def donut_slices(spending: list[dict[str, Any]], total: Decimal) -> list[dict[str, Any]]:
    """Ring slices, largest first: the top categories plus one "Other" slice, so
    the ring is always complete. Lengths come from exact shares (not rounded %)."""
    if total <= 0:
        return []
    ranked = sorted(spending, key=lambda s: s["total"], reverse=True)
    top = ranked if len(ranked) <= DONUT_MAX_SLICES else ranked[: DONUT_MAX_SLICES - 1]
    rest = ranked[len(top) :]
    if rest:
        top = [
            *top,
            {
                "name": f"Other ({len(rest)})",
                "color": "#cbd5e1",
                "category_id": None,
                "total": sum((r["total"] for r in rest), Decimal("0")),
                "other": True,
            },
        ]
    slices, offset = [], Decimal("0")
    for slot in top:
        share = slot["total"] / total
        length = share * DONUT_CIRCUMFERENCE
        slices.append(
            {
                **slot,
                "pct": int((share * 100).quantize(Decimal("1"))),
                "stroke_len": float(length.quantize(Decimal("0.01"))),
                "stroke_offset": float(offset.quantize(Decimal("0.01"))),
            }
        )
        offset -= length
    return slices


SANKEY_MAX_TARGETS = 7  # spending categories shown; the rest are "Other"
SANKEY_UNITS_PER_SPAN = 80  # desktop grid twelfth ≈ 80px on a typical screen


def sankey_for(
    txs: list[Transaction],
    spending: list[dict[str, Any]],
    categories: dict[UUID, Category],
    transfer_cats: set[UUID],
    income_cats: set[UUID],
    rng: DateRange,
    width: float = sankey.WIDTH,
) -> Sankey | None:
    """Money in (by income category) → spending by category, plus saved/overspent."""
    query = f"start={rng.start.isoformat()}&end={rng.end.isoformat()}"
    earned: dict[UUID | None, Decimal] = {}
    for tx in txs:
        kind = cash_flow_kind(tx, transfer_cats, income_cats)
        if kind in (CashFlowKind.EARNED, CashFlowKind.OTHER_INCOME):
            key = tx.category_id if kind is CashFlowKind.EARNED else None
            earned[key] = earned.get(key, Decimal("0")) + tx.amount
    sources = [
        sankey.Flow(
            categories[cat_id].name if cat_id in categories else "Other income",
            amount,
            "#10b981" if cat_id else "#34d399",
            f"/transactions?category_id={cat_id}&{query}"
            if cat_id
            else f"/transactions?type=other_income&{query}",
        )
        for cat_id, amount in sorted(earned.items(), key=lambda kv: -kv[1])
        if amount > 0
    ]
    ranked = sorted(spending, key=lambda s: s["total"], reverse=True)
    shown = ranked if len(ranked) <= SANKEY_MAX_TARGETS else ranked[: SANKEY_MAX_TARGETS - 1]
    targets = [
        sankey.Flow(
            s["name"],
            s["total"],
            s["color"],
            f"/transactions?category_id={s['category_id'] or UNCATEGORIZED}&{query}",
        )
        for s in shown
    ]
    if rest := ranked[len(shown) :]:
        other = sum((r["total"] for r in rest), Decimal("0"))
        targets.append(
            sankey.Flow(
                f"Other ({len(rest)})", other, "#cbd5e1", f"/transactions?type=expense&{query}"
            )
        )
    money_in = sum((f.amount for f in sources), Decimal("0"))
    money_out = sum((f.amount for f in targets), Decimal("0"))
    if money_in > money_out:
        targets.append(sankey.Flow("Saved", money_in - money_out, "#3b82f6", "/accounts"))
    elif money_out > money_in:
        sources.append(sankey.Flow("From savings", money_out - money_in, "#f43f5e", "/accounts"))
    return sankey.build(sources, targets, width)


@router.get("/", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard(
    request: Request, provider: Provider, period: str | None = None
) -> HTMLResponse:
    """Main dashboard for one month (``period`` = ``YYYY-MM``, default this month)."""
    user_id = OWNER_USER_ID
    rng = dashboard_range(period)
    saved = await provider.settings.get_settings(user_id)
    await record_snapshots(provider, user_id)  # daily balances for net worth history

    summary = await provider.dashboard.get_summary(
        user_id,
        start=rng.start,
        end=rng.end,
        compare_start=rng.compare_start,
        compare_end=rng.compare_end,
    )
    recent_txs = await provider.transactions.list_transactions(user_id, limit=RECENT_COUNT)
    # Same refresh and window as the Bills page, so both list the same due dates
    await refresh_bills(provider, user_id, date.today())
    upcoming_bills = await provider.bills.list_upcoming_occurrences(
        user_id, days_ahead=UPCOMING_DAYS
    )

    groups = await provider.categories.list_groups(user_id)
    all_categories = await provider.categories.list_categories(user_id, include_hidden=True)
    categories = {c.id: c for c in all_categories}
    transfer_cats = transfer_category_ids(all_categories)
    income_cats = income_category_ids(groups, all_categories)
    accounts = await provider.accounts.list_accounts(user_id)
    # Accounts whose "Income & spending" setting lets anything count
    spending_account_ids = {
        a.id for a in accounts if a.is_active and a.cash_flow != AccountCashFlow.NONE
    }

    # Net spending by category for the month (donut chart); refunds reduce a category.
    # Investment accounts add only their income (dividends, interest).
    period_txs = cash_flow_transactions(
        (
            t
            for t in await provider.transactions.list_transactions(
                user_id, start_date=rng.start, end_date=rng.end, limit=100_000
            )
            if not t.is_pending
        ),
        accounts,
        income_cats,
    )
    spending: list[dict[str, Any]] = []
    for cat_id, total in spending_by_category(period_txs, transfer_cats, income_cats).items():
        if total <= 0:
            continue
        cat = categories.get(cat_id) if cat_id else None
        spending.append(
            {
                "name": cat.name if cat else "Uncategorized",
                "color": cat.color if cat else "#94a3b8",
                "category_id": cat_id,
                "total": total,
            }
        )

    # Income by category for the Income card (uncategorized money in last)
    income_lines = sorted(
        (
            {
                "name": categories[cat_id].name if cat_id in categories else "Uncategorized",
                "category_id": cat_id if cat_id in categories else None,
                "total": total,
            }
            for cat_id, total in income_by_category(period_txs, transfer_cats, income_cats).items()
            if total != 0
        ),
        key=lambda line: (line["category_id"] is None, -line["total"]),
    )

    # Transactions this month that count as income/spending but have no category
    uncategorized = [t for t in period_txs if needs_category(t, transfer_cats)]
    uncategorized_deposits = sum(1 for t in uncategorized if t.amount > 0)
    ai_guess_count = len(await guesses_for(provider, user_id, [t.id for t in uncategorized]))

    total_spending = sum((s["total"] for s in spending), Decimal("0"))
    donut = donut_slices(spending, total_spending)
    layouts = {d: dashboard_layout.layout(saved.dashboard_layout, d) for d in DEVICES}
    flow_args = (period_txs, spending, categories, transfer_cats, income_cats, rng)
    desktop_span = next((p.span for p in layouts["desktop"] if p.id == "sankey"), 12)

    # Months with any data, newest first (for the month dropdown), always incl. this month
    history = await provider.transactions.list_transactions(user_id, limit=HISTORY_LIMIT)
    months = sorted(
        {t.date.replace(day=1) for t in history if t.account_id in spending_account_ids}
        | {date.today().replace(day=1), rng.month.start},
        reverse=True,
    )
    periods = [(m.strftime("%Y-%m"), m.strftime("%b %Y")) for m in months]

    # Banking balance each day of the month, from the end of the month before
    # (the "↑ $342.17 (2.8%) this month" line and trend on the balance card).
    today = date.today()
    baseline = rng.month.start - timedelta(days=1)
    last_day = min(rng.month.end, today)
    trend = balance_series(
        [
            a
            for a in accounts
            if a.is_active and a.account_type in BANKING_TYPES and a.show_in_balance
        ],
        [t for t in history if t.date > baseline],
        await provider.accounts.list_balance_snapshots(user_id),
        [baseline + timedelta(days=i) for i in range((last_day - baseline).days + 1)],
        today,
        await provider.transactions.pending_totals(user_id),
    )
    balance_change = trend[-1][1] - trend[0][1] if len(trend) > 1 else None
    start_balance = trend[0][1] if trend else Decimal("0")
    balance_change_pct = (
        balance_change / abs(start_balance) * 100
        if balance_change is not None and start_balance
        else None
    )

    return get_templates(request).TemplateResponse(
        request=request,
        name="pages/dashboard.html",
        context={
            "active_page": "dashboard",
            "greeting": greeting(),
            "summary": summary,
            "income_lines": income_lines,
            "recent_transactions": recent_txs,
            "upcoming_bills": upcoming_bills,
            "upcoming_days": UPCOMING_DAYS,
            # Today's balance, or the month-end balance when looking at a past month
            "banking_balance": trend[-1][1] if trend else summary.total_banking_balance,
            "balance_as_of": trend[-1][0] if trend else today,
            "balance_change": balance_change,
            "balance_change_pct": balance_change_pct,
            "balance_chart": line_chart(trend),
            "range": rng,
            "month": rng.month,
            "available_periods": periods,
            "range_query": f"start={rng.start.isoformat()}&end={rng.end.isoformat()}",
            "compare_label": rng.compare_label,
            "period_phrase": "this month" if rng.is_current else f"in {rng.month.label}",
            "uncategorized_count": len(uncategorized),
            "ai_guess_count": ai_guess_count,
            "uncategorized_deposits": uncategorized_deposits,
            "dashboard_categories": donut,
            # About one SVG unit per pixel on a phone and on a computer
            "sankey_mobile": sankey_for(*flow_args),
            "sankey_desktop": sankey_for(*flow_args, width=desktop_span * SANKEY_UNITS_PER_SPAN),
            "layouts": layouts,
            "layout_editor": dashboard_layout.editor_data(saved.dashboard_layout),
            "donut_circumference": float(DONUT_CIRCUMFERENCE),
            "uncategorized_value": UNCATEGORIZED,
            "spending_total": total_spending,
            # For the transaction detail modal
            "categories": [c for c in all_categories if not c.is_hidden],
            "accounts": accounts,
            "account_names": {a.id: a.name for a in accounts},
            "rule_choices": await rule_choices(provider, all_categories),
        },
    )


@router.post("/dashboard/layout")
async def save_layout(request: Request, provider: Provider) -> JSONResponse:
    """Save one device's cards: ``{"device": "mobile", "cards": [{"id", "span"}]}``,
    or ``{"device": ..., "reset": true}`` for the default."""
    try:
        body = await request.json()
    except ValueError:
        body = None
    device = body.get("device") if isinstance(body, dict) else None
    if device not in DEVICES:
        return JSONResponse({"error": "Unknown device."}, status_code=400)
    placed = None if body.get("reset") else dashboard_layout.clean(device, body.get("cards"))
    if placed is None and not body.get("reset"):
        return JSONResponse({"error": "Cards must be a list."}, status_code=400)
    saved = await provider.settings.get_settings(OWNER_USER_ID)
    stored = dashboard_layout.to_stored(saved.dashboard_layout, device, placed)
    await provider.settings.save_settings(saved.model_copy(update={"dashboard_layout": stored}))
    return JSONResponse({"ok": True, "cards": len(dashboard_layout.layout(stored, device))})
