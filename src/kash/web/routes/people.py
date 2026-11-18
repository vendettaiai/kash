"""People in the household: the owner invites others, who log in to the same data (ADR-019).

An invite is a one-time link (``/join/<token>``) the owner sends however they like. The
same kind of link lets someone who forgot their password set a new one. Kash keeps only
the token's hash, so the link is shown once, right after it is made (a short-lived cookie
carries it to the Settings page).
"""

from typing import Annotated
from urllib.parse import urlencode
from uuid import UUID

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from kash.config import get_settings
from kash.domain.models import User
from kash.domain.protocols import DataProvider
from kash.services import auth
from kash.web.deps import OWNER_USER_ID, Provider, get_templates, redirect_to
from kash.web.routes.auth import login_response

router = APIRouter()

LINK_COOKIE = "kash_new_link"  # the link just made, shown once on Settings
MAX_PEOPLE = 20


def _to_people(**params: str) -> RedirectResponse:
    return RedirectResponse(url=f"/settings?{urlencode(params)}#people", status_code=303)


def _with_link(response: RedirectResponse, token: str) -> RedirectResponse:
    response.set_cookie(
        LINK_COOKIE,
        token,
        max_age=600,
        path="/settings",
        httponly=True,
        samesite="lax",
        secure=get_settings().secure_cookies,
    )
    return response


async def _member(request: Request, provider: Provider, user_id: UUID) -> User | str:
    """The person the owner is acting on (never the owner), or why not."""
    if not request.state.is_owner:
        return "Only the owner can manage people."
    person = await provider.users.get_user(user_id)
    if person is None or person.id == OWNER_USER_ID:
        return "That person isn't in this household."
    return person


async def people_context(request: Request, provider: DataProvider) -> dict[str, object]:
    """Settings → People: everyone with a login, and a link made a moment ago."""
    people = await provider.users.list_users()
    owner = next((p for p in people if p.id == OWNER_USER_ID), None)
    new_link = None
    token = request.cookies.get(LINK_COOKIE)
    if token and request.state.is_owner:
        person = await provider.users.get_user_by_invite(auth.invite_hash(token))
        if person is not None and not auth.invite_expired(person.invite_expires_at):
            new_link = {
                "person": person,
                "url": str(request.url_for("join_page", token=token)),
                "joined": auth.has_account(person.hashed_password),
            }
    return {
        "people": sorted(people, key=lambda p: (p.id != OWNER_USER_ID,)),
        "owner_id": OWNER_USER_ID,
        "owner_name": (owner.display_name or owner.email) if owner else "",
        "new_link": new_link,
        "can_add_people": len(people) < MAX_PEOPLE,
        "has_account": auth.has_account,
        "invite_expired": auth.invite_expired,
        "invite_days": auth.INVITE_DAYS,
    }


@router.post("/settings/people")
async def invite_person(
    request: Request,
    provider: Provider,
    email: Annotated[str, Form()],
    name: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Add someone to the household and make their join link."""
    if not request.state.is_owner:
        return _to_people(error="Only the owner can add people.")
    email = email.strip().lower()
    if "@" not in email or len(email) > 255:
        return _to_people(error="Enter their email address.")
    if await provider.users.get_user_by_email(email):
        return _to_people(error=f"{email} already has a login here.")
    if len(await provider.users.list_users()) >= MAX_PEOPLE:
        return _to_people(error=f"A household can have up to {MAX_PEOPLE} people.")
    token, token_hash, expires = auth.new_invite()
    await provider.users.create_user(
        User(
            email=email,
            hashed_password=auth.UNSET_PASSWORD,
            display_name=name.strip()[:100],
            invite_token_hash=token_hash,
            invite_expires_at=expires,
        )
    )
    return _with_link(_to_people(notice="Invite link ready. Send it to them."), token)


@router.post("/settings/people/{user_id}/link")
async def new_link(request: Request, provider: Provider, user_id: UUID) -> RedirectResponse:
    """A fresh link: to join (if they haven't yet) or to set a new password. Older links stop
    working."""
    person = await _member(request, provider, user_id)
    if isinstance(person, str):
        return _to_people(error=person)
    token, token_hash, expires = auth.new_invite()
    await provider.users.save_invite(person.id, token_hash, expires)
    return _with_link(_to_people(notice="New link ready."), token)


@router.post("/settings/people/{user_id}/remove")
async def remove_person(request: Request, provider: Provider, user_id: UUID) -> RedirectResponse:
    """Remove someone's login. The household's data stays as it is."""
    person = await _member(request, provider, user_id)
    if isinstance(person, str):
        return _to_people(error=person)
    await provider.users.delete_user(person.id)
    return _to_people(notice=f"Removed {person.display_name or person.email}.")


async def _invited(provider: Provider, token: str) -> User | None:
    """The person a link belongs to, if it is still valid."""
    person = await provider.users.get_user_by_invite(auth.invite_hash(token))
    if person is None or auth.invite_expired(person.invite_expires_at) or not person.is_active:
        return None
    return person


@router.get("/join/{token}", response_class=HTMLResponse, name="join_page")
async def join_page(request: Request, provider: Provider, token: str) -> HTMLResponse:
    person = await _invited(provider, token)
    owner = await provider.users.get_user(OWNER_USER_ID)
    return get_templates(request).TemplateResponse(
        request=request,
        name="pages/join.html",
        context={
            "person": person,
            "reset": person is not None and auth.has_account(person.hashed_password),
            "owner_name": (owner.display_name or owner.email) if owner else "",
            "token": token,
            "error": request.query_params.get("error"),
        },
    )


@router.post("/join/{token}")
async def join(
    provider: Provider,
    token: str,
    password: Annotated[str, Form()],
    confirm: Annotated[str, Form()],
    name: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Choose a password from the link; the link is used up and they are logged in."""
    person = await _invited(provider, token)
    if person is None:
        return redirect_to(f"/join/{token}")
    if len(password) < auth.MIN_PASSWORD_LENGTH:
        return redirect_to(
            f"/join/{token}", error=f"Use at least {auth.MIN_PASSWORD_LENGTH} characters."
        )
    if password != confirm:
        return redirect_to(f"/join/{token}", error="The passwords don't match.")
    hashed = auth.hash_password(password)
    await provider.users.save_credentials(person.id, person.email, hashed)
    if name.strip():
        await provider.users.save_display_name(person.id, name.strip()[:100])
    return login_response("/", person.id, hashed)
