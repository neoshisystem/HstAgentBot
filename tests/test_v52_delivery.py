"""Offline Telegram delivery, retry and no-refund acceptance for W09.7."""
from __future__ import annotations

import base64
import socket
import sqlite3
import urllib.request

import pytest

import config
from crypto_store import encrypt_secret
from outbox_service import enqueue_delivery
from outbox_worker import StaleDeliveryLease, deliver_one


def _db(monkeypatch):
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
        CREATE TABLE account_access_grants(
            account_id INTEGER,grantee_tg_id INTEGER,revoked_at INTEGER
        );
        CREATE TABLE panel_operations(operation_id TEXT PRIMARY KEY,state TEXT);
        CREATE TABLE payment_orders(order_id TEXT PRIMARY KEY,status TEXT);
        CREATE TABLE financial_ledger(id INTEGER PRIMARY KEY,business_key TEXT);
        CREATE TABLE refund_requests(refund_id TEXT PRIMARY KEY,state TEXT);
    """)
    conn.execute(
        "INSERT INTO bot_registry VALUES(9001,NULL,1,?,'primary','active',10,1)",
        (encrypt_secret("synthetic-telegram-token", "hstagent/columns/v1"),),
    )
    conn.execute(
        "INSERT INTO bot_registry VALUES(9002,202,0,?,'rep','disabled',10,1)",
        (encrypt_secret("synthetic-rep-token", "hstagent/columns/v1"),),
    )
    return conn


def _enqueue(conn, event, *, bot=9001, chat=101, thread=None, kind="notice", resource="r1"):
    return enqueue_delivery(
        conn, event_key=event, bot_id=bot, chat_id=chat, thread_id=thread,
        purpose=kind, resource_type="account" if kind == "private" else "notice",
        resource_id=resource,
    )["delivery_id"]


def test_retry_after_no_refund(monkeypatch):
    conn = _db(monkeypatch)
    network_attempts = []
    def blocked(*args, **kwargs):
        network_attempts.append("network")
        raise AssertionError("network access is forbidden in this test")
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(urllib.request, "urlopen", blocked)

    root_notices = []
    retry_id = _enqueue(conn, "rate-limit", thread=10)
    class RetryAfter(Exception):
        def __init__(self):
            super().__init__("rate limited")
            self.retry_after = 7

    retry = deliver_one(
        conn, delivery_id=retry_id, now=100,
        send=lambda *args: (_ for _ in ()).throw(RetryAfter()),
        resolve_resource=lambda db, row: {"text": "retry-safe notice"},
        authorize_delivery=lambda db, row: True,
        notify_backlog=lambda bot, root, details: root_notices.append((bot, root, details)),
    )
    assert retry["state"] == "pending"
    row = conn.execute(
        "SELECT next_attempt_at,attempts,last_error_code FROM delivery_outbox WHERE id=?",
        (retry_id,),
    ).fetchone()
    assert tuple(row) == (107, 1, "telegram_rate_limited")
    assert root_notices and root_notices[0][1] == 777
    assert set(root_notices[0][2]) == {"delivery_id", "state", "attempts"}

    timeout_id = _enqueue(conn, "send-timeout")
    send_calls = []
    def timeout_send(*args):
        send_calls.append("send")
        raise TimeoutError("send completion is ambiguous")
    before = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("panel_operations", "payment_orders", "financial_ledger", "refund_requests")
    }
    timeout = deliver_one(
        conn, delivery_id=timeout_id, now=120, send=timeout_send,
        resolve_resource=lambda db, row: {"text": "one-time notification"},
        authorize_delivery=lambda db, row: True,
        rate_limiter=lambda bot, chat: 0,
    )
    assert timeout["state"] == "delivery_unknown" and send_calls == ["send"]
    assert deliver_one(
        conn, delivery_id=timeout_id, now=500, send=timeout_send,
        resolve_resource=lambda db, row: {"text": "must not resend"},
        authorize_delivery=lambda db, row: True,
    )["state"] == "not_claimed"
    after = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before
    }
    assert after == before and send_calls == ["send"]

    # Distinct topics under one bot/chat share the same rate budget key.
    first_topic = _enqueue(conn, "topic-one", thread=10)
    second_topic = _enqueue(conn, "topic-two", thread=20)
    budget_keys = []
    def shared_budget(bot_id, chat_id):
        budget_keys.append((bot_id, chat_id))
        return 0 if len(budget_keys) == 1 else 3

    success = deliver_one(
        conn, delivery_id=first_topic, now=200,
        send=lambda bot, token, chat, thread, payload: {"message_id": 44},
        resolve_resource=lambda db, row: {"text": "success"},
        authorize_delivery=lambda db, row: True,
        rate_limiter=shared_budget,
    )
    throttled = deliver_one(
        conn, delivery_id=second_topic, now=201,
        send=lambda *args: pytest.fail("shared bot/chat limiter must postpone second topic"),
        resolve_resource=lambda db, row: {"text": "postponed"},
        authorize_delivery=lambda db, row: True,
        rate_limiter=shared_budget,
    )
    assert success["state"] == "sent"
    assert conn.execute(
        "SELECT message_id,state FROM delivery_outbox WHERE id=?", (first_topic,)
    ).fetchone()[:] == (44, "sent")
    assert throttled["state"] == "pending"
    assert conn.execute(
        "SELECT next_attempt_at FROM delivery_outbox WHERE id=?", (second_topic,)
    ).fetchone()[0] == 204
    assert budget_keys == [(9001, 101), (9001, 101)]

    # A revoked grant is checked immediately before resource resolution.
    conn.execute("INSERT INTO account_access_grants VALUES(303,101,999)")
    private_id = _enqueue(conn, "revoked-private", kind="private", resource="303")
    resolved = []
    blocked_private = deliver_one(
        conn, delivery_id=private_id, now=300,
        send=lambda *args: pytest.fail("revoked ACL must block send"),
        resolve_resource=lambda db, row: resolved.append("secret-resolved") or {"text": "private"},
        authorize_delivery=lambda db, row: db.execute(
            "SELECT revoked_at FROM account_access_grants WHERE account_id=? AND grantee_tg_id=?",
            (int(row["resource_id"]), int(row["chat_id"])),
        ).fetchone()[0] is None,
    )
    assert blocked_private["state"] == "delivery_blocked" and resolved == []

    # A disabled bot does not consume or discard pending work.
    disabled_id = _enqueue(conn, "disabled-pending", bot=9002, chat=202)
    disabled = deliver_one(
        conn, delivery_id=disabled_id, now=400,
        send=lambda *args: pytest.fail("disabled bot must not send"),
        resolve_resource=lambda db, row: {"text": "pending"},
        authorize_delivery=lambda db, row: True,
    )
    assert disabled["state"] == "not_claimed"
    assert conn.execute(
        "SELECT state,attempts FROM delivery_outbox WHERE id=?", (disabled_id,)
    ).fetchone()[:] == ("pending", 0)

    # Even a successful Telegram return cannot be committed by a worker whose lease changed.
    stale_id = _enqueue(conn, "stale-lease")
    def steal_lease(bot, token, chat, thread, payload):
        conn.execute(
            "UPDATE delivery_outbox SET lease_token='new-owner' WHERE id=?", (stale_id,)
        )
        conn.commit()
        return {"message_id": 55}
    with pytest.raises(StaleDeliveryLease):
        deliver_one(
            conn, delivery_id=stale_id, now=500, send=steal_lease,
            resolve_resource=lambda db, row: {"text": "lease fenced"},
            authorize_delivery=lambda db, row: True,
            rate_limiter=lambda bot, chat: 0,
        )
    assert conn.execute(
        "SELECT state,message_id,lease_token FROM delivery_outbox WHERE id=?", (stale_id,)
    ).fetchone()[:] == ("sending", None, "new-owner")
    assert network_attempts == []
    conn.close()
