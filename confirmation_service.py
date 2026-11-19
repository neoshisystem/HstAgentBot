"""One-use actor/bot-bound confirmations for HstAgentBot v5.2."""
from __future__ import annotations
import json
import re
import secrets
import sqlite3
import uuid
from collections.abc import Mapping
from typing import Any

_NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{24}$")
_FORBIDDEN_KEY_PARTS = (
    "secret", "password", "token", "cipher", "authorization",
    "credential", "private_key", "api_key", "sub_id", "session",
)

class ConfirmationError(ValueError):
    """A confirmation cannot be created or consumed safely."""
class ConfirmationMismatch(ConfirmationError):
    """The confirmation belongs to another actor, bot or action."""
class ConfirmationExpired(ConfirmationError):
    """The confirmation expired before consumption."""
class ConfirmationStale(ConfirmationError):
    """The confirmed resource revision no longer matches."""
class ConfirmationConsumed(ConfirmationError):
    """A consumed confirmation has no resolvable operation."""

def _positive(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfirmationError(f"{name} must be a positive integer")
    return value

def _timestamp(value: Any, name: str = "now") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ConfirmationError(f"{name} must be a non-negative epoch-seconds integer")
    return value

def _public_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 12:
        raise ConfirmationError("confirmation payload nesting is too deep")
    if value is None or isinstance(value, (str, int, float, bool)):
        if isinstance(value, float):
            raise ConfirmationError("floating-point values are not accepted in confirmation snapshots")
        if isinstance(value, str) and len(value) > 2048:
            raise ConfirmationError("confirmation snapshot string is too long")
        return value
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 100:
                raise ConfirmationError("confirmation payload keys must be short strings")
            if any(part in key.casefold() for part in _FORBIDDEN_KEY_PARTS):
                raise ConfirmationError(f"sensitive field is not allowed in confirmation payload: {key}")
            result[key] = _public_value(item, depth=depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        if len(value) > 100:
            raise ConfirmationError("confirmation payload list is too large")
        return [_public_value(item, depth=depth + 1) for item in value]
    raise ConfirmationError("confirmation payload must contain JSON-compatible public values")

def _canonical_json(value: Any) -> str:
    safe = _public_value(value)
    try:
        return json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ConfirmationError("confirmation payload is not canonical JSON") from exc

def _require_transaction(conn: sqlite3.Connection) -> None:
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    if not conn.in_transaction:
        raise ConfirmationError("confirmation changes require the caller-owned transaction")

def create_confirmation(
    conn: sqlite3.Connection, *, actor_tg_id: int, bot_id: int, action: str,
    target_type: str, target_id: str | int, expected_revision: int,
    payload: Mapping[str, Any], now: int, ttl_seconds: int = 300,
) -> dict[str, Any]:
    """Persist a cryptographically random 24-character nonce in the caller transaction."""
    _require_transaction(conn)
    actor_tg_id = _positive(actor_tg_id, "actor_tg_id")
    bot_id = _positive(bot_id, "bot_id")
    expected_revision = _positive(expected_revision, "expected_revision")
    now = _timestamp(now)
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 3600:
        raise ConfirmationError("ttl_seconds must be between 1 and 3600")
    action = str(action or "").strip()
    target_type = str(target_type or "").strip()
    target_id = str(target_id).strip()
    if not action or len(action) > 100 or not target_type or len(target_type) > 60:
        raise ConfirmationError("action and target_type are required")
    if not target_id or len(target_id) > 200:
        raise ConfirmationError("target_id is required and must be short")
    if not isinstance(payload, Mapping):
        raise ConfirmationError("payload must be a mapping of non-secret snapshot values")
    payload_json = _canonical_json(payload)
    expires_at = now + ttl_seconds
    for _ in range(5):
        nonce = secrets.token_urlsafe(18)
        if not _NONCE_RE.fullmatch(nonce):
            continue
        try:
            conn.execute(
                "INSERT INTO action_confirmations("
                "nonce,actor_tg_id,bot_id,action,target_type,target_id,expected_revision,"
                "payload_json,expires_at,consumed_at,operation_id"
                ") VALUES(?,?,?,?,?,?,?,?,?,NULL,NULL)",
                (nonce, actor_tg_id, bot_id, action, target_type, target_id,
                 expected_revision, payload_json, expires_at),
            )
        except sqlite3.IntegrityError:
            continue
        callback_ok, callback_no = f"v52:ok:{nonce}", f"v52:no:{nonce}"
        if max(len(callback_ok.encode("utf-8")), len(callback_no.encode("utf-8"))) >= 64:
            raise AssertionError("confirmation callback exceeds Telegram's 64-byte limit")
        return {
            "nonce": nonce, "callback_ok": callback_ok, "callback_no": callback_no,
            "expires_at": expires_at, "expected_revision": expected_revision, "replayed": False,
        }
    raise ConfirmationError("could not allocate a unique confirmation nonce")

def consume_confirmation(
    conn: sqlite3.Connection, *, nonce: str, actor_tg_id: int, bot_id: int,
    action: str, target_type: str, target_id: str | int, expected_revision: int,
    now: int, operation_id: str | None = None,
) -> dict[str, Any]:
    """CAS-consume a valid confirmation; a replay resolves to its original operation."""
    _require_transaction(conn)
    actor_tg_id = _positive(actor_tg_id, "actor_tg_id")
    bot_id = _positive(bot_id, "bot_id")
    expected_revision = _positive(expected_revision, "expected_revision")
    now = _timestamp(now)
    if not isinstance(nonce, str) or not _NONCE_RE.fullmatch(nonce):
        raise ConfirmationError("nonce must be exactly 24 URL-safe characters")
    action, target_type, target_id = str(action or "").strip(), str(target_type or "").strip(), str(target_id).strip()
    row = conn.execute(
        "SELECT actor_tg_id,bot_id,action,target_type,target_id,expected_revision,"
        "expires_at,consumed_at,operation_id FROM action_confirmations WHERE nonce=?",
        (nonce,),
    ).fetchone()
    if row is None:
        raise ConfirmationMismatch("confirmation does not exist")
    if (int(row[0]) != actor_tg_id or int(row[1]) != bot_id or str(row[2]) != action
        or str(row[3]) != target_type or str(row[4]) != target_id):
        raise ConfirmationMismatch("confirmation actor, bot, action or target does not match")
    if int(row[5]) != expected_revision:
        raise ConfirmationStale("confirmation preview revision does not match")
    if row[7] is not None:
        prior_operation = str(row[8] or "")
        if not prior_operation:
            raise ConfirmationConsumed("consumed confirmation has no operation_id")
        return {"nonce": nonce, "operation_id": prior_operation, "consumed_at": int(row[7]), "replayed": True}
    if int(row[6]) <= now:
        raise ConfirmationExpired("confirmation has expired")
    selected_operation = str(operation_id or uuid.uuid4())
    if len(selected_operation) > 200 or not selected_operation:
        raise ConfirmationError("operation_id is invalid")
    changed = conn.execute(
        "UPDATE action_confirmations SET consumed_at=?,operation_id=? "
        "WHERE nonce=? AND consumed_at IS NULL AND actor_tg_id=? AND bot_id=? "
        "AND expected_revision=? AND expires_at>?",
        (now, selected_operation, nonce, actor_tg_id, bot_id, expected_revision, now),
    )
    if changed.rowcount != 1:
        latest = conn.execute(
            "SELECT consumed_at,operation_id FROM action_confirmations WHERE nonce=?", (nonce,)
        ).fetchone()
        if latest is not None and latest[0] is not None and latest[1]:
            return {"nonce": nonce, "operation_id": str(latest[1]), "consumed_at": int(latest[0]), "replayed": True}
        raise ConfirmationStale("confirmation changed before its compare-and-swap")
    return {"nonce": nonce, "operation_id": selected_operation, "consumed_at": now, "replayed": False}

__all__ = [
    "ConfirmationError", "ConfirmationMismatch", "ConfirmationExpired",
    "ConfirmationStale", "ConfirmationConsumed", "create_confirmation", "consume_confirmation",
]
