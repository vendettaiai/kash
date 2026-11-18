"""Shared dependencies and small helpers for the HTML routes."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache
from typing import Annotated, cast
from urllib.parse import urlencode
from uuid import UUID

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from kash.domain.models import Category
from kash.domain.protocols import BankProvider, DataProvider
from kash.owner import OWNER_USER_ID
from kash.services.bank import SimpleFINProvider
from kash.services.dates import shift_months
from kash.services.ledger import income_category_ids

__all__ = [
    "OWNER_USER_ID",
    "BankProviders",
    "DateRange",
    "MonthWindow",
    "Provider",
    "get_bank_providers",
    "get_data_provider",
    "get_templates",
    "income_category_ids",
    "dashboard_range",
    "month_window",
    "redirect_to",
    "rule_choices",
    "safe_local_path",
    "shift_months",
    "transfer_category_ids",
]


@lru_cache
def get_data_provider() -> DataProvider:
    """The application's data provider (SQLite / PostgreSQL)."""
    from kash.db.repositories.provider import DatabaseDataProvider

    return DatabaseDataProvider()


def get_bank_providers() -> dict[str, BankProvider]:
    """Bank sync providers available to the app, keyed by provider name."""
    return {"simplefin": SimpleFINProvider()}


Provider = Annotated[DataProvider, Depends(get_data_provider)]
BankProviders = Annotated[dict[str, BankProvider], Depends(get_bank_providers)]


def get_templates(request: Request) -> Jinja2Templates:
    """Jinja2 environment stored on the app at startup."""
    return cast(Jinja2Templates, request.app.state.templates)


def safe_local_path(url: str | None, default: str) -> str:
    """Only allow same-site relative paths as redirect targets (prevents open redirects)."""
    candidate = (url or "").strip()
    if not candidate.startswith("/") or candidate.startswith("//") or "\\" in candidate:
        return default
    return candidate


def _without_flash(path: str, params: dict[str, str]) -> str:
    """``path`` without an old notice/error that the new ones replace (a page that links
    back to itself would otherwise pile them up)."""
    base, sep, query = path.partition("?")
    if not sep:
        return path
    drop = {key for key in ("notice", "error") if key in params}
    kept = [pair for pair in query.split("&") if pair and pair.split("=", 1)[0] not in drop]
    return f"{base}?{'&'.join(kept)}" if kept else base


def redirect_to(path: str, **params: str) -> RedirectResponse:
    """303 redirect to ``path`` with properly URL-encoded query parameters."""
    if params:
        path = _without_flash(path, params)
        sep = "&" if "?" in path else "?"
        path = f"{path}{sep}{urlencode(params)}"
    return RedirectResponse(url=path, status_code=303)


@dataclass(frozen=True)
class MonthWindow:
    """A calendar month selected in the UI."""

    period: str  # "YYYY-MM"
    start: date
    end: date
    label: str  # "September 2026"
    prev_period: str
    next_period: str


def month_window(period: str | None, today: date | None = None) -> MonthWindow:
    """Parse ``YYYY-MM`` (falling back to the current month) into its date range."""
    today = today or date.today()
    start = today.replace(day=1)
    if period:
        try:
            year, month = (int(part) for part in period.split("-", 1))
            start = date(year, month, 1)
        except ValueError:
            pass
    next_start = (start + timedelta(days=32)).replace(day=1)
    end = next_start - timedelta(days=1)
    prev_start = (start - timedelta(days=1)).replace(day=1)
    return MonthWindow(
        period=start.strftime("%Y-%m"),
        start=start,
        end=end,
        label=start.strftime("%B %Y"),
        prev_period=prev_start.strftime("%Y-%m"),
        next_period=next_start.strftime("%Y-%m"),
    )


async def rule_choices(
    provider: DataProvider, categories: Sequence[Category]
) -> list[dict[str, str]]:
    """Categorization rules that set a category, for "add to existing rule" pickers."""
    from kash.services.rules.engine import rule_category_id

    names = {str(c.id): c.name for c in categories}
    choices = []
    for rule in await provider.rules.list_rules(OWNER_USER_ID, rule_type="categorization"):
        cat_id = rule_category_id(rule)
        if cat_id and cat_id in names:
            choices.append(
                {
                    "id": str(rule.id),
                    "name": rule.name,
                    "category_id": cat_id,
                    "category": names[cat_id],
                }
            )
    return sorted(choices, key=lambda c: c["name"].lower())


def transfer_category_ids(categories: Sequence[Category]) -> set[UUID]:
    """Categories flagged "treat as transfer" (never income or spending)."""
    return {c.id for c in categories if c.is_transfer}


@dataclass(frozen=True)
class DateRange:
    """A month shown on the dashboard plus the earlier span it is compared with."""

    month: MonthWindow
    start: date
    end: date
    compare_start: date
    compare_end: date
    compare_label: str
    is_current: bool


def dashboard_range(period: str | None, today: date | None = None) -> DateRange:
    """The dashboard month (``YYYY-MM``, default this month).

    The current month runs to today and is compared with the same days last
    month; a past month is compared with the whole month before it.
    """
    today = today or date.today()
    window = month_window(period, today)
    if window.start <= today <= window.end:
        return DateRange(
            window,
            window.start,
            today,
            shift_months(window.start, -1),
            shift_months(today, -1),
            "vs same days last month",
            True,
        )
    prev = month_window(window.prev_period, today)
    return DateRange(
        window, window.start, window.end, prev.start, prev.end, f"vs {prev.start:%B}", False
    )
