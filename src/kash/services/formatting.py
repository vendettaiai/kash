"""Money and date formatting from the owner's display settings.

One display currency and one locale for the whole app (single owner, ADR-010).
The app loads the saved settings at startup and after they change (``configure``);
templates use the ``money`` / ``day`` / ``fulldate`` filters and Python code calls
the functions below, so nothing hardcodes "$" or US date order.

Month names stay in English; only order, separators and symbol placement change.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DateOrder = Literal["MDY", "DMY", "YMD"]


@dataclass(frozen=True)
class LocaleFormat:
    label: str
    group: str  # thousands separator
    decimal: str
    symbol_after: bool  # "1.234,56 €" instead of "€1,234.56"
    symbol_space: bool
    dates: DateOrder
    clock24: bool
    intl: str  # BCP 47 tag for the browser's Intl.NumberFormat


LOCALES: dict[str, LocaleFormat] = {
    "en_US": LocaleFormat(
        "English (US) · 1,234.56 · Sep 27, 2026", ",", ".", False, False, "MDY", False, "en-US"
    ),
    "en_GB": LocaleFormat(
        "English (UK) · 1,234.56 · 27 Sep 2026", ",", ".", False, False, "DMY", True, "en-GB"
    ),
    "en_AU": LocaleFormat(
        "English (Australia) · 1,234.56 · 27 Sep 2026",
        ",",
        ".",
        False,
        False,
        "DMY",
        False,
        "en-AU",
    ),
    "en_CA": LocaleFormat(
        "English (Canada) · 1,234.56 · 2026-09-27", ",", ".", False, False, "YMD", False, "en-CA"
    ),
    "de_DE": LocaleFormat(
        "Deutsch · 1.234,56 · 27 Sep 2026", ".", ",", True, True, "DMY", True, "de-DE"
    ),
    "fr_FR": LocaleFormat(
        "Français · 1 234,56 · 27 Sep 2026", " ", ",", True, True, "DMY", True, "fr-FR"
    ),
    "es_ES": LocaleFormat(
        "Español · 1.234,56 · 27 Sep 2026", ".", ",", True, True, "DMY", True, "es-ES"
    ),
    "it_IT": LocaleFormat(
        "Italiano · 1.234,56 · 27 Sep 2026", ".", ",", True, True, "DMY", True, "it-IT"
    ),
    "nl_NL": LocaleFormat(
        "Nederlands · 1.234,56 · 27 Sep 2026", ".", ",", False, True, "DMY", True, "nl-NL"
    ),
    "pt_BR": LocaleFormat(
        "Português (Brasil) · 1.234,56 · 27 Sep 2026", ".", ",", False, True, "DMY", True, "pt-BR"
    ),
    "sv_SE": LocaleFormat(
        "Svenska · 1 234,56 · 2026-09-27", " ", ",", True, True, "YMD", True, "sv-SE"
    ),
    "ja_JP": LocaleFormat(
        "日本語 · 1,234.56 · 2026-09-27", ",", ".", False, False, "YMD", True, "ja-JP"
    ),
}

# ISO 4217 code -> (symbol, decimal places)
CURRENCIES: dict[str, tuple[str, int]] = {
    "USD": ("$", 2),
    "CAD": ("$", 2),
    "AUD": ("$", 2),
    "MXN": ("$", 2),
    "EUR": ("€", 2),
    "GBP": ("£", 2),
    "CHF": ("CHF", 2),
    "SEK": ("kr", 2),
    "BRL": ("R$", 2),
    "INR": ("₹", 2),
    "JPY": ("¥", 0),
}

THEMES = ("light", "dark", "system")

# Text size (% of the browser's default); every size in the app scales with it
FONT_SCALES = {90: "Small", 100: "Default", 110: "Large", 125: "Larger", 140: "Largest"}


@dataclass(frozen=True)
class DisplaySettings:
    currency: str = "USD"
    locale: str = "en_US"
    theme: str = "light"
    timezone: str = "UTC"  # times are stored in UTC and shown in this zone
    font_scale: int = 100  # FONT_SCALES

    @property
    def fmt(self) -> LocaleFormat:
        return LOCALES.get(self.locale, LOCALES["en_US"])

    @property
    def symbol(self) -> str:
        return CURRENCIES.get(self.currency, ("$", 2))[0]

    @property
    def decimals(self) -> int:
        return CURRENCIES.get(self.currency, ("$", 2))[1]


_current = DisplaySettings()


def configure(settings: DisplaySettings) -> None:
    """Use these settings from now on (called at startup and when they are saved)."""
    global _current
    _current = settings


def current() -> DisplaySettings:
    return _current


def number(value: Decimal | float | int, decimals: int = 2) -> str:
    """``1234.5`` -> ``"1,234.50"`` (or ``"1.234,50"``…), no sign handling."""
    fmt = _current.fmt
    quant = Decimal(1).scaleb(-decimals)
    text = f"{Decimal(str(value)).quantize(quant, rounding=ROUND_HALF_UP):,.{decimals}f}"
    return text.replace(",", "\0").replace(".", fmt.decimal).replace("\0", fmt.group)


def money(
    value: Decimal | float | int | None, decimals: int | None = None, sign: bool = False
) -> str:
    """Amount with the currency symbol: ``-$1,234.56``; ``sign`` adds ``+`` to positives.

    ``decimals`` defaults to the currency's (``0`` gives whole amounts).
    """
    amount = Decimal(str(value or 0))
    places = _current.decimals if decimals is None else min(decimals, _current.decimals)
    fmt = _current.fmt
    digits = number(abs(amount), places)
    space = " " if fmt.symbol_space else ""
    body = (
        f"{digits}{space}{_current.symbol}"
        if fmt.symbol_after
        else f"{_current.symbol}{space}{digits}"
    )
    rounded_zero = Decimal(digits.replace(fmt.group, "").replace(fmt.decimal, ".")) == 0
    if amount < 0 and not rounded_zero:
        return f"-{body}"
    return f"+{body}" if sign and amount > 0 and not rounded_zero else body


def local(value: date | datetime) -> date | datetime:
    """A stored time (UTC, or naive = UTC) in the display time zone; dates unchanged."""
    if not isinstance(value, datetime):
        return value
    try:
        zone = ZoneInfo(_current.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        return value
    aware = value if value.tzinfo else value.replace(tzinfo=UTC)
    return aware.astimezone(zone)


def day(value: date | datetime) -> str:
    """Short date without the year: ``Sep 27`` / ``27 Sep`` / ``09-27``."""
    value = local(value)
    order = _current.fmt.dates
    if order == "YMD":
        return f"{value:%m-%d}"
    return f"{value:%b} {value.day}" if order == "MDY" else f"{value.day} {value:%b}"


def fulldate(value: date | datetime) -> str:
    """Date with the year: ``Sep 27, 2026`` / ``27 Sep 2026`` / ``2026-09-27``."""
    value = local(value)
    order = _current.fmt.dates
    if order == "YMD":
        return f"{value:%Y-%m-%d}"
    return (
        f"{value:%b} {value.day}, {value.year}"
        if order == "MDY"
        else f"{value.day} {value:%b} {value.year}"
    )


def dayheading(value: date | datetime, today: date | None = None) -> str:
    """Heading for a day's transactions: ``Today``, ``Yesterday``, ``Wed, Sep 27``
    (the year is added when it isn't this year)."""
    value = local(value)
    if isinstance(value, datetime):
        value = value.date()
    if today is None:
        today = local(datetime.now(UTC)).date()  # type: ignore[union-attr]
    if value == today:
        return "Today"
    if (today - value).days == 1:
        return "Yesterday"
    label = fulldate(value) if value.year != today.year else day(value)
    return f"{value:%a}, {label}"


def clock(value: datetime) -> str:
    """``3:05 PM`` or ``15:05``, in the display time zone."""
    value = local(value)  # type: ignore[assignment]
    if _current.fmt.clock24:
        return f"{value:%H:%M}"
    return f"{value.hour % 12 or 12}:{value:%M} {value:%p}"
