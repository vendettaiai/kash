"""SimpleFIN Bridge bank synchronization provider implementation."""

import asyncio
import base64
import logging
import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

import httpx

from kash.config import get_settings
from kash.domain.models import (
    NO_DESCRIPTION,
    AccountType,
    ExternalAccount,
    ExternalAccountSet,
    ExternalTransaction,
)
from kash.services.bank.exceptions import (
    BankAuthenticationError,
    BankInvalidTokenError,
    BankNetworkError,
    BankRateLimitError,
)

logger = logging.getLogger(__name__)

# Sent as the payee of recent transactions until the real one is known
PAYEE_PLACEHOLDER = "(Enter transaction description here.)"

# Waits before retrying when SimpleFIN can't be reached at all (DNS lookup or
# connection failed, e.g. a container's resolver hiccup). Such attempts never
# reach SimpleFIN, so they don't use its daily request quota.
CONNECT_RETRY_DELAYS: tuple[float, ...] = (2.0, 5.0, 15.0)

# SimpleFIN can take a while to answer, most of all at busy times (e.g. on the
# hour, when many apps sync). Connecting still fails fast.
CONNECT_TIMEOUT_SECONDS = 15.0
READ_TIMEOUT_SECONDS = 120.0
# One more try after a timeout. The first request may have reached SimpleFIN and
# counted against its daily quota, so this is done once, and only after a pause.
TIMEOUT_RETRY_DELAY = 30.0

# Rolling request tracker for rate limiting: key -> list of UTC timestamps
_REQUEST_HISTORY: dict[str, list[datetime]] = defaultdict(list)


def _check_and_record_rate_limit(key: str, max_requests_per_day: int) -> None:
    """Check if request is within rolling 24-hour rate limit and record timestamp."""
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=24)

    # Prune timestamps older than 24 hours
    _REQUEST_HISTORY[key] = [t for t in _REQUEST_HISTORY[key] if t > cutoff]

    if len(_REQUEST_HISTORY[key]) >= max_requests_per_day:
        oldest = _REQUEST_HISTORY[key][0]
        retry_after = int((oldest + timedelta(hours=24) - now).total_seconds())
        count = len(_REQUEST_HISTORY[key])
        raise BankRateLimitError(
            f"SimpleFIN rate limit reached ({count}/{max_requests_per_day} reqs/24h). "
            f"Try again in {max(0, retry_after // 60)} minutes.",
            provider_name="simplefin",
            retry_after_seconds=max(0, retry_after),
        )

    _REQUEST_HISTORY[key].append(now)


def reset_rate_limit_tracker() -> None:
    """Clear in-memory rate limit tracker (primarily for test isolation)."""
    _REQUEST_HISTORY.clear()


async def _get_with_connect_retries(
    client: httpx.AsyncClient,
    url: str,
    params: dict[str, Any],
    auth: tuple[str, str] | None,
) -> httpx.Response:
    """GET, retrying only failures to connect (nothing reached the server yet).

    Timeouts after the request was sent are not retried, so a request is never
    counted twice by SimpleFIN.
    """
    for attempt, delay in enumerate((*CONNECT_RETRY_DELAYS, None), start=1):
        try:
            return await client.get(url, params=params, auth=auth)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            if delay is None:
                raise
            logger.warning(
                "SimpleFIN unreachable (%s); retry %d/%d in %.0fs",
                exc,
                attempt,
                len(CONNECT_RETRY_DELAYS),
                delay,
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")


def _describe(exc: httpx.RequestError) -> str:
    """A readable reason; some httpx errors (timeouts) have an empty message."""
    if isinstance(exc, httpx.ConnectTimeout):
        return f"timed out connecting (after {CONNECT_TIMEOUT_SECONDS:.0f}s)"
    if isinstance(exc, httpx.TimeoutException):
        return f"timed out waiting for SimpleFIN to answer (after {READ_TIMEOUT_SECONDS:.0f}s)"
    return str(exc) or type(exc).__name__


def _error_messages(data: dict[str, Any]) -> list[str]:
    """Problems reported in an Account Set: v1 ``errors`` (strings) and v2 ``errlist``
    (``{code, msg, conn_id, account_id}``)."""
    messages = [str(e) for e in data.get("errors") or [] if e]
    for item in data.get("errlist") or []:
        if isinstance(item, dict):
            text = str(item.get("msg") or item.get("code") or "").strip()
            code = str(item.get("code") or "").strip()
            if code and code not in text:
                text = f"{text} ({code})" if text else code
        else:
            text = str(item)
        if text and text not in messages:
            messages.append(text)
    return messages


class SimpleFINProvider:
    """Production SimpleFIN Bridge provider adhering to the SimpleFIN protocol v2."""

    def __init__(self, client: httpx.AsyncClient | None = None):
        self._client = client

    @property
    def provider_name(self) -> str:
        return "simplefin"

    async def exchange_token(self, setup_token: str) -> tuple[str, str]:
        """Decode setup token and claim SimpleFIN access URL.

        Returns (institution_name, access_url).
        """
        token_str = setup_token.strip()

        # Case 1: Direct Access URL provided (already claimed)
        if token_str.startswith(("http://", "https://")) and "@" in token_str:
            split = urlsplit(token_str)
            institution_name = f"SimpleFIN ({split.hostname or 'Bridge'})"
            return institution_name, token_str

        # Case 2: Direct Claim URL provided
        if token_str.startswith(("http://", "https://")):
            claim_url = token_str
        else:
            # Case 3: Base64 decode setup token to extract claim URL
            try:
                # Handle potential missing base64 padding
                padded_token = token_str + "=" * (-len(token_str) % 4)
                claim_url = base64.b64decode(padded_token).decode("utf-8").strip()
            except Exception as e:
                raise BankInvalidTokenError(
                    "Failed to decode SimpleFIN setup token: invalid base64 string.",
                    provider_name=self.provider_name,
                ) from e

        if not claim_url.startswith(("http://", "https://")):
            raise BankInvalidTokenError(
                "Decoded SimpleFIN token is not a valid claim URL.",
                provider_name=self.provider_name,
            )

        # Step 2: POST to claim URL to receive Access URL
        client = self._client or httpx.AsyncClient(timeout=15.0)
        close_client = self._client is None
        try:
            resp = await client.post(claim_url)
            if resp.status_code in (401, 403):
                raise BankAuthenticationError(
                    "SimpleFIN token claim rejected: token already used, expired, or invalid.",
                    provider_name=self.provider_name,
                )
            if resp.status_code >= 400:
                raise BankInvalidTokenError(
                    f"SimpleFIN token claim failed with HTTP {resp.status_code}: {resp.text}",
                    provider_name=self.provider_name,
                )

            access_url = resp.text.strip()
            if not access_url.startswith(("http://", "https://")):
                raise BankInvalidTokenError(
                    "Invalid Access URL returned by SimpleFIN claim endpoint.",
                    provider_name=self.provider_name,
                )

            # Extract default institution from host
            split = urlsplit(access_url)
            institution_name = "SimpleFIN Financial"
            if split.hostname:
                institution_name = f"SimpleFIN ({split.hostname})"

            return institution_name, access_url
        except httpx.RequestError as e:
            raise BankNetworkError(
                f"Network error connecting to SimpleFIN claim URL: {e}",
                provider_name=self.provider_name,
            ) from e
        finally:
            if close_client:
                await client.aclose()

    def _infer_account_type(self, name: str) -> AccountType:
        """Heuristically determine AccountType from account label."""
        name_lower = name.lower()
        if any(term in name_lower for term in ("saving", "hysa", "share")):
            return AccountType.SAVINGS
        if any(term in name_lower for term in ("credit", "card", "visa", "mastercard", "amex")):
            return AccountType.CREDIT_CARD
        if any(
            term in name_lower
            for term in (
                "invest",
                "ira",
                "401k",
                "brokerage",
                "stock",
                "individual",
                "fidelity",
                "schwab",
                "vanguard",
                "etrade",
                "robinhood",
                "core account",
                "money market",
                "spaxx",
                "roth",
                "portfolio",
            )
        ):
            return AccountType.INVESTMENT
        if any(term in name_lower for term in ("loan", "mortgage", "auto", "student")):
            return AccountType.LOAN
        return AccountType.CHECKING

    def _extract_mask(self, name: str) -> str | None:
        """Extract trailing account digits (mask) from account name if present."""
        match = re.search(r"(\d{4})\b", name)
        return match.group(1) if match else None

    async def _request_simplefin_accounts(
        self,
        access_url: str,
        since: datetime | None = None,
        until: datetime | None = None,
        balances_only: bool = False,
        account_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Make authenticated GET request to SimpleFIN Bridge accounts endpoint.

        ``balances_only`` asks for accounts and balances without transactions;
        ``account_ids`` limits the answer to those accounts."""
        settings = get_settings()

        # Rate limiting check
        split_url = urlsplit(access_url)
        rate_key = split_url.netloc or access_url
        _check_and_record_rate_limit(rate_key, settings.simplefin_max_requests_per_day)

        # Build endpoint URL: access_url/accounts?version=2
        base = access_url.rstrip("/")
        if not base.endswith("/accounts"):
            endpoint = f"{base}/accounts"
        else:
            endpoint = base

        params: dict[str, Any] = {"version": "2"}
        if balances_only:
            params["balances-only"] = "1"
        else:
            params["pending"] = "1"
            # Handle 90-day transaction query window
            now = datetime.now(UTC)
            default_since = now - timedelta(days=90)
            query_since = since if since is not None else default_since
            params["start-date"] = str(int(query_since.timestamp()))
            if until is not None:
                params["end-date"] = str(int(until.timestamp()))
        if account_ids:
            params["account"] = list(account_ids)

        # Extract basic auth credentials if embedded in access URL
        auth = None
        clean_endpoint = endpoint
        if split_url.username and split_url.password:
            auth = (split_url.username, split_url.password)
            host = split_url.hostname or ""
            if split_url.port:
                host = f"{host}:{split_url.port}"
            clean_endpoint = split_url._replace(netloc=host).geturl().rstrip("/")
            if not clean_endpoint.endswith("/accounts"):
                clean_endpoint = f"{clean_endpoint}/accounts"

        client = self._client or httpx.AsyncClient(
            timeout=httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
        )
        close_client = self._client is None
        try:
            # SimpleFIN access URL contains HTTP Basic Auth credentials
            try:
                resp = await _get_with_connect_retries(client, clean_endpoint, params, auth)
            except httpx.TimeoutException as exc:
                if isinstance(exc, httpx.ConnectTimeout):
                    raise  # already retried; never reached SimpleFIN
                logger.warning(
                    "SimpleFIN %s; trying once more in %.0fs",
                    _describe(exc),
                    TIMEOUT_RETRY_DELAY,
                )
                await asyncio.sleep(TIMEOUT_RETRY_DELAY)
                _check_and_record_rate_limit(rate_key, settings.simplefin_max_requests_per_day)
                resp = await _get_with_connect_retries(client, clean_endpoint, params, auth)

            if resp.status_code in (401, 403):
                raise BankAuthenticationError(
                    "SimpleFIN authentication failed. Credentials may have expired.",
                    provider_name=self.provider_name,
                )
            if resp.status_code == 429:
                raise BankRateLimitError(
                    "SimpleFIN Bridge rate limit exceeded (HTTP 429).",
                    provider_name=self.provider_name,
                )
            if resp.status_code >= 400:
                raise BankNetworkError(
                    f"SimpleFIN request failed with HTTP {resp.status_code}: {resp.text}",
                    provider_name=self.provider_name,
                )

            data = resp.json()
            if not isinstance(data, dict):
                raise BankNetworkError(
                    "Invalid JSON response structure from SimpleFIN.",
                    provider_name=self.provider_name,
                )

            # Problems SimpleFIN reports in the body fail the request only when no
            # account came back; otherwise they are warnings next to usable data.
            messages = _error_messages(data)
            if messages and not data.get("accounts"):
                error_str = ", ".join(messages)
                if any("auth" in msg.lower() for msg in messages):
                    raise BankAuthenticationError(
                        f"SimpleFIN authentication error: {error_str}",
                        provider_name=self.provider_name,
                    )
                raise BankNetworkError(
                    f"SimpleFIN reported provider error: {error_str}",
                    provider_name=self.provider_name,
                )

            return data
        except httpx.RequestError as e:
            raise BankNetworkError(
                f"Network communication failure with SimpleFIN bridge: {_describe(e)}",
                provider_name=self.provider_name,
            ) from e
        finally:
            if close_client:
                await client.aclose()

    def _parse_accounts(self, data: dict[str, Any]) -> list[ExternalAccount]:
        """Accounts and balances from an Account Set (protocol v1 or v2)."""
        # v2 names the bank on a connection (accounts carry its conn_id); v1 had an "org"
        connections = {
            str(c.get("conn_id")): c
            for c in data.get("connections") or []
            if isinstance(c, dict) and c.get("conn_id")
        }
        accounts: list[ExternalAccount] = []
        for raw_acc in data.get("accounts", []):
            ext_id = str(raw_acc.get("id", ""))
            name = str(raw_acc.get("name", "Unknown Account"))
            currency = str(raw_acc.get("currency", "USD"))

            balance_str = str(raw_acc.get("balance", "0.0000"))
            try:
                current_balance = Decimal(balance_str)
            except Exception:
                current_balance = Decimal("0.0000")

            avail_str = raw_acc.get("available-balance")
            available_balance = None
            if avail_str is not None:
                try:
                    available_balance = Decimal(str(avail_str))
                except Exception:
                    available_balance = None

            balance_as_of = None
            balance_epoch = raw_acc.get("balance-date")
            if isinstance(balance_epoch, int | float) and balance_epoch > 0:
                try:
                    balance_as_of = datetime.fromtimestamp(balance_epoch, tz=UTC)
                except (OverflowError, OSError, ValueError):
                    balance_as_of = None

            org_name = institution_id = None
            conn = connections.get(str(raw_acc.get("conn_id")))
            if conn:
                org_name = conn.get("name")
                institution_id = conn.get("org_id") or conn.get("org_url")
            elif isinstance(raw_acc.get("org"), dict):
                org = raw_acc["org"]
                org_name = org.get("name")
                institution_id = org.get("id") or org.get("domain") or org.get("sfin-url")

            accounts.append(
                ExternalAccount(
                    external_id=ext_id,
                    name=name,
                    account_type=self._infer_account_type(name),
                    currency=currency,
                    current_balance=current_balance,
                    available_balance=available_balance,
                    mask=self._extract_mask(name),
                    institution_name=org_name or "SimpleFIN",
                    institution_id=str(institution_id) if institution_id else None,
                    balance_as_of=balance_as_of,
                )
            )
        return accounts

    def _parse_transactions(self, data: dict[str, Any]) -> tuple[list[ExternalTransaction], int]:
        """Transactions of every account in an Account Set, and how many were unreadable
        (no id or no valid amount) and left out rather than saved wrong."""
        transactions: list[ExternalTransaction] = []
        rejected = 0
        for raw_acc in data.get("accounts", []):
            acc_id = str(raw_acc.get("id", ""))
            raw_txs = raw_acc.get("transactions") or []

            for raw_tx in raw_txs:
                tx_id = str(raw_tx.get("id") or "").strip()
                try:
                    amount = Decimal(str(raw_tx["amount"]))
                    if not amount.is_finite():
                        raise ValueError("not a number")
                except Exception:
                    amount = None
                if not tx_id or amount is None:
                    rejected += 1
                    continue

                transacted_epoch = raw_tx.get("transacted_at")
                transacted_at = None
                if transacted_epoch:
                    try:
                        transacted_at = datetime.fromtimestamp(transacted_epoch, tz=UTC)
                    except Exception:
                        transacted_at = None

                # Pending transactions have no posted date yet (SimpleFIN sends 0);
                # date them by when they happened, or today.
                posted_epoch = raw_tx.get("posted") or 0
                posted_at = None
                if posted_epoch > 0:
                    try:
                        posted_at = datetime.fromtimestamp(posted_epoch, tz=UTC)
                    except Exception:
                        posted_at = None
                if posted_at is None:
                    posted_at = transacted_at or datetime.now(UTC)

                payee = str(raw_tx.get("payee", "")).strip() or None
                if payee == PAYEE_PLACEHOLDER:
                    payee = None
                memo = str(raw_tx.get("memo", "")).strip() or None
                # Some banks' feeds leave the description empty and fill in only the payee
                description = (
                    str(raw_tx.get("description", "")).strip() or payee or memo or NO_DESCRIPTION
                )
                is_pending = bool(raw_tx.get("pending", False))

                transactions.append(
                    ExternalTransaction(
                        external_id=tx_id,
                        account_external_id=acc_id,
                        posted_at=posted_at,
                        transacted_at=transacted_at,
                        amount=amount,
                        description=description,
                        payee=payee,
                        memo=memo,
                        is_pending=is_pending,
                    )
                )

        return transactions, rejected

    async def fetch_account_set(
        self,
        credentials: str,
        since: datetime | None = None,
        account_ids: list[str] | None = None,
    ) -> ExternalAccountSet:
        """Accounts, balances and transactions (posted on/after ``since``, plus pending)
        in one request, so balances and transactions always come from the same answer
        and a sync uses one request of SimpleFIN's daily allowance."""
        data = await self._request_simplefin_accounts(
            credentials, since=since, account_ids=account_ids
        )
        transactions, rejected = self._parse_transactions(data)
        return ExternalAccountSet(
            accounts=self._parse_accounts(data),
            transactions=transactions,
            warnings=_error_messages(data),
            rejected_transactions=rejected,
        )

    async def fetch_accounts(self, credentials: str) -> list[ExternalAccount]:
        """Fetch normalized accounts from SimpleFIN Bridge (balances only, no transactions)."""
        data = await self._request_simplefin_accounts(credentials, balances_only=True)
        return self._parse_accounts(data)

    async def fetch_transactions(
        self,
        credentials: str,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> list[ExternalTransaction]:
        """Fetch normalized transactions from SimpleFIN Bridge across all linked accounts."""
        data = await self._request_simplefin_accounts(credentials, since=since, until=until)
        return self._parse_transactions(data)[0]

    async def test_connection(self, credentials: str) -> bool:
        """Validate connection by fetching accounts."""
        try:
            await self.fetch_accounts(credentials)
            return True
        except Exception:
            return False
