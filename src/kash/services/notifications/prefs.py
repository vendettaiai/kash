"""Which alerts a person wants (Settings → Notifications).

Stored as JSON in ``users.notification_prefs``; anything missing or invalid falls back
to the default, so an empty ``{}`` means "all defaults".
"""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

DEFAULT_HOUR = 8
OVERDUE_DAYS = (1, 2, 3, 5, 7, 14)
DUE_DAYS = (1, 2, 3, 5, 7)
BUDGET_PERCENTS = (50, 75, 80, 90, 100)
DEFAULT_LARGE_AMOUNT = Decimal("500")


@dataclass(frozen=True)
class NotificationPrefs:
    hour: int = DEFAULT_HOUR  # when the daily alerts go out, in the household's time zone
    bill_overdue: bool = True
    overdue_days: int = 3
    bill_due: bool = True
    due_days: int = 3
    sync: bool = True
    needs_category: bool = True
    budget: bool = True
    budget_percent: int = 100
    large_tx: bool = False
    large_amount: Decimal = DEFAULT_LARGE_AMOUNT
    low_balance: bool = False
    balance_limits: dict[str, Decimal] = field(default_factory=dict)  # account id -> limit


def parse_amount(value: Any) -> Decimal | None:
    """A positive amount (kept as text in the JSON, so it stays exact), or None."""
    try:
        amount = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    return amount if amount.is_finite() and amount > 0 else None


def _section(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name)
    return value if isinstance(value, dict) else {}


def _on(section: dict[str, Any], default: bool) -> bool:
    value = section.get("on")
    return value if isinstance(value, bool) else default


def _choice(value: Any, choices: tuple[int, ...], default: int) -> int:
    return value if value in choices else default


def from_stored(data: Any) -> NotificationPrefs:
    """Preferences from ``users.notification_prefs`` (bad or missing values → defaults)."""
    if not isinstance(data, dict):
        return NotificationPrefs()
    default = NotificationPrefs()
    hour = data.get("hour")
    overdue = _section(data, "bill_overdue")
    due = _section(data, "bill_due")
    budget = _section(data, "budget")
    large = _section(data, "large_tx")
    low = _section(data, "low_balance")
    limits = {key: parse_amount(value) for key, value in _section(low, "limits").items()}
    return NotificationPrefs(
        hour=hour if isinstance(hour, int) and 0 <= hour <= 23 else default.hour,
        bill_overdue=_on(overdue, default.bill_overdue),
        overdue_days=_choice(overdue.get("days"), OVERDUE_DAYS, default.overdue_days),
        bill_due=_on(due, default.bill_due),
        due_days=_choice(due.get("days"), DUE_DAYS, default.due_days),
        sync=_on(_section(data, "sync"), default.sync),
        needs_category=_on(_section(data, "needs_category"), default.needs_category),
        budget=_on(budget, default.budget),
        budget_percent=_choice(budget.get("percent"), BUDGET_PERCENTS, default.budget_percent),
        large_tx=_on(large, default.large_tx),
        large_amount=parse_amount(large.get("amount")) or default.large_amount,
        low_balance=_on(low, default.low_balance),
        balance_limits={key: limit for key, limit in limits.items() if limit is not None},
    )


def to_stored(prefs: NotificationPrefs) -> dict[str, Any]:
    return {
        "hour": prefs.hour,
        "bill_overdue": {"on": prefs.bill_overdue, "days": prefs.overdue_days},
        "bill_due": {"on": prefs.bill_due, "days": prefs.due_days},
        "sync": {"on": prefs.sync},
        "needs_category": {"on": prefs.needs_category},
        "budget": {"on": prefs.budget, "percent": prefs.budget_percent},
        "large_tx": {"on": prefs.large_tx, "amount": str(prefs.large_amount)},
        "low_balance": {
            "on": prefs.low_balance,
            "limits": {key: str(limit) for key, limit in prefs.balance_limits.items()},
        },
    }
