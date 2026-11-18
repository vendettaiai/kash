"""Five-field cron expressions for the automatic bank sync schedule.

``minute hour day-of-month month day-of-week``, for example ``0 4-8 * * *``: at
4:00, 5:00, 6:00, 7:00 and 8:00 every day. Each field is a comma-separated list of
``*``, a number, a range ``a-b``, each optionally with a step (``*/15``, ``0-30/10``,
``5/20``). Months and weekdays also take names (``jan``, ``mon-fri``); weekday 0 and
7 are both Sunday. As in standard cron, when both day fields are restricted, a day
that matches either one counts.

Time zone: times are wall-clock times in the schedule's time zone (Settings > Bank
sync). Daylight saving time: a time skipped when clocks spring forward (e.g. 2:30 AM)
runs an hour later that day (3:30 AM); a time that happens twice when clocks fall
back runs once, the first time. A run is never repeated.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta

MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
WEEKDAYS = ("sun", "mon", "tue", "wed", "thu", "fri", "sat")
MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)
WEEKDAY_NAMES = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")

MAX_RUNS_PER_DAY = 24  # SimpleFIN expects about 24 requests a day
_SEARCH_DAYS = 8 * 366  # Feb 29 can be 8 years away (2096 -> 2104)


class CronError(ValueError):
    """Not a valid five-field cron expression (the message says why)."""


@dataclass(frozen=True)
class _Field:
    name: str
    low: int
    high: int
    names: tuple[str, ...] = ()
    name_offset: int = 0  # value of names[0]


_FIELDS = (
    _Field("minute", 0, 59),
    _Field("hour", 0, 23),
    _Field("day of month", 1, 31),
    _Field("month", 1, 12, MONTHS, 1),
    _Field("day of week", 0, 7, WEEKDAYS, 0),
)


def _value(text: str, field: _Field) -> int:
    lowered = text.lower()
    if field.names and lowered in field.names:
        return field.names.index(lowered) + field.name_offset
    if not text.isdigit():
        raise CronError(f"“{text}” isn't a valid {field.name}")
    number = int(text)
    if not field.low <= number <= field.high:
        raise CronError(f"{field.name} must be {field.low}–{field.high}, not {number}")
    return number


def _parse_field(text: str, field: _Field) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        if not part:
            raise CronError(f"empty value in the {field.name} field")
        base, _, step_text = part.partition("/")
        step = 1
        if step_text:
            if not step_text.isdigit() or int(step_text) == 0:
                raise CronError(f"“/{step_text}” isn't a valid step in the {field.name} field")
            step = int(step_text)
        if base == "*":
            start, end = field.low, field.high
        elif "-" in base:
            first, _, last = base.partition("-")
            start, end = _value(first, field), _value(last, field)
            if start > end:
                raise CronError(f"the range {base} in the {field.name} field runs backwards")
        else:
            start = _value(base, field)
            end = field.high if step_text else start  # "5/20" = from 5, every 20
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class CronExpression:
    source: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days: frozenset[int]
    months: frozenset[int]
    weekdays: frozenset[int]  # 0 = Sunday
    any_day: bool  # day-of-month field starts with "*"
    any_weekday: bool  # day-of-week field starts with "*"

    @classmethod
    def parse(cls, text: str) -> "CronExpression":
        """Parse and check an expression; raises ``CronError`` with a readable reason."""
        source = " ".join(text.split())
        parts = source.split(" ")
        if len(parts) != 5 or not source:
            raise CronError(
                "use five fields: minute hour day-of-month month day-of-week (e.g. 0 4-8 * * *)"
            )
        fields = [_parse_field(p, f) for p, f in zip(parts, _FIELDS, strict=True)]
        expr = cls(
            source=source,
            minutes=fields[0],
            hours=fields[1],
            days=fields[2],
            months=fields[3],
            weekdays=frozenset(d % 7 for d in fields[4]),
            any_day=parts[2].startswith("*"),
            any_weekday=parts[4].startswith("*"),
        )
        if not any(expr.matches_day(d) for d in _days(date(2024, 1, 1), _SEARCH_DAYS)):
            raise CronError("this never runs (no such date)")
        return expr

    @property
    def runs_per_day(self) -> int:
        """Most runs on any one day."""
        return len(self.minutes) * len(self.hours)

    def matches_day(self, day: date) -> bool:
        if day.month not in self.months:
            return False
        in_month = day.day in self.days
        in_week = (day.isoweekday() % 7) in self.weekdays
        if self.any_day or self.any_weekday:
            return in_month and in_week
        return in_month or in_week

    def times(self) -> list[time]:
        return [time(h, m) for h in sorted(self.hours) for m in sorted(self.minutes)]

    def next_after(self, now: datetime) -> datetime:
        """The first run strictly after ``now`` (an aware time), in ``now``'s zone."""
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        zone = now.tzinfo
        after = now.timestamp()
        times = self.times()
        start = now.date() - timedelta(days=1)  # compare instants, not wall clocks
        for day in _days(start, _SEARCH_DAYS):
            if not self.matches_day(day):
                continue
            for t in times:
                candidate = datetime.combine(day, t, tzinfo=zone)  # fold=0: see module doc
                if candidate.timestamp() > after:
                    # Through UTC, so a time that doesn't exist (spring forward) comes
                    # back as the real moment, e.g. 2:30 -> 3:30 AM daylight time
                    return candidate.astimezone(UTC).astimezone(zone)
        raise CronError("this never runs")  # pragma: no cover - parse() rules it out

    def upcoming(self, now: datetime, count: int = 5) -> list[datetime]:
        runs: list[datetime] = []
        at = now
        for _ in range(count):
            at = self.next_after(at)
            runs.append(at)
        return runs

    def describe(self, clock24: bool = False) -> str:
        """Plain-English summary, e.g. ``Every day at 4:00, 5:00, 6:00, 7:00 and 8:00 AM``."""
        days, times = self._days_text(), self._times_text(clock24)
        if days == "Every day" and not times.startswith("at "):
            return times[0].upper() + times[1:]  # "Every hour at :30, …", not "Every day every…"
        return f"{days} {times}"

    def _days_text(self) -> str:
        weekdays = sorted(self.weekdays)
        month_part = ""
        if len(self.months) < 12:
            month_part = " in " + _join([MONTH_NAMES[m - 1] for m in sorted(self.months)])
        if self.any_day and self.any_weekday:
            return ("Every day" + month_part) if month_part else "Every day"
        week_part = ""
        if not self.any_weekday:
            if weekdays == [1, 2, 3, 4, 5]:
                week_part = "on weekdays"
            elif weekdays == [0, 6]:
                week_part = "on weekends"
            else:
                week_part = "on " + _join([WEEKDAY_NAMES[d] + "s" for d in weekdays])
        day_part = ""
        if not self.any_day:
            day_part = "on day " + _join([str(d) for d in sorted(self.days)]) + " of the month"
        joined = " or ".join(p for p in (day_part, week_part) if p)
        return (joined[0].upper() + joined[1:]) + month_part

    def _times_text(self, clock24: bool) -> str:
        times = self.times()
        if len(times) <= 8:
            return "at " + _join([_clock(t, clock24) for t in times])
        minutes, hours = sorted(self.minutes), sorted(self.hours)
        at = "every minute" if len(minutes) == 60 else "at " + _join([f":{m:02d}" for m in minutes])
        if len(hours) == 24:
            return f"every hour {at}"
        gaps = {b - a for a, b in zip(hours, hours[1:], strict=False)}
        if len(gaps) == 1:
            gap = gaps.pop()
            every = "every hour" if gap == 1 else f"every {gap} hours"
            first, last = times[0], times[-1]
            return f"{every} {at}, {_clock(first, clock24)} to {_clock(last, clock24)}"
        hour_list = _join([_clock(time(h, 0), clock24) for h in hours])
        return f"{len(times)} times a day: {at} in the hours starting {hour_list}"


def _days(start: date, count: int) -> Iterator[date]:
    for offset in range(count):
        yield start + timedelta(days=offset)


def _clock(t: time, clock24: bool) -> str:
    if clock24:
        return f"{t:%H:%M}"
    return f"{t.hour % 12 or 12}:{t:%M} {'AM' if t.hour < 12 else 'PM'}"


def _join(items: list[str]) -> str:
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


def validate_for_sync(text: str) -> CronExpression:
    """Parse an expression and check it suits bank sync (at most 24 runs a day)."""
    expr = CronExpression.parse(text)
    if expr.runs_per_day > MAX_RUNS_PER_DAY:
        raise CronError(
            f"that's up to {expr.runs_per_day} syncs a day; SimpleFIN allows about "
            f"{MAX_RUNS_PER_DAY} requests a day, so use at most {MAX_RUNS_PER_DAY} times a day"
        )
    return expr
