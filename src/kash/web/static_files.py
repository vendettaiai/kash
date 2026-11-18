"""Versioned URLs for static files, so browsers pick up new CSS/JS after an update.

``static_url("css/app.css")`` -> ``/static/css/app.css?v=3f9a1c2e``: a short hash of
the file's contents. The URL changes only when the file does, so browsers keep
caching it between updates but never keep an old copy after one. The hash is
recomputed when the file's modification time changes (e.g. a CSS rebuild during
development).
"""

import hashlib
from pathlib import Path
from urllib.parse import parse_qs

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

STATIC_DIR = Path(__file__).resolve().parent / "static"

_cache: dict[str, tuple[int, str]] = {}  # path -> (mtime_ns, hash)


def static_url(path: str) -> str:
    file = (STATIC_DIR / path).resolve()
    if not file.is_relative_to(STATIC_DIR) or not file.is_file():
        return f"/static/{path}"
    mtime = file.stat().st_mtime_ns
    cached = _cache.get(path)
    if cached is None or cached[0] != mtime:
        cached = (mtime, hashlib.sha256(file.read_bytes()).hexdigest()[:8])
        _cache[path] = cached
    return f"/static/{path}?v={cached[1]}"


class VersionedStaticFiles(StaticFiles):
    """/static/ files. One asked for with ``?v=<hash>`` (from ``static_url``) never changes
    at that address, so browsers may keep it for a year without checking back; others are
    checked each time (ETag)."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        versioned = "v" in parse_qs(scope.get("query_string", b"").decode("latin-1"))
        if versioned and response.status_code in (200, 304):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response
