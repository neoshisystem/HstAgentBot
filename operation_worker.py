"""One-shot, lease-claimed dispatch for prepared v5.2 panel operations."""
from __future__ import annotations
from contextlib import contextmanager
import json
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any, Iterator

from crypto_store import decrypt_secret
from v52_models import MutationResult

class DispatchError(RuntimeError):
    """A prepared operation could not be safely claimed or dispatched."""
class StaleDispatchResult(DispatchError):
    """A result belongs to an old generation or no longer-owned lease."""
class DispatchLeaseExpired(DispatchError):
    """An expired dispatch lease is UNKNOWN and must not be resent."""

_FORBIDDEN_EVIDENCE_PARTS = (
    "secret", "password", "token", "cipher", "authorization",
    "credential", "private_key", "api_key", "sub_id", "session",
)

@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[None]:
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    if conn.in_transaction:
        conn.execute("SAVEPOINT w09_dispatch")
        try:
            yield
            conn.execute("RELEASE SAVEPOINT w09_dispatch")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT w09_dispatch")
            conn.execute("RELEASE SAVEPOINT w09_dispatch")
            raise
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise

def _epoch(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DispatchError(f"{name} must be a non-negative epoch-seconds integer")
    return value

def _public_evidence(value: Any, *, depth: int = 0) -> Any:
    if depth > 10:
        raise DispatchError("dispatch evidence nesting is too deep")
    if value is None or isinstance(value, (str, int, bool)):
        if isinstance(value, str) and len(value) > 2048:
            raise DispatchError("dispatch evidence string is too long")
        return value
    if isinstance(value, float):
        raise DispatchError("dispatch evidence must use exact integers, not floats")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise DispatchError("dispatch evidence keys must be strings")
            if any(part in key.casefold() for part in _FORBIDDEN_EVIDENCE_PARTS):
                raise DispatchError(f"secret-like evidence field is forbidden: {key}")
            result[key] = _public_evidence(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 100:
            raise DispatchError("dispatch evidence list is too large")
        return [_public_evidence(item, depth=depth + 1) for item in value]
    raise DispatchError("dispatch evidence must be JSON-compatible")

def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

def claim_prepared(
    conn: sqlite3.Connection, *, operation_id: str, now: int, lease_seconds: int = 30,
) -> dict[str, Any] | None:
    """Atomically claim PREPARED once; an expired DISPATCHING lease becomes UNKNOWN."""
    operation_id, now = str(operation_id or "").strip(), _epoch(now, "now")
    if not operation_id or len(operation_id) > 200:
        raise DispatchError("operation_id is required")
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 300:
        raise DispatchError("lease_seconds must be between 1 and 300")
    cipher = expected_json = None
    generation = lease_until = None
    lock_key = lock_token = None
    outcome: dict[str, Any] | None = None
    with _atomic(conn):
        row = conn.execute(
            "SELECT state,generation,lease_until,request_cipher,expected_effect_json,lock_key,lock_token "
            "FROM panel_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is None:
            raise DispatchError("panel operation does not exist")
        state = str(row["state"])
        if state == "prepared":
            lease_until = now + lease_seconds
            claimed = conn.execute(
                "UPDATE panel_operations SET state='dispatching',generation=generation+1,"
                "lease_until=?,updated_at=? WHERE operation_id=? AND state='prepared' RETURNING generation",
                (lease_until, now, operation_id),
            ).fetchone()
            if claimed is None:
                return None
            generation = int(claimed[0])
            conn.execute(
                "INSERT INTO operation_attempts(operation_id,attempt_no,started_at,finished_at,outcome,evidence_json) "
                "VALUES(?,?,?,NULL,NULL,NULL)", (operation_id, generation, now)
            )
            cipher, expected_json = str(row["request_cipher"]), str(row["expected_effect_json"])
            lock_key, lock_token = str(row["lock_key"]), str(row["lock_token"])
            outcome = {"state": "claimed"}
        elif state == "dispatching":
            current_lease = int(row["lease_until"]) if row["lease_until"] is not None else None
            if current_lease is not None and current_lease <= now:
                evidence = _json({
                    "outcome": "unknown", "reason": "dispatch_lease_expired",
                    "generation": int(row["generation"]),
                })
                changed = conn.execute(
                    "UPDATE panel_operations SET state='needs_reconciliation',lease_until=NULL,"
                    "evidence_json=?,updated_at=? WHERE operation_id=? AND state='dispatching' "
                    "AND generation=? AND lease_until<=?",
                    (evidence, now, operation_id, int(row["generation"]), now),
                )
                if changed.rowcount == 1:
                    conn.execute(
                        "UPDATE operation_attempts SET finished_at=?,outcome='unknown',evidence_json=? "
                        "WHERE operation_id=? AND attempt_no=? AND finished_at IS NULL",
                        (now, evidence, operation_id, int(row["generation"])),
                    )
                outcome = {"state": "needs_reconciliation", "reason": "dispatch_lease_expired"}
            else:
                return None
        elif state == "needs_reconciliation":
            return {"state": state, "operation_id": operation_id, "replayed": False}
        else:
            return None

    if outcome is None or outcome["state"] != "claimed":
        return outcome
    try:
        request_payload = json.loads(decrypt_secret(cipher, "hstagent/columns/v1"))
        expected_effect = json.loads(expected_json)
        if not isinstance(request_payload, dict) or not isinstance(expected_effect, dict):
            raise ValueError("stored operation payload has an invalid shape")
    except Exception as exc:
        reason = _json({
            "outcome": "unknown", "reason": "request_decryption_failed",
            "exception_type": type(exc).__name__, "generation": generation,
        })
        with _atomic(conn):
            conn.execute(
                "UPDATE panel_operations SET state='needs_reconciliation',lease_until=NULL,"
                "evidence_json=?,updated_at=? WHERE operation_id=? AND state='dispatching' AND generation=?",
                (reason, now, operation_id, generation),
            )
            conn.execute(
                "UPDATE operation_attempts SET finished_at=?,outcome='unknown',evidence_json=? "
                "WHERE operation_id=? AND attempt_no=? AND finished_at IS NULL",
                (now, reason, operation_id, generation),
            )
        return {"state": "needs_reconciliation", "operation_id": operation_id, "generation": generation}
    return {
        "state": "claimed", "operation_id": operation_id, "generation": generation,
        "lease_until": lease_until, "lock_key": lock_key, "lock_token": lock_token,
        "request": request_payload, "expected_effect": expected_effect,
    }

def dispatch_claimed(
    conn: sqlite3.Connection, *, claim: Mapping[str, Any],
    dispatch: Callable[[dict[str, Any]], MutationResult], now: int,
) -> dict[str, Any]:
    """Run the injected panel mutation after claim commit, then record only its own generation."""
    now = _epoch(now, "now")
    if not isinstance(claim, Mapping) or claim.get("state") != "claimed":
        raise DispatchError("a successful claim_prepared result is required")
    if not callable(dispatch):
        raise TypeError("dispatch must be a callable")
    operation_id, generation = str(claim.get("operation_id") or ""), claim.get("generation")
    if not operation_id or isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise DispatchError("claim identity is invalid")
    current = conn.execute(
        "SELECT state,generation,lease_until FROM panel_operations WHERE operation_id=?", (operation_id,)
    ).fetchone()
    if (current is None or str(current["state"]) != "dispatching"
        or int(current["generation"]) != generation or current["lease_until"] is None
        or int(current["lease_until"]) <= now):
        raise StaleDispatchResult("claim generation or lease is no longer current")
    try:
        returned = dispatch(dict(claim["request"]))
        if not isinstance(returned, MutationResult):
            raise TypeError("dispatcher must return v52_models.MutationResult")
        result = returned
        evidence = _public_evidence(result.evidence or {})
        if result.expected_effect is not None:
            evidence["expected_effect"] = _public_evidence(result.expected_effect)
        evidence["generation"], evidence["outcome"] = generation, result.state
    except Exception as exc:
        result = MutationResult(
            state="unknown",
            evidence={"reason": "dispatch_exception", "exception_type": type(exc).__name__, "generation": generation},
        )
        evidence = _public_evidence(result.evidence)
        evidence["outcome"] = "unknown"
    evidence_json = _json(evidence)
    new_state = "needs_reconciliation" if result.state == "unknown" else "dispatching"
    with _atomic(conn):
        changed = conn.execute(
            "UPDATE panel_operations SET state=?,lease_until=NULL,evidence_json=?,updated_at=? "
            "WHERE operation_id=? AND state='dispatching' AND generation=? AND lease_until>?",
            (new_state, evidence_json, now, operation_id, generation, now),
        )
        if changed.rowcount != 1:
            raise StaleDispatchResult("dispatch result belongs to an expired or superseded generation")
        attempt = conn.execute(
            "UPDATE operation_attempts SET finished_at=?,outcome=?,evidence_json=? "
            "WHERE operation_id=? AND attempt_no=? AND finished_at IS NULL",
            (now, result.state, evidence_json, operation_id, generation),
        )
        if attempt.rowcount != 1:
            raise StaleDispatchResult("dispatch attempt is already finished or missing")
    return {
        "operation_id": operation_id, "generation": generation,
        "dispatch_outcome": result.state, "operation_state": new_state, "evidence": evidence,
    }

__all__ = [
    "DispatchError", "StaleDispatchResult", "DispatchLeaseExpired", "claim_prepared", "dispatch_claimed",
]
