"""AI settings beyond the basics (Settings → AI categorization → Advanced).

Stored as JSON in ``user_settings.ai_options``; anything missing or invalid
falls back to the default, so an empty ``{}`` means "all defaults". The same
JSON also remembers which rule proposals the user dismissed.
"""

from dataclasses import dataclass, field
from typing import Any

from kash.services.ai.prompt import is_default

DEFAULT_THRESHOLD = 0.90  # apply automatically when at least this sure
THRESHOLDS = (0.95, 0.90, 0.85, 0.80, 0.70, 0.60)

DEFAULT_TEMPERATURE = 0.0
DEFAULT_BATCH_SIZE = 25  # merchants per request
BATCH_SIZES = (5, 10, 25, 50)  # smaller for local models with little context
MAX_TOKENS_RANGE = (256, 32_000)
TOKENS_PER_ITEM = 80  # automatic output limit: this much per merchant, plus room
MAX_PROMPT_CHARS = 8_000


@dataclass(frozen=True)
class AIOptions:
    system_prompt: str | None = None  # None = the built-in prompt (prompt.DEFAULT_INSTRUCTIONS)
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int | None = None  # None = automatic, sized to the request
    batch_size: int = DEFAULT_BATCH_SIZE
    send_examples: bool = True  # include "merchant → category" examples from history
    dismissed_proposals: tuple[str, ...] = field(default_factory=tuple)

    def output_tokens(self, items: int) -> int:
        return self.max_tokens or TOKENS_PER_ITEM * items + 200


def _float(value: Any, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if low <= number <= high else default


def from_stored(data: Any) -> AIOptions:
    """Options from ``user_settings.ai_options`` (bad or missing values → defaults)."""
    if not isinstance(data, dict):
        return AIOptions()
    prompt = data.get("system_prompt")
    tokens = data.get("max_tokens")
    low, high = MAX_TOKENS_RANGE
    dismissed = data.get("dismissed_proposals")
    if isinstance(prompt, str) and is_default(prompt):
        prompt = None  # a saved copy of the built-in prompt: keep following the current one
    return AIOptions(
        system_prompt=prompt.replace("\r\n", "\n").strip()[:MAX_PROMPT_CHARS] or None
        if isinstance(prompt, str)
        else None,
        temperature=_float(data.get("temperature"), DEFAULT_TEMPERATURE, 0.0, 1.0),
        max_tokens=tokens if isinstance(tokens, int) and low <= tokens <= high else None,
        batch_size=data["batch_size"]
        if data.get("batch_size") in BATCH_SIZES
        else DEFAULT_BATCH_SIZE,
        send_examples=data.get("send_examples", True) is not False,
        dismissed_proposals=tuple(str(d) for d in dismissed) if isinstance(dismissed, list) else (),
    )


def to_stored(options: AIOptions) -> dict[str, Any]:
    """Only what differs from the defaults (so improved defaults reach everyone)."""
    stored: dict[str, Any] = {}
    if options.system_prompt:
        stored["system_prompt"] = options.system_prompt
    if options.temperature != DEFAULT_TEMPERATURE:
        stored["temperature"] = options.temperature
    if options.max_tokens:
        stored["max_tokens"] = options.max_tokens
    if options.batch_size != DEFAULT_BATCH_SIZE:
        stored["batch_size"] = options.batch_size
    if not options.send_examples:
        stored["send_examples"] = False
    if options.dismissed_proposals:
        stored["dismissed_proposals"] = list(options.dismissed_proposals)
    return stored


def threshold(saved_threshold: float | None) -> float:
    """The auto-apply threshold in effect (older installs saved None for "never")."""
    return saved_threshold if saved_threshold in THRESHOLDS else DEFAULT_THRESHOLD
