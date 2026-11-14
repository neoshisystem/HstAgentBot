"""Offline evidence-only reconciliation acceptance for W09.5."""
from __future__ import annotations

import json
import sqlite3

import pytest

from crypto_store import encrypt_secret
from reconciliation_service import mark_unknown, reconcile_operation
from v52_models import LookupResult, ReconcileResult


def _db(monkeypatch):
    import base64
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE panel_operations(
            operation_id TEXT PRIMARY KEY,panel_id INTEGER,state TEXT,generation INTEGER,
            request_cipher TEXT,expected_effect_json TEXT,evidence_json TEXT,
            lease_until INTEGER,updated_at INTEGER,lock_key TEXT,lock_token TEXT
        );
        CREATE TABLE operation_attempts(
            operation_id TEXT,attempt_no INTEGER,finished_at INTEGER,outcome TEXT,evidence_json TEXT,
            PRIMARY KEY(operation_id,attempt_no)
        );
    """)
    return conn


def _add(conn, operation_id, *, state="dispatching", generation=3):
    payload = json.dumps({"candidate_id": "candidate-" + operation_id, "request": {"method": "renew_client"}})
    expected = json.dumps({
        "snapshot": {"account_id": 303, "panel_id": 501},
        "target": {"quota_bytes": 200, "expires_at": 900},
    })
    conn.execute(
        "INSERT INTO panel_operations VALUES(?,?,?,?,?,?,NULL,500,1,?,?)",
        (operation_id, 501, state, generation,
         encrypt_secret(payload, "hstagent/columns/v1"), expected,
         "account:303", "owner-" + operation_id),
    )
    conn.execute(
        "INSERT INTO operation_attempts(operation_id,attempt_no) VALUES(?,?)",
        (operation_id, generation),
    )


def test_absence_not_proof_and_effect_check(monkeypatch):
    import reconciliation_service as service

    conn = _db(monkeypatch)
    for operation_id in ("absent", "old", "applied", "proven", "manual"):
        _add(conn, operation_id)
    calls = []
    def lookup_absent(panel_id, candidate_id):
        calls.append((panel_id, candidate_id))
        return LookupResult(state="not_found", evidence={"source": "same_panel_read"})

    absent = reconcile_operation(conn, operation_id="absent", lookup=lookup_absent, now=100)
    assert absent["state"] == "needs_reconciliation"
    row = conn.execute(
        "SELECT state,lock_key,lock_token FROM panel_operations WHERE operation_id='absent'"
    ).fetchone()
    assert tuple(row) == ("needs_reconciliation", "account:303", "owner-absent")
    assert calls == [(501, "candidate-absent")]

    # A commercial manual credit is not technical proof and cannot clear UNKNOWN.
    manual = mark_unknown(
        conn, operation_id="absent", generation=3,
        evidence={"reason": "root_manual_credit_recorded"}, now=101,
    )
    assert manual["state"] == "needs_reconciliation" and manual["replayed"]
    assert conn.execute(
        "SELECT state,lock_token FROM panel_operations WHERE operation_id='absent'"
    ).fetchone()[:] == ("needs_reconciliation", "owner-absent")

    old = reconcile_operation(
        conn, operation_id="old",
        lookup=lambda panel, candidate: LookupResult(
            state="found", client={"quota_bytes": 100, "expires_at": 900}
        ),
        now=102,
    )
    assert old["state"] == "needs_reconciliation"
    assert conn.execute(
        "SELECT evidence_json FROM panel_operations WHERE operation_id='old'"
    ).fetchone()[0].find("client_effect_conflict") >= 0

    finalized = []
    def finalize_ok(conn, **kwargs):
        finalized.append(("success", kwargs))
        conn.execute(
            "UPDATE panel_operations SET state='succeeded' WHERE operation_id=?",
            (kwargs["operation_id"],),
        )
        return {"operation_id": kwargs["operation_id"], "state": "succeeded"}

    def finalize_bad(conn, **kwargs):
        finalized.append(("failure", kwargs))
        conn.execute(
            "UPDATE panel_operations SET state='definite_failure' WHERE operation_id=?",
            (kwargs["operation_id"],),
        )
        return {"operation_id": kwargs["operation_id"], "state": "definite_failure"}

    monkeypatch.setattr(service, "finalize_success", finalize_ok)
    monkeypatch.setattr(service, "finalize_failure", finalize_bad)

    applied = reconcile_operation(
        conn, operation_id="applied",
        lookup=lambda panel, candidate: LookupResult(
            state="found", client={"quota_bytes": 200, "expires_at": 900}
        ),
        now=103,
    )
    assert applied["state"] == "succeeded"
    assert finalized[-1][0] == "success"
    assert finalized[-1][1]["account_mapping"] == {}
    assert finalized[-1][1]["evidence"]["reconciliation_state"] == "applied"

    proven = reconcile_operation(
        conn, operation_id="proven",
        lookup=lambda panel, candidate: ReconcileResult(
            state="not_applied_proven", details={"proof_id": "panel-audit-verified-9"}
        ),
        now=104,
    )
    assert proven["state"] == "definite_failure"
    assert finalized[-1][0] == "failure"
    assert finalized[-1][1]["evidence"]["not_applied_proven"] is True

    conflict = reconcile_operation(
        conn, operation_id="manual",
        lookup=lambda panel, candidate: ReconcileResult(
            state="applied", details={"expected_effect": {"quota_bytes": 200, "expires_at": 899}}
        ),
        now=105,
    )
    assert conflict["state"] == "needs_reconciliation"
    assert conn.execute(
        "SELECT lock_token FROM panel_operations WHERE operation_id='manual'"
    ).fetchone()[0] == "owner-manual"
    assert calls == [(501, "candidate-absent")]
    conn.close()
