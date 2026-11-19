"""Offline durable-outbox and private-routing acceptance for W09.6."""
from __future__ import annotations

import base64
import sqlite3

import pytest

import config
from crypto_store import decrypt_secret
from outbox_service import OutboxError, claim_delivery, enqueue_delivery


def test_duplicate_enqueue_and_private_routing(monkeypatch):
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 777, raising=False)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE bot_registry(
            bot_id INTEGER PRIMARY KEY,rep_id INTEGER,is_primary INTEGER,
            token_cipher TEXT,username TEXT,status TEXT,verified_at INTEGER,revision INTEGER
        );
        CREATE TABLE delivery_outbox(
            id INTEGER PRIMARY KEY AUTOINCREMENT,event_key TEXT NOT NULL,
            bot_id INTEGER NOT NULL,chat_id INTEGER NOT NULL,thread_id INTEGER,
            purpose TEXT NOT NULL,resource_type TEXT NOT NULL,resource_id TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt_at INTEGER NOT NULL DEFAULT 0,lease_token TEXT,lease_until INTEGER,
            last_error_code TEXT,message_id INTEGER,
            UNIQUE(event_key,bot_id,chat_id,purpose)
        );
    """)
    conn.executemany(
        "INSERT INTO bot_registry VALUES(?,?,?,?,?,?,?,?)",
        [
            (9001, None, 1, None, "primary", "active", 10, 1),
            (9002, 202, 0, None, "representative", "active", 10, 1),
        ],
    )

    first = enqueue_delivery(
        conn, event_key="account:303:updated", bot_id=9002, chat_id=101,
        thread_id=55, purpose="account_notice", resource_type="account",
        resource_id="303",
    )
    replay = enqueue_delivery(
        conn, event_key="account:303:updated", bot_id=9002, chat_id=101,
        thread_id=55, purpose="account_notice", resource_type="account",
        resource_id="303",
    )
    assert first["delivery_id"] == replay["delivery_id"]
    assert conn.execute(
        "SELECT COUNT(*) FROM delivery_outbox WHERE event_key='account:303:updated'"
    ).fetchone()[0] == 1
    stored_reference = conn.execute(
        "SELECT resource_id FROM delivery_outbox WHERE id=?", (first["delivery_id"],)
    ).fetchone()[0]
    assert decrypt_secret(stored_reference, "hstagent/columns/v1") == "303"
    with pytest.raises(OutboxError):
        enqueue_delivery(
            conn, event_key="account:303:updated", bot_id=9002, chat_id=101,
            thread_id=55, purpose="account_notice", resource_type="account",
            resource_id="different-account",
        )

    abuse = enqueue_delivery(
        conn, event_key="abuse:case-9", bot_id=9002, chat_id=-100000004444,
        thread_id=88, purpose="abusereport", resource_type="abuse_case",
        resource_id="case-9",
    )
    routed = conn.execute(
        "SELECT bot_id,chat_id,thread_id,purpose,resource_type,resource_id "
        "FROM delivery_outbox WHERE id=?", (abuse["delivery_id"],)
    ).fetchone()
    assert tuple(routed[:5]) == (9001, 777, None, "abusereport", "abuse_case")
    assert decrypt_secret(str(routed[5]), "hstagent/columns/v1") == "case-9"

    claimed = claim_delivery(conn, delivery_id=first["delivery_id"], now=100, lease_seconds=20)
    assert claimed is not None and claimed["state"] == "sending"
    assert claimed["attempts"] == 1 and claimed["lease_token"]
    # A second worker cannot own the same unexpired lease.
    assert claim_delivery(conn, delivery_id=first["delivery_id"], now=101) is None
    assert conn.execute(
        "SELECT state,attempts FROM delivery_outbox WHERE id=?", (first["delivery_id"],)
    ).fetchone()[:] == ("sending", 1)

    disabled = enqueue_delivery(
        conn, event_key="disabled:notice", bot_id=9002, chat_id=101,
        thread_id=None, purpose="account_notice", resource_type="account",
        resource_id="303",
    )
    conn.execute("UPDATE bot_registry SET status='disabled' WHERE bot_id=9002")
    assert claim_delivery(conn, delivery_id=disabled["delivery_id"], now=200) is None
    assert conn.execute(
        "SELECT state,attempts FROM delivery_outbox WHERE id=?", (disabled["delivery_id"],)
    ).fetchone()[:] == ("pending", 0)

    # No payload, credential or customer secret is stored in the outbox schema.
    columns = {row[1] for row in conn.execute("PRAGMA table_info(delivery_outbox)")}
    assert not ({"payload", "secret", "token", "password", "ciphertext"} & columns)
    conn.close()
