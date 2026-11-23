"""Acceptance coverage for v1 Fernet envelopes and key separation."""
import base64

import pytest

import crypto_store
from crypto_store import CryptoStoreError


def test_cipher_tamper_separation_missing_key(monkeypatch):
    master_key = bytes(range(32))
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(master_key).decode("ascii").rstrip("="),
    )
    plaintext = "sensitive-column-value-محرمانه"

    columns_ciphertext = crypto_store.encrypt_secret(plaintext, "hstagent/columns/v1")
    archive_ciphertext = crypto_store.encrypt_secret(plaintext, "hstagent/archive/v1")

    assert columns_ciphertext.startswith("v1.")
    assert archive_ciphertext.startswith("v1.")
    assert plaintext not in columns_ciphertext
    assert plaintext not in archive_ciphertext
    assert columns_ciphertext != archive_ciphertext
    assert crypto_store.decrypt_secret(columns_ciphertext, "hstagent/columns/v1") == plaintext
    assert crypto_store.decrypt_secret(archive_ciphertext, "hstagent/archive/v1") == plaintext

    with pytest.raises(CryptoStoreError):
        crypto_store.decrypt_secret(columns_ciphertext, "hstagent/archive/v1")

    tampered = columns_ciphertext[:-1] + (
        "A" if columns_ciphertext[-1] != "A" else "B"
    )
    with pytest.raises(CryptoStoreError, match="authentication"):
        crypto_store.decrypt_secret(tampered, "hstagent/columns/v1")

    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY")
    with pytest.raises(CryptoStoreError, match="key"):
        crypto_store.load_master_key()
    with pytest.raises(CryptoStoreError):
        crypto_store.decrypt_secret(columns_ciphertext, "hstagent/columns/v1")

    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "not-a-32-byte-base64url-key")
    with pytest.raises(CryptoStoreError, match="key"):
        crypto_store.encrypt_secret(plaintext, "hstagent/columns/v1")
