"""Bills: upcoming due dates, recurring bills, and detected-bill review."""

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from kash.domain.models import (
    Bill,
    BillFrequency,
    BillOccurrenceStatus,
    BillStatus,
    CandidateStatus,
)
from kash.services import formatting
from kash.services.bills import from_transaction
from kash.services.bills.service import (
    PRICE_CHANGE,
    accept_candidate,
    candidate_reasons,
    dismiss_candidate,
    mark_occurrence_paid,
    month_progress,
    refresh_bills,
    scan_for_bills,
)
from kash.web.deps import OWNER_USER_ID, Provider, get_templates, redirect_to, safe_local_path

router = APIRouter()

UPCOMING_DAYS = 14
FREQUENCY_LABELS = {
    BillFrequency.WEEKLY: "Weekly",
    BillFrequency.BIWEEKLY: "Every 2 weeks",
    BillFrequency.SEMI_MONTHLY: "Twice a month",
    BillFrequency.MONTHLY: "Monthly",
    BillFrequency.QUARTERLY: "Quarterly",
    BillFrequency.ANNUAL: "Yearly",
}


def _money(value: str) -> Decimal | None:
    try:
        amount = abs(Decimal(value.strip().replace(",", "").replace("$", "")))
    except (InvalidOperation, ValueError, AttributeError):
        return None
    return amount if amount.is_finite() else None


def _date(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


@router.get("/bills", response_class=HTMLResponse)
async def bills(request: Request, provider: Provider) -> HTMLResponse:
    """Upcoming bills, all recurring bills, and bills detected for review."""
    user_id = OWNER_USER_ID
    today = date.today()
    await refresh_bills(provider, user_id, today)  # record payments, roll due dates

    upcoming = await provider.bills.list_upcoming_occurrences(user_id, days_ahead=UPCOMING_DAYS)
    progress = await month_progress(provider, user_id, today)
    all_bills = [
        b for b in await provider.bills.list_bills(user_id) if b.status != BillStatus.ARCHIVED
    ]
    all_bills.sort(key=lambda b: (b.status != BillStatus.ACTIVE, b.next_due_date))

    # Most recent closed due date per bill (paid / missed), for the status line
    last_closed: dict[UUID, Any] = {}
    for bill in all_bills:
        closed = [
            o
            for o in await provider.bills.list_occurrences(bill.id)
            if o.status in (BillOccurrenceStatus.PAID, BillOccurrenceStatus.MISSED)
        ]
        if closed:
            last_closed[bill.id] = closed[-1]
    price_changed = {
        bill_id
        for bill_id, occ in last_closed.items()
        if occ.paid_amount
        and not next(b for b in all_bills if b.id == bill_id).is_variable_amount
        and abs(occ.paid_amount - occ.expected_amount) > occ.expected_amount * PRICE_CHANGE
    }

    candidates = await provider.bills.list_detection_candidates(
        user_id, CandidateStatus.PENDING_REVIEW
    )
    reasons = {c.id: await candidate_reasons(provider, c) for c in candidates}
    categories = await provider.categories.list_categories(user_id)

    return get_templates(request).TemplateResponse(
        request=request,
        name="pages/bills.html",
        context={
            "active_page": "bills",
            "today": today,
            "upcoming_bills": upcoming,
            "progress": progress,
            "upcoming_days": UPCOMING_DAYS,
            "recurring_bills": all_bills,
            "last_closed": last_closed,
            "price_changed": price_changed,
            "candidates": candidates,
            "candidate_reasons": reasons,
            "category_map": {c.id: c for c in categories},
            "categories": categories,
            "frequency_labels": FREQUENCY_LABELS,
            "total_upcoming": sum((occ.expected_amount for _, occ in upcoming), Decimal("0")),
        },
    )


@router.post("/bills/scan")
async def scan_history(provider: Provider) -> RedirectResponse:
    """Look through transaction history for recurring bills now."""
    result = await scan_for_bills(provider, OWNER_USER_ID)
    await refresh_bills(provider, OWNER_USER_ID)
    if result.new:
        notice = f"Found {result.new} possible bill{'s' if result.new != 1 else ''} to review."
    elif result.updated:
        notice = "No new bills found. Existing suggestions were updated."
    else:
        notice = "No new recurring bills found."
    return redirect_to("/bills", notice=notice)


@router.post("/bills/candidates/{candidate_id}/accept")
async def accept_bill_candidate(
    candidate_id: UUID,
    provider: Provider,
    name: Annotated[str, Form()] = "",
    amount: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Add a detected bill (optionally renamed / with a different amount)."""
    bill = await accept_candidate(provider, candidate_id, name=name, amount=_money(amount))
    if bill is None:
        return redirect_to("/bills", error="That suggestion is no longer available.")
    return redirect_to("/bills", notice=f"Added {bill.name}.")


@router.post("/bills/candidates/{candidate_id}/dismiss")
async def dismiss_bill_candidate(candidate_id: UUID, provider: Provider) -> RedirectResponse:
    """Not a bill: hide it and don't suggest it again."""
    await dismiss_candidate(provider, candidate_id)
    return redirect_to("/bills")


@router.post("/bills/occurrences/{occurrence_id}/paid")
async def mark_paid(occurrence_id: UUID, provider: Provider) -> RedirectResponse:
    """Mark a due date as paid by hand, then move the bill to its next due date."""
    await mark_occurrence_paid(provider, occurrence_id)
    await refresh_bills(provider, OWNER_USER_ID)
    return redirect_to("/bills")


@router.post("/bills/new")
async def create_bill(
    provider: Provider,
    name: Annotated[str, Form()],
    expected_amount: Annotated[str, Form()],
    frequency: Annotated[str, Form()] = "monthly",
    next_due_date: Annotated[str, Form()] = "",
    merchant: Annotated[str, Form()] = "",
    category_id: Annotated[str, Form()] = "",
    is_variable: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Add a bill manually."""
    try:
        parsed_frequency = BillFrequency(frequency.strip().lower())
    except ValueError:
        return redirect_to("/bills", error="Unknown bill frequency.")
    amount = _money(expected_amount)
    if amount is None:
        return redirect_to("/bills", error="Enter the bill amount.")
    due = _date(next_due_date) or date.today() + timedelta(days=14)

    await provider.bills.create_bill(
        Bill(
            id=uuid4(),
            user_id=OWNER_USER_ID,
            category_id=_uuid(category_id),
            name=name.strip(),
            merchant_name=merchant.strip() or name.strip(),
            expected_amount=amount,
            is_variable_amount=bool(is_variable),
            frequency=parsed_frequency,
            due_day=due.day,
            next_due_date=due,
        )
    )
    return redirect_to("/bills")


def _uuid(value: str) -> UUID | None:
    try:
        return UUID(value) if value else None
    except ValueError:
        return None


@router.post("/bills/{bill_id}/edit")
async def edit_bill(
    bill_id: UUID,
    provider: Provider,
    name: Annotated[str, Form()],
    expected_amount: Annotated[str, Form()],
    frequency: Annotated[str, Form()] = "monthly",
    next_due_date: Annotated[str, Form()] = "",
    merchant: Annotated[str, Form()] = "",
    category_id: Annotated[str, Form()] = "",
    is_variable: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "active",
) -> RedirectResponse:
    """Change a bill. A new next due date replaces the open due date."""
    bill = await provider.bills.get_bill(bill_id)
    if bill is None:
        return redirect_to("/bills", error="Bill not found.")
    amount = _money(expected_amount)
    if amount is None:
        return redirect_to("/bills", error="Enter the bill amount.")
    try:
        bill.frequency = BillFrequency(frequency)
        bill.status = BillStatus(status)
    except ValueError:
        return redirect_to("/bills", error="Unknown frequency or status.")
    bill.name = name.strip() or bill.name
    bill.merchant_name = merchant.strip() or bill.merchant_name
    bill.expected_amount = amount
    bill.is_variable_amount = bool(is_variable)
    bill.category_id = _uuid(category_id)

    new_due = _date(next_due_date)
    open_occurrences = [
        o
        for o in await provider.bills.list_occurrences(bill.id)
        if o.status == BillOccurrenceStatus.UPCOMING
    ]
    if new_due:
        bill.next_due_date = new_due
        bill.due_day = new_due.day
    for occ in open_occurrences:
        occ.expected_amount = amount
        if new_due:
            occ.due_date = new_due
        await provider.bills.save_occurrence(occ)
    await provider.bills.update_bill(bill)
    return redirect_to("/bills", notice=f"Saved {bill.name}.")


@router.post("/bills/{bill_id}/delete")
async def delete_bill(bill_id: UUID, provider: Provider) -> RedirectResponse:
    """Delete a bill and its due dates (transactions are not touched)."""
    await provider.bills.delete_bill(bill_id)
    return redirect_to("/bills", notice="Bill deleted.")


# ---------------------------------------------------------------------------
# Make a bill from a transaction (transaction pop-up; services/bills/from_transaction.py)
# ---------------------------------------------------------------------------


@router.get("/transactions/{transaction_id}/bill-draft", response_class=HTMLResponse)
async def transaction_bill_draft(
    request: Request, transaction_id: UUID, provider: Provider, next: str = "/transactions"
) -> HTMLResponse:
    """The pre-filled "Make this a bill" form (or a link to the merchant's existing bill)."""
    bill_draft = await from_transaction.draft(provider, OWNER_USER_ID, transaction_id)
    categories = await provider.categories.list_categories(OWNER_USER_ID)
    return get_templates(request).TemplateResponse(
        request=request,
        name="components/tx_bill_form.html",
        context={
            "draft": bill_draft,
            "next_dues": {k: v.isoformat() for k, v in bill_draft.next_dues.items()},
            "categories": [c for c in categories if not c.is_hidden and not c.is_transfer],
            "frequency_labels": FREQUENCY_LABELS,
            "next": safe_local_path(next, "/transactions"),
        },
    )


@router.post("/transactions/{transaction_id}/make-bill")
async def make_bill_from_transaction(
    transaction_id: UUID,
    provider: Provider,
    name: Annotated[str, Form()],
    expected_amount: Annotated[str, Form()],
    frequency: Annotated[str, Form()] = "monthly",
    next_due_date: Annotated[str, Form()] = "",
    merchant: Annotated[str, Form()] = "",
    category_id: Annotated[str, Form()] = "",
    is_variable: Annotated[str, Form()] = "",
    next: Annotated[str, Form()] = "/transactions",
) -> RedirectResponse:
    """Create the bill; this transaction is recorded as its first payment."""
    target = safe_local_path(next, "/transactions")
    try:
        parsed_frequency = BillFrequency(frequency)
    except ValueError:
        return redirect_to(target, error="Unknown bill frequency.")
    amount, due = _money(expected_amount), _date(next_due_date)
    if amount is None or due is None:
        return redirect_to(target, error="Enter the amount and the next due date.")
    result = await from_transaction.create(
        provider,
        OWNER_USER_ID,
        transaction_id,
        name=name,
        amount=amount,
        frequency=parsed_frequency,
        due=due,
        merchant=merchant,
        category_id=_uuid(category_id),
        variable=bool(is_variable),
    )
    if isinstance(result, str):
        return redirect_to(target, error=result)
    await refresh_bills(provider, OWNER_USER_ID)
    due_text = formatting.fulldate(result.next_due_date)
    return redirect_to(
        target,
        notice=f"Bill “{result.name}” added with this payment as its first. Next due {due_text}.",
    )
