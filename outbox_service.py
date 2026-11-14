"""Durable, idempotent and privacy-aware Telegram delivery outbox."""
from __future__ import annotations

import secrets
import sqlite3
from typing import Any

from crypto_store import decrypt_secret, encrypt_secret


class OutboxError(ValueError):
    """A delivery cannot be queued or claimed safely."""


def _positive(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise OutboxError(f"{name} must be a positive integer")
    return value


def _chat_id(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value == 0:
        raise OutboxError("chat_id must be a non-zero Telegram chat integer")
    return value


def _atomic(conn: sqlite3.Connection):
    class Atomic:
        def __enter__(self):
            if conn.in_transaction:
                self.point = "w09_outbox_" + secrets.token_hex(4)
                conn.execute(f"SAVEPOINT {self.point}")
            else:
                self.point = None
                conn.execute("BEGIN IMMEDIATE")
            return self

        def __exit__(self, kind, value, traceback):
            if self.point:
                if kind:
                    conn.execute(f"ROLLBACK TO SAVEPOINT {self.point}")
                conn.execute(f"RELEASE SAVEPOINT {self.point}")
            elif kind:
                conn.rollback()
            else:
                conn.commit()
            return False
    return Atomic()


def enqueue_delivery(
    conn: sqlite3.Connection, *, event_key: str, bot_id: int, chat_id: int,
    thread_id: int | None, purpose: str, resource_type: str, resource_id: str,
) -> dict[str, Any]:
    """Store only a resource reference; duplicate events resolve to the same row.

    Abuse reports are forcibly routed through the configured primary bot to the
    configured root administrator DM, without a thread or group fallback.
    """
    event_key = str(event_key or "").strip()
    purpose = str(purpose or "").strip().lower()
    resource_type = str(resource_type or "").strip().lower()
    resource_id = str(resource_id or "").strip()
    if not event_key or len(event_key) > 240 or not purpose or len(purpose) > 80:
        raise OutboxError("event_key and purpose are required")
    if not resource_type or len(resource_type) > 80 or not resource_id or len(resource_id) > 240:
        raise OutboxError("resource reference is required")
    bot_id, chat_id = _positive(bot_id, "bot_id"), _chat_id(chat_id)
    if thread_id is not None:
        thread_id = _positive(thread_id, "thread_id")

    if purpose in {"abuse", "abusereport", "abuse_report"}:
        try:
            import config
            root_id = _positive(getattr(config, "ADMIN_TELEGRAM_ID", 0), "ADMIN_TELEGRAM_ID")
        except Exception as exc:
            raise OutboxError("root DM identity is unavailable for abuse routing") from exc
        primary = conn.execute(
            "SELECT bot_id FROM bot_registry WHERE is_primary=1 ORDER BY bot_id LIMIT 1"
        ).fetchone()
        if primary is None:
            raise OutboxError("primary bot is unavailable for abuse routing")
        bot_id, chat_id, thread_id = int(primary[0]), root_id, None

    resource_cipher = encrypt_secret(resource_id, "hstagent/columns/v1")
    with _atomic(conn):
        conn.execute(
            "INSERT INTO delivery_outbox(event_key,bot_id,chat_id,thread_id,purpose,"
            "resource_type,resource_id,state,attempts,next_attempt_at) "
            "VALUES(?,?,?,?,?,?,?,'pending',0,0) "
            "ON CONFLICT(event_key,bot_id,chat_id,purpose) DO NOTHING",
            (event_key, bot_id, chat_id, thread_id, purpose, resource_type, resource_cipher),
        )
        row = conn.execute(
            "SELECT id,event_key,bot_id,chat_id,thread_id,purpose,resource_type,resource_id,state "
            "FROM delivery_outbox WHERE event_key=? AND bot_id=? AND chat_id=? AND purpose=?",
            (event_key, bot_id, chat_id, purpose),
        ).fetchone()
        try:
            stored_resource = decrypt_secret(str(row[7]), "hstagent/columns/v1") if row else ""
        except Exception as exc:
            raise OutboxError("stored resource reference failed authentication") from exc
        expected = (event_key, bot_id, chat_id, thread_id, purpose, resource_type, resource_id)
        actual = (str(row[1]), int(row[2]), int(row[3]), row[4],
                  str(row[5]), str(row[6]), stored_resource) if row else None
        if row is None or actual != expected:
            raise OutboxError("delivery idempotency key conflicts with another resource or route")
        return {"delivery_id": int(row[0]), "state": str(row[8]), "replayed": str(row[8]) != "pending"}


def claim_delivery(
    conn: sqlite3.Connection, *, delivery_id: int, now: int, lease_seconds: int = 30,
) -> dict[str, Any] | None:
    """Claim one pending row with a compare-and-swap lease; never replay expired sends."""
    delivery_id = _positive(delivery_id, "delivery_id")
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise OutboxError("now must be a non-negative UTC epoch integer")
    if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or not 1 <= lease_seconds <= 300:
        raise OutboxError("lease_seconds must be between 1 and 300")
    with _atomic(conn):
        row = conn.execute(
            "SELECT d.id,d.event_key,d.bot_id,d.chat_id,d.thread_id,d.purpose,d.resource_type,"
            "d.resource_id,d.state,d.attempts,d.next_attempt_at,d.lease_until,d.lease_token,"
            "b.status AS bot_status FROM delivery_outbox d "
            "JOIN bot_registry b ON b.bot_id=d.bot_id WHERE d.id=?",
            (delivery_id,),
        ).fetchone()
        if row is None:
            raise OutboxError("delivery row or its registered bot does not exist")
        state = str(row["state"])
        if state == "sending":
            lease_until = int(row["lease_until"]) if row["lease_until"] is not None else 0
            if lease_until <= now:
                conn.execute(
                    "UPDATE delivery_outbox SET state='delivery_unknown',lease_until=NULL,"
                    "lease_token=NULL,last_error_code='lease_expired' "
                    "WHERE id=? AND state='sending' AND lease_until<=?",
                    (delivery_id, now),
                )
            return None
        if state != "pending" or int(row["next_attempt_at"] or 0) > now:
            return None
        if str(row["bot_status"]) != "active":
            return None
        lease_token = secrets.token_urlsafe(24)
        changed = conn.execute(
            "UPDATE delivery_outbox SET state='sending',attempts=attempts+1,"
            "lease_token=?,lease_until=? WHERE id=? AND state='pending' AND next_attempt_at<=? "
            "AND EXISTS(SELECT 1 FROM bot_registry b WHERE b.bot_id=delivery_outbox.bot_id "
            "AND b.status='active')",
            (lease_token, now + lease_seconds, delivery_id, now),
        )
        if changed.rowcount != 1:
            return None
        claimed = conn.execute(
            "SELECT id,event_key,bot_id,chat_id,thread_id,purpose,resource_type,resource_id,"
            "state,attempts,next_attempt_at,lease_until,lease_token FROM delivery_outbox WHERE id=?",
            (delivery_id,),
        ).fetchone()
        if claimed is None:
            return None
        result = dict(claimed)
        try:
            result["resource_id"] = decrypt_secret(
                str(result["resource_id"]), "hstagent/columns/v1"
            )
        except Exception:
            conn.execute(
                "UPDATE delivery_outbox SET state='delivery_blocked',lease_token=NULL,"
                "lease_until=NULL,last_error_code='resource_reference_authentication_failed' "
                "WHERE id=? AND state='sending' AND lease_token=?",
                (delivery_id, lease_token),
            )
            return None
        return result


__all__ = ["OutboxError", "enqueue_delivery", "claim_delivery"]
