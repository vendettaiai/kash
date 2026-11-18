"""Merchant normalization: turn messy bank descriptions into a stable key.

``NETFLIX.COM 866-579-7172 CA``, ``Netflix.com`` and
``POS PURCHASE NETFLIX.COM LOS GATOS`` should all be recognised as Netflix. The
result is a list of tokens; the first token groups charges, and the tokens all
charges in a group share become that merchant's key.
"""

import re
import unicodedata

from kash.domain.models import Transaction

# Bank/processor boilerplate in front of the merchant name
_PREFIXES = re.compile(
    r"^(?:"
    r"POINT OF SALE (?:WITHDRAWAL|PURCHASE|DEBIT)|POS (?:PURCHASE|DEBIT|WITHDRAWAL)|POS"
    r"|DEBIT CARD (?:PURCHASE|PAYMENT)|DEBIT CARD|CHECK ?CARD(?: \d+)?|VISA DDA PUR|DBT CRD \d+"
    r"|(?:PURCHASE )?AUTHORIZED ON \d+/\d+|PURCHASE(?: RETURN)?"
    r"|RECURRING (?:PAYMENT|DEBIT|CHARGE|PURCHASE)|PREAUTHORIZED (?:DEBIT|PAYMENT)"
    r"|ACH (?:DEBIT|WITHDRAWAL|PAYMENT)|ACH|ELECTRONIC (?:PAYMENT|WITHDRAWAL)"
    r"|EXTERNAL WITHDRAWAL|ONLINE (?:PAYMENT|PMT|TRANSFER)(?: TO)?|BILL ?PAY(?:MENT)?(?: TO)?"
    r"|PAYMENT TO|WITHDRAWAL|AUTOPAY"
    r"|SQ ?\*|TST ?\*|SP ?\*|PAYPAL ?\*|PP ?\*|IN ?\*|DD ?\*"
    r")[\s:/*#-]*"
)
# Everything after these markers is reference data (e.g. "NETFLIX DES:PAYMENT ID:123")
_TAIL_MARKERS = re.compile(r"\s(?:DES|ID|INDN|CO ID|PPD ID|WEB ID|TRACE|REF|CONF)\s*[:#].*$")
_TLD = re.compile(r"\.(?:COM|NET|ORG|CO|IO|TV|APP)\b")
_NOISE = {
    "INC", "LLC", "LTD", "CORP", "CO", "USA", "US", "PAYMENT", "PAYMT", "PYMT", "PMT",
    "PMTS", "AUTOPAY", "AUTO", "RECURRING", "ONLINE", "WEB", "WWW", "BILL", "BILLING",
    "PPD", "CCD", "ACH", "DEBIT", "PURCHASE", "MONTHLY", "SUBSCRIPTION", "THE",
}  # fmt: skip
_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE",
    "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD",
    "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
}  # fmt: skip
MAX_TOKENS = 3


def merchant_tokens(text: str) -> list[str]:
    """Normalized merchant words, most significant first (may be empty)."""
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = text.upper().strip()
    text = _TAIL_MARKERS.sub("", text)
    for _ in range(3):  # prefixes can stack: "POS PURCHASE SQ *COFFEE"
        stripped = _PREFIXES.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    text = _TLD.sub(" ", text)
    text = re.sub(r"(?<=[A-Z])[-'](?=[A-Z])", "", text)  # T-MOBILE -> TMOBILE
    raw = re.split(r"[^A-Z0-9&]+", text)
    tokens = [
        t for t in raw if t and not any(ch.isdigit() for ch in t) and len(t) > 1 and t not in _NOISE
    ]
    while len(tokens) > 1 and tokens[-1] in _STATES:
        tokens.pop()
    return tokens[:MAX_TOKENS]


def description_words(*texts: str | None) -> set[str]:
    """Every word in the given texts, uppercased, in any order (no prefix stripping,
    no cap): for "does this transaction mention these words anywhere?" checks."""
    words: set[str] = set()
    for text in texts:
        text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
        text = re.sub(r"(?<=[A-Z])[-'](?=[A-Z])", "", text.upper())  # T-MOBILE -> TMOBILE
        words.update(
            w
            for w in re.split(r"[^A-Z0-9&]+", text)
            if len(w) > 1 and not any(c.isdigit() for c in w)
        )
    return words


def normalize_merchant(text: str) -> str:
    """Merchant key as a string, e.g. ``"NETFLIX"`` (empty if nothing meaningful)."""
    return " ".join(merchant_tokens(text))


def transaction_merchant(tx: Transaction) -> str:
    """The best text to identify who was paid: payee if known, else the description."""
    return tx.merchant_name or tx.clean_description or tx.original_description


def common_prefix(token_lists: list[list[str]]) -> list[str]:
    """Longest run of leading tokens shared by every list."""
    if not token_lists:
        return []
    prefix = token_lists[0]
    for tokens in token_lists[1:]:
        n = 0
        while n < min(len(prefix), len(tokens)) and prefix[n] == tokens[n]:
            n += 1
        prefix = prefix[:n]
    return prefix


def same_merchant(key: str, other: str) -> bool:
    """True if two merchant keys refer to the same merchant (one extends the other)."""
    a, b = key.split(), other.split()
    if not a or not b:
        return False
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def display_name(key: str) -> str:
    """Readable merchant name from a key: ``"PLANET FITNESS"`` -> ``"Planet Fitness"``."""
    return " ".join(w if "&" in w else w.title() for w in key.split())
