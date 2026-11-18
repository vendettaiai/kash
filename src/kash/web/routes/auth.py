"""First-run account setup, login and logout (ADR-016); several logins per household (ADR-019)."""

import hmac
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from kash.config import get_settings
from kash.services import auth
from kash.web.deps import OWNER_USER_ID, Provider, get_templates, redirect_to, safe_local_path

router = APIRouter()


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def login_response(target: str, user_id: UUID, password_hash: str) -> RedirectResponse:
    settings = get_settings()
    response = RedirectResponse(url=target, status_code=303)
    response.set_cookie(
        settings.session_cookie_name,
        auth.make_session(
            settings.secret_key, user_id, password_hash, settings.session_expire_seconds
        ),
        max_age=settings.session_expire_seconds,
        httponly=True,
        samesite="lax",
        secure=settings.secure_cookies,
    )
    return response


def _page(request: Request, name: str, **context: object) -> HTMLResponse:
    return get_templates(request).TemplateResponse(request=request, name=name, context=context)


@router.get("/setup", response_class=HTMLResponse, response_model=None)
async def setup_page(request: Request, provider: Provider) -> Response:
    """Create the owner account (only before one exists)."""
    user = await provider.users.get_user(OWNER_USER_ID)
    if user and auth.has_account(user.hashed_password):
        return redirect_to("/login")
    return _page(
        request,
        "pages/setup.html",
        error=request.query_params.get("error"),
        needs_token=bool(get_settings().setup_token),
    )


@router.post("/setup")
async def setup(
    provider: Provider,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    confirm: Annotated[str, Form()],
    setup_token: Annotated[str, Form()] = "",
    name: Annotated[str, Form()] = "",
) -> RedirectResponse:
    user = await provider.users.get_user(OWNER_USER_ID)
    if user and auth.has_account(user.hashed_password):
        return redirect_to("/login")
    expected = get_settings().setup_token
    if expected and not hmac.compare_digest(setup_token.strip(), expected):
        return redirect_to("/setup", error="The setup token is wrong.")
    email = email.strip().lower()
    if "@" not in email:
        return redirect_to("/setup", error="Enter your email address.")
    if len(password) < auth.MIN_PASSWORD_LENGTH:
        return redirect_to("/setup", error=f"Use at least {auth.MIN_PASSWORD_LENGTH} characters.")
    if password != confirm:
        return redirect_to("/setup", error="The passwords don't match.")
    hashed = auth.hash_password(password)
    await provider.users.save_credentials(OWNER_USER_ID, email, hashed)
    await provider.users.save_display_name(OWNER_USER_ID, name.strip()[:100])
    return login_response("/", OWNER_USER_ID, hashed)


@router.get("/login", response_class=HTMLResponse, response_model=None)
async def login_page(request: Request, provider: Provider) -> Response:
    user = await provider.users.get_user(OWNER_USER_ID)
    if not user or not auth.has_account(user.hashed_password):
        return redirect_to("/setup")
    return _page(
        request,
        "pages/login.html",
        error=request.query_params.get("error"),
        next=safe_local_path(request.query_params.get("next"), "/"),
    )


@router.post("/login")
async def login(
    request: Request,
    provider: Provider,
    email: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",  # noqa: A002 - form field name
) -> RedirectResponse:
    client = _client(request)
    target = safe_local_path(next, "/")
    if auth.throttle.blocked(client):
        return redirect_to("/login", error="Too many attempts. Try again in 15 minutes.")
    user = await provider.users.get_user_by_email(email)
    if (
        user is None
        or not user.is_active
        or not auth.verify_password(user.hashed_password, password)
    ):
        auth.throttle.failed(client)
        return redirect_to("/login", error="Wrong email or password.", next=target)
    auth.throttle.succeeded(client)
    return login_response(target, user.id, user.hashed_password)


@router.post("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(get_settings().session_cookie_name)
    return response


@router.post("/settings/password")
async def change_password(
    request: Request,
    provider: Provider,
    current: Annotated[str, Form()],
    password: Annotated[str, Form()],
    confirm: Annotated[str, Form()],
) -> RedirectResponse:
    """Change your own password; your other devices are logged out."""
    user = await provider.users.get_user(request.state.user_id)
    if user is None or not auth.verify_password(user.hashed_password, current):
        return redirect_to("/settings", error="Your current password is wrong.")
    if len(password) < auth.MIN_PASSWORD_LENGTH:
        return redirect_to(
            "/settings", error=f"Use at least {auth.MIN_PASSWORD_LENGTH} characters."
        )
    if password != confirm:
        return redirect_to("/settings", error="The new passwords don't match.")
    hashed = auth.hash_password(password)
    await provider.users.save_credentials(user.id, user.email, hashed)
    return login_response("/settings?notice=Password+changed.", user.id, hashed)


@router.post("/settings/account")
async def save_account(
    request: Request, provider: Provider, name: Annotated[str, Form()] = ""
) -> RedirectResponse:
    """Change your name shown in the greeting and sidebar."""
    await provider.users.save_display_name(request.state.user_id, name.strip()[:100])
    return redirect_to("/settings", notice="Name saved.")
