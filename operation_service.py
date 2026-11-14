"""Durable, encrypted preparation of v5.2 panel operations."""
from __future__ import annotations
from contextlib import contextmanager
import hashlib
import json
import sqlite3
import uuid
from collections.abc import Mapping
from typing import Any, Iterator

from access_service import _actor, _authorize_row
from confirmation_service import consume_confirmation
from crypto_store import decrypt_secret, encrypt_secret
from eligibility_service import can_renew_existing

class OperationError(ValueError):
    """A panel operation cannot be prepared safely."""
class OperationUnauthorized(OperationError):
    """The actor cannot operate on this account through this bot."""
class OperationConflict(OperationError):
    """An order, confirmation or idempotency key conflicts with this operation."""
class OperationBusy(OperationError):
    """The account already has an unresolved panel operation."""

_FORBIDDEN_EXPECTED_PARTS = (
    "secret", "password", "token", "cipher", "authorization",
    "credential", "private_key", "api_key", "sub_id", "session",
)

@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[None]:
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    if conn.in_transaction:
        conn.execute("SAVEPOINT w09_prepare")
        try:
            yield
            conn.execute("RELEASE SAVEPOINT w09_prepare")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT w09_prepare")
            conn.execute("RELEASE SAVEPOINT w09_prepare")
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

def _positive(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OperationError(f"{name} must be a positive integer")
    return value

def _now(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OperationError("now must be a non-negative UTC epoch-seconds integer")
    return value

def _canonical(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise OperationError("operation payload must be finite JSON data") from exc

def _expected_public(value: Any, *, depth: int = 0) -> Any:
    if depth > 12:
        raise OperationError("expected_effect nesting is too deep")
    if value is None or isinstance(value, (str, int, bool)):
        if isinstance(value, str) and len(value) > 2048:
            raise OperationError("expected_effect string is too long")
        return value
    if isinstance(value, float):
        raise OperationError("expected_effect must use exact integer values, not floats")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 100:
                raise OperationError("expected_effect keys must be short strings")
            if any(part in key.casefold() for part in _FORBIDDEN_EXPECTED_PARTS):
                raise OperationError(f"secret-like field is forbidden in expected_effect: {key}")
            result[key] = _expected_public(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 100:
            raise OperationError("expected_effect list is too large")
        return [_expected_public(item, depth=depth + 1) for item in value]
    raise OperationError("expected_effect must contain only JSON-compatible public values")

def _operation_result(conn: sqlite3.Connection, row: sqlite3.Row, *, replayed: bool) -> dict[str, Any]:
    try:
        request_payload = json.loads(decrypt_secret(str(row["request_cipher"]), "hstagent/columns/v1"))
    except Exception as exc:
        raise OperationError("stored operation request failed authentication") from exc
    return {
        "operation_id": str(row["operation_id"]),
        "order_id": str(row["order_id"]) if row["order_id"] is not None else None,
        "account_id": int(row["account_id"]) if row["account_id"] is not None else None,
        "panel_id": int(row["panel_id"]), "kind": str(row["kind"]),
        "state": str(row["state"]), "generation": int(row["generation"]),
        "lock_key": str(row["lock_key"]),
        "candidate_id": str(request_payload["candidate_id"]),
        "request": request_payload["request"],
        "expected_effect": json.loads(str(row["expected_effect_json"])),
        "replayed": replayed,
    }

def prepare_operation(
    conn: sqlite3.Connection, *, confirmation_nonce: str, actor_tg_id: int,
    bot_id: int, order_id: str, account_id: int, kind: str,
    request: Mapping[str, Any], expected_effect: Mapping[str, Any],
    idempotency_key: str, now: int,
) -> dict[str, Any]:
    """Re-authorize, lock, consume confirmation and persist an encrypted request atomically.

    This method performs no network I/O. The order remains accepted until W09.4
    supplies evidence-backed finalization; its revision is advanced here as the
    durable preparation boundary.
    """
    actor_tg_id, bot_id, account_id = (
        _positive(actor_tg_id, "actor_tg_id"), _positive(bot_id, "bot_id"),
        _positive(account_id, "account_id"),
    )
    now = _now(now)
    order_id, kind, idempotency_key = (
        str(order_id or "").strip(), str(kind or "").strip().lower(),
        str(idempotency_key or "").strip(),
    )
    if not order_id or len(order_id) > 200 or not kind or len(kind) > 60:
        raise OperationError("order_id and kind are required")
    if not idempotency_key or len(idempotency_key) > 200:
        raise OperationError("idempotency_key is required")
    if not isinstance(confirmation_nonce, str):
        raise OperationError("confirmation_nonce is required")
    if not isinstance(request, Mapping) or not isinstance(expected_effect, Mapping):
        raise OperationError("request and expected_effect must be mappings")
    if not isinstance(expected_effect.get("snapshot"), Mapping):
        raise OperationError("expected_effect.snapshot is required")

    request_value = dict(request)
    snapshot = _expected_public(expected_effect["snapshot"])
    target_effect = _expected_public(expected_effect.get("target", {}))
    snapshot["account_id"] = account_id
    snapshot["panel_id"] = int(expected_effect["snapshot"].get("panel_id", 0) or 0)
    snapshot_revision = expected_effect["snapshot"].get("account_revision")
    if snapshot_revision is not None and (
        isinstance(snapshot_revision, bool) or not isinstance(snapshot_revision, int)
    ):
        raise OperationError("snapshot.account_revision must be an integer")
    with _atomic(conn):
        prior = conn.execute(
            "SELECT * FROM panel_operations WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        if prior is not None:
            if (str(prior["order_id"] or "") != order_id
                or int(prior["actor_tg_id"]) != actor_tg_id
                or int(prior["bot_id"]) != bot_id
                or int(prior["account_id"] or 0) != account_id
                or str(prior["kind"]) != kind):
                raise OperationConflict("idempotency_key is already bound to another operation")
            result = _operation_result(conn, prior, replayed=True)
            supplied_snapshot = dict(snapshot)
            if supplied_snapshot.get("account_revision") is None:
                supplied_snapshot["account_revision"] = result["expected_effect"]["snapshot"].get("account_revision")
            if supplied_snapshot.get("panel_id") == 0:
                supplied_snapshot["panel_id"] = result["panel_id"]
            if result["request"] != request_value:
                raise OperationConflict("idempotency replay changed the encrypted request")
            supplied_hash = hashlib.sha256(_canonical(supplied_snapshot).encode("utf-8")).hexdigest()
            if result["expected_effect"].get("snapshot_sha256") != supplied_hash:
                raise OperationConflict("idempotency replay changed the expected snapshot")
            return result

        account = conn.execute(
            "SELECT * FROM accounts WHERE id=? AND deleted_at IS NULL", (account_id,)
        ).fetchone()
        if account is None or account["panel_id"] is None:
            raise OperationUnauthorized("account is missing, deleted or not bound to a panel")
        panel_id = int(account["panel_id"])
        if snapshot["panel_id"] not in {0, panel_id}:
            raise OperationConflict("expected snapshot panel differs from the account panel")
        snapshot["panel_id"] = panel_id
        snapshot["account_revision"] = int(account["revision"])
        snapshot_json = _canonical(snapshot)
        snapshot_hash = hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
        if str(snapshot.get("time_mode", "dated")) == "on_hold":
            raise OperationUnauthorized("on-hold account cannot be prepared for renewal")

        actor_row = conn.execute(
            "SELECT role,rep_id,is_blocked FROM bot_users WHERE telegram_id=?", (actor_tg_id,)
        ).fetchone()
        if actor_row is None:
            raise OperationUnauthorized("active Telegram actor is not registered")
        actor = _actor({
            "actor_tg_id": actor_tg_id,
            "is_blocked": bool(actor_row["is_blocked"]),
            "rep_id": actor_row["rep_id"],
        })
        if not _authorize_row(conn, actor, account, "renew"):
            raise OperationUnauthorized("actor is not authorized to renew this account")

        bot = conn.execute(
            "SELECT rep_id,is_primary,status FROM bot_registry WHERE bot_id=?", (bot_id,)
        ).fetchone()
        if bot is None or str(bot["status"]) != "active":
            raise OperationUnauthorized("active bot registry identity is unavailable")
        if bool(bot["is_primary"]):
            if bot["rep_id"] is not None:
                raise OperationUnauthorized("primary bot cannot be assigned to a representative")
            merchant_key = "root"
        else:
            if bot["rep_id"] is None:
                raise OperationUnauthorized("dedicated bot has no merchant identity")
            merchant_key = f"rep:{int(bot['rep_id'])}"
        if str(account["seller_key"] or "root") != merchant_key:
            raise OperationUnauthorized("account seller does not match the registered bot")

        order = conn.execute(
            "SELECT o.order_id,o.actor_tg_id,o.bot_id,o.merchant_key,o.purpose,o.status,"
            "o.revision,q.account_id,q.panel_id "
            "FROM payment_orders o JOIN quotes q ON q.quote_id=o.quote_id WHERE o.order_id=?",
            (order_id,),
        ).fetchone()
        if (order is None or int(order["actor_tg_id"]) != actor_tg_id
            or int(order["bot_id"]) != bot_id or str(order["merchant_key"]) != merchant_key
            or str(order["status"]) != "accepted"
            or str(order["purpose"]) not in {"vpn_renew", "vpn_addon"}
            or int(order["account_id"] or 0) != account_id
            or int(order["panel_id"] or 0) != panel_id):
            raise OperationConflict("accepted order does not match the actor, bot, merchant and account")

        actor_type = "rep" if str(actor_row["role"]).lower() in {"rep", "representative", "reseller"} else "end_user"
        if not can_renew_existing(
            conn, panel_id=panel_id, actor_type=actor_type,
            rep_id=int(actor_row["rep_id"]) if actor_row["rep_id"] is not None else None,
            is_root=bool(actor["root"]), on_hold=False,
        ):
            raise OperationUnauthorized("panel lifecycle, audience or capability does not allow renewal")

        confirmation = conn.execute(
            "SELECT action,target_type,target_id,expected_revision,expires_at,consumed_at,operation_id "
            "FROM action_confirmations WHERE nonce=?", (confirmation_nonce,)
        ).fetchone()
        if confirmation is None:
            raise OperationConflict("one-use confirmation does not exist")
        if (str(confirmation["action"]) != "panel.operation.prepare"
            or str(confirmation["target_type"]) != "account"
            or str(confirmation["target_id"]) != str(account_id)
            or int(confirmation["expected_revision"]) != int(account["revision"])):
            raise OperationConflict("confirmation action, target or account revision is stale")
        if confirmation["consumed_at"] is not None:
            linked = str(confirmation["operation_id"] or "")
            if not linked:
                raise OperationConflict("consumed confirmation has no operation link")
            linked_row = conn.execute(
                "SELECT * FROM panel_operations WHERE operation_id=?", (linked,)
            ).fetchone()
            if linked_row is None:
                raise OperationConflict("consumed confirmation operation is missing")
            if str(linked_row["idempotency_key"]) != idempotency_key:
                raise OperationConflict("confirmation replay must resolve to its original idempotency key")
            return _operation_result(conn, linked_row, replayed=True)

        lock_key = f"account:{account_id}"
        locked = conn.execute(
            "SELECT operation_id,idempotency_key,state FROM panel_operations "
            "WHERE lock_key=? AND state IN ('prepared','dispatching','needs_reconciliation') "
            "ORDER BY created_at DESC LIMIT 1", (lock_key,)
        ).fetchone()
        if locked is not None:
            raise OperationBusy("account has an unresolved operation; reconciliation is required")

        operation_id, candidate_id = str(uuid.uuid4()), str(uuid.uuid4())
        request_payload = {
            "candidate_id": candidate_id, "account_id": account_id,
            "panel_id": panel_id, "order_id": order_id, "kind": kind,
            "request": request_value,
        }
        request_cipher = encrypt_secret(_canonical(request_payload), "hstagent/columns/v1")
        expected_json = _canonical({
            "snapshot": snapshot, "snapshot_sha256": snapshot_hash, "target": target_effect,
        })
        lock_token = uuid.uuid4().hex
        consumed = consume_confirmation(
            conn, nonce=confirmation_nonce, actor_tg_id=actor_tg_id, bot_id=bot_id,
            action="panel.operation.prepare", target_type="account", target_id=account_id,
            expected_revision=int(account["revision"]), now=now, operation_id=operation_id,
        )
        if consumed["operation_id"] != operation_id or consumed["replayed"]:
            raise OperationConflict("confirmation was consumed by another operation")
        changed = conn.execute(
            "UPDATE payment_orders SET revision=revision+1 "
            "WHERE order_id=? AND status='accepted' AND revision=?",
            (order_id, int(order["revision"])),
        )
        if changed.rowcount != 1:
            raise OperationConflict("order changed while the operation was being prepared")
        conn.execute(
            "INSERT INTO panel_operations("
            "operation_id,order_id,actor_tg_id,bot_id,account_id,panel_id,kind,"
            "request_cipher,expected_effect_json,state,lock_key,lock_token,idempotency_key,"
            "generation,lease_until,evidence_json,created_at,updated_at"
            ") VALUES(?,?,?,?,?,?,?,?,?,'prepared',?,?,?,0,NULL,NULL,?,?)",
            (operation_id, order_id, actor_tg_id, bot_id, account_id, panel_id,
             kind, request_cipher, expected_json, lock_key, lock_token,
             idempotency_key, now, now),
        )
        row = conn.execute("SELECT * FROM panel_operations WHERE operation_id=?", (operation_id,)).fetchone()
        return _operation_result(conn, row, replayed=False)

__all__ = [
    "OperationError", "OperationUnauthorized", "OperationConflict", "OperationBusy",
    "prepare_operation",
]


def _finalization_evidence(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise OperationError("finalization evidence must be a mapping")
    result: dict[str, Any] = {}
    forbidden = ("secret", "password", "token", "cipher", "authorization",
                 "credential", "private_key", "api_key", "sub_id", "session")
    for key, item in value.items():
        if not isinstance(key, str) or not key or any(part in key.casefold() for part in forbidden):
            raise OperationError("finalization evidence contains a forbidden field")
        if isinstance(item, Mapping):
            result[key] = _finalization_evidence(item)
        elif isinstance(item, (list, tuple)):
            result[key] = list(item)
        elif item is None or isinstance(item, (str, int, bool)):
            result[key] = item
        else:
            raise OperationError("finalization evidence must contain JSON-safe values")
    return result


def _same_effect(expected: Mapping[str, Any], observed: Mapping[str, Any]) -> bool:
    """Compare every expected target field; extra live fields are harmless."""
    return all(key in observed and observed[key] == value for key, value in expected.items())


def _insert_operation_notice(
    conn: sqlite3.Connection, *, operation_id: str, bot_id: int,
    recipient_tg_id: int, outcome: str,
) -> None:
    event_key = f"panel-operation:{operation_id}:{outcome}"
    resource_cipher = encrypt_secret(operation_id, "hstagent/columns/v1")
    conn.execute(
        "INSERT INTO delivery_outbox(event_key,bot_id,chat_id,thread_id,purpose,"
        "resource_type,resource_id,state,next_attempt_at) "
        "VALUES(?,?,?,NULL,'panel_operation_result','operation',?,'pending',0) "
        "ON CONFLICT(event_key,bot_id,chat_id,purpose) DO NOTHING",
        (event_key, bot_id, recipient_tg_id, resource_cipher),
    )
    row = conn.execute(
        "SELECT resource_type,resource_id,chat_id,thread_id FROM delivery_outbox "
        "WHERE event_key=? AND bot_id=? AND purpose='panel_operation_result'",
        (event_key, bot_id),
    ).fetchone()
    try:
        stored_resource = decrypt_secret(str(row[1]), "hstagent/columns/v1") if row is not None else ""
    except Exception as exc:
        raise OperationConflict("stored operation notification reference failed authentication") from exc
    if row is None or (str(row[0]), stored_resource, int(row[2]), row[3]) != (
        "operation", operation_id, recipient_tg_id, None
    ):
        raise OperationConflict("operation notification idempotency key conflicts")


def finalize_success(
    conn: sqlite3.Connection, *, operation_id: str, generation: int,
    evidence: Mapping[str, Any], now: int,
    account_mapping: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Atomically map the account, capture holds, complete the order and enqueue delivery.

    The caller must supply evidence from the same dispatch generation or a
    reconciliation that positively matched the stored expected effect.  This
    function performs no network I/O.
    """
    now = _now(now)
    operation_id = str(operation_id or "").strip()
    if not operation_id or len(operation_id) > 200:
        raise OperationError("operation_id is required")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise OperationError("generation must be a positive integer")
    safe_evidence = _finalization_evidence(evidence)
    replay_state = None
    with _atomic(conn):
        row = conn.execute(
            "SELECT operation_id,order_id,actor_tg_id,bot_id,account_id,panel_id,state,"
            "generation,lock_token,expected_effect_json,evidence_json "
            "FROM panel_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
        if row is None:
            raise OperationError("panel operation does not exist")
        state, current_generation = str(row["state"]), int(row["generation"])
        if state == "succeeded":
            if current_generation != generation:
                raise OperationConflict("success replay belongs to another generation")
            prior = json.loads(str(row["evidence_json"] or "{}"))
            return {"operation_id": operation_id, "state": "succeeded", "replayed": True,
                    "evidence": prior}
        if state not in {"dispatching", "needs_reconciliation"} or current_generation != generation:
            raise OperationConflict("operation state or generation is not finalizable")
        if not str(row["lock_token"] or ""):
            raise OperationConflict("operation lock ownership token is missing")
        outcome = str(safe_evidence.get("outcome", ""))
        reconciled = safe_evidence.get("reconciliation_state") == "applied"
        if outcome != "success" and not reconciled:
            raise OperationConflict("success requires positive applied evidence")
        expected = json.loads(str(row["expected_effect_json"] or "{}"))
        expected_target = expected.get("target", {}) if isinstance(expected, dict) else {}
        observed = safe_evidence.get("expected_effect", safe_evidence.get("actual_effect", {}))
        if not isinstance(observed, Mapping):
            raise OperationConflict("observed expected_effect must be an object")
        if isinstance(observed.get("target"), Mapping):
            observed = observed["target"]
        if expected_target and not _same_effect(expected_target, observed):
            raise OperationConflict("observed panel effect does not match the stored target")

        allowed = {"email", "customer_name", "enable", "total_gb", "expiry_time", "sub_id"}
        mapping = account_mapping if account_mapping is not None else {
            key: value for key, value in observed.items() if key in allowed
        }
        if mapping is not None:
            if not isinstance(mapping, Mapping):
                raise OperationError("account_mapping must be a mapping")
            allowed = {"email", "customer_name", "enable", "total_gb", "expiry_time", "sub_id"}
            unknown = set(mapping) - allowed
            if unknown:
                raise OperationError("account_mapping contains unsupported fields")
            assignments: list[str] = []
            values: list[Any] = []
            for key in ("email", "customer_name", "enable", "total_gb", "expiry_time"):
                if key in mapping:
                    value = mapping[key]
                    if key == "enable" and value not in (0, 1, False, True):
                        raise OperationError("account enable must be boolean")
                    if key in {"total_gb", "expiry_time"} and (
                        isinstance(value, bool) or not isinstance(value, (int, float))
                    ):
                        raise OperationError(f"account {key} must be numeric")
                    if key in {"email", "customer_name"} and not isinstance(value, str):
                        raise OperationError(f"account {key} must be text")
                    assignments.append(f"{key}=?")
                    values.append(int(value) if key == "enable" else value)
            if "sub_id" in mapping:
                raw_sub_id = mapping["sub_id"]
                if not isinstance(raw_sub_id, str) or not raw_sub_id:
                    raise OperationError("sub_id must be non-empty text")
                assignments.extend(["sub_id=NULL", "sub_id_cipher=?"])
                values.append(encrypt_secret(raw_sub_id, "hstagent/columns/v1"))
            if assignments:
                values.extend([int(row["account_id"]), int(row["panel_id"])])
                changed = conn.execute(
                    "UPDATE accounts SET " + ",".join(assignments) +
                    " WHERE id=? AND panel_id=? AND deleted_at IS NULL",
                    tuple(values),
                )
                if changed.rowcount != 1:
                    raise OperationConflict("account mapping target is missing or moved to another panel")

        order_id = str(row["order_id"] or "")
        if order_id:
            order = conn.execute(
                "SELECT status FROM payment_orders WHERE order_id=?", (order_id,)
            ).fetchone()
            if order is None:
                raise OperationConflict("operation payment order is missing")
            order_state = str(order[0])
            if order_state == "accepted":
                hold_count = int(conn.execute(
                    "SELECT COUNT(*) FROM financial_holds WHERE order_id=? "
                    "AND state IN ('active','captured')", (order_id,)
                ).fetchone()[0])
                if hold_count:
                    from financial_service import capture_holds
                    capture_holds(conn, order_id=order_id, actor_id=int(row["actor_tg_id"]), now=now)
                else:
                    conn.execute(
                        "UPDATE payment_orders SET status='completed',revision=revision+1 "
                        "WHERE order_id=? AND status='accepted'", (order_id,)
                    )
            elif order_state != "completed":
                raise OperationConflict("payment order is not capturable")
        _insert_operation_notice(
            conn, operation_id=operation_id, bot_id=int(row["bot_id"]),
            recipient_tg_id=int(row["actor_tg_id"]), outcome="succeeded",
        )
        evidence_json = _canonical(safe_evidence)
        changed = conn.execute(
            "UPDATE panel_operations SET state='succeeded',evidence_json=?,lease_until=NULL,"
            "updated_at=? WHERE operation_id=? AND generation=? "
            "AND lock_token=? AND state IN ('dispatching','needs_reconciliation')",
            (evidence_json, now, operation_id, generation, str(row["lock_token"])),
        )
        if changed.rowcount != 1:
            raise OperationConflict("operation generation or lock ownership changed")
        replay_state = {"operation_id": operation_id, "state": "succeeded",
                        "replayed": False, "evidence": safe_evidence}
    return replay_state


def finalize_failure(
    conn: sqlite3.Connection, *, operation_id: str, generation: int,
    evidence: Mapping[str, Any], now: int,
) -> dict[str, Any]:
    """Finalize only a proven non-application; UNKNOWN never releases funds or locks."""
    now = _now(now)
    operation_id = str(operation_id or "").strip()
    if not operation_id or isinstance(generation, bool) or not isinstance(generation, int) or generation <= 0:
        raise OperationError("operation identity and generation are required")
    safe_evidence = _finalization_evidence(evidence)
    if safe_evidence.get("not_applied_proven") is not True or not str(safe_evidence.get("proof") or "").strip():
        raise OperationConflict("definite failure requires explicit not_applied_proven evidence")
    result: dict[str, Any]
    with _atomic(conn):
        row = conn.execute(
            "SELECT order_id,actor_tg_id,bot_id,state,generation,lock_token "
            "FROM panel_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is None:
            raise OperationError("panel operation does not exist")
        state = str(row["state"])
        if state == "definite_failure":
            if int(row["generation"]) != generation:
                raise OperationConflict("failure replay belongs to another generation")
            return {"operation_id": operation_id, "state": state, "replayed": True}
        if state not in {"dispatching", "needs_reconciliation"} or int(row["generation"]) != generation:
            raise OperationConflict("operation state or generation is not finalizable")
        if not str(row["lock_token"] or ""):
            raise OperationConflict("operation lock ownership token is missing")
        order_id = str(row["order_id"] or "")
        if order_id:
            order = conn.execute(
                "SELECT status FROM payment_orders WHERE order_id=?", (order_id,)
            ).fetchone()
            if order is None:
                raise OperationConflict("operation payment order is missing")
            order_state = str(order[0])
            if order_state == "accepted":
                from financial_service import release_holds
                release_holds(conn, order_id=order_id)
                conn.execute(
                    "UPDATE payment_orders SET status='cancelled',revision=revision+1 "
                    "WHERE order_id=? AND status='accepted'", (order_id,)
                )
            elif order_state == "completed":
                from financial_service import reverse_order
                reversed_result = reverse_order(
                    conn, order_id=order_id, actor_id=int(row["actor_tg_id"]), now=now
                )
                if reversed_result.get("status") not in {"refunded", "operator_exception", "replayed"}:
                    raise OperationConflict("completed payment order could not be reversed")
            elif order_state not in {"cancelled", "closed_refunded", "exception"}:
                raise OperationConflict("payment order is not eligible for definite failure")
        _insert_operation_notice(
            conn, operation_id=operation_id, bot_id=int(row["bot_id"]),
            recipient_tg_id=int(row["actor_tg_id"]), outcome="failed",
        )
        evidence_json = _canonical(safe_evidence)
        changed = conn.execute(
            "UPDATE panel_operations SET state='definite_failure',evidence_json=?,lease_until=NULL,"
            "updated_at=? WHERE operation_id=? AND generation=? AND lock_token=? "
            "AND state IN ('dispatching','needs_reconciliation')",
            (evidence_json, now, operation_id, generation, str(row["lock_token"])),
        )
        if changed.rowcount != 1:
            raise OperationConflict("operation generation or lock ownership changed")
        result = {"operation_id": operation_id, "state": "definite_failure", "replayed": False}
    return result


__all__ = list(__all__) + ["finalize_success", "finalize_failure"]
