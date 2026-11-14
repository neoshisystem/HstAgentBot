"""Offline W10.1 registration, identity and disable-preservation test."""
from __future__ import annotations
import asyncio
import base64
import logging
import sqlite3
import pytest
import config
from bot_registry_service import (
    BotRegistryConflict, BotRegistryPermissionError, BotVerificationError,
    activate_bot, disable_bot, prepare_bot_disable, verify_bot_registration,
)
from crypto_store import decrypt_secret, encrypt_secret


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE representatives(id INTEGER PRIMARY KEY,is_active INTEGER,deleted_at INTEGER);
        CREATE TABLE bot_registry(
            bot_id INTEGER PRIMARY KEY,rep_id INTEGER REFERENCES representatives(id),
            is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0,1)),
            token_cipher TEXT,username TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK(status IN ('pending','active','disabled')),
            verified_at INTEGER,revision INTEGER NOT NULL DEFAULT 1
        );
        CREATE UNIQUE INDEX ux_primary_bot ON bot_registry(is_primary) WHERE is_primary=1;
        CREATE UNIQUE INDEX ux_active_rep_bot ON bot_registry(rep_id)
            WHERE rep_id IS NOT NULL AND status='active';
        CREATE TABLE action_confirmations(
            nonce TEXT PRIMARY KEY,actor_tg_id INTEGER NOT NULL,bot_id INTEGER NOT NULL,
            action TEXT NOT NULL,target_type TEXT NOT NULL,target_id TEXT NOT NULL,
            expected_revision INTEGER NOT NULL,payload_json TEXT NOT NULL,
            expires_at INTEGER NOT NULL,consumed_at INTEGER,operation_id TEXT
        );
        CREATE TABLE payment_orders(order_id TEXT PRIMARY KEY,bot_id INTEGER,status TEXT);
        CREATE TABLE bot_customers(bot_id INTEGER,user_tg_id INTEGER,started_at INTEGER,
            PRIMARY KEY(bot_id,user_tg_id));
    """)
    conn.execute("INSERT INTO representatives VALUES(202,1,NULL)")
    conn.commit()
    return conn


def test_getme_mismatch_and_duplicate(monkeypatch, caplog):
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 777, raising=False)
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="))
    conn = _db()
    caplog.set_level(logging.DEBUG)
    calls = []

    def get_me(token):
        assert not conn.in_transaction
        calls.append(token)
        return {"id": 9001, "username": "primary_bot"}

    with pytest.raises(BotRegistryPermissionError):
        asyncio.run(verify_bot_registration(
            conn, actor_tg_id=202, bot_id=9001, token="V52_PRIMARY_TOKEN",
            get_me=get_me, now=100, is_primary=True))
    assert calls == []

    with pytest.raises(BotVerificationError):
        asyncio.run(verify_bot_registration(
            conn, actor_tg_id=777, bot_id=9002, token="V52_WRONG_ID_TOKEN",
            get_me=get_me, now=100, is_primary=False, rep_id=202))
    assert conn.execute("SELECT COUNT(*) FROM bot_registry").fetchone()[0] == 0

    preview = asyncio.run(verify_bot_registration(
        conn, actor_tg_id=777, bot_id=9001, token="V52_PRIMARY_TOKEN",
        get_me=get_me, now=101, is_primary=True))
    assert preview["status"] == "pending" and preview["username"] == "primary_bot"
    assert "V52_PRIMARY_TOKEN" not in repr(preview)
    stored = conn.execute(
        "SELECT token_cipher,status,rep_id,is_primary FROM bot_registry WHERE bot_id=9001"
    ).fetchone()
    assert decrypt_secret(stored["token_cipher"], "hstagent/columns/v1") == "V52_PRIMARY_TOKEN"
    assert tuple(stored[1:]) == ("pending", None, 1)

    nonce = preview["confirmation"]["nonce"]
    assert activate_bot(conn, actor_tg_id=777, bot_id=9001, nonce=nonce, now=102)["replayed"] is False
    assert activate_bot(conn, actor_tg_id=777, bot_id=9001, nonce=nonce, now=103)["replayed"] is True
    assert conn.execute("SELECT status,revision FROM bot_registry WHERE bot_id=9001").fetchone()[:] == ("active",2)

    # Historical duplicate credential is still treated as occupied.
    conn.execute(
        "INSERT INTO bot_registry VALUES(8999,202,0,?,'duplicate','disabled',NULL,2)",
        (encrypt_secret("V52_PRIMARY_TOKEN", "hstagent/columns/v1"),))
    conn.commit()
    with pytest.raises(BotRegistryConflict):
        asyncio.run(verify_bot_registration(
            conn, actor_tg_id=777, bot_id=9001, token="V52_PRIMARY_TOKEN",
            get_me=get_me, now=104, is_primary=False, rep_id=202))

    rep_preview = asyncio.run(verify_bot_registration(
        conn, actor_tg_id=777, bot_id=9002, token="V52_REP_TOKEN",
        get_me=lambda token: {"id":9002,"username":"rep_bot"},
        now=105, is_primary=False, rep_id=202))
    activate_bot(conn, actor_tg_id=777, bot_id=9002,
                 nonce=rep_preview["confirmation"]["nonce"], now=106)
    second_preview = asyncio.run(verify_bot_registration(
        conn, actor_tg_id=777, bot_id=9003, token="V52_REP_TOKEN_2",
        get_me=lambda token: {"id":9003,"username":"rep_bot_2"},
        now=107, is_primary=False, rep_id=202))
    with pytest.raises(BotRegistryConflict):
        activate_bot(conn, actor_tg_id=777, bot_id=9003,
                     nonce=second_preview["confirmation"]["nonce"], now=108)
    assert conn.execute("SELECT status FROM bot_registry WHERE bot_id=9003").fetchone()[0] == "pending"

    conn.execute("INSERT INTO bot_customers VALUES(9002,101,100)")
    conn.execute("INSERT INTO payment_orders VALUES('pending-1',9002,'awaiting_payment')")
    orders_before = [tuple(r) for r in conn.execute(
        "SELECT order_id,bot_id,status FROM payment_orders WHERE bot_id=9002")]
    customers_before = [tuple(r) for r in conn.execute(
        "SELECT bot_id,user_tg_id,started_at FROM bot_customers WHERE bot_id=9002")]
    disable_preview = prepare_bot_disable(conn, actor_tg_id=777, bot_id=9002, now=109)
    assert disable_preview["pending_orders"] == 1
    assert disable_preview["linked_customers"] == 1
    disabled = disable_bot(conn, actor_tg_id=777, bot_id=9002,
        nonce=disable_preview["confirmation"]["nonce"], now=110)
    assert disabled["status"] == "disabled"
    assert [tuple(r) for r in conn.execute(
        "SELECT order_id,bot_id,status FROM payment_orders WHERE bot_id=9002")] == orders_before
    assert [tuple(r) for r in conn.execute(
        "SELECT bot_id,user_tg_id,started_at FROM bot_customers WHERE bot_id=9002")] == customers_before
    assert "V52_PRIMARY_TOKEN" not in caplog.text and "V52_REP_TOKEN" not in caplog.text
    conn.close()
