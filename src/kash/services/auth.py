"""Passwords, session cookies, invite links and login throttling (ADR-016, ADR-019).

- Passwords are hashed with Argon2id (``argon2-cffi`` defaults).
- The session cookie is ``<user id>.<expiry>.<signature>``, signed with HMAC-SHA256
  using ``SECRET_KEY`` and a fingerprint of the password hash, so changing the
  password (or the secret) logs every session out.
- Invite links (join, or set a new password) carry a random token; only its
  SHA-256 is stored, and it expires after ``INVITE_DAYS``.
- Failed logins are limited per client address, in memory.
"""

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

MIN_PASSWORD_LENGTH = 10
UNSET_PASSWORD = "!"  # seeded owner row, or an invited person who hasn't joined yet
INVITE_DAYS = 7

_hasher = PasswordHasher()


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(hashed: str, password: str) -> bool:
    if not has_account(hashed):
        return False
    try:
        return _hasher.verify(hashed, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def has_account(hashed: str | None) -> bool:
    """Whether a password is set (the owner: else first-run setup; others: else not joined).

    Only a real Argon2 hash counts: older installs seeded placeholder values
    (e.g. "!") that can never be logged in with.
    """
    return hashed is not None and hashed.startswith("$argon2")


def new_invite(now: datetime | None = None) -> tuple[str, str, datetime]:
    """A fresh link token: (token for the URL, hash to store, expiry)."""
    token = secrets.token_urlsafe(32)
    expires = (now or datetime.now(UTC)) + timedelta(days=INVITE_DAYS)
    return token, invite_hash(token), expires


def invite_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def invite_expired(expires_at: datetime | None, now: datetime | None = None) -> bool:
    if expires_at is None:
        return True
    if expires_at.tzinfo is None:  # SQLite returns naive datetimes; they are stored as UTC
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= (now or datetime.now(UTC))


def _signature(secret: str, user_id: UUID, expires: int, password_hash: str) -> str:
    key = hashlib.sha256(f"{secret}|{password_hash}".encode()).digest()
    return hmac.new(key, f"{user_id}.{expires}".encode(), hashlib.sha256).hexdigest()


def make_session(
    secret: str, user_id: UUID, password_hash: str, lifetime: int, now: float | None = None
) -> str:
    expires = int((time.time() if now is None else now) + lifetime)
    return f"{user_id}.{expires}.{_signature(secret, user_id, expires, password_hash)}"


def session_user(token: str | None, now: float | None = None) -> tuple[UUID, int, str] | None:
    """Parse a cookie into (user id, expiry, signature) if it is well-formed and unexpired."""
    try:
        raw_id, raw_expires, signature = (token or "").split(".")
        user_id, expires = UUID(raw_id), int(raw_expires)
    except ValueError:
        return None
    if expires < (time.time() if now is None else now):
        return None
    return user_id, expires, signature


def session_valid(
    secret: str, token: str | None, password_hash: str, now: float | None = None
) -> UUID | None:
    """The user id in a valid, unexpired session cookie, else None."""
    parsed = session_user(token, now)
    if parsed is None or not has_account(password_hash):
        return None
    user_id, expires, signature = parsed
    expected = _signature(secret, user_id, expires, password_hash)
    return user_id if hmac.compare_digest(signature, expected) else None


class LoginThrottle:
    """At most ``limit`` failed logins per client in ``window`` seconds."""

    def __init__(self, limit: int = 5, window: float = 15 * 60):
        self.limit, self.window = limit, window
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _recent(self, client: str, now: float) -> deque[float]:
        failures = self._failures[client]
        while failures and failures[0] <= now - self.window:
            failures.popleft()
        return failures

    def blocked(self, client: str, now: float | None = None) -> bool:
        return len(self._recent(client, time.time() if now is None else now)) >= self.limit

    def failed(self, client: str, now: float | None = None) -> None:
        at = time.time() if now is None else now
        self._recent(client, at).append(at)

    def succeeded(self, client: str) -> None:
        self._failures.pop(client, None)


throttle = LoginThrottle()
