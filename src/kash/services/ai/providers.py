"""AI providers: presets and one client for OpenAI-style chat APIs (docs/AI_PLAN.md).

OpenAI, Google Gemini (its OpenAI-compatible endpoint), Ollama and other
OpenAI-compatible servers all accept ``POST {base}/chat/completions`` and
``GET {base}/models``, so a single ``httpx`` client covers every preset.

The API key is only ever sent to the configured base URL (redirects are not
followed) and never appears in errors or logs.
"""

import json
import time
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any
from urllib.parse import urlsplit

import httpx

TIMEOUT_SECONDS = 20.0  # connection test, model list
CHAT_TIMEOUT_SECONDS = 90.0  # a categorization batch can take a while


@dataclass(frozen=True)
class Preset:
    label: str
    base_url: str  # default; editable only when ``custom_url``
    needs_key: bool
    custom_url: bool
    model_hint: str  # placeholder text, not a default: models change often


PRESETS: dict[str, Preset] = {
    "openai": Preset("OpenAI", "https://api.openai.com/v1", True, False, "e.g. gpt-4o-mini"),
    "gemini": Preset(
        "Google Gemini",
        "https://generativelanguage.googleapis.com/v1beta/openai",
        True,
        False,
        "e.g. gemini-2.0-flash",
    ),
    "openrouter": Preset(
        "OpenRouter", "https://openrouter.ai/api/v1", True, False, "e.g. openai/gpt-4o-mini"
    ),
    "ollama": Preset("Ollama (local)", "http://localhost:11434/v1", False, True, "e.g. llama3.1"),
    "openai_compatible": Preset("OpenAI-compatible", "", False, True, "model name"),
}


class AIProviderError(Exception):
    """A problem talking to the provider, worded for the Settings page."""


@dataclass(frozen=True)
class Connection:
    """What the client needs: where, which model, and the (decrypted) key."""

    provider: str
    base_url: str
    model: str
    api_key: str = ""

    @classmethod
    def build(cls, provider: str, base_url: str, model: str, api_key: str) -> "Connection":
        """Apply the preset's URL for fixed-URL providers; check the URL otherwise."""
        preset = PRESETS.get(provider)
        if preset is None:
            raise AIProviderError("Choose a provider.")
        url = (base_url.strip() if preset.custom_url else "") or preset.base_url
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise AIProviderError("Enter the provider's address, e.g. http://192.168.1.20:11434/v1")
        if preset.needs_key and not api_key.strip():
            raise AIProviderError(f"{preset.label} needs an API key.")
        return cls(provider, url.rstrip("/"), model.strip(), api_key.strip())


def is_plain_http_to_remote(url: str) -> bool:
    """True for ``http://`` to a public host (the key and data would travel unencrypted)."""
    parts = urlsplit(url)
    if parts.scheme != "http":
        return False
    host = parts.hostname or ""
    if host in ("localhost",) or host.endswith((".local", ".lan", ".internal")):
        return False
    try:
        address = ip_address(host)
    except ValueError:
        return "." in host  # a public-looking name like api.example.com
    return not (address.is_private or address.is_loopback or address.is_link_local)


def _headers(conn: Connection) -> dict[str, str]:
    return {"Authorization": f"Bearer {conn.api_key}"} if conn.api_key else {}


# Key prefixes that give away which service issued a key: (prefix, provider, label)
_KEY_PREFIXES = (("sk-or-", "openrouter", "an OpenRouter"), ("AIza", "gemini", "a Google Gemini"))


def key_mismatch_hint(conn: Connection) -> str:
    """ "This looks like an OpenRouter key…" when the key doesn't fit the chosen provider."""
    for prefix, provider, label in _KEY_PREFIXES:
        if conn.api_key.startswith(prefix) and conn.provider != provider:
            return (
                f" This looks like {label} key: choose {PRESETS[provider].label} as the provider."
            )
    return ""


def _reason(resp: httpx.Response, conn: Connection) -> str:
    """The provider's own error message (e.g. "User not found."), without the key."""
    try:
        body = resp.json()
        error = body.get("error") if isinstance(body, dict) else None
        text = error.get("message") if isinstance(error, dict) else error or body.get("message")
    except (ValueError, AttributeError):
        text = resp.text
    text = " ".join(str(text or "").split())
    if conn.api_key:
        text = text.replace(conn.api_key, "[key]")
    return text[:160] + ("…" if len(text) > 160 else "")


def _explain(exc: Exception, conn: Connection) -> AIProviderError:
    """Turn transport/HTTP failures into a message without the key or response secrets."""
    where = urlsplit(conn.base_url).netloc
    if isinstance(exc, httpx.TimeoutException):
        return AIProviderError(f"No answer from {where} in time. Try again later.")
    if isinstance(exc, httpx.ConnectError):
        return AIProviderError(
            f"Couldn't reach {where}. Is the address right and the server running?"
        )
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        reason = _reason(exc.response, conn)
        detail = f" ({code}: {reason})" if reason else f" (HTTP {code})"
        if code in (401, 403):
            hint = key_mismatch_hint(conn) or ("" if conn.api_key else " No API key was entered.")
            return AIProviderError(f"{where} rejected the API key{detail}.{hint}")
        if code == 404:
            return AIProviderError(
                f"Not found at {where}{detail}: check the address and model name."
            )
        if code == 429:
            return AIProviderError(
                f"{where} is rate-limiting requests or the quota is used up{detail}."
            )
        return AIProviderError(f"{where} answered with an error{detail}.")
    if isinstance(exc, httpx.HTTPError):
        return AIProviderError(f"Couldn't talk to {where}.")
    return AIProviderError("Unexpected reply from the provider.")


@dataclass
class ChatResult:
    data: dict[str, Any]
    prompt_tokens: int
    completion_tokens: int


class AIClient:
    def __init__(self, conn: Connection, http: httpx.AsyncClient | None = None):
        self.conn = conn
        self._http = http

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        own = self._http is None
        http = self._http or httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False)
        try:
            resp = await http.request(
                method, f"{self.conn.base_url}{path}", headers=_headers(self.conn), **kwargs
            )
            resp.raise_for_status()
            return resp
        except httpx.HTTPError as exc:
            raise _explain(exc, self.conn) from None
        finally:
            if own:
                await http.aclose()

    async def list_models(self) -> list[str]:
        resp = await self._request("GET", "/models")
        try:
            items = resp.json().get("data", [])
            return sorted({str(m["id"]).removeprefix("models/") for m in items if m.get("id")})
        except (ValueError, AttributeError, TypeError, KeyError):
            raise AIProviderError("The provider's model list wasn't understood.") from None

    async def chat_json(
        self, system: str, user: str, max_tokens: int = 1000, temperature: float = 0.0
    ) -> ChatResult:
        """One chat completion that must answer with a JSON object."""
        if not self.conn.model:
            raise AIProviderError("Choose a model.")
        body = {
            "model": self.conn.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        resp = await self._request(
            "POST", "/chat/completions", json=body, timeout=CHAT_TIMEOUT_SECONDS
        )
        try:
            payload = resp.json()
            content = payload["choices"][0]["message"]["content"]
            data = json.loads(content)
            usage = payload.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError):
            raise AIProviderError("The model didn't answer with JSON.") from None
        if not isinstance(data, dict):
            raise AIProviderError("The model didn't answer with a JSON object.")
        return ChatResult(
            data,
            int(usage.get("prompt_tokens") or 0),
            int(usage.get("completion_tokens") or 0),
        )


@dataclass
class ConnectionCheck:
    ok: bool
    message: str
    models: list[str]
    seconds: float = 0.0


async def check_connection(
    conn: Connection, http: httpx.AsyncClient | None = None
) -> ConnectionCheck:
    """List models, then ask the chosen model for a tiny JSON answer (no transaction data)."""
    client = AIClient(conn, http)
    started = time.monotonic()
    models: list[str] = []
    try:
        try:
            models = await client.list_models()
        except AIProviderError:
            if conn.provider != "openai_compatible":
                raise
            models = []  # some compatible servers have no model list; the chat test decides
        if not conn.model:
            return ConnectionCheck(True, "Connected. Now choose a model.", models)
        if models and conn.model not in models:
            return ConnectionCheck(
                False, f'Connected, but there is no model "{conn.model}".', models
            )
        result = await client.chat_json(
            "You are a health check. Answer only with JSON.", 'Reply with {"ok": true}', 20
        )
        if result.data.get("ok") is not True:
            return ConnectionCheck(
                False, "Connected, but the model's answer wasn't as expected.", models
            )
    except AIProviderError as exc:
        return ConnectionCheck(False, str(exc), models)
    return ConnectionCheck(True, "Connection works.", models, round(time.monotonic() - started, 1))
