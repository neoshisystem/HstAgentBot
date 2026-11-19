"""Lease-fenced Telegram delivery with shared bot/chat throttling and safe retries."""
from __future__ import annotations

import math
import sqlite3
import threading
import time
from datetime import timedelta
from typing import Any, Callable, Mapping

from crypto_store import decrypt_secret
from outbox_service import claim_delivery


class DeliveryError(RuntimeError):
    """A delivery could not be performed safely."""


class StaleDeliveryLease(DeliveryError):
    """A worker no longer owns the delivery lease."""


_RATE_LOCK = threading.Lock()
_LAST_SEND: dict[tuple[int, int], float] = {}


def retry_delay(attempt: int, *, retry_after: Any = None, maximum: int = 3600) -> int:
    """Return an integer delay; Telegram retry_after takes precedence over backoff."""
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise ValueError("attempt must be a positive integer")
    if isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < 1:
        raise ValueError("maximum must be a positive integer")
    if retry_after is not None:
        seconds = retry_after.total_seconds() if isinstance(retry_after, timedelta) else retry_after
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not math.isfinite(seconds):
            raise ValueError("retry_after must be a finite number of seconds")
        return min(maximum, max(1, math.ceil(seconds)))
    return min(maximum, 2 ** min(attempt, 12))


def _default_rate_delay(bot_id: int, chat_id: int, *, interval: float = 1.0) -> float:
    """One in-process budget per (bot, chat); thread/topic is deliberately absent."""
    key = (bot_id, chat_id)
    now = time.monotonic()
    with _RATE_LOCK:
        prior = _LAST_SEND.get(key)
        wait = 0.0 if prior is None else max(0.0, interval - (now - prior))
        if wait == 0.0:
            _LAST_SEND[key] = now
        return wait


def _update_owned(
    conn: sqlite3.Connection, *, delivery_id: int, lease_token: str,
    state: str, next_attempt_at: int | None = None,
    message_id: int | None = None, error_code: str | None = None,
) -> None:
    changed = conn.execute(
        "UPDATE delivery_outbox SET state=?,lease_until=NULL,lease_token=NULL,"
        "next_attempt_at=COALESCE(?,next_attempt_at),message_id=COALESCE(?,message_id),"
        "last_error_code=? WHERE id=? AND state='sending' AND lease_token=?",
        (state, next_attempt_at, message_id, error_code, delivery_id, lease_token),
    )
    if changed.rowcount != 1:
        raise StaleDeliveryLease("delivery lease changed before result could be recorded")


def _notify_backlog(
    callback: Callable[[int, int, Mapping[str, Any]], Any] | None,
    *, bot_id: int, delivery_id: int, state: str, attempts: int,
) -> None:
    if callback is None:
        return
    try:
        import config
        root_id = int(getattr(config, "ADMIN_TELEGRAM_ID", 0) or 0)
    except Exception:
        root_id = 0
    if root_id > 0:
        # Deliberately excludes resource payloads, bot credentials and customer secrets.
        callback(bot_id, root_id, {
            "delivery_id": delivery_id, "state": state, "attempts": attempts,
        })


def deliver_one(
    conn: sqlite3.Connection, *, delivery_id: int, now: int,
    send: Callable[[int, str, int, int | None, Mapping[str, Any]], Any],
    resolve_resource: Callable[[sqlite3.Connection, Mapping[str, Any]], Mapping[str, Any]],
    authorize_delivery: Callable[[sqlite3.Connection, Mapping[str, Any]], bool],
    rate_limiter: Callable[[int, int], int | float] | None = None,
    notify_backlog: Callable[[int, int, Mapping[str, Any]], Any] | None = None,
    lease_seconds: int = 30,
) -> dict[str, Any]:
    """Claim, re-authorize and send one reference-backed delivery outside transactions.

    The ACL callback must perform a fresh object-level authorization immediately
    before resolving any private resource. Send timeouts are UNKNOWN and never
    trigger financial compensation or automatic duplicate delivery.
    """
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise DeliveryError("now must be a non-negative UTC epoch integer")
    if not callable(send) or not callable(resolve_resource) or not callable(authorize_delivery):
        raise TypeError("send, resolve_resource and authorize_delivery callbacks are required")
    claim = claim_delivery(conn, delivery_id=delivery_id, now=now, lease_seconds=lease_seconds)
    if claim is None:
        return {"delivery_id": delivery_id, "state": "not_claimed"}

    delivery_id = int(claim["id"])
    bot_id, chat_id = int(claim["bot_id"]), int(claim["chat_id"])
    thread_id = int(claim["thread_id"]) if claim["thread_id"] is not None else None
    lease_token = str(claim["lease_token"])
    attempts = int(claim["attempts"])

    def record(state: str, *, next_at: int | None = None, error: str | None = None,
               message_id: int | None = None) -> dict[str, Any]:
        with conn:
            _update_owned(
                conn, delivery_id=delivery_id, lease_token=lease_token, state=state,
                next_attempt_at=next_at, message_id=message_id, error_code=error,
            )
        if state in {"pending", "delivery_unknown", "delivery_blocked"}:
            _notify_backlog(
                notify_backlog, bot_id=bot_id, delivery_id=delivery_id,
                state=state, attempts=attempts,
            )
        return {"delivery_id": delivery_id, "state": state, "attempts": attempts}

    try:
        allowed = authorize_delivery(conn, claim)
    except Exception:
        return record("delivery_blocked", error="acl_check_failed")
    if allowed is not True:
        return record("delivery_blocked", error="acl_revoked")

    bot = conn.execute(
        "SELECT token_cipher,status FROM bot_registry WHERE bot_id=?", (bot_id,)
    ).fetchone()
    if bot is None or str(bot["status"]) != "active":
        # A disabled bot retains queued work; it does not erase the row.
        return record("pending", next_at=now, error="bot_disabled")
    try:
        token_cipher = str(bot["token_cipher"] or "")
        if not token_cipher:
            raise ValueError("bot credential unavailable")
        token = decrypt_secret(token_cipher, "hstagent/columns/v1")
        resource = resolve_resource(conn, claim)
        if not isinstance(resource, Mapping):
            raise TypeError("resource resolver must return a mapping")
        resource = dict(resource)
        if "file_id" in resource and int(resource.get("file_bot_id") or 0) != bot_id:
            return record("delivery_blocked", error="file_id_bot_mismatch")
    except Exception:
        return record("delivery_blocked", error="resource_or_credential_unavailable")

    if rate_limiter is None:
        wait = _default_rate_delay(bot_id, chat_id)
    else:
        wait = rate_limiter(bot_id, chat_id)
    if isinstance(wait, bool) or not isinstance(wait, (int, float)) or not math.isfinite(wait) or wait < 0:
        return record("delivery_blocked", error="invalid_rate_limit_result")
    if wait > 0:
        return record("pending", next_at=now + max(1, math.ceil(wait)), error="rate_limited")

    try:
        response = send(bot_id, token, chat_id, thread_id, resource)
    except Exception as exc:
        retry_after = getattr(exc, "retry_after", None)
        if retry_after is not None:
            delay = retry_delay(attempts, retry_after=retry_after)
            return record("pending", next_at=now + delay, error="telegram_rate_limited")
        name = type(exc).__name__.casefold()
        if isinstance(exc, (TimeoutError,)) or "timeout" in name or name in {"timedout", "networktimeout"}:
            return record("delivery_unknown", error="send_timeout_effect_unknown")
        if "forbidden" in name or bool(getattr(exc, "forbidden", False)):
            return record("delivery_blocked", error="telegram_forbidden")
        delay = retry_delay(attempts)
        return record("pending", next_at=now + delay, error="telegram_transient_error")

    message_id = response.get("message_id") if isinstance(response, Mapping) else getattr(response, "message_id", response)
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id <= 0:
        return record("delivery_unknown", error="send_result_missing_message_id")
    return record("sent", message_id=message_id, error=None)


__all__ = [
    "DeliveryError", "StaleDeliveryLease", "retry_delay", "deliver_one",
]
