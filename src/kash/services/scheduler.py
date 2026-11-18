"""Automatic bank sync at fixed hours of the day (e.g. every 4 hours: 0, 4, 8, 12, 16, 20),
or on a custom cron schedule (e.g. ``0 4-8 * * *``: hourly from 4 to 8 AM).

Runs inside the app process (one uvicorn worker). Each run syncs every active
connection the same way the "Sync with Bank" button does. Six runs a day stay
well under SimpleFIN's 24 requests/day; the provider's own rate-limit guard
still applies.

The schedule comes from Settings → Bank sync; anything not set there falls back
to ``SYNC_SCHEDULE_HOURS`` / ``SYNC_TIMEZONE``. Saving settings wakes the loop
(``notify_schedule_changed``), so changes apply without a restart. Exactly one
mode is used at a time: in cron mode the interval hours are kept (to switch back
to) but never run. The schedule lives in the database, so after a restart the
loop picks up from the next due time; runs missed while the app was down are
not made up.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from kash.config import Settings
from kash.domain.models import BankConnectionStatus, UserSettings
from kash.domain.protocols import BankProvider, DataProvider
from kash.owner import OWNER_USER_ID
from kash.services.ai.categorize import run_after_sync
from kash.services.bank.sync import BankSyncService
from kash.services.bills.service import after_import_hook
from kash.services.cron import CronError, CronExpression, validate_for_sync
from kash.services.reports import record_snapshots

logger = logging.getLogger(__name__)

INTERVALS = (1, 2, 3, 4, 6, 8, 12, 24)  # hours; each divides the day evenly
DEFAULT_INTERVAL = 4
MAX_SLEEP_SECONDS = 3600.0


@dataclass(frozen=True)
class Schedule:
    enabled: bool
    hours: list[int]
    timezone: str
    mode: str = "interval"  # "interval" (the hours) or "cron"
    cron: CronExpression | None = None  # cron mode, when the saved expression is valid
    cron_text: str = ""  # the saved expression, even if invalid (shown in Settings)
    error: str | None = None  # why automatic sync can't run as saved

    @property
    def interval(self) -> int | None:
        """Every N hours from midnight, if the hours form such a pattern."""
        for n in INTERVALS:
            if self.hours == list(range(0, 24, n)):
                return n
        return None

    @property
    def runs(self) -> bool:
        """Whether automatic sync runs (on, and the schedule is usable)."""
        return self.enabled and self.error is None

    def next_after(self, now: datetime) -> datetime:
        """The next run after ``now`` (aware, in the schedule's zone)."""
        if self.mode == "cron" and self.cron is not None:
            return self.cron.next_after(now)
        return next_run(now, self.hours)


def interval_hours(every: int) -> list[int]:
    """Every ``every`` hours starting at midnight: 4 -> [0, 4, 8, 12, 16, 20]."""
    return list(range(0, 24, every))


def valid_timezone(name: str) -> bool:
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return False
    return True


def effective_schedule(saved: UserSettings, env: Settings) -> Schedule:
    """Settings saved in the app win; otherwise the environment variables."""
    try:
        env_hours = parse_hours(env.sync_schedule_hours)
    except ValueError:
        logger.error("Invalid SYNC_SCHEDULE_HOURS=%r; ignoring it", env.sync_schedule_hours)
        env_hours = []
    enabled = saved.sync_enabled if saved.sync_enabled is not None else bool(env_hours)
    if saved.sync_interval_hours in INTERVALS:
        hours = interval_hours(saved.sync_interval_hours or DEFAULT_INTERVAL)
    else:
        hours = env_hours or interval_hours(DEFAULT_INTERVAL)
    timezone = saved.sync_timezone or env.sync_timezone
    if not valid_timezone(timezone):
        logger.error("Unknown time zone %r; using UTC", timezone)
        timezone = "UTC"
    if saved.sync_mode != "cron":
        return Schedule(
            enabled=enabled, hours=hours, timezone=timezone, cron_text=saved.sync_cron or ""
        )
    text = saved.sync_cron or ""
    try:
        cron = validate_for_sync(text)
    except CronError as exc:
        # Saved before a rule changed, or edited in the database: don't guess another
        # schedule; automatic sync waits until it's fixed (Settings shows why).
        logger.error("Custom sync schedule %r is invalid (%s); automatic sync paused", text, exc)
        return Schedule(
            enabled=enabled,
            hours=hours,
            timezone=timezone,
            mode="cron",
            cron_text=text,
            error=f"The custom schedule “{text}” can't be used: {exc}.",
        )
    return Schedule(
        enabled=enabled, hours=hours, timezone=timezone, mode="cron", cron=cron, cron_text=text
    )


# Loop state, for the Settings page and the notification check (one process).
next_run_at: datetime | None = None
sync_all_running = False
_changed: asyncio.Event | None = None


def notify_schedule_changed() -> None:
    """Wake the scheduler so it re-reads the settings (call after saving them)."""
    if _changed is not None:
        _changed.set()


def parse_hours(value: str) -> list[int]:
    """``"0,4,8"`` -> ``[0, 4, 8]``; empty means off. Raises ValueError on bad input."""
    hours = sorted({int(part) for part in value.replace(" ", "").split(",") if part})
    if any(h < 0 or h > 23 for h in hours):
        raise ValueError(f"Sync hours must be between 0 and 23: {value!r}")
    return hours


def next_run(now: datetime, hours: list[int]) -> datetime:
    """The next time (after ``now``, same time zone) at one of ``hours``:00."""
    for day in range(2):
        base = (now + timedelta(days=day)).replace(minute=0, second=0, microsecond=0)
        for hour in hours:
            candidate = base.replace(hour=hour)
            if candidate > now:
                return candidate
    raise ValueError("no sync hours")


async def sync_all(provider: DataProvider, bank_providers: dict[str, BankProvider]) -> int:
    """Sync every active connection; returns how many synced without errors."""
    global sync_all_running
    sync_all_running = True
    try:
        return await _sync_all(provider, bank_providers)
    finally:
        sync_all_running = False


async def _sync_all(provider: DataProvider, bank_providers: dict[str, BankProvider]) -> int:
    service = BankSyncService(
        provider, providers=bank_providers, on_transactions_imported=after_import_hook(provider)
    )
    ok = 0
    outcomes: list[str] = []
    for connection in await provider.bank_connections.list_connections(OWNER_USER_ID):
        # A failed last sync (ERROR) is retried; paused or needs-new-token ones are not.
        if connection.status not in (BankConnectionStatus.ACTIVE, BankConnectionStatus.ERROR):
            logger.info(
                "bank_sync scheduled skip connection=%s status=%s",
                connection.id.hex[:8],
                connection.status.value,
            )
            continue
        try:
            result = await service.sync_connection(
                connection.id, OWNER_USER_ID, trigger="scheduled"
            )
        except Exception:
            logger.exception("Scheduled sync failed for connection %s", connection.id.hex[:8])
            outcomes.append("failed")
            continue
        outcomes.append(result.outcome)
        if result.errors:
            logger.warning(
                "Scheduled sync of connection %s failed: %s",
                connection.id.hex[:8],
                "; ".join(result.errors),
            )
        else:
            ok += 1  # saved (possibly with warnings, which the sync logged)
    await record_snapshots(provider, OWNER_USER_ID)
    await run_after_sync(provider, OWNER_USER_ID)  # no-op unless AI is on
    logger.info(
        "bank_sync scheduled done connections=%d ok=%d partial=%d failed=%d",
        len(outcomes),
        outcomes.count("ok"),
        outcomes.count("partial"),
        outcomes.count("failed"),
    )
    return ok


async def run_forever(
    get_provider: Callable[[], DataProvider],
    get_bank_providers: Callable[[], dict[str, BankProvider]],
    get_env: Callable[[], Settings],
) -> None:
    """Sleep until the next scheduled hour, then sync; re-read the schedule whenever
    settings change. Cancel the task to stop it."""
    global next_run_at, _changed
    _changed = asyncio.Event()
    while True:
        _changed.clear()
        try:
            saved = await get_provider().settings.get_settings(OWNER_USER_ID)
            schedule = effective_schedule(saved, get_env())
        except Exception:
            # e.g. the database is briefly unreachable: try again shortly
            logger.exception("Couldn't read the sync schedule; retrying in a minute")
            next_run_at = None
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(_changed.wait(), timeout=60)
            continue
        if not schedule.runs:
            next_run_at = None
            logger.info(
                "Automatic bank sync is %s",
                "paused (invalid schedule)" if schedule.error else "off",
            )
            await _changed.wait()
            continue
        now = datetime.now(ZoneInfo(schedule.timezone))
        due = schedule.next_after(now)
        if due != next_run_at:
            logger.info(
                "Next bank sync at %s (%s)",
                due.isoformat(),
                f"cron {schedule.cron_text}" if schedule.mode == "cron" else "interval",
            )
        next_run_at = due
        delay = max(0.0, due.timestamp() - now.timestamp())  # DST-safe
        if delay > MAX_SLEEP_SECONDS:
            # Long waits are taken in steps so the time is re-checked against the
            # clock (a suspended VM or clock change can't make a run late)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(_changed.wait(), timeout=MAX_SLEEP_SECONDS)
            continue
        try:
            await asyncio.wait_for(_changed.wait(), timeout=delay)
            continue  # settings changed: recompute
        except TimeoutError:
            pass
        try:
            synced = await sync_all(get_provider(), get_bank_providers())
            logger.info("Scheduled bank sync done (%d connection(s))", synced)
        except Exception:
            logger.exception("Scheduled bank sync failed")
