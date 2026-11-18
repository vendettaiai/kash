"""Kash main FastAPI application entry point."""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates

from kash import __version__
from kash.config import check_production_secrets, get_settings
from kash.db.session import close_db, init_db, seed_initial_defaults
from kash.services import formatting, merchant_icons
from kash.web.static_files import VersionedStaticFiles, static_url

settings = get_settings()
logger = logging.getLogger("kash")


def configure_logging(level: str) -> None:
    """Print Kash's own log lines (``kash.*``) to stderr, next to uvicorn's.

    Without a handler Python shows only warnings, so e.g. which scheduled sync ran
    and what it imported was never visible in the container log.
    """
    if any(getattr(h, "_kash", False) for h in logger.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler._kash = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))


configure_logging(settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create the schema and default categories/rules on startup; dispose the engine on exit."""
    check_production_secrets(settings)
    await init_db()
    await seed_initial_defaults()
    from kash.web.routes.settings import load_display_settings

    await load_display_settings()
    from kash.owner import OWNER_USER_ID
    from kash.web.deps import get_data_provider

    await merchant_icons.load_custom(get_data_provider(), OWNER_USER_ID)
    sync_task = _start_sync_schedule()
    notifications_task = _start_notifications()
    yield
    notifications_task.cancel()
    sync_task.cancel()
    await close_db()


def _start_sync_schedule() -> asyncio.Task[None]:
    """Background bank sync; its schedule comes from Settings (or SYNC_* env vars)."""
    from kash.services import scheduler
    from kash.web.deps import get_bank_providers, get_data_provider

    return asyncio.create_task(
        scheduler.run_forever(get_data_provider, get_bank_providers, get_settings)
    )


def _start_notifications() -> asyncio.Task[None]:
    """The hourly check for things worth a push notification."""
    from kash.services.notifications import checker
    from kash.web.deps import get_data_provider

    return asyncio.create_task(checker.run_forever(get_data_provider, get_settings))


app = FastAPI(
    title=settings.app_name,
    description="Self-hosted personal finance and budgeting application",
    version=__version__,
    docs_url="/api/docs" if settings.debug else None,
    redoc_url=None,
    lifespan=lifespan,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "web" / "static"
TEMPLATES_DIR = BASE_DIR / "web" / "templates"

app.mount("/static", VersionedStaticFiles(directory=STATIC_DIR), name="static")

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
templates.env.globals["display"] = formatting.current  # {{ display().theme }}
templates.env.globals["static_url"] = static_url  # {{ static_url("css/app.css") }}
templates.env.globals["app_version"] = __version__  # shown in Settings
# Merchant logos (components/merchant_icon.html): {{ merchant_box(tx=tx, ...) }}
templates.env.globals["merchant_icon"] = merchant_icons.lookup
templates.env.globals["transaction_icon"] = merchant_icons.for_transaction
templates.env.globals["bill_icon"] = merchant_icons.for_bill
templates.env.globals["transaction_icon_info"] = merchant_icons.transaction_info
templates.env.filters["money"] = (
    formatting.money
)  # {{ amount | money }}, money(0), money(sign=True)
templates.env.filters["day"] = formatting.day  # Sep 27 / 27 Sep
templates.env.filters["fulldate"] = formatting.fulldate  # Sep 27, 2026 / 27 Sep 2026
templates.env.filters["dayheading"] = formatting.dayheading  # Today / Wed, Sep 27
templates.env.filters["clock"] = formatting.clock  # 3:05 PM / 15:05
app.state.templates = templates

from kash.web.middleware import AuthMiddleware  # noqa: E402

app.add_middleware(AuthMiddleware)
# Outermost: compress pages and CSS/JS (the 500-row ledger, ~60 KB of CSS) for phones away
# from home. Session cookies are SameSite=Lax, so another site can't make a logged-in
# browser fetch pages with guessed text in them (what compression-size attacks need).
app.add_middleware(GZipMiddleware, minimum_size=1024)

from kash.web.router import router as web_router  # noqa: E402

app.include_router(web_router)


@app.get("/healthz", response_class=JSONResponse)
async def healthz() -> dict[str, str]:
    """Basic liveness health check."""
    return {"status": "ok", "app": settings.app_name, "version": __version__}


@app.get("/readyz", response_class=JSONResponse)
async def readyz() -> dict[str, Any]:
    """Readiness probe."""
    database = "sqlite" if settings.database_url.startswith("sqlite") else "postgresql"
    return {
        "status": "ready",
        "app": settings.app_name,
        "database": database,
        "environment": settings.environment,
    }
