"""Versioned and domain-separated secret encryption for HstAgentBot."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import re
from typing import Final

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

_ENV_KEY: Final[str] = "BACKUP_ENCRYPTION_KEY"
_HKDF_SALT: Final[bytes] = b"hstagent/crypto-store/v1"
_ALLOWED_LABELS: Final[frozenset[str]] = frozenset(
    {"hstagent/columns/v1", "hstagent/archive/v1"}
)
_ENVELOPE_VERSION: Final[str] = "v1"
_KEY_ENCODING_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]{43}={0,1}$")
_FINGERPRINT_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{16}$")


class CryptoStoreError(ValueError):
    """Safe error for invalid key material, domain, or ciphertext."""


def load_master_key() -> bytes:
    """Load exactly 32 bytes from BACKUP_ENCRYPTION_KEY (base64url)."""
    encoded = os.environ.get(_ENV_KEY)
    if not isinstance(encoded, str) or not _KEY_ENCODING_RE.fullmatch(encoded):
        raise CryptoStoreError("encryption key is missing or invalid")
    try:
        raw = encoded.encode("ascii")
        padded = raw + b"=" * (-len(raw) % 4)
        key = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (UnicodeEncodeError, ValueError):
        raise CryptoStoreError("encryption key is missing or invalid") from None
    canonical = base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii")
    if len(key) != 32 or canonical != encoded.rstrip("="):
        raise CryptoStoreError("encryption key is missing or invalid")
    return key


def derive_key(master_key: bytes, label: str) -> bytes:
    """Derive a Fernet-compatible HKDF-SHA256 key for one approved label."""
    if not isinstance(master_key, bytes) or len(master_key) != 32:
        raise CryptoStoreError("master key is invalid")
    if not isinstance(label, str) or label not in _ALLOWED_LABELS:
        raise CryptoStoreError("encryption domain is not approved")
    derived = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_HKDF_SALT,
        info=label.encode("ascii"),
    ).derive(master_key)
    return base64.urlsafe_b64encode(derived)


def _key_fingerprint(master_key: bytes) -> str:
    # Digest prefix is a non-secret identifier; raw key material is never emitted.
    return hashlib.sha256(master_key).hexdigest()[:16]


def encrypt_secret(plaintext: str, label: str) -> str:
    """Encrypt text into a versioned envelope; never emit plaintext on failure."""
    if not isinstance(plaintext, str):
        raise CryptoStoreError("plaintext must be text")
    master_key = load_master_key()
    try:
        token = Fernet(derive_key(master_key, label)).encrypt(plaintext.encode("utf-8"))
    except (UnicodeError, ValueError):
        raise CryptoStoreError("secret encryption failed") from None
    return f"{_ENVELOPE_VERSION}.{_key_fingerprint(master_key)}.{token.decode('ascii')}"


def decrypt_secret(ciphertext: str, label: str) -> str:
    """Authenticate and decrypt a v1 envelope. Plaintext fallback is forbidden."""
    if not isinstance(ciphertext, str):
        raise CryptoStoreError("ciphertext is invalid")
    master_key = load_master_key()
    try:
        version, fingerprint, token = ciphertext.split(".", 2)
    except ValueError:
        raise CryptoStoreError("ciphertext is invalid") from None
    if (
        version != _ENVELOPE_VERSION
        or not _FINGERPRINT_RE.fullmatch(fingerprint)
        or not token
        or not hmac.compare_digest(fingerprint, _key_fingerprint(master_key))
    ):
        raise CryptoStoreError("ciphertext authentication failed")
    try:
        plaintext = Fernet(derive_key(master_key, label)).decrypt(token.encode("ascii"))
        return plaintext.decode("utf-8")
    except (InvalidToken, UnicodeError, ValueError):
        raise CryptoStoreError("ciphertext authentication failed") from None
