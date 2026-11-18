"""Cryptographic security utilities for encrypting credentials at rest."""

import base64
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from kash.config import get_settings


def _derive_aes_key(key: str | bytes | None = None) -> bytes:
    """Derive a 256-bit (32-byte) key for AES-GCM authenticated encryption."""
    if key is not None:
        if isinstance(key, str):
            key_bytes = key.encode("utf-8")
        else:
            key_bytes = key
        return hashlib.sha256(key_bytes).digest()

    settings = get_settings()
    if settings.data_encryption_key:
        return hashlib.sha256(settings.data_encryption_key.encode("utf-8")).digest()

    # Deterministic fallback derived from application secret key
    secret = (settings.secret_key + "_kash_cred_enc_salt_v1").encode("utf-8")
    return hashlib.sha256(secret).digest()


def encrypt_credentials(plaintext: str, key: str | bytes | None = None) -> str:
    """Encrypt bank credentials or sensitive tokens using AES-GCM (256-bit).

    Returns a URL-safe Base64 encoded string containing the 12-byte nonce
    and the ciphertext with authentication tag.
    """
    if not plaintext:
        return ""

    key_bytes = _derive_aes_key(key)
    aesgcm = AESGCM(key_bytes)
    nonce = os.urandom(12)  # Standard 96-bit nonce for AES-GCM

    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")


def decrypt_credentials(ciphertext_b64: str, key: str | bytes | None = None) -> str:
    """Decrypt AES-GCM encrypted credentials.

    Validates the 128-bit authentication tag to ensure authenticity and integrity.
    Raises ValueError on tampering or invalid key.
    """
    if not ciphertext_b64:
        return ""

    try:
        raw_payload = base64.urlsafe_b64decode(ciphertext_b64.encode("ascii"))
    except Exception as e:
        raise ValueError("Invalid base64 encoding in encrypted credentials") from e

    # Minimum length: 12 bytes nonce + 16 bytes tag = 28 bytes
    if len(raw_payload) < 28:
        raise ValueError("Encrypted credentials payload is too short")

    nonce = raw_payload[:12]
    ciphertext_with_tag = raw_payload[12:]

    key_bytes = _derive_aes_key(key)
    aesgcm = AESGCM(key_bytes)

    try:
        decrypted_bytes = aesgcm.decrypt(nonce, ciphertext_with_tag, None)
        return decrypted_bytes.decode("utf-8")
    except InvalidTag as e:
        raise ValueError(
            "Failed to decrypt credentials: authentication tag mismatch or corrupted data"
        ) from e
