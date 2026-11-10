"""Offline acceptance for one-shot claims and crash-safe panel dispatch."""
from __future__ import annotations
import base64
from pathlib import Path
import sqlite3
import pytest
from crypto_store import encrypt_secret
from operation_worker import StaleDispatchResult, claim_prepared, dispatch_claimed

def _database(path, now):
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("""
        CREATE TABLE panel_operations (
            operation_id TEXT PRIMARY KEY, order_id TEXT, actor_tg_id INTEGER NOT NULL,
            bot_id INTEGER NOT NULL, account_id INTEGER, panel_id INTEGER NOT NULL,
            kind TEXT NOT NULL, request_cipher TEXT NOT NULL, expected_effect_json TEXT NOT NULL,
            state TEXT NOT NULL, lock_key TEXT NOT NULL, lock_token TEXT NOT NULL,
            idempotency_key TEXT NOT NULL UNIQUE, generation INTEGER NOT NULL DEFAULT 0,
            lease_until INTEGER, evidence_json TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE operation_attempts (
            id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL, attempt_no INTEGER NOT NULL,
            started_at INTEGER NOT NULL, finished_at INTEGER, outcome TEXT, evidence_json TEXT,
            UNIQUE(operation_id,attempt_no)
        )
    """)
    encrypted = encrypt_secret(
        '{"candidate_id":"candidate-w09","request":{"method":"renew_client"}}',
        "hstagent/columns/v1",
    )
    conn.execute(
        "INSERT INTO panel_operations(operation_id,order_id,actor_tg_id,bot_id,account_id,panel_id,"
        "kind,request_cipher,expected_effect_json,state,lock_key,lock_token,idempotency_key,"
        "generation,lease_until,evidence_json,created_at,updated_at) "
        "VALUES('operation-crash','order-1',101,9001,303,501,'vpn_renew',?,"
        "'{\"snapshot_sha256\":\"abc\"}','prepared','account:303','persistent-lock-token',"
        "'idem-crash',0,NULL,NULL,?,?)", (encrypted, now, now)
    )
    conn.commit()
    return conn

def test_crash_after_send_no_replay(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    attempts = []
    def blocked(*args, **kwargs):
        attempts.append("network")
        raise AssertionError("real network access is forbidden")
    monkeypatch.setattr("socket.create_connection", blocked)
    monkeypatch.setattr("socket.socket.connect", blocked)
    monkeypatch.setattr("urllib.request.urlopen", blocked)
    now = 100
    first = _database(tmp_path / "w093-dispatch.sqlite3", now)
    second = sqlite3.connect(str(tmp_path / "w093-dispatch.sqlite3"), timeout=10)
    second.row_factory = sqlite3.Row
    claim = claim_prepared(first, operation_id="operation-crash", now=now, lease_seconds=5)
    assert claim is not None and claim["state"] == "claimed" and claim["generation"] == 1
    assert claim["request"]["candidate_id"] == "candidate-w09"
    assert claim_prepared(second, operation_id="operation-crash", now=now + 1) is None
    sent = []
    def panel_send(request):
        sent.append(request["candidate_id"])
        raise TimeoutError("simulated crash after the remote mutation")
    result = dispatch_claimed(first, claim=claim, dispatch=panel_send, now=now + 2)
    assert result["dispatch_outcome"] == "unknown"
    assert result["operation_state"] == "needs_reconciliation"
    assert sent == ["candidate-w09"]
    later = claim_prepared(second, operation_id="operation-crash", now=now + 100)
    assert later["state"] == "needs_reconciliation" and sent == ["candidate-w09"]
    with pytest.raises(StaleDispatchResult):
        dispatch_claimed(first, claim=claim, dispatch=lambda request: sent.append("stale"), now=now + 101)
    assert sent == ["candidate-w09"]
    row = first.execute(
        "SELECT state,generation,lock_key,lock_token,lease_until FROM panel_operations "
        "WHERE operation_id='operation-crash'"
    ).fetchone()
    assert tuple(row) == ("needs_reconciliation", 1, "account:303", "persistent-lock-token", None)
    attempt = first.execute(
        "SELECT attempt_no,finished_at,outcome FROM operation_attempts WHERE operation_id='operation-crash'"
    ).fetchone()
    assert tuple(attempt) == (1, now + 2, "unknown")

    encrypted = encrypt_secret(
        '{"candidate_id":"candidate-expired","request":{"method":"renew_client"}}',
        "hstagent/columns/v1",
    )
    first.execute(
        "INSERT INTO panel_operations(operation_id,order_id,actor_tg_id,bot_id,account_id,panel_id,"
        "kind,request_cipher,expected_effect_json,state,lock_key,lock_token,idempotency_key,"
        "generation,lease_until,evidence_json,created_at,updated_at) "
        "VALUES('operation-expired','order-2',101,9001,304,501,'vpn_renew',?,"
        "'{\"snapshot_sha256\":\"def\"}','prepared','account:304','persistent-lock-token-2',"
        "'idem-expired',0,NULL,NULL,?,?)", (encrypted, now, now)
    )
    first.commit()
    expired_claim = claim_prepared(first, operation_id="operation-expired", now=200, lease_seconds=5)
    assert expired_claim["state"] == "claimed"
    recon = claim_prepared(second, operation_id="operation-expired", now=205)
    assert recon["state"] == "needs_reconciliation"
    with pytest.raises(StaleDispatchResult):
        dispatch_claimed(first, claim=expired_claim, dispatch=lambda request: sent.append("expired"), now=206)
    assert sent == ["candidate-w09"] and attempts == []
    first.close()
    second.close()
