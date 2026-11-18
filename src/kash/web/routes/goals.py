"""Savings goals."""

from datetime import date
from decimal import Decimal
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from kash.domain.models import Goal
from kash.web.deps import OWNER_USER_ID, Provider, get_templates, redirect_to

router = APIRouter()


@router.get("/goals", response_class=HTMLResponse)
async def goals(request: Request, provider: Provider) -> HTMLResponse:
    """Savings goals with overall progress."""
    goals_list = await provider.goals.list_goals(OWNER_USER_ID)
    total_saved = sum((g.current_amount for g in goals_list), Decimal("0"))
    total_target = sum((g.target_amount for g in goals_list), Decimal("0"))
    overall_pct = (
        int((total_saved / total_target * 100).quantize(Decimal("1"))) if total_target else 0
    )

    return get_templates(request).TemplateResponse(
        request=request,
        name="pages/goals.html",
        context={
            "active_page": "goals",
            "goals": goals_list,
            "total_saved": total_saved,
            "total_target": total_target,
            "overall_pct": overall_pct,
        },
    )


@router.post("/goals/new")
async def create_goal(
    provider: Provider,
    name: Annotated[str, Form()],
    target_amount: Annotated[Decimal, Form()],
    current_amount: Annotated[Decimal, Form()] = Decimal("0.00"),
    target_date: Annotated[str | None, Form()] = None,
    notes: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    """Create a savings goal."""
    try:
        parsed_date = date.fromisoformat(target_date) if target_date else None
    except ValueError:
        parsed_date = None
    await provider.goals.create_goal(
        Goal(
            id=uuid4(),
            user_id=OWNER_USER_ID,
            name=name.strip(),
            target_amount=target_amount,
            current_amount=current_amount,
            target_date=parsed_date,
            notes=(notes or "").strip() or None,
        )
    )
    return redirect_to("/goals")
