"""Merchant logos: which picture to show for a merchant, anywhere in the UI.

The icon belongs to the merchant, not to a transaction or bill. Everything
goes through ``lookup(merchant_text)`` with the bill's merchant, or a
transaction's payee/description; the text is normalized with the bills
merchant normalizer (store numbers, "POS", cities … removed).

Order:
1. A logo the user uploaded for that merchant (overrides the built-in one).
2. A built-in logo (``CATALOG``: Simple Icons SVGs in ``static/merchants/``).
3. None, and the template shows the category/transaction icon as before.

Uploaded logos live in the database (``merchant_icons``) and are indexed in
memory at startup and after every change, so templates can look them up
without a query per row (one owner, one process).
"""

import re
from dataclasses import dataclass
from uuid import UUID

from kash.domain.models import Bill, MerchantIcon, Transaction
from kash.domain.protocols import DataProvider
from kash.services.bills.merchant import normalize_merchant, transaction_merchant
from kash.services.rules.engine import normalize_text

MAX_UPLOAD_BYTES = 512 * 1024
# Raster formats only: an uploaded SVG could carry scripts.
IMAGE_TYPES = {
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"\xff\xd8\xff": "image/jpeg",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
}


@dataclass(frozen=True)
class Brand:
    slug: str  # file name in static/merchants/
    title: str
    words: tuple[str, ...]  # matched as whole words in the normalized merchant
    background: str = "#ffffff"  # tile colour (brand colour when the logo is drawn dark)
    generic: bool = False  # platforms other merchants bill through (Google, Apple, PayPal)


# Built-in logos. More specific names are listed with more words, so "YOUTUBE
# MUSIC" beats "YOUTUBE"; generic platforms lose to the merchant billed through
# them ("GOOGLE TWITCH" → Twitch).
CATALOG: tuple[Brand, ...] = (
    Brand("netflix", "Netflix", ("NETFLIX",)),
    Brand("spotify", "Spotify", ("SPOTIFY",), background="#1ED760"),
    Brand("youtubemusic", "YouTube Music", ("YOUTUBE MUSIC", "YOUTUBEMUSIC")),
    Brand("youtube", "YouTube", ("YOUTUBE", "YOUTUBEPREMIUM", "YOUTUBE PREMIUM", "YOUTUBE TV")),
    Brand("paypal", "PayPal", ("PAYPAL",), generic=True),
    Brand("zelle", "Zelle", ("ZELLE",)),
    Brand("googleplay", "Google Play", ("GOOGLE PLAY",), generic=True),
    Brand("google", "Google", ("GOOGLE",), generic=True),
    Brand("mcdonalds", "McDonald's", ("MCDONALDS", "MCDONALD", "MC DONALDS"), background="#FBC817"),
    Brand("burgerking", "Burger King", ("BURGER KING",)),
    Brand("lyft", "Lyft", ("LYFT",)),
    Brand("ubereats", "Uber Eats", ("UBER EATS", "UBEREATS")),
    Brand("uber", "Uber", ("UBER",)),
    Brand("shell", "Shell", ("SHELL",), background="#FFD500"),
    Brand("ebay", "eBay", ("EBAY",)),
    Brand("tesla", "Tesla", ("TESLA", "SUPERCHARGER")),
    Brand("steam", "Steam", ("STEAM", "STEAMPOWERED", "STEAMGAMES")),
    Brand("discover", "Discover", ("DISCOVER",)),
    Brand("target", "Target", ("TARGET",)),
    Brand("cashapp", "Cash App", ("CASH APP", "CASHAPP", "SQUARE CASH")),
    Brand("venmo", "Venmo", ("VENMO",)),
    Brand("applemusic", "Apple Music", ("APPLE MUSIC",)),
    Brand("apple", "Apple", ("APPLE", "ITUNES"), generic=True),
    Brand("twitch", "Twitch", ("TWITCH",)),
    Brand("claude", "Claude", ("CLAUDE",)),
    Brand("anthropic", "Anthropic", ("ANTHROPIC",)),
    Brand("starbucks", "Starbucks", ("STARBUCKS",)),
    Brand("doordash", "DoorDash", ("DOORDASH",)),
    Brand("instacart", "Instacart", ("INSTACART",)),
    Brand("kfc", "KFC", ("KFC",)),
    Brand("tacobell", "Taco Bell", ("TACO BELL", "TACOBELL")),
    Brand("verizon", "Verizon", ("VERIZON",)),
    Brand("hbo", "HBO", ("HBO",)),
    Brand("paramountplus", "Paramount+", ("PARAMOUNT",)),
    Brand("audible", "Audible", ("AUDIBLE",), background="#F8991C"),
    Brand("playstation", "PlayStation", ("PLAYSTATION", "SONY PLAYSTATION")),
    Brand("samsclub", "Sam's Club", ("SAMS CLUB", "SAMSCLUB")),
    Brand("etsy", "Etsy", ("ETSY",)),
    Brand("klarna", "Klarna", ("KLARNA",), background="#FFB3C7"),
    Brand("afterpay", "Afterpay", ("AFTERPAY",), background="#B2FCE4"),
    Brand("westernunion", "Western Union", ("WESTERN UNION",), background="#FFDD00"),
    Brand("moneygram", "MoneyGram", ("MONEYGRAM",)),
)


@dataclass(frozen=True)
class Icon:
    url: str
    title: str
    background: str
    custom: bool = False
    custom_id: str = ""
    merchant: str = ""  # the uploaded icon's merchant name (custom only)

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "url": self.url,
            "title": self.title,
            "background": self.background,
            "custom": self.custom,
            "custom_id": self.custom_id,
            "merchant": self.merchant,
        }


# Uploaded logos: normalized merchant -> (icon id, version) — see load_custom().
_custom: dict[str, tuple[str, int]] = {}


def merchant_key(text: str | None) -> str:
    """The normalized merchant name used for matching ("NETFLIX", "PANDA EXPRESS")."""
    return normalize_merchant(text or "")


def _has_words(key: str, words: str) -> bool:
    return f" {words} " in f" {key} "


def _brand(key: str) -> Brand | None:
    matches = [(not b.generic, len(w), b) for b in CATALOG for w in b.words if _has_words(key, w)]
    return max(matches, key=lambda m: (m[0], m[1]))[2] if matches else None


def _plain_words(text: str) -> str:
    """Upper-case words of the raw text (the normalizer keeps only the first few)."""
    return " ".join(re.sub(r"[^A-Z0-9]+", " ", normalize_text(text).replace("'", "")).split())


def lookup(text: str | None) -> Icon | None:
    """The icon for a merchant name, payee or description (None = use the fallback)."""
    key = merchant_key(text)
    if not key:
        return None
    # Matched against the normalized name, then the plain words of the whole text.
    key = f"{key} | {_plain_words(text or '')}"
    # The longest uploaded name contained in the key wins ("PANDA EXPRESS" also
    # covers "PANDA EXPRESS MILFORD").
    custom = [m for m in _custom if _has_words(key, m)]
    if custom:
        merchant = max(custom, key=len)
        icon_id, version = _custom[merchant]
        return Icon(
            f"/merchant-icons/{icon_id}?v={version}",
            merchant.title(),
            "#ffffff",
            True,
            icon_id,
            merchant,
        )
    brand = _brand(key)
    if brand is None:
        return None
    return Icon(f"/static/merchants/{brand.slug}.svg", brand.title, brand.background)


def for_transaction(tx: Transaction) -> Icon | None:
    return lookup(transaction_merchant(tx))


def transaction_info(tx: Transaction) -> dict[str, object]:
    """For the transaction pop-up: the merchant name to attach an upload to (the
    existing upload's name, else the normalized payee) and the current icon."""
    icon = for_transaction(tx)
    name = icon.merchant if icon and icon.custom else merchant_key(transaction_merchant(tx))
    return {"merchant": name, "icon": icon.as_dict() if icon else None}


def for_bill(bill: Bill) -> Icon | None:
    return lookup(bill.merchant_name or bill.name)


def sniff(data: bytes) -> str | None:
    """The image type from the file's first bytes (not the browser's claim)."""
    for magic, content_type in IMAGE_TYPES.items():
        if data.startswith(magic):
            return content_type
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _index(icons: list[MerchantIcon]) -> None:
    _custom.clear()
    for icon in icons:
        _custom[icon.merchant] = (str(icon.id), int(icon.updated_at.timestamp()))


async def load_custom(provider: DataProvider, user_id: UUID) -> None:
    """(Re)read the uploaded logos (startup, and after every upload or removal)."""
    _index(await provider.merchant_icons.list_icons(user_id))
