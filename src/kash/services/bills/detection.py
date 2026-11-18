"""Find recurring bills in transaction history (explainable heuristics, ADR-004).

Pipeline:
1. Normalize merchants and group outgoing charges by their first merchant word.
2. For each group (and, if the whole group is not regular, for each band of
   similar amounts inside it) look at the gaps between charges.
3. Map the typical gap to a frequency and score how regular the gaps are, how
   stable the amount is, how many charges there are, and how recent the last is.
4. Anything scoring at least ``MIN_CONFIDENCE`` becomes a candidate with a
   plain-English list of reasons.

Pure functions only: no database access, so it is easy to test with fixtures.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from statistics import mean, median, pstdev
from uuid import UUID

from kash.domain.models import BillFrequency, Transaction
from kash.services import formatting
from kash.services.bills.merchant import (
    common_prefix,
    display_name,
    merchant_tokens,
    transaction_merchant,
)
from kash.services.dates import PERIOD_DAYS, next_due

MIN_CONFIDENCE = 75

# (lowest, highest) typical gap in days for each frequency we detect
_GAP_RANGES: list[tuple[BillFrequency, float, float]] = [
    (BillFrequency.WEEKLY, 5, 9),
    (BillFrequency.BIWEEKLY, 12, 17),
    (BillFrequency.MONTHLY, 26, 35),
    (BillFrequency.QUARTERLY, 80, 100),
    (BillFrequency.ANNUAL, 330, 400),
]
# How far a single gap may be from the typical gap and still count as "on schedule"
_TOLERANCE_DAYS = {
    BillFrequency.WEEKLY: 1,
    BillFrequency.BIWEEKLY: 2,
    BillFrequency.MONTHLY: 4,
    BillFrequency.QUARTERLY: 10,
    BillFrequency.ANNUAL: 15,
}
_MIN_CHARGES = {
    BillFrequency.WEEKLY: 4,
    BillFrequency.BIWEEKLY: 3,
    BillFrequency.MONTHLY: 3,
    BillFrequency.QUARTERLY: 3,
    BillFrequency.ANNUAL: 2,
}
_LABEL = {
    BillFrequency.WEEKLY: "weekly",
    BillFrequency.BIWEEKLY: "every 2 weeks",
    BillFrequency.MONTHLY: "monthly",
    BillFrequency.QUARTERLY: "quarterly",
    BillFrequency.ANNUAL: "yearly",
}
VARIABLE_CV = 0.05  # amounts varying more than 5% make a "variable" bill (e.g. utilities)
AMOUNT_BAND = 0.15  # charges within 15% of each other can be the same subscription


@dataclass
class DetectedBill:
    """A recurring charge found in history, with the evidence behind it."""

    merchant_key: str
    name: str
    frequency: BillFrequency
    average_amount: Decimal
    last_amount: Decimal
    is_variable: bool
    confidence: int
    next_date: date
    transaction_ids: list[UUID]
    category_id: UUID | None
    account_id: UUID | None
    reasons: list[str] = field(default_factory=list)


def _frequency_for_gap(gap: float) -> BillFrequency | None:
    for freq, low, high in _GAP_RANGES:
        if low <= gap <= high:
            return freq
    return None


def _amount_score(cv: float) -> float:
    if cv <= 0.02:
        return 1.0
    if cv <= VARIABLE_CV:
        return 0.85
    if cv <= 0.15:
        return 0.6
    if cv <= 0.30:
        return 0.4
    return 0.1


def _money(value: Decimal | float) -> str:
    return formatting.money(Decimal(str(value)))


def _one_per_day(txs: list[Transaction]) -> list[Transaction]:
    by_day: dict[date, Transaction] = {}
    for tx in sorted(txs, key=lambda t: (t.date, t.created_at)):
        by_day.setdefault(tx.date, tx)
    return list(by_day.values())


def evaluate_series(
    txs: list[Transaction], tokens: list[list[str]], today: date
) -> DetectedBill | None:
    """Score one group of charges from the same merchant. None if it isn't a bill."""
    series = _one_per_day(txs)
    if len(series) < 2:
        return None
    dates = [t.date for t in series]
    gaps = [(b - a).days for a, b in zip(dates, dates[1:], strict=False)]
    frequency = _frequency_for_gap(median(gaps))
    if frequency is None or len(series) < _MIN_CHARGES[frequency]:
        return None

    period = PERIOD_DAYS[frequency]
    tol = _TOLERANCE_DAYS[frequency]
    on_schedule = sum(1 for g in gaps if abs(g - period) <= tol)
    regularity = on_schedule / len(gaps)

    amounts = [float(abs(t.amount)) for t in series]
    avg = mean(amounts)
    cv = pstdev(amounts) / avg if avg else 1.0
    is_variable = cv > VARIABLE_CV
    if is_variable and frequency in (BillFrequency.WEEKLY, BillFrequency.BIWEEKLY):
        return None  # frequent charges of varying size are shopping habits, not bills

    since_last = (today - dates[-1]).days
    if since_last > 2.5 * period + tol:
        return None  # stopped: probably cancelled
    recency = 1.0 if since_last <= 1.5 * period + tol else 0.4

    minimum = _MIN_CHARGES[frequency]
    count = min(1.0, 0.6 + 0.4 * (len(series) - minimum) / 3)

    confidence = round(
        100 * (0.45 * regularity + 0.30 * _amount_score(cv) + 0.15 * count + 0.10 * recency)
    )

    anchor = dates[-1].day
    upcoming = next_due(dates[-1], frequency, anchor)
    while upcoming < today:
        upcoming = next_due(upcoming, frequency, anchor)

    key_tokens = common_prefix(tokens) or tokens[0][:1]
    key = " ".join(key_tokens)
    last = series[-1]
    category = Counter(t.category_id for t in series if t.category_id).most_common(1)
    account = Counter(t.account_id for t in series).most_common(1)

    amount_reason = (
        f"Amount varies {_money(min(amounts))}–{_money(max(amounts))} (average {_money(avg)})"
        if is_variable
        else f"Same amount each time ({_money(amounts[-1])})"
        if cv == 0
        else f"About the same amount each time ({_money(min(amounts))}–{_money(max(amounts))})"
    )
    reasons = [
        f"{len(series)} charges from {formatting.fulldate(dates[0])} to "
        f"{formatting.fulldate(dates[-1])}",
        f"Charged {_LABEL[frequency]}: {on_schedule} of {len(gaps)} gaps are "
        f"{period:.0f} days ± {tol}",
        amount_reason,
        f"Last charged {formatting.day(dates[-1])} ({since_last} days ago)",
    ]

    return DetectedBill(
        merchant_key=key,
        name=display_name(key),
        frequency=frequency,
        average_amount=Decimal(str(round(avg, 2))),
        last_amount=abs(last.amount),
        is_variable=is_variable,
        confidence=confidence,
        next_date=upcoming,
        transaction_ids=[t.id for t in series],
        category_id=category[0][0] if category else None,
        account_id=account[0][0] if account else None,
        reasons=reasons,
    )


def _amount_bands(txs: list[Transaction]) -> list[list[Transaction]]:
    """Split charges into groups of similar amounts (e.g. two plans at one merchant)."""
    bands: list[list[Transaction]] = []
    for tx in sorted(txs, key=lambda t: abs(t.amount)):
        amount = float(abs(tx.amount))
        if bands:
            base = float(abs(bands[-1][0].amount))
            if amount <= base * (1 + AMOUNT_BAND) + 1:
                bands[-1].append(tx)
                continue
        bands.append([tx])
    return bands


def detect_recurring(
    transactions: list[Transaction], today: date | None = None
) -> list[DetectedBill]:
    """All recurring outgoing charges scoring at least ``MIN_CONFIDENCE``, best first.

    ``transactions`` should already be limited to real spending (no transfers,
    ignored, or pending transactions); credits are skipped here.
    """
    today = today or date.today()
    groups: dict[str, list[tuple[Transaction, list[str]]]] = {}
    for tx in transactions:
        if tx.amount >= 0:
            continue
        tokens = merchant_tokens(transaction_merchant(tx))
        if tokens:
            groups.setdefault(tokens[0], []).append((tx, tokens))

    found: list[DetectedBill] = []
    for members in groups.values():
        txs = [tx for tx, _ in members]
        token_map = {tx.id: toks for tx, toks in members}
        whole = evaluate_series(txs, [token_map[t.id] for t in txs], today)
        if whole and whole.confidence >= MIN_CONFIDENCE:
            found.append(whole)
            continue
        for band in _amount_bands(txs):
            hit = evaluate_series(band, [token_map[t.id] for t in band], today)
            if hit and hit.confidence >= MIN_CONFIDENCE:
                found.append(hit)

    # Two bands of the same merchant would share a key; tell them apart by amount.
    keys = Counter(d.merchant_key for d in found)
    for d in found:
        if keys[d.merchant_key] > 1:
            d.name = f"{d.name} ({_money(d.average_amount)})"
    return sorted(found, key=lambda d: (-d.confidence, d.name))
