"""Settings: display currency, number/date format, theme, and automatic bank sync."""

from datetime import date, datetime
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from kash.config import get_settings
from kash.domain.models import UserSettings
from kash.services import formatting, scheduler
from kash.services.cron import CronError, validate_for_sync
from kash.web.deps import OWNER_USER_ID, Provider, get_data_provider, get_templates, redirect_to
from kash.web.routes.ai import settings_context as ai_settings_context
from kash.web.routes.notifications import settings_context as push_settings_context
from kash.web.routes.people import LINK_COOKIE, people_context

router = APIRouter()

# Offered in the sync time zone list (the saved one is always included).
COMMON_TIMEZONES = (
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Phoenix",
    "America/Los_Angeles",
    "America/Anchorage",
    "Pacific/Honolulu",
    "America/Toronto",
    "America/Vancouver",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Asia/Tokyo",
    "Australia/Sydney",
    "UTC",
)


def _display(saved: UserSettings) -> formatting.DisplaySettings:
    return formatting.DisplaySettings(
        currency=saved.currency_code if saved.currency_code in formatting.CURRENCIES else "USD",
        locale=saved.locale if saved.locale in formatting.LOCALES else "en_US",
        theme=saved.theme if saved.theme in formatting.THEMES else "light",
        font_scale=saved.font_scale if saved.font_scale in formatting.FONT_SCALES else 100,
        timezone=scheduler.effective_schedule(saved, get_settings()).timezone,
    )


def _wall_time(value: datetime) -> str:
    """``Thu Oct 9, 4:00 AM`` in the value's own time zone (the form's, maybe unsaved)."""
    clock = (
        f"{value:%H:%M}"
        if formatting.current().fmt.clock24
        else f"{value.hour % 12 or 12}:{value:%M} {'AM' if value.hour < 12 else 'PM'}"
    )
    return f"{value:%a} {formatting.day(value.date())}, {clock}"


def cron_info(text: str, timezone: str) -> dict[str, Any] | None:
    """What a custom schedule means: a description and the next runs, or the problem."""
    if not text.strip():
        return None
    try:
        expr = validate_for_sync(text)
    except CronError as exc:
        return {"error": f"{str(exc)[0].upper()}{str(exc)[1:]}."}
    zone = ZoneInfo(timezone if scheduler.valid_timezone(timezone) else "UTC")
    runs = expr.upcoming(datetime.now(zone), 4)
    return {
        "description": expr.describe(clock24=formatting.current().fmt.clock24),
        "runs": [_wall_time(r) for r in runs],
    }


async def load_display_settings() -> None:
    """Read the saved settings and use them for all formatting (at startup)."""
    provider = get_data_provider()
    formatting.configure(_display(await provider.settings.get_settings(OWNER_USER_ID)))


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request, provider: Provider) -> HTMLResponse:
    """Application preferences."""
    user = await provider.users.get_user(request.state.user_id)
    saved = await provider.settings.get_settings(OWNER_USER_ID)
    schedule = scheduler.effective_schedule(saved, get_settings())
    connections = await provider.bank_connections.list_connections(OWNER_USER_ID)
    timezones = list(COMMON_TIMEZONES)
    if schedule.timezone not in timezones:
        timezones.insert(0, schedule.timezone)
    response = get_templates(request).TemplateResponse(
        request=request,
        name="pages/settings.html",
        context={
            "active_page": "settings",
            "account_email": user.email if user else "",
            "account_name": user.display_name if user else "",
            "current": formatting.current(),
            "my_theme": request.state.theme,
            "my_font_scale": request.state.font_scale,
            "currencies": formatting.CURRENCIES,
            "locales": formatting.LOCALES,
            "font_scales": formatting.FONT_SCALES,
            "today_example": date.today(),
            "sync": schedule,
            "sync_intervals": scheduler.INTERVALS,
            "sync_timezones": timezones,
            "sync_next": scheduler.next_run_at if schedule.runs else None,
            "cron_draft": request.query_params.get("cron_draft"),
            "cron_info": cron_info(
                request.query_params.get("cron_draft") or schedule.cron_text, schedule.timezone
            ),
            "connections": connections,
            **ai_settings_context(saved),
            **await push_settings_context(request, provider),
            **await people_context(request, provider),
            "sample": {
                "money": formatting.money(-1234.56),
                "income": formatting.money(1234.5, sign=True),
            },
        },
    )
    response.delete_cookie(LINK_COOKIE, path="/settings")  # a new link is shown only once
    return response


async def _save(provider: Provider, **changes: str | int) -> formatting.DisplaySettings:
    saved = await provider.settings.get_settings(OWNER_USER_ID)
    updated = saved.model_copy(update=changes)
    updated.currency_symbol = formatting.CURRENCIES.get(updated.currency_code, ("$", 2))[0]
    display = _display(await provider.settings.save_settings(updated))
    formatting.configure(display)
    return display


@router.post("/settings")
async def save_settings(
    request: Request,
    provider: Provider,
    currency: Annotated[str, Form()],
    locale: Annotated[str, Form()],
    theme: Annotated[str, Form()] = "light",
    font_scale: Annotated[int, Form()] = 100,
) -> RedirectResponse:
    """Save display preferences: currency and format for the household, theme and text
    size for the person saving. They apply to every page right away."""
    if (
        currency not in formatting.CURRENCIES
        or locale not in formatting.LOCALES
        or theme not in formatting.THEMES
        or font_scale not in formatting.FONT_SCALES
    ):
        return redirect_to("/settings", error="Unknown currency, format, theme, or text size.")
    await _save(provider, currency_code=currency, locale=locale)
    await provider.users.save_preferences(request.state.user_id, theme, font_scale)
    return redirect_to("/settings", notice="Settings saved.")


@router.post("/settings/theme")
async def save_theme(
    request: Request, provider: Provider, theme: Annotated[str, Form()]
) -> Response:
    """The sidebar light/dark switch (called in the background, no page reload)."""
    if theme not in formatting.THEMES:
        return Response(status_code=400)
    await provider.users.save_preferences(request.state.user_id, theme, request.state.font_scale)
    return Response(status_code=204)


@router.post("/settings/sync")
async def save_sync(
    provider: Provider,
    interval: Annotated[str, Form()],
    timezone: Annotated[str, Form()],
    enabled: Annotated[str, Form()] = "",
    cron: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Automatic bank sync: on/off, every N hours from midnight or a custom (cron)
    schedule, and time zone. The interval and the expression are both kept, so
    switching between them brings the other back."""
    if not scheduler.valid_timezone(timezone):
        return redirect_to("/settings", error="Unknown sync interval or time zone.")
    saved = await provider.settings.get_settings(OWNER_USER_ID)
    changes: dict[str, object] = {"sync_enabled": bool(enabled), "sync_timezone": timezone}
    if interval == "cron":
        try:
            expr = validate_for_sync(cron)
        except CronError as exc:
            return redirect_to("/settings", error=f"Custom schedule: {exc}.", cron_draft=cron)
        changes.update(sync_mode="cron", sync_cron=expr.source)
    else:
        hours = int(interval) if interval.isdigit() else None
        if hours not in scheduler.INTERVALS:
            return redirect_to("/settings", error="Unknown sync interval or time zone.")
        changes.update(sync_mode=None, sync_interval_hours=hours)
    updated = await provider.settings.save_settings(saved.model_copy(update=changes))
    formatting.configure(_display(updated))  # times are shown in this zone too
    scheduler.notify_schedule_changed()
    return redirect_to("/settings", notice="Sync settings saved.")


@router.get("/settings/sync/preview", response_class=HTMLResponse)
async def sync_preview(request: Request, cron: str = "", timezone: str = "UTC") -> HTMLResponse:
    """What a custom schedule means, while it's typed (htmx)."""
    return get_templates(request).TemplateResponse(
        request=request,
        name="components/sync_cron_preview.html",
        context={"cron_info": cron_info(cron, timezone)},
    )
