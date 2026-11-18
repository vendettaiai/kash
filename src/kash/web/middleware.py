"""Login required for every page, and a same-origin check on form posts (ADR-016, ADR-019)."""

from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from fastapi import Request
from fastapi.responses import RedirectResponse, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from kash.config import get_settings
from kash.services import auth, formatting
from kash.web.deps import OWNER_USER_ID, get_data_provider

# The PWA manifest and service worker are fetched without the login cookie
PUBLIC_PATHS = {"/login", "/setup", "/healthz", "/readyz", "/manifest.webmanifest", "/sw.js"}
PUBLIC_PREFIXES = ("/static/", "/join/")  # /join/<token>: invite links
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
FLASH_PARAMS = {"notice", "error", "synced"}  # one-time messages (base.html)


def _same_origin(request: Request) -> bool:
    """Browsers send Origin (or Referer) on form posts; it must be this site.

    Requests without either header (curl, tests) are allowed: without the
    session cookie they are refused anyway, and the cookie is SameSite=Lax.
    """
    source = request.headers.get("origin") or request.headers.get("referer")
    if source == "null":  # sandboxed or privacy-stripped origin: can't be ours
        return False
    if not source:
        return True
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return urlsplit(source).netloc.lower() == host.split(",")[0].strip().lower()


def _stay_on_page(request: Request, response: Response) -> Response:
    """A save sent from a page in the browser (HTMX, see static/js/kash.js) that redirects
    back to that page returns to the exact address it was sent from, so its filters, month
    and open tab survive (``/rules`` -> ``/rules?tab=transfer&q=gas``). Parameters in the
    redirect win; an old notice or error is dropped. Plain form posts are unchanged."""
    if request.method not in UNSAFE_METHODS or response.status_code not in (302, 303):
        return response
    current = request.headers.get("hx-current-url")
    location = response.headers.get("location")
    if not request.headers.get("hx-request") or not current or not location:
        return response
    here, there = urlsplit(current), urlsplit(location)
    if there.scheme or there.netloc or here.path != there.path:
        return response
    redirect_params = parse_qsl(there.query, keep_blank_values=True)
    replaced = FLASH_PARAMS | {key for key, _ in redirect_params}
    kept = [
        (key, value)
        for key, value in parse_qsl(here.query, keep_blank_values=True)
        if key not in replaced
    ]
    query = urlencode(kept + redirect_params)
    response.headers["location"] = urlunsplit(("", "", there.path, query, there.fragment))
    return response


def _to(request: Request, path: str) -> Response:
    """Redirect, or tell HTMX to navigate (it would otherwise swap the login page in)."""
    if request.headers.get("hx-request"):
        return Response(status_code=204, headers={"HX-Redirect": path})
    return RedirectResponse(url=path, status_code=303)


class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if request.method in UNSAFE_METHODS and not _same_origin(request):
            return Response("Cross-site request refused.", status_code=403)
        if path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES):
            return await call_next(request)

        users = get_data_provider().users
        owner = await users.get_user(OWNER_USER_ID)
        if owner is None or not auth.has_account(owner.hashed_password):
            return _to(request, "/setup")
        settings = get_settings()
        token = request.cookies.get(settings.session_cookie_name)
        parsed = auth.session_user(token)
        user = owner if parsed and parsed[0] == OWNER_USER_ID else None
        if parsed and user is None:
            user = await users.get_user(parsed[0])
        if (
            user is None
            or not user.is_active
            or auth.session_valid(settings.secret_key, token, user.hashed_password) is None
        ):
            target = path + (f"?{request.url.query}" if request.url.query else "")
            next_part = "" if target == "/" else f"?next={quote(target, safe='')}"
            return _to(request, f"/login{next_part}")
        # Who is logged in. The data they see is the household's (OWNER_USER_ID, ADR-019).
        request.state.user_id = user.id
        request.state.is_owner = user.id == OWNER_USER_ID
        request.state.user_name = user.display_name  # greeting and sidebar
        saved = await get_data_provider().settings.get_settings(OWNER_USER_ID)
        request.state.ai_enabled = saved.ai_enabled  # sidebar link
        # Their own look; empty = the household's (base.html)
        request.state.theme = user.theme if user.theme in formatting.THEMES else saved.theme
        request.state.font_scale = (
            user.font_scale if user.font_scale in formatting.FONT_SCALES else saved.font_scale
        )
        return _stay_on_page(request, await call_next(request))
