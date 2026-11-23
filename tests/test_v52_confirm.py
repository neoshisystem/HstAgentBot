"""Offline acceptance for one-use, actor/bot-bound confirmations."""
from __future__ import annotations
import re
import sqlite3
import pytest
from confirmation_service import (
    ConfirmationExpired, ConfirmationMismatch, ConfirmationStale,
    create_confirmation, consume_confirmation,
)

def _connection():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE action_confirmations (
            nonce TEXT PRIMARY KEY, actor_tg_id INTEGER NOT NULL, bot_id INTEGER NOT NULL,
            action TEXT NOT NULL, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
            expected_revision INTEGER NOT NULL, payload_json TEXT NOT NULL,
            expires_at INTEGER NOT NULL, consumed_at INTEGER, operation_id TEXT
        )
    """)
    return conn

def test_stale_cross_bot_replay():
    conn = _connection()
    conn.execute("BEGIN IMMEDIATE")
    created = create_confirmation(
        conn, actor_tg_id=101, bot_id=9001, action="panel.operation.prepare",
        target_type="account", target_id=303, expected_revision=7,
        payload={"account_id": 303, "quota_bytes": 4096, "revision": 7},
        now=100, ttl_seconds=300,
    )
    conn.commit()
    nonce = created["nonce"]
    assert re.fullmatch(r"[A-Za-z0-9_-]{24}", nonce)
    assert len(created["callback_ok"].encode("utf-8")) < 64
    assert len(created["callback_no"].encode("utf-8")) < 64
    assert created["callback_ok"] == f"v52:ok:{nonce}"
    assert created["callback_no"] == f"v52:no:{nonce}"

    with pytest.raises(ConfirmationMismatch):
        conn.execute("BEGIN IMMEDIATE")
        try:
            consume_confirmation(
                conn, nonce=nonce, actor_tg_id=102, bot_id=9001,
                action="panel.operation.prepare", target_type="account",
                target_id=303, expected_revision=7, now=101,
            )
        finally:
            conn.rollback()
    with pytest.raises(ConfirmationMismatch):
        conn.execute("BEGIN IMMEDIATE")
        try:
            consume_confirmation(
                conn, nonce=nonce, actor_tg_id=101, bot_id=9002,
                action="panel.operation.prepare", target_type="account",
                target_id=303, expected_revision=7, now=101,
            )
        finally:
            conn.rollback()
    with pytest.raises(ConfirmationExpired):
        conn.execute("BEGIN IMMEDIATE")
        try:
            consume_confirmation(
                conn, nonce=nonce, actor_tg_id=101, bot_id=9001,
                action="panel.operation.prepare", target_type="account",
                target_id=303, expected_revision=7, now=400,
            )
        finally:
            conn.rollback()
    with pytest.raises(ConfirmationStale):
        conn.execute("BEGIN IMMEDIATE")
        try:
            consume_confirmation(
                conn, nonce=nonce, actor_tg_id=101, bot_id=9001,
                action="panel.operation.prepare", target_type="account",
                target_id=303, expected_revision=8, now=101,
            )
        finally:
            conn.rollback()

    conn.execute("BEGIN IMMEDIATE")
    first = consume_confirmation(
        conn, nonce=nonce, actor_tg_id=101, bot_id=9001,
        action="panel.operation.prepare", target_type="account",
        target_id=303, expected_revision=7, now=101, operation_id="operation-w09-1",
    )
    conn.commit()
    assert first["replayed"] is False
    assert first["operation_id"] == "operation-w09-1"
    conn.execute("BEGIN IMMEDIATE")
    replay = consume_confirmation(
        conn, nonce=nonce, actor_tg_id=101, bot_id=9001,
        action="panel.operation.prepare", target_type="account",
        target_id=303, expected_revision=7, now=900,
        operation_id="must-not-replace-original",
    )
    conn.commit()
    assert replay["replayed"] is True
    assert replay["operation_id"] == first["operation_id"]
    assert tuple(conn.execute(
        "SELECT consumed_at,operation_id FROM action_confirmations WHERE nonce=?", (nonce,)
    ).fetchone()) == (101, "operation-w09-1")

    with pytest.raises(ValueError):
        conn.execute("BEGIN IMMEDIATE")
        try:
            create_confirmation(
                conn, actor_tg_id=101, bot_id=9001, action="renew",
                target_type="account", target_id=303, expected_revision=7,
                payload={"api_token": "must-not-be-stored"}, now=100,
            )
        finally:
            conn.rollback()
    conn.close()
