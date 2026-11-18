"""Push notifications: turning them on or off on a device, and a test message."""

from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response

from kash.domain.models import PushSubscription
from kash.domain.protocols import DataProvider
from kash.services import formatting
from kash.services.notifications import prefs as notification_prefs
from kash.services.notifications import push
from kash.services.notifications.prefs import (
    BUDGET_PERCENTS,
    DUE_DAYS,
    OVERDUE_DAYS,
    NotificationPrefs,
    parse_amount,
)
from kash.services.notifications.rules import BANKING_TYPES
from kash.web.deps import OWNER_USER_ID, Provider, redirect_to

router = APIRouter()

SECTION = "/settings?section=notifications"


def get_push_http() -> httpx.AsyncClient | None:
    """The client for push services; None makes a real one. Tests replace this."""
    return None


PushHttp = Annotated[httpx.AsyncClient | None, Depends(get_push_http)]


async def settings_context(request: Request, provider: DataProvider) -> dict[str, Any]:
    """For Settings. Keys are made here, not on the button: browsers only allow the
    permission prompt straight after a tap, with nothing awaited before it."""
    subscriptions = await provider.notifications.list_subscriptions(request.state.user_id)
    try:
        public_key = (await push.get_keys(provider)).public
    except push.KeysUnreadable:
        public_key = ""
    user = await provider.users.get_user(request.state.user_id)
    accounts = await provider.accounts.list_accounts(OWNER_USER_ID)
    return {
        "alert_accounts": [a for a in accounts if a.account_type in BANKING_TYPES],
        "push_public_key": public_key,
        "push_devices": len(subscriptions),
        "alerts": notification_prefs.from_stored(user.notification_prefs if user else None),
        "alert_hours": [(hour, _hour_label(hour)) for hour in range(24)],
        "alert_overdue_days": OVERDUE_DAYS,
        "alert_due_days": DUE_DAYS,
        "alert_percents": BUDGET_PERCENTS,
    }


def _hour_label(hour: int) -> str:
    if formatting.current().fmt.clock24:
        return f"{hour:02d}:00"
    return f"{hour % 12 or 12} {'AM' if hour < 12 else 'PM'}"


@router.post("/settings/notifications")
async def save_alerts(
    request: Request,
    provider: Provider,
    hour: Annotated[int, Form()],
    overdue_days: Annotated[int, Form()],
    due_days: Annotated[int, Form()],
    budget_percent: Annotated[int, Form()],
    bill_overdue: Annotated[str, Form()] = "",
    bill_due: Annotated[str, Form()] = "",
    sync: Annotated[str, Form()] = "",
    needs_category: Annotated[str, Form()] = "",
    budget: Annotated[str, Form()] = "",
    large_tx: Annotated[str, Form()] = "",
    large_amount: Annotated[str, Form()] = "",
    low_balance: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Which alerts this person gets; everyone chooses their own (ADR-021)."""
    if (
        not 0 <= hour <= 23
        or overdue_days not in OVERDUE_DAYS
        or due_days not in DUE_DAYS
        or budget_percent not in BUDGET_PERCENTS
    ):
        return redirect_to(SECTION, error="Unknown alert setting.")
    amount = parse_amount(large_amount)
    if amount is None:
        return redirect_to(SECTION, error="Enter an amount for large transactions.")
    form = await request.form()
    limits = {}
    for account in await provider.accounts.list_accounts(OWNER_USER_ID):
        text = str(form.get(f"limit_{account.id}", "")).strip()
        if not text or account.account_type not in BANKING_TYPES:
            continue  # blank: no limit for this account
        limit = parse_amount(text)
        if limit is None:
            return redirect_to(SECTION, error=f"Enter a limit above zero for {account.name}.")
        limits[str(account.id)] = limit
    chosen = NotificationPrefs(
        hour=hour,
        bill_overdue=bool(bill_overdue),
        overdue_days=overdue_days,
        bill_due=bool(bill_due),
        due_days=due_days,
        sync=bool(sync),
        needs_category=bool(needs_category),
        budget=bool(budget),
        budget_percent=budget_percent,
        large_tx=bool(large_tx),
        large_amount=amount,
        low_balance=bool(low_balance),
        balance_limits=limits,
    )
    await provider.users.save_notification_prefs(
        request.state.user_id, notification_prefs.to_stored(chosen)
    )
    return redirect_to(SECTION, notice="Alerts saved.")


@router.post("/notifications/subscribe")
async def subscribe(
    request: Request,
    provider: Provider,
    endpoint: Annotated[str, Form()],
    p256dh: Annotated[str, Form()],
    auth: Annotated[str, Form()],
) -> Response:
    """This device accepts notifications for whoever is logged in."""
    if not push.valid_subscription(endpoint, p256dh, auth):
        return Response(status_code=400)
    await provider.notifications.save_subscription(
        PushSubscription(user_id=request.state.user_id, endpoint=endpoint, p256dh=p256dh, auth=auth)
    )
    return Response(status_code=204)


@router.post("/notifications/unsubscribe")
async def unsubscribe(
    request: Request, provider: Provider, endpoint: Annotated[str, Form()]
) -> Response:
    await provider.notifications.delete_subscription(request.state.user_id, endpoint)
    return Response(status_code=204)


@router.post("/notifications/test")
async def send_test(request: Request, provider: Provider, http: PushHttp) -> JSONResponse:
    """A sample to this person's devices; the page says how it went."""
    try:
        keys = await push.get_keys(provider)
    except push.KeysUnreadable:
        return JSONResponse({"sent": 0}, status_code=409)
    result = await push.send_test(provider, keys, request.state.user_id, http)
    return JSONResponse({"sent": result.accepted})


@router.post("/notifications/reset")
async def reset(request: Request, provider: Provider) -> RedirectResponse:
    """After the encryption key changed: new keys, and everyone turns them on again."""
    if not request.state.is_owner:
        return redirect_to(SECTION, error="Only the owner can reset notifications.")
    await push.reset_keys(provider)
    return redirect_to(SECTION, notice="Notifications reset. Turn them on again on each device.")
