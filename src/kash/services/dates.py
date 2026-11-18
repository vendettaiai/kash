"""Calendar helpers shared by budgets, the dashboard, and bills."""

from datetime import date, timedelta

from kash.domain.models import BillFrequency


def shift_months(day: date, months: int, anchor_day: int | None = None) -> date:
    """Move ``day`` by whole months, clamping to the last day of the target month.

    ``anchor_day`` keeps a bill on its usual day (e.g. the 31st) instead of drifting
    after a short month.
    """
    index = day.year * 12 + day.month - 1 + months
    year, month = divmod(index, 12)
    first = date(year, month + 1, 1)
    last = (first + timedelta(days=32)).replace(day=1) - timedelta(days=1)
    return first.replace(day=min(anchor_day or day.day, last.day))


# Typical gap between payments, in days
PERIOD_DAYS: dict[BillFrequency, float] = {
    BillFrequency.WEEKLY: 7,
    BillFrequency.BIWEEKLY: 14,
    BillFrequency.SEMI_MONTHLY: 15.2,
    BillFrequency.MONTHLY: 30.4,
    BillFrequency.QUARTERLY: 91.3,
    BillFrequency.ANNUAL: 365.25,
}

_MONTHS = {BillFrequency.MONTHLY: 1, BillFrequency.QUARTERLY: 3, BillFrequency.ANNUAL: 12}


def next_due(day: date, frequency: BillFrequency, anchor_day: int | None = None) -> date:
    """The due date after ``day`` for a bill with this frequency."""
    if frequency in _MONTHS:
        return shift_months(day, _MONTHS[frequency], anchor_day)
    if frequency == BillFrequency.SEMI_MONTHLY:
        return day + timedelta(days=15)
    return day + timedelta(days=int(PERIOD_DAYS[frequency]))


def previous_due(day: date, frequency: BillFrequency, anchor_day: int | None = None) -> date:
    """The due date before ``day`` for a bill with this frequency."""
    if frequency in _MONTHS:
        return shift_months(day, -_MONTHS[frequency], anchor_day)
    if frequency == BillFrequency.SEMI_MONTHLY:
        return day - timedelta(days=15)
    return day - timedelta(days=int(PERIOD_DAYS[frequency]))
