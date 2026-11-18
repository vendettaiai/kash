"""The hourly check for things worth a push notification.

Runs inside the app process, next to the sync schedule and not on it: that loop waits
forever when automatic sync is off, and a Sync button never goes through it. Each tick
sends what's new to everyone who has a device: hourly alerts always, daily ones the first
tick after the time each person chose. "Told already" lives in the database, so a restart
catches up on a missed day and can't repeat anything.
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx

from kash.config import Settings
from kash.domain.models import PushSubscription
from kash.domain.protocols import DataProvider
from kash.owner import OWNER_USER_ID
from kash.services import scheduler
from kash.services.bank.sync import any_sync_running
from kash.services.bills.service import refresh_bills
from kash.services.notifications import prefs as notification_prefs
from kash.services.notifications import push
from kash.services.notifications.prefs import NotificationPrefs
from kash.services.notifications.rules import RULES, Facts, Rule, build_message

logger = logging.getLogger(__name__)

TICK_MINUTE = 15  # after syncs that start on the hour
QUIET_HOURS = (22, 7)  # default syncs run overnight; news about them can wait for the morning
SYNC_WAIT_SECONDS = 300.0
SYNC_WAIT_TRIES = 3
KEEP_SENT_DAYS = 400  # past every rule's lookback, so nothing pruned can qualify again
DAILY = "daily"
BILL_KINDS = ("bill_overdue", "bill_due")


def _quiet(hour: int) -> bool:
    return hour >= QUIET_HOURS[0] or hour < QUIET_HOURS[1]


def _sync_in_progress() -> bool:
    return scheduler.sync_all_running or any_sync_running()


async def _wait_for_sync() -> bool:
    """Transfers get paired and the AI files things after a sync, so what looks new
    mid-sync may not be. False if it's still going: the next tick will do."""
    for _ in range(SYNC_WAIT_TRIES):
        if not _sync_in_progress():
            return True
        await asyncio.sleep(SYNC_WAIT_SECONDS)
    return not _sync_in_progress()


async def _send_rule(
    provider: DataProvider,
    keys: push.Keys,
    rule: Rule,
    facts: Facts,
    user_id: UUID,
    prefs: NotificationPrefs,
    devices: list[PushSubscription],
    http: httpx.AsyncClient | None,
) -> bool:
    """Tell this person what's new for one rule. False if no device took it, in which
    case it's unclaimed for the next try. Claiming first means a crash loses a message
    rather than sending it twice."""
    if rule.recovered:
        await provider.notifications.release(user_id, rule.kind, await rule.recovered(facts, prefs))
    found = await rule.find(facts, prefs)
    new = set(await provider.notifications.claim(user_id, rule.kind, [a.key for a in found]))
    if not new:
        return True
    message = build_message(rule, [a for a in found if a.key in new], facts.today)
    result = await push.deliver(provider, keys, devices, message, http)
    if result.accepted == 0:
        await provider.notifications.release(user_id, rule.kind, list(new))
        return False
    return True


async def _check_person(
    provider: DataProvider,
    keys: push.Keys,
    facts: Facts,
    user_id: UUID,
    prefs: NotificationPrefs,
    devices: list[PushSubscription],
    daily: bool,
    http: httpx.AsyncClient | None,
) -> None:
    sent = True
    for rule in RULES:
        if not rule.enabled(prefs) or (rule.daily and not daily):
            continue
        if rule.holds_overnight and _quiet(facts.now.hour):
            continue
        told = await _send_rule(provider, keys, rule, facts, user_id, prefs, devices, http)
        sent = told and sent
    if daily and sent:
        await provider.notifications.claim(user_id, DAILY, [str(facts.today)])


async def check(
    provider: DataProvider,
    env: Settings,
    now: datetime | None = None,
    http: httpx.AsyncClient | None = None,
) -> None:
    """One tick. ``now`` and ``http`` are for tests."""
    subscriptions = await provider.notifications.list_all_subscriptions()
    if not subscriptions or not await _wait_for_sync():
        return
    devices: dict[UUID, list[PushSubscription]] = defaultdict(list)
    for subscription in subscriptions:
        devices[subscription.user_id].append(subscription)

    saved = await provider.settings.get_settings(OWNER_USER_ID)
    schedule = scheduler.effective_schedule(saved, env)
    local = (now or datetime.now(UTC)).astimezone(ZoneInfo(schedule.timezone))
    try:
        keys = await push.get_keys(provider)
    except push.KeysUnreadable:
        logger.error("Push keys can't be read (encryption key changed); not sending")
        return

    people = {}
    for user in await provider.users.list_users():
        if user.is_active and user.id in devices:
            people[user.id] = notification_prefs.from_stored(user.notification_prefs)
    done = {
        user_id: str(local.date()) in await provider.notifications.list_claimed(user_id, DAILY)
        for user_id in people
    }
    daily = {
        user_id: local.hour >= prefs.hour and not done[user_id] for user_id, prefs in people.items()
    }
    if any(daily[u] and (p.bill_overdue or p.bill_due) for u, p in people.items()):
        await refresh_bills(provider, OWNER_USER_ID, local.date())

    facts = Facts(provider, schedule, local)
    for user_id, prefs in people.items():
        try:
            await _check_person(
                provider, keys, facts, user_id, prefs, devices[user_id], daily[user_id], http
            )
        except Exception:
            logger.exception("Notification check failed for one person")
    if any(daily.values()):
        await provider.notifications.prune_sent(local - timedelta(days=KEEP_SENT_DAYS))


def _seconds_to_next_tick(now: datetime) -> float:
    tick = now.replace(minute=TICK_MINUTE, second=0, microsecond=0)
    if tick <= now:
        tick += timedelta(hours=1)
    return (tick - now).total_seconds()


async def run_forever(
    get_provider: Callable[[], DataProvider], get_env: Callable[[], Settings]
) -> None:
    """Sleep to the next tick, then check. Cancel the task to stop it."""
    while True:
        await asyncio.sleep(_seconds_to_next_tick(datetime.now(UTC)))
        try:
            await check(get_provider(), get_env())
        except Exception:
            logger.exception("Notification check failed")
