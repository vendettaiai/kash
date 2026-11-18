"""Which cards the dashboard shows, in what order (TASK-P7-04).

Phones and computers have their own layout, because the cards differ (a phone
shows income and expenses as two small cards, a computer as a row of stats).
The defaults are the dashboards as they were before customizing existed.

Stored in ``user_settings.dashboard_widgets`` as
``{"mobile": [{"id": "balance"}, ...], "desktop": [{"id": "stats", "span": 12}, ...]}``;
a device that was never customized uses its default. Desktop cards sit in a
12-column grid and ``span`` is their width.
"""

from dataclasses import dataclass
from typing import Any, Literal

Device = Literal["mobile", "desktop"]
DEVICES: tuple[Device, ...] = ("mobile", "desktop")
SPANS = (4, 5, 6, 7, 8, 12)  # desktop widths, in twelfths


@dataclass(frozen=True)
class Card:
    id: str
    title: str
    description: str
    icon: str
    devices: tuple[Device, ...]
    span: int = 12  # default desktop width


CATALOG: tuple[Card, ...] = (
    Card("balance", "Banking balance", "Balance, change this month and trend", "bank", ("mobile",)),
    Card(
        "income_expenses", "Income & expenses", "Two cards with details", "arrow-up-right",
        ("mobile",),
    ),
    Card("net_flow", "Surplus or deficit", "What's left this month", "trending-up", ("mobile",)),
    Card("stats", "Summary", "Balance, income, expenses and surplus", "wallet", ("desktop",)),
    Card(
        "net_worth", "Net worth", "Assets minus debts, change this month", "landmark",
        ("mobile", "desktop"), span=5,
    ),
    Card(
        "categories", "Spending by category", "Where the money went", "pie-chart",
        ("mobile", "desktop"), span=5,
    ),
    Card(
        "sankey", "Money flow", "Diagram of income flowing into spending and savings",
        "arrow-left-right", ("mobile", "desktop"), span=12,
    ),
    Card("budget", "Budget", "Spent vs. budgeted", "budget", ("mobile", "desktop"), span=4),
    Card(
        "recent", "Recent transactions", "The latest activity", "transactions",
        ("mobile", "desktop"), span=7,
    ),
    Card(
        "bills", "Upcoming bills", "Due in the next two weeks", "calendar",
        ("mobile", "desktop"), span=4,
    ),
    Card(
        "cash_flow", "Income vs. expenses", "Two bars and the net result", "bar-chart",
        ("mobile", "desktop"), span=4,
    ),
)  # fmt: skip
CARDS = {c.id: c for c in CATALOG}

DEFAULTS: dict[Device, tuple[str, ...]] = {
    "mobile": ("balance", "income_expenses", "net_flow", "budget", "recent"),
    "desktop": ("stats", "categories", "recent", "bills", "budget", "cash_flow"),
}


@dataclass(frozen=True)
class Placed:
    """A card on the dashboard."""

    card: Card
    span: int

    @property
    def id(self) -> str:
        return self.card.id


def default_layout(device: Device) -> list[Placed]:
    return [Placed(CARDS[i], CARDS[i].span) for i in DEFAULTS[device]]


def clean(device: Device, entries: Any) -> list[Placed] | None:
    """Validated cards from stored/submitted data, or None if it isn't a list.
    Unknown, duplicate and other-device cards are dropped; bad widths reset."""
    if not isinstance(entries, list):
        return None
    placed: list[Placed] = []
    seen: set[str] = set()
    for entry in entries:
        card_id = entry.get("id") if isinstance(entry, dict) else entry
        card = CARDS.get(card_id) if isinstance(card_id, str) else None
        if card is None or device not in card.devices or card.id in seen:
            continue
        span = entry.get("span") if isinstance(entry, dict) else None
        seen.add(card.id)
        placed.append(Placed(card, span if span in SPANS else card.span))
    return placed


def layout(stored: Any, device: Device) -> list[Placed]:
    """The saved layout for ``device``, or its default."""
    saved = clean(device, stored.get(device)) if isinstance(stored, dict) else None
    return default_layout(device) if saved is None else saved


def to_stored(stored: Any, device: Device, placed: list[Placed] | None) -> dict[str, Any]:
    """``stored`` with ``device`` replaced (None = back to the default)."""
    result = {d: v for d, v in stored.items() if d in DEVICES} if isinstance(stored, dict) else {}
    if placed is None:
        result.pop(device, None)
    else:
        result[device] = [
            {"id": p.id, "span": p.span} if device == "desktop" else {"id": p.id} for p in placed
        ]
    return result


def editor_data(stored: Any) -> dict[str, Any]:
    """Everything the Customize panel needs, per device."""
    return {
        device: {
            "cards": [
                {
                    "id": c.id,
                    "title": c.title,
                    "description": c.description,
                    "icon": c.icon,
                    "span": c.span,
                }
                for c in CATALOG
                if device in c.devices
            ],  # fmt: skip
            "layout": [{"id": p.id, "span": p.span} for p in layout(stored, device)],
            "default": [{"id": p.id, "span": p.span} for p in default_layout(device)],
        }
        for device in DEVICES
    }
