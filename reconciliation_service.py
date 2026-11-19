"""Evidence-only reconciliation for uncertain panel operations."""
from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from crypto_store import decrypt_secret
from operation_service import finalize_failure, finalize_success
from v52_models import LookupResult, ReconcileResult


class ReconciliationError(RuntimeError):
    """An operation cannot be reconciled safely."""


def _atomic(conn: sqlite3.Connection):
    class Atomic:
        def __enter__(self):
            if conn.in_transaction:
                self.savepoint = "w09_reconcile_" + secrets.token_hex(4)
                conn.execute(f"SAVEPOINT {self.savepoint}")
            else:
                self.savepoint = None
                conn.execute("BEGIN IMMEDIATE")
            return self

        def __exit__(self, exc_type, exc, tb):
            if self.savepoint:
                if exc_type:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {self.savepoint}")
                conn.execute(f"RELEASE SAVEPOINT {self.savepoint}")
            elif exc_type:
                conn.rollback()
            else:
                conn.commit()
            return False
    return Atomic()


def _epoch(now: Any) -> int:
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise ReconciliationError("now must be a non-negative UTC epoch integer")
    return now


def _safe_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    forbidden = ("secret", "password", "token", "cipher", "authorization",
                 "credential", "private_key", "api_key", "sub_id", "session")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not key or any(word in key.casefold() for word in forbidden):
            raise ReconciliationError("reconciliation evidence contains a forbidden field")
        if isinstance(item, Mapping):
            result[key] = _safe_evidence(item)
        elif isinstance(item, (list, tuple)):
            result[key] = list(item)
        elif item is None or isinstance(item, (str, int, bool)):
            result[key] = item
        else:
            raise ReconciliationError("reconciliation evidence must be JSON-safe")
    return result


def _matches(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> bool:
    return all(key in actual and actual[key] == value for key, value in expected.items())


def mark_unknown(
    conn: sqlite3.Connection, *, operation_id: str, generation: int,
    evidence: Mapping[str, Any], now: int,
) -> dict[str, Any]:
    """Record ambiguity while preserving the operation's durable account lock."""
    now = _epoch(now)
    operation_id = str(operation_id or "").strip()
    if not operation_id or isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise ReconciliationError("operation identity and generation are required")
    safe = _safe_evidence(evidence)
    encoded = json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with _atomic(conn):
        row = conn.execute(
            "SELECT state,generation FROM panel_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise ReconciliationError("panel operation does not exist")
        state, current_generation = str(row[0]), int(row[1])
        if current_generation != generation:
            raise ReconciliationError("reconciliation generation is stale")
        if state == "needs_reconciliation":
            return {"operation_id": operation_id, "state": state, "replayed": True}
        if state != "dispatching":
            raise ReconciliationError("only a dispatching operation can become unknown")
        changed = conn.execute(
            "UPDATE panel_operations SET state='needs_reconciliation',lease_until=NULL,"
            "evidence_json=?,updated_at=? WHERE operation_id=? AND state='dispatching' "
            "AND generation=?",
            (encoded, now, operation_id, generation),
        )
        if changed.rowcount != 1:
            raise ReconciliationError("operation changed during unknown transition")
        conn.execute(
            "UPDATE operation_attempts SET finished_at=COALESCE(finished_at,?),"
            "outcome='unknown',evidence_json=COALESCE(evidence_json,?) "
            "WHERE operation_id=? AND attempt_no=?",
            (now, encoded, operation_id, generation),
        )
    return {"operation_id": operation_id, "state": "needs_reconciliation", "replayed": False}


def reconcile_operation(
    conn: sqlite3.Connection, *, operation_id: str,
    lookup: Callable[[int, str], LookupResult | ReconcileResult],
    now: int,
) -> dict[str, Any]:
    """Read the same panel/candidate outside a DB transaction; never redispatch a mutation.

    lookup(panel_id, candidate_id) must be a read-only adapter. A missing client,
    unavailable panel, old snapshot, or effect conflict remains UNKNOWN.
    """
    now = _epoch(now)
    operation_id = str(operation_id or "").strip()
    if not operation_id or not callable(lookup):
        raise ReconciliationError("operation_id and a read-only lookup adapter are required")
    row = conn.execute(
        "SELECT operation_id,panel_id,state,generation,request_cipher,expected_effect_json "
        "FROM panel_operations WHERE operation_id=?",
        (operation_id,),
    ).fetchone()
    if row is None:
        raise ReconciliationError("panel operation does not exist")
    generation = int(row["generation"])
    if str(row["state"]) not in {"dispatching", "needs_reconciliation"}:
        raise ReconciliationError("operation is not awaiting reconciliation")
    try:
        request = json.loads(decrypt_secret(str(row["request_cipher"]), "hstagent/columns/v1"))
        candidate_id = str(request["candidate_id"])
        expected = json.loads(str(row["expected_effect_json"] or "{}"))
        target = expected.get("target", {})
        if not candidate_id or not isinstance(target, Mapping):
            raise ValueError("stored candidate or target is invalid")
    except Exception as exc:
        return mark_unknown(
            conn, operation_id=operation_id, generation=generation,
            evidence={"reason": "stored_candidate_unavailable",
                      "exception_type": type(exc).__name__}, now=now,
        )

    try:
        result = lookup(int(row["panel_id"]), candidate_id)
    except Exception as exc:
        return mark_unknown(
            conn, operation_id=operation_id, generation=generation,
            evidence={"reason": "panel_lookup_unavailable",
                      "exception_type": type(exc).__name__}, now=now,
        )

    if isinstance(result, ReconcileResult):
        if result.state == "not_applied_proven":
            details = result.details or {}
            proof = str(details.get("proof") or details.get("proof_id") or "").strip()
            if not proof:
                return mark_unknown(
                    conn, operation_id=operation_id, generation=generation,
                    evidence={"reason": "not_applied_result_without_proof"}, now=now,
                )
            return finalize_failure(
                conn, operation_id=operation_id, generation=generation,
                evidence={"not_applied_proven": True, "proof": proof,
                          "reconciliation_state": "not_applied_proven"}, now=now,
            )
        if result.state == "applied":
            details = result.details or {}
            actual = details.get("expected_effect", details.get("client", details))
            if isinstance(actual, Mapping) and _matches(target, actual):
                return finalize_success(
                    conn, operation_id=operation_id, generation=generation,
                    evidence={"outcome": "success", "reconciliation_state": "applied",
                              "expected_effect": dict(actual)}, account_mapping={}, now=now,
                )
            return mark_unknown(
                conn, operation_id=operation_id, generation=generation,
                evidence={"reason": "applied_effect_conflict"}, now=now,
            )
        return mark_unknown(
            conn, operation_id=operation_id, generation=generation,
            evidence={"reason": result.state}, now=now,
        )

    if not isinstance(result, LookupResult):
        return mark_unknown(
            conn, operation_id=operation_id, generation=generation,
            evidence={"reason": "invalid_lookup_result"}, now=now,
        )
    if result.state != "found":
        return mark_unknown(
            conn, operation_id=operation_id, generation=generation,
            evidence={"reason": "client_" + result.state,
                      "lookup_evidence": result.evidence or {}}, now=now,
        )
    actual = result.client or {}
    if _matches(target, actual):
        return finalize_success(
            conn, operation_id=operation_id, generation=generation,
            evidence={"outcome": "success", "reconciliation_state": "applied",
                      "expected_effect": actual}, account_mapping={}, now=now,
        )
    return mark_unknown(
        conn, operation_id=operation_id, generation=generation,
        evidence={"reason": "client_effect_conflict",
                  "lookup_evidence": result.evidence or {}}, now=now,
    )


__all__ = ["ReconciliationError", "mark_unknown", "reconcile_operation"]
