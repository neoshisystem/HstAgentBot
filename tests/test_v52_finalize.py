"""Offline atomic-finalization acceptance for W09.4."""
from __future__ import annotations

import base64
import sqlite3

import pytest

import financial_service
import operation_service


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE accounts(
            id INTEGER PRIMARY KEY, panel_id INTEGER NOT NULL, deleted_at INTEGER,
            email TEXT NOT NULL, customer_name TEXT, enable INTEGER,
            total_gb REAL, expiry_time INTEGER, sub_id TEXT, sub_id_cipher TEXT
        );
        CREATE TABLE payment_orders(order_id TEXT PRIMARY KEY,status TEXT,revision INTEGER);
        CREATE TABLE financial_holds(
            hold_id TEXT PRIMARY KEY,order_id TEXT,state TEXT,source_key TEXT,
            amount_toman INTEGER,captured_ledger_id INTEGER
        );
        CREATE TABLE financial_ledger(
            id INTEGER PRIMARY KEY AUTOINCREMENT,business_key TEXT UNIQUE,
            user_id INTEGER,transaction_type TEXT,amount INTEGER
        );
        CREATE TABLE delivery_outbox(
            id INTEGER PRIMARY KEY AUTOINCREMENT,event_key TEXT,bot_id INTEGER,
            chat_id INTEGER,thread_id INTEGER,purpose TEXT,resource_type TEXT,
            resource_id TEXT,state TEXT,next_attempt_at INTEGER,
            UNIQUE(event_key,bot_id,chat_id,purpose)
        );
        CREATE TABLE panel_operations(
            operation_id TEXT PRIMARY KEY,order_id TEXT,actor_tg_id INTEGER,
            bot_id INTEGER,account_id INTEGER,panel_id INTEGER,state TEXT,
            generation INTEGER,lock_token TEXT,expected_effect_json TEXT,
            evidence_json TEXT,lease_until INTEGER,updated_at INTEGER
        );
    """)
    conn.execute(
        "INSERT INTO accounts VALUES(303,501,NULL,'old@example.invalid','Old',1,10,0,NULL,NULL)"
    )
    return conn


def _operation(conn, operation_id, order_id, state="dispatching", generation=1):
    conn.execute(
        "INSERT INTO panel_operations(operation_id,order_id,actor_tg_id,bot_id,account_id,"
        "panel_id,state,generation,lock_token,expected_effect_json,evidence_json,lease_until,updated_at) "
        "VALUES(?,?,101,9001,303,501,?,?,?,?,NULL,500,1)",
        (operation_id, order_id, state, generation, "owned-" + operation_id,
         '{"target":{"quota_bytes":21474836480}}'),
    )


def test_commit_failure_and_once_capture(monkeypatch):
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    conn = _db()
    conn.execute("INSERT INTO payment_orders VALUES('order-success','accepted',1)")
    conn.execute("INSERT INTO financial_holds VALUES('hold-success','order-success','active','wallet:root:101',100, NULL)")
    _operation(conn, "op-success", "order-success")
    conn.execute("INSERT INTO payment_orders VALUES('order-failure','completed',2)")
    conn.execute("INSERT INTO financial_ledger(business_key,user_id,transaction_type,amount) "
                 "VALUES('order:order-failure:capture:wallet:root:101',101,'debit',50)")
    _operation(conn, "op-failure", "order-failure", state="needs_reconciliation", generation=2)

    capture_calls = []
    def capture(conn, *, order_id, actor_id, now):
        assert conn.in_transaction
        capture_calls.append(order_id)
        conn.execute(
            "INSERT INTO financial_ledger(business_key,user_id,transaction_type,amount) "
            "VALUES(?,?,?,?)",
            (f"order:{order_id}:capture:wallet:root:101", 101, "debit", 100),
        )
        conn.execute(
            "UPDATE financial_holds SET state='captured',captured_ledger_id=1 "
            "WHERE order_id=? AND state='active'", (order_id,)
        )
        conn.execute(
            "UPDATE payment_orders SET status='completed',revision=revision+1 "
            "WHERE order_id=? AND status='accepted'", (order_id,)
        )
        return {"order_id": order_id, "ledger_ids": [1]}

    reversal_calls = []
    def reverse(conn, *, order_id, actor_id, now):
        assert conn.in_transaction
        reversal_calls.append(order_id)
        conn.execute(
            "INSERT OR IGNORE INTO financial_ledger(business_key,user_id,transaction_type,amount) "
            "VALUES(?,?,?,?)",
            (f"order:{order_id}:reverse:1", 101, "credit", 50),
        )
        conn.execute(
            "UPDATE payment_orders SET status='closed_refunded',revision=revision+1 "
            "WHERE order_id=? AND status='completed'", (order_id,)
        )
        return {"order_id": order_id, "status": "refunded", "reversed_ids": [2]}

    monkeypatch.setattr(financial_service, "capture_holds", capture)
    monkeypatch.setattr(financial_service, "reverse_order", reverse)

    conn.execute(
        "CREATE TRIGGER fail_outbox BEFORE INSERT ON delivery_outbox "
        "WHEN NEW.event_key='panel-operation:op-success:succeeded' "
        "BEGIN SELECT RAISE(ABORT,'injected outbox failure'); END"
    )
    success_evidence = {
        "outcome": "success",
        "expected_effect": {"quota_bytes": 21474836480},
    }
    with pytest.raises(sqlite3.IntegrityError):
        operation_service.finalize_success(
            conn, operation_id="op-success", generation=1,
            evidence=success_evidence, account_mapping={"email": "new@example.invalid"},
            now=100,
        )
    assert conn.execute("SELECT status FROM payment_orders WHERE order_id='order-success'").fetchone()[0] == "accepted"
    assert conn.execute("SELECT state FROM financial_holds WHERE hold_id='hold-success'").fetchone()[0] == "active"
    assert conn.execute("SELECT COUNT(*) FROM financial_ledger WHERE business_key LIKE 'order:order-success:capture:%'").fetchone()[0] == 0
    assert conn.execute("SELECT state FROM panel_operations WHERE operation_id='op-success'").fetchone()[0] == "dispatching"
    assert conn.execute("SELECT email FROM accounts WHERE id=303").fetchone()[0] == "old@example.invalid"
    assert conn.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0] == 0

    conn.execute("DROP TRIGGER fail_outbox")
    first = operation_service.finalize_success(
        conn, operation_id="op-success", generation=1,
        evidence=success_evidence, account_mapping={"email": "new@example.invalid"},
        now=101,
    )
    replay = operation_service.finalize_success(
        conn, operation_id="op-success", generation=1,
        evidence=success_evidence, account_mapping={"email": "new@example.invalid"},
        now=102,
    )
    assert first["state"] == "succeeded" and not first["replayed"]
    assert replay["state"] == "succeeded" and replay["replayed"]
    assert conn.execute("SELECT status FROM payment_orders WHERE order_id='order-success'").fetchone()[0] == "completed"
    assert conn.execute("SELECT state FROM financial_holds WHERE hold_id='hold-success'").fetchone()[0] == "captured"
    assert conn.execute("SELECT COUNT(*) FROM financial_ledger WHERE business_key LIKE 'order:order-success:capture:%'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM delivery_outbox WHERE event_key='panel-operation:op-success:succeeded'").fetchone()[0] == 1
    assert conn.execute("SELECT email FROM accounts WHERE id=303").fetchone()[0] == "new@example.invalid"

    failure = operation_service.finalize_failure(
        conn, operation_id="op-failure", generation=2,
        evidence={"not_applied_proven": True, "proof": "panel-audit-verified-77"},
        now=103,
    )
    failure_replay = operation_service.finalize_failure(
        conn, operation_id="op-failure", generation=2,
        evidence={"not_applied_proven": True, "proof": "panel-audit-verified-77"},
        now=104,
    )
    assert failure["state"] == "definite_failure" and not failure["replayed"]
    assert failure_replay["replayed"] is True
    assert reversal_calls == ["order-failure"]
    assert conn.execute(
        "SELECT COUNT(*) FROM financial_ledger WHERE business_key='order:order-failure:reverse:1'"
    ).fetchone()[0] == 1
    assert conn.execute("SELECT status FROM payment_orders WHERE order_id='order-failure'").fetchone()[0] == "closed_refunded"
    assert conn.execute("SELECT COUNT(*) FROM delivery_outbox WHERE event_key='panel-operation:op-failure:failed'").fetchone()[0] == 1
    assert capture_calls == ["order-success", "order-success"]
    conn.close()
