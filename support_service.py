"""Private support-ticket workflows with explicit visibility and offline-safe snapshots."""
from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Mapping
import json
import secrets
import sqlite3
import time
from typing import Any

import config
import database
from access_service import authorize_account
from crypto_store import decrypt_secret, encrypt_secret
from outbox_service import enqueue_delivery

_LABEL = "hstagent/columns/v1"
_TICKET_TYPES = frozenset({"general", "finance", "disorder", "delete_request"})
_VISIBILITIES = frozenset({"customer", "internal", "root_only"})


class SupportError(ValueError):
    """A support operation cannot be completed safely."""


def _value(ctx: Any, name: str, default=None):
    if isinstance(ctx, Mapping):
        return ctx.get(name, default)
    return getattr(ctx, name, default)


def _positive(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise SupportError(f"{name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise SupportError(f"{name} must be a positive integer") from None
    if result <= 0:
        raise SupportError(f"{name} must be a positive integer")
    return result


def _actor_id(ctx: Any) -> int:
    return _positive(_value(ctx, "actor_tg_id", _value(ctx, "telegram_id")), "actor_tg_id")


def _root_id() -> int:
    value = getattr(config, "ADMIN_TELEGRAM_ID", 0)
    if isinstance(value, bool):
        return 0
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0


def _is_root(ctx: Any) -> bool:
    return _root_id() > 0 and _actor_id(ctx) == _root_id()


def _bot_id(ctx: Any, explicit: int | None = None) -> int:
    value = explicit if explicit is not None else _value(ctx, "bot_id")
    return _positive(value, "bot_id")


def _timestamp(now: int | None = None) -> int:
    result = int(time.time()) if now is None else now
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        raise SupportError("now must be a non-negative UTC epoch-seconds integer")
    return result


@contextmanager
def _atomic(conn: sqlite3.Connection):
    if conn.in_transaction:
        point = "w14_support_" + secrets.token_hex(5)
        conn.execute(f"SAVEPOINT {point}")
        try:
            yield
            conn.execute(f"RELEASE SAVEPOINT {point}")
        except Exception:
            conn.execute(f"ROLLBACK TO SAVEPOINT {point}")
            conn.execute(f"RELEASE SAVEPOINT {point}")
            raise
    else:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise


def _validate_bot(conn: sqlite3.Connection, bot_id: int) -> None:
    row = conn.execute(
        "SELECT status FROM bot_registry WHERE bot_id=?", (bot_id,)
    ).fetchone()
    if row is None or str(row["status"]) != "active":
        raise SupportError("the selected Telegram bot is not registered and active")


def _rep_ids_for_actor(conn: sqlite3.Connection, actor_id: int, requested=None) -> set[int]:
    if requested is not None:
        try:
            rep_id = _positive(requested, "rep_id")
        except SupportError:
            return set()
        rows = conn.execute(
            "SELECT id FROM representatives WHERE id=? AND telegram_id=? "
            "AND is_active=1 AND deleted_at IS NULL",
            (rep_id, actor_id),
        ).fetchall()
        return {int(row["id"]) for row in rows}
    rows = conn.execute(
        "SELECT id FROM representatives WHERE telegram_id=? "
        "AND is_active=1 AND deleted_at IS NULL ORDER BY id",
        (actor_id,),
    ).fetchall()
    return {int(row["id"]) for row in rows}


def _operator_for_ticket(conn: sqlite3.Connection, ctx: Any, ticket) -> bool:
    if _is_root(ctx):
        return True
    assigned = str(ticket["assigned_key"] or "")
    if not assigned.startswith("rep:"):
        return False
    try:
        assigned_id = int(assigned[4:])
    except (TypeError, ValueError):
        return False
    return assigned_id in _rep_ids_for_actor(
        conn, _actor_id(ctx), _value(ctx, "rep_id")
    )


def _insert_message(
    conn: sqlite3.Connection, *, ticket_id: str, actor_tg_id: int,
    visibility: str, body: str, attachment_file_id: str | None,
    attachment_bot_id: int | None, now: int,
) -> int:
    if visibility not in _VISIBILITIES:
        raise SupportError("unsupported message visibility")
    if not isinstance(body, str) or len(body) > 8000:
        raise SupportError("message body must be text of at most 8000 characters")
    if not body.strip() and not attachment_file_id:
        raise SupportError("a message must contain text or an attachment")
    if attachment_file_id is not None:
        if not isinstance(attachment_file_id, str) or not attachment_file_id.strip():
            raise SupportError("attachment_file_id must be non-empty text")
        if len(attachment_file_id) > 2048:
            raise SupportError("attachment reference is too long")
        attachment_cipher = encrypt_secret(attachment_file_id, _LABEL)
        attachment_bot_id = _positive(attachment_bot_id, "attachment_bot_id")
    else:
        attachment_cipher = None
        attachment_bot_id = None
    body_cipher = encrypt_secret(body, _LABEL)
    cursor = conn.execute(
        "INSERT INTO support_messages(ticket_id,actor_tg_id,visibility,body_cipher,"
        "attachment_file_id,attachment_bot_id,created_at) VALUES(?,?,?,?,?,?,?)",
        (ticket_id, actor_tg_id, visibility, body_cipher, attachment_cipher,
         attachment_bot_id, now),
    )
    return int(cursor.lastrowid)


def _decode_message(row, ticket_bot_id: int) -> dict:
    try:
        body = decrypt_secret(str(row["body_cipher"]), _LABEL)
        attachment = (
            decrypt_secret(str(row["attachment_file_id"]), _LABEL)
            if row["attachment_file_id"] else None
        )
    except Exception as exc:
        raise SupportError("stored support message failed authentication") from exc
    attachment_bot = (
        int(row["attachment_bot_id"]) if row["attachment_bot_id"] is not None else None
    )
    reupload = bool(attachment and attachment_bot != int(ticket_bot_id))
    return {
        "id": int(row["id"]),
        "ticket_id": str(row["ticket_id"]),
        "actor_tg_id": int(row["actor_tg_id"]),
        "visibility": str(row["visibility"]),
        "body": body,
        "attachment_file_id": None if reupload else attachment,
        "attachment_bot_id": attachment_bot,
        "attachment_reupload_required": reupload,
        "created_at": int(row["created_at"]),
    }


def build_diagnostic_snapshot(
    ctx: Any, account_id: int, *, panel_reader=None, now: int | None = None
) -> dict:
    """Read a minimal panel snapshot outside a write transaction; never include subscription secrets."""
    exact_id = _positive(account_id, "account_id")
    observed_at = _timestamp(now)
    if not authorize_account(ctx, exact_id, "view_stats"):
        raise PermissionError("actor cannot view diagnostics for this account")

    with database.get_db() as conn:
        row = conn.execute(
            "SELECT id,account_code,panel_id,deleted_at FROM accounts WHERE id=?",
            (exact_id,),
        ).fetchone()
    if row is None or row["deleted_at"] is not None:
        raise SupportError("account does not exist or is deleted")

    snapshot = {
        "account_id": exact_id,
        "account_code": str(row["account_code"]),
        "panel_id": int(row["panel_id"]) if row["panel_id"] is not None else None,
        "observed_at": observed_at,
        "state": "unavailable",
        "unavailable": True,
        "app": None,
        "last_seen": None,
    }
    if panel_reader is None:
        snapshot["unavailable_reason"] = "reader_unconfigured"
        return snapshot

    # The injected reader receives no subId, UUID, token, or open SQL connection.
    try:
        result = panel_reader({
            "account_id": exact_id,
            "account_code": snapshot["account_code"],
            "panel_id": snapshot["panel_id"],
            "observed_at": observed_at,
        })
        if not isinstance(result, Mapping):
            raise SupportError("diagnostic reader returned an invalid result")
        if result.get("available", True) is False:
            raise TimeoutError("panel unavailable")
        app = result.get("app")
        last_seen = result.get("last_seen")
        snapshot.update({
            "state": "available",
            "unavailable": False,
            "app": str(app)[:120] if app is not None else None,
            "last_seen": str(last_seen)[:120] if last_seen is not None else None,
        })
    except Exception:
        snapshot["unavailable_reason"] = "panel_unavailable"
    return snapshot


def _assignment(conn: sqlite3.Connection, account) -> tuple[str, int | None]:
    seller_key = str(account["seller_key"] or "root")
    rep_id = account["rep_id"]
    if seller_key.startswith("rep:"):
        try:
            selected = int(seller_key[4:])
        except (TypeError, ValueError):
            return "root", None
        if rep_id is None or int(rep_id) != selected:
            return "root", None
        rep = conn.execute(
            "SELECT id FROM representatives WHERE id=? AND is_active=1 "
            "AND deleted_at IS NULL",
            (selected,),
        ).fetchone()
        if rep is not None:
            return f"rep:{selected}", selected
    return "root", None


def create_ticket(
    ctx: Any, account_id: int, body: str = "", *,
    ticket_type: str = "disorder", attachment_file_id: str | None = None,
    attachment_bot_id: int | None = None, panel_reader=None, now: int | None = None,
) -> dict:
    """Create a support ticket even when the panel snapshot is unavailable."""
    actor_id = _actor_id(ctx)
    exact_id = _positive(account_id, "account_id")
    bot_id = _bot_id(ctx)
    selected_type = str(ticket_type or "").strip().lower()
    if selected_type not in _TICKET_TYPES:
        raise SupportError("unsupported support ticket type")
    if not authorize_account(ctx, exact_id, "view_stats"):
        raise PermissionError("actor cannot open a ticket for this account")
    timestamp = _timestamp(now)
    snapshot = build_diagnostic_snapshot(
        ctx, exact_id, panel_reader=panel_reader, now=timestamp
    )
    snapshot_cipher = encrypt_secret(
        json.dumps(snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        _LABEL,
    )
    ticket_id = secrets.token_urlsafe(18)
    source_bot = _positive(attachment_bot_id, "attachment_bot_id") if attachment_bot_id is not None else bot_id

    with database.get_db() as conn, _atomic(conn):
        _validate_bot(conn, bot_id)
        account = conn.execute(
            "SELECT id,rep_id,seller_key,deleted_at FROM accounts WHERE id=?",
            (exact_id,),
        ).fetchone()
        if account is None or account["deleted_at"] is not None:
            raise SupportError("account does not exist or is deleted")
        assigned_key, assigned_rep = _assignment(conn, account)
        conn.execute(
            "INSERT INTO support_tickets(ticket_id,sender_tg_id,bot_id,account_id,rep_id,"
            "ticket_type,assigned_key,status,snapshot_cipher,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,'unseen',?,?,?)",
            (ticket_id, actor_id, bot_id, exact_id, assigned_rep,
             selected_type, assigned_key, snapshot_cipher, timestamp, timestamp),
        )
        message_id = _insert_message(
            conn, ticket_id=ticket_id, actor_tg_id=actor_id,
            visibility="customer", body=body,
            attachment_file_id=attachment_file_id,
            attachment_bot_id=source_bot if attachment_file_id else None,
            now=timestamp,
        )
    return {
        "ticket_id": ticket_id,
        "ticket_type": selected_type,
        "assigned_key": assigned_key,
        "status": "unseen",
        "snapshot": snapshot,
        "initial_message_id": message_id,
    }


def _visible_ticket(conn: sqlite3.Connection, ctx: Any, ticket) -> bool:
    return (
        _is_root(ctx)
        or int(ticket["sender_tg_id"]) == _actor_id(ctx)
        or _operator_for_ticket(conn, ctx, ticket)
    )


def list_tickets(ctx: Any, *, status: str | None = None) -> list[dict]:
    """List generic tickets; abuse reports are excluded from every non-root view."""
    actor_id = _actor_id(ctx)
    with database.get_db() as conn:
        if _is_root(ctx):
            rows = conn.execute(
                "SELECT ticket_id,sender_tg_id,bot_id,account_id,rep_id,ticket_type,"
                "assigned_key,status,created_at,updated_at FROM support_tickets "
                "WHERE (? IS NULL OR status=?) ORDER BY created_at DESC,ticket_id",
                (status, status),
            ).fetchall()
        else:
            reps = _rep_ids_for_actor(conn, actor_id, _value(ctx, "rep_id"))
            keys = sorted(f"rep:{item}" for item in reps)
            clauses = ["sender_tg_id=?"]
            params: list[Any] = [actor_id]
            if keys:
                clauses.append("assigned_key IN (" + ",".join("?" for _ in keys) + ")")
                params.extend(keys)
            params.extend([status, status])
            rows = conn.execute(
                "SELECT ticket_id,sender_tg_id,bot_id,account_id,rep_id,ticket_type,"
                "assigned_key,status,created_at,updated_at FROM support_tickets "
                "WHERE ticket_type<>'abuse_report' AND (" + " OR ".join(clauses) + ") "
                "AND (? IS NULL OR status=?) ORDER BY created_at DESC,ticket_id",
                params,
            ).fetchall()
        return [dict(row) for row in rows]


def count_tickets(ctx: Any, *, status: str | None = None) -> int:
    """Count visible generic tickets without leaking abuse-report volume."""
    actor_id = _actor_id(ctx)
    with database.get_db() as conn:
        if _is_root(ctx):
            row = conn.execute(
                "SELECT COUNT(*) FROM support_tickets WHERE (? IS NULL OR status=?)",
                (status, status),
            ).fetchone()
        else:
            reps = _rep_ids_for_actor(conn, actor_id, _value(ctx, "rep_id"))
            keys = sorted(f"rep:{item}" for item in reps)
            clauses = ["sender_tg_id=?"]
            params: list[Any] = [actor_id]
            if keys:
                clauses.append("assigned_key IN (" + ",".join("?" for _ in keys) + ")")
                params.extend(keys)
            params.extend([status, status])
            row = conn.execute(
                "SELECT COUNT(*) FROM support_tickets WHERE ticket_type<>'abuse_report' "
                "AND (" + " OR ".join(clauses) + ") AND (? IS NULL OR status=?)",
                params,
            ).fetchone()
        return int(row[0])


def search_tickets(ctx: Any, query: str) -> list[dict]:
    """Search only tickets visible to the actor; non-root search never touches abuse rows."""
    needle = str(query or "").strip()
    if not needle:
        return []
    actor_id = _actor_id(ctx)
    with database.get_db() as conn:
        if _is_root(ctx):
            rows = conn.execute(
                "SELECT ticket_id,sender_tg_id,bot_id,account_id,rep_id,ticket_type,"
                "assigned_key,status,created_at,updated_at FROM support_tickets "
                "WHERE ticket_id LIKE ? OR CAST(account_id AS TEXT)=? "
                "ORDER BY created_at DESC",
                (f"%{needle}%", needle),
            ).fetchall()
        else:
            reps = _rep_ids_for_actor(conn, actor_id, _value(ctx, "rep_id"))
            keys = sorted(f"rep:{item}" for item in reps)
            clauses = ["sender_tg_id=?"]
            params: list[Any] = [actor_id]
            if keys:
                clauses.append("assigned_key IN (" + ",".join("?" for _ in keys) + ")")
                params.extend(keys)
            params.extend([f"%{needle}%", needle])
            rows = conn.execute(
                "SELECT ticket_id,sender_tg_id,bot_id,account_id,rep_id,ticket_type,"
                "assigned_key,status,created_at,updated_at FROM support_tickets "
                "WHERE ticket_type<>'abuse_report' AND (" + " OR ".join(clauses) + ") "
                "AND (ticket_id LIKE ? OR CAST(account_id AS TEXT)=?) "
                "ORDER BY created_at DESC",
                params,
            ).fetchall()
        return [dict(row) for row in rows]


def list_ticket_messages(ctx: Any, ticket_id: str) -> list[dict]:
    exact = str(ticket_id or "").strip()
    with database.get_db() as conn:
        ticket = conn.execute(
            "SELECT * FROM support_tickets WHERE ticket_id=? AND ticket_type<>'abuse_report'",
            (exact,),
        ).fetchone()
        if ticket is None:
            raise SupportError("support ticket does not exist")
        operator = _operator_for_ticket(conn, ctx, ticket)
        sender = int(ticket["sender_tg_id"]) == _actor_id(ctx)
        if not operator and not sender:
            raise PermissionError("actor cannot view this support ticket")
        rows = conn.execute(
            "SELECT * FROM support_messages WHERE ticket_id=? "
            + ("" if operator else "AND visibility='customer' ")
            + "ORDER BY id",
            (exact,),
        ).fetchall()
        return [_decode_message(row, int(ticket["bot_id"])) for row in rows]


def export_ticket(ctx: Any, ticket_id: str) -> dict:
    exact = str(ticket_id or "").strip()
    with database.get_db() as conn:
        ticket = conn.execute(
            "SELECT * FROM support_tickets WHERE ticket_id=? AND ticket_type<>'abuse_report'",
            (exact,),
        ).fetchone()
        if ticket is None:
            raise SupportError("support ticket does not exist")
        if not _visible_ticket(conn, ctx, ticket):
            raise PermissionError("actor cannot export this support ticket")
        data = {
            key: ticket[key]
            for key in (
                "ticket_id", "sender_tg_id", "bot_id", "account_id", "rep_id",
                "ticket_type", "assigned_key", "status", "created_at", "updated_at",
            )
        }
    messages = list_ticket_messages(ctx, exact)
    return {"ticket": data, "messages": messages}


def reply_ticket(
    ctx: Any, ticket_id: str, body: str, *,
    visibility: str = "customer", attachment_file_id: str | None = None,
    attachment_bot_id: int | None = None, now: int | None = None,
) -> dict:
    actor_id = _actor_id(ctx)
    selected_visibility = str(visibility or "").strip().lower()
    if selected_visibility not in _VISIBILITIES:
        raise SupportError("unsupported message visibility")
    timestamp = _timestamp(now)
    with database.get_db() as conn, _atomic(conn):
        ticket = conn.execute(
            "SELECT * FROM support_tickets WHERE ticket_id=? AND ticket_type<>'abuse_report'",
            (str(ticket_id),),
        ).fetchone()
        if ticket is None:
            raise SupportError("support ticket does not exist")
        root = _is_root(ctx)
        operator = _operator_for_ticket(conn, ctx, ticket)
        sender = int(ticket["sender_tg_id"]) == actor_id
        if not operator and not sender:
            raise PermissionError("actor cannot reply to this support ticket")
        if sender and selected_visibility != "customer":
            raise PermissionError("customers may only add customer-visible messages")
        if selected_visibility == "root_only" and not root:
            raise PermissionError("root-only notes require the configured root")
        message_id = _insert_message(
            conn, ticket_id=str(ticket_id), actor_tg_id=actor_id,
            visibility=selected_visibility, body=body,
            attachment_file_id=attachment_file_id,
            attachment_bot_id=attachment_bot_id or _bot_id(ctx),
            now=timestamp,
        )
        conn.execute(
            "UPDATE support_tickets SET updated_at=? WHERE ticket_id=?",
            (timestamp, str(ticket_id)),
        )
        queued = None
        if selected_visibility == "customer" and operator:
            queued = enqueue_delivery(
                conn,
                event_key=f"support-reply:{ticket_id}:{message_id}",
                bot_id=int(ticket["bot_id"]),
                chat_id=int(ticket["sender_tg_id"]),
                thread_id=None,
                purpose="support_reply",
                resource_type="support_message",
                resource_id=str(message_id),
            )
    return {"message_id": message_id, "visibility": selected_visibility, "delivery": queued}


def escalate_ticket(ctx: Any, ticket_id: str, *, now: int | None = None) -> dict:
    timestamp = _timestamp(now)
    exact = str(ticket_id or "").strip()
    with database.get_db() as conn, _atomic(conn):
        ticket = conn.execute(
            "SELECT * FROM support_tickets WHERE ticket_id=? AND ticket_type<>'abuse_report'",
            (exact,),
        ).fetchone()
        if ticket is None:
            raise SupportError("support ticket does not exist")
        if not (_is_root(ctx) or _operator_for_ticket(conn, ctx, ticket)):
            raise PermissionError("only the assigned seller or root may escalate this ticket")
        changed = conn.execute(
            "UPDATE support_tickets SET assigned_key='root',status='escalated',updated_at=? "
            "WHERE ticket_id=? AND ticket_type<>'abuse_report'",
            (timestamp, exact),
        ).rowcount
        if changed != 1:
            raise SupportError("ticket escalation lost a concurrent update")
        return {"ticket_id": exact, "assigned_key": "root", "status": "escalated"}


__all__ = [
    "SupportError", "build_diagnostic_snapshot", "create_ticket",
    "list_tickets", "count_tickets", "search_tickets", "list_ticket_messages",
    "export_ticket", "reply_ticket", "escalate_ticket",
]
