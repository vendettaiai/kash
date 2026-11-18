"""Send Web Push messages: the household's keys, encryption and delivery.

Messages are encrypted for the device (RFC 8291) and signed with the household's key
(VAPID, RFC 8292), so the push service in between (Apple, Google, Mozilla) sees neither
the text nor who it is for. One httpx client and no vendor SDK, like the AI (ADR-018).
The keys are made here the first time they're needed, so there's nothing to configure.
"""

import asyncio
import base64
import ipaddress
import json
import logging
import time
from dataclasses import dataclass
from urllib.parse import urlsplit
from uuid import UUID

import http_ece  # type: ignore[import-untyped]
import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid02  # type: ignore[import-untyped]

from kash.domain.models import PushSubscription
from kash.domain.protocols import DataProvider
from kash.owner import OWNER_USER_ID
from kash.services.security import decrypt_credentials, encrypt_credentials

logger = logging.getLogger(__name__)

# Sent with every message instead of an email address, so no personal data reaches the
# push service. Strict py_vapid wants a bare host or mailto: here, hence "no-strict".
SUBJECT = "https://github.com/Fahmula/kash"
TOKEN_LIFETIME_SECONDS = 12 * 3600
TIMEOUT_SECONDS = 10
MAX_CONCURRENT = 4
MAX_ENDPOINT_LENGTH = 2048
TTL_TEST = 60


class KeysUnreadable(Exception):
    """The saved private key can't be decrypted (the encryption key changed)."""


@dataclass(frozen=True)
class Keys:
    public: str  # what the browser subscribes with, base64url
    private_pem: str


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    url: str = "/"  # where a tap opens: a path inside Kash
    tag: str = ""  # a newer message with the same tag replaces the older one
    ttl: int = TTL_TEST  # seconds the push service may hold it for a phone that's off


@dataclass
class DeliveryResult:
    accepted: int = 0  # devices the push service took it for
    removed: int = 0  # devices that are gone, and were deleted
    failed: int = 0  # might work next time


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _generate() -> tuple[str, str]:
    vapid = Vapid02()
    vapid.generate_keys()
    public = vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return _b64encode(public), vapid.private_pem().decode("ascii")


async def get_keys(provider: DataProvider) -> Keys:
    """The household's keys, made once. Raises KeysUnreadable rather than replacing them:
    a mistyped SECRET_KEY shouldn't cost every device its subscription."""
    stored = await provider.settings.get_vapid_keys(OWNER_USER_ID)
    if stored is None:
        public, private_pem = _generate()
        stored = await provider.settings.claim_vapid_keys(
            OWNER_USER_ID, public, encrypt_credentials(private_pem)
        )
    try:
        return Keys(public=stored[0], private_pem=decrypt_credentials(stored[1]))
    except ValueError as exc:
        raise KeysUnreadable from exc


async def reset_keys(provider: DataProvider) -> None:
    """New keys for everyone. Devices must turn notifications on again."""
    await provider.notifications.delete_all_subscriptions()
    await provider.settings.clear_vapid_keys(OWNER_USER_ID)


def valid_subscription(endpoint: str, p256dh: str, auth: str) -> bool:
    """What a browser sent is what the server will POST to, so keep it to public HTTPS
    hosts (no redirects either; see deliver) and well-formed keys."""
    if len(endpoint) > MAX_ENDPOINT_LENGTH:
        return False
    try:
        parts = urlsplit(endpoint)
        host = parts.hostname or ""
    except ValueError:
        return False
    if parts.scheme != "https" or "." not in host or host.endswith((".local", ".localhost")):
        return False
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return False
    try:
        return len(_b64decode(p256dh)) == 65 and len(_b64decode(auth)) == 16
    except ValueError:
        return False


def _authorization(keys: Keys, endpoint: str) -> str:
    parts = urlsplit(endpoint)
    vapid = Vapid02.from_pem(keys.private_pem.encode("ascii"))
    vapid.conf["no-strict"] = True
    claims = {
        "aud": f"{parts.scheme}://{parts.netloc}",
        "sub": SUBJECT,
        "exp": int(time.time()) + TOKEN_LIFETIME_SECONDS,
    }
    return str(vapid.sign(claims)["Authorization"])


def _encrypt(subscription: PushSubscription, message: Message) -> bytes:
    payload = {
        "title": message.title,
        "body": message.body,
        "url": message.url,
        "tag": message.tag,
    }
    return bytes(
        http_ece.encrypt(
            json.dumps(payload).encode("utf-8"),
            private_key=ec.generate_private_key(ec.SECP256R1()),
            dh=_b64decode(subscription.p256dh),
            auth_secret=_b64decode(subscription.auth),
            version="aes128gcm",
        )
    )


async def _send(
    http: httpx.AsyncClient, keys: Keys, subscription: PushSubscription, message: Message
) -> int | None:
    """The push service's status, or None if it couldn't be reached. Logs hosts only: an
    endpoint is a secret address for that device."""
    host = urlsplit(subscription.endpoint).hostname
    try:
        response = await http.post(
            subscription.endpoint,
            content=_encrypt(subscription, message),
            headers={
                "Authorization": _authorization(keys, subscription.endpoint),
                "Content-Encoding": "aes128gcm",
                "Content-Type": "application/octet-stream",
                "TTL": str(message.ttl),
                "Urgency": "normal",
            },
        )
    except Exception as exc:  # the network, or a device's keys that won't encrypt
        logger.warning("push send to %s failed: %s", host, type(exc).__name__)
        return None
    if response.status_code >= 300 and response.status_code not in (404, 410):
        logger.warning("push send to %s refused: %d", host, response.status_code)
    return response.status_code


async def deliver(
    provider: DataProvider,
    keys: Keys,
    subscriptions: list[PushSubscription],
    message: Message,
    http: httpx.AsyncClient | None = None,
) -> DeliveryResult:
    """Send to each device. Never raises: a push service that's down must not stop a
    check. Devices the service no longer knows are removed."""
    result = DeliveryResult()
    limit = asyncio.Semaphore(MAX_CONCURRENT)
    client = http or httpx.AsyncClient(timeout=TIMEOUT_SECONDS, follow_redirects=False)

    async def send_one(subscription: PushSubscription) -> None:
        async with limit:
            status = await _send(client, keys, subscription, message)
        if status is not None and 200 <= status < 300:
            result.accepted += 1
        elif status in (404, 410):
            await provider.notifications.delete_endpoint(subscription.endpoint)
            result.removed += 1
        else:
            result.failed += 1

    try:
        await asyncio.gather(*(send_one(s) for s in subscriptions))
    finally:
        if http is None:
            await client.aclose()
    return result


async def send_test(
    provider: DataProvider, keys: Keys, user_id: UUID, http: httpx.AsyncClient | None = None
) -> DeliveryResult:
    """A sample notification to every device of this person."""
    message = Message(
        title="Kash",
        body="Notifications are working on this device.",
        url="/settings?section=notifications",
        tag="test",
    )
    subscriptions = await provider.notifications.list_subscriptions(user_id)
    return await deliver(provider, keys, subscriptions, message, http)
