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
