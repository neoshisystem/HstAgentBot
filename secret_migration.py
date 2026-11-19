"""Restartable, fail-closed migration of legacy plaintext secrets."""
from __future__ import annotations

import json
import os
import re
import sqlite3
from typing import Any

import audit_service
import crypto_store


COLUMNS_DOMAIN = "hstagent/columns/v1"
KNOWN_SENSITIVE_SETTINGS = frozenset(
    {
        "bot_token",
        "telegram_bot_token",
        "api_token",
        "api_key",
        "xui_api_key",
        "xui_token",
        "xui_password",
        "panel_password",
        "secret_key",
        "private_key",
        "client_secret",
        "webhook_secret",
        "backup_encryption_key",
    }
)
_ENVELOPE = re.compile(r"^v1\.[0-9a-f]{16}\.")


class SecretMigrationError(RuntimeError):
    """Safe migration failure that never includes the affected secret."""


def _db_path(db_path: str | None) -> str:
    if db_path is not None:
        return os.fspath(db_path)
    import database

    return os.fspath(database.DB_PATH)


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def _sensitive_setting(key: str) -> bool:
    return str(key).strip().casefold() in KNOWN_SENSITIVE_SETTINGS


def _is_sensitive_payload(value: Any) -> bool:
    return audit_service.redact_sensitive(value) != value


def _audit_metadata(row: sqlite3.Row, parsed: Any) -> str:
    metadata = {
        "action": str(row["action"] or ""),
        "result": str(row["result_status"] or ""),
    }
    if isinstance(parsed, dict):
        for key in ("resource", "actor_persona"):
            if key in parsed:
                metadata[key] = audit_service.redact_sensitive(parsed[key])
    return json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)


def _ensure_schema(conn: sqlite3.Connection) -> None:
    if _table_exists(conn, "accounts") and "sub_id_cipher" not in _columns(conn, "accounts"):
        conn.execute("ALTER TABLE accounts ADD COLUMN sub_id_cipher TEXT")
    if _table_exists(conn, "audit_logs") and "details_cipher" not in _columns(conn, "audit_logs"):
        conn.execute("ALTER TABLE audit_logs ADD COLUMN details_cipher TEXT")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS bank_cards ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "card_number_cipher TEXT NOT NULL,"
        "card_holder TEXT,"
        "bank_name TEXT,"
        "legacy_source TEXT UNIQUE,"
        "created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
    )


def migrate_legacy_secrets(db_path: str | None = None, batch_size: int = 100) -> dict:
    """Encrypt legacy account IDs, bank cards, audit details and known settings.

    The key is validated before opening or mutating the database. All data
    movement occurs in one SQLite transaction; reruns skip existing envelopes.
    """
    try:
        crypto_store.load_master_key()
    except Exception:
        raise SecretMigrationError("encryption key is missing or invalid; no data changed") from None

    path = _db_path(db_path)
    batch_size = max(1, int(batch_size))
    counts = {"accounts": 0, "bank_cards": 0, "audit_logs": 0, "settings": 0}
    conn = None
    try:
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("BEGIN IMMEDIATE")
        _ensure_schema(conn)

        if _table_exists(conn, "accounts"):
            columns = _columns(conn, "accounts")
            if {"id", "sub_id", "sub_id_cipher"}.issubset(columns):
                rows = conn.execute(
                    "SELECT id, sub_id, sub_id_cipher FROM accounts "
                    "WHERE sub_id IS NOT NULL AND sub_id<>'' ORDER BY id"
                ).fetchmany(batch_size)
                for row in rows:
                    if row["sub_id_cipher"]:
                        conn.execute("UPDATE accounts SET sub_id=NULL WHERE id=?", (row["id"],))
                        continue
                    cipher = crypto_store.encrypt_secret(str(row["sub_id"]), COLUMNS_DOMAIN)
                    conn.execute(
                        "UPDATE accounts SET sub_id_cipher=?,sub_id=NULL "
                        "WHERE id=? AND sub_id_cipher IS NULL",
                        (cipher, row["id"]),
                    )
                    counts["accounts"] += 1

        if _table_exists(conn, "card_settings"):
            card_columns = _columns(conn, "card_settings")
            if {"id", "card_number"}.issubset(card_columns):
                rows = conn.execute(
                    "SELECT * FROM card_settings ORDER BY id"
                ).fetchmany(batch_size)
                for row in rows:
                    card_number = str(row["card_number"] or "")
                    if not card_number or card_number == "[REDACTED]":
                        continue
                    source = f"card_settings:{row['id']}"
                    cipher = crypto_store.encrypt_secret(card_number, COLUMNS_DOMAIN)
                    conn.execute(
                        "INSERT OR IGNORE INTO bank_cards"
                        "(card_number_cipher,card_holder,bank_name,legacy_source) VALUES(?,?,?,?)",
                        (
                            cipher,
                            row["card_holder"] if "card_holder" in card_columns else None,
                            row["bank_name"] if "bank_name" in card_columns else None,
                            source,
                        ),
                    )
                    conn.execute(
                        "UPDATE card_settings SET card_number='[REDACTED]' WHERE id=?",
                        (row["id"],),
                    )
                    counts["bank_cards"] += 1

        if _table_exists(conn, "audit_logs"):
            audit_columns = _columns(conn, "audit_logs")
            if {"id", "details_json", "details_cipher", "action", "result_status"}.issubset(audit_columns):
                rows = conn.execute(
                    "SELECT id,action,result_status,details_json,details_cipher "
                    "FROM audit_logs WHERE details_json IS NOT NULL ORDER BY id"
                ).fetchmany(batch_size)
                for row in rows:
                    raw = str(row["details_json"])
                    if row["details_cipher"]:
                        continue
                    try:
                        parsed = json.loads(raw)
                    except (ValueError, TypeError):
                        parsed = raw
                    if not _is_sensitive_payload(parsed):
                        continue
                    cipher = crypto_store.encrypt_secret(raw, COLUMNS_DOMAIN)
                    safe = _audit_metadata(row, parsed)
                    conn.execute(
                        "UPDATE audit_logs SET details_cipher=?,details_json=? WHERE id=?",
                        (cipher, safe, row["id"]),
                    )
                    counts["audit_logs"] += 1

        if _table_exists(conn, "system_settings"):
            setting_columns = _columns(conn, "system_settings")
            if {"key", "value"}.issubset(setting_columns):
                rows = conn.execute(
                    "SELECT key,value FROM system_settings ORDER BY key"
                ).fetchmany(batch_size)
                for row in rows:
                    key = str(row["key"] or "")
                    value = str(row["value"] or "")
                    if not value or not _sensitive_setting(key) or _ENVELOPE.match(value):
                        continue
                    cipher = crypto_store.encrypt_secret(value, COLUMNS_DOMAIN)
                    conn.execute(
                        "UPDATE system_settings SET value=? WHERE key=?",
                        (cipher, key),
                    )
                    counts["settings"] += 1

        conn.commit()
        # Remove recoverable plaintext from WAL and SQLite free pages only
        # after the encrypted replacements are committed.
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return counts
    except SecretMigrationError:
        if conn is not None:
            conn.rollback()
        raise
    except Exception:
        if conn is not None:
            conn.rollback()
        raise SecretMigrationError("legacy secret migration failed; rerun after inspection") from None
    finally:
        if conn is not None:
            conn.close()


def read_account_sub_id(account_id: int, db_path: str | None = None) -> str | None:
    path = _db_path(db_path)
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT sub_id,sub_id_cipher FROM accounts WHERE id=?", (int(account_id),)
        ).fetchone()
    if not row:
        return None
    if row[1]:
        return crypto_store.decrypt_secret(row[1], COLUMNS_DOMAIN)
    return row[0]


def read_bank_card(card_id: int, db_path: str | None = None) -> dict | None:
    path = _db_path(db_path)
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM bank_cards WHERE id=?", (int(card_id),)).fetchone()
    if not row:
        return None
    return {
        "id": row["id"],
        "card_number": crypto_store.decrypt_secret(row["card_number_cipher"], COLUMNS_DOMAIN),
        "card_holder": row["card_holder"],
        "bank_name": row["bank_name"],
    }


def read_system_setting(key: str, db_path: str | None = None) -> str | None:
    path = _db_path(db_path)
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key=?", (str(key),)
        ).fetchone()
    if not row:
        return None
    value = row[0]
    if _sensitive_setting(key) and isinstance(value, str) and _ENVELOPE.match(value):
        return crypto_store.decrypt_secret(value, COLUMNS_DOMAIN)
    return value
