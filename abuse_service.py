"""Root-DM-only abuse reports with reporter-scoped copies and durable private outbox."""
from __future__ import annotations

from collections.abc import Mapping
import json
import secrets
import time
from typing import Any

import config
import database
from crypto_store import decrypt_secret, encrypt_secret
from outbox_service import enqueue_delivery
import support_service as support

_LABEL = "hstagent/columns/v1"


class AbuseError(ValueError):
    """An abuse-report operation cannot be completed safely."""


def _actor_id(ctx: Any) -> int:
    return support._actor_id(ctx)


def _is_root(ctx: Any) -> bool:
    return support._is_root(ctx)


def _timestamp(now: int | None = None) -> int:
    try:
        return support._timestamp(now)
    except support.SupportError as exc:
        raise AbuseError(str(exc)) from exc


def _decode(row, ticket_bot_id: int) -> dict:
    try:
        body = decrypt_secret(str(row["body_cipher"]), _LABEL)
        attachment = (
            decrypt_secret(str(row["attachment_file_id"]), _LABEL)
            if row["attachment_file_id"] else None
        )
    except Exception as exc:
        raise AbuseError("stored abuse message failed authentication") from exc
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


def create_abuse_report(
    ctx: Any, body: str = "", *, bot_id: int | None = None,
    attachment_file_id: str | None = None, attachment_bot_id: int | None = None,
    now: int | None = None,
) -> dict:
    """Persist a root-only report and enqueue it to the primary bot's root DM."""
    actor_id = _actor_id(ctx)
    source_bot = support._bot_id(ctx, bot_id)
    timestamp = _timestamp(now)
    ticket_id = secrets.token_urlsafe(18)
    selected_attachment_bot = (
        support._positive(attachment_bot_id, "attachment_bot_id")
        if attachment_bot_id is not None else source_bot
    )

    with database.get_db() as conn, support._atomic(conn):
        support._validate_bot(conn, source_bot)
        conn.execute(
            "INSERT INTO support_tickets(ticket_id,sender_tg_id,bot_id,account_id,rep_id,"
            "ticket_type,assigned_key,status,snapshot_cipher,created_at,updated_at) "
            "VALUES(?,?,?,NULL,NULL,'abuse_report','root','unseen',NULL,?,?)",
            (ticket_id, actor_id, source_bot, timestamp, timestamp),
        )
        message_id = support._insert_message(
            conn, ticket_id=ticket_id, actor_tg_id=actor_id,
            visibility="root_only", body=body,
            attachment_file_id=attachment_file_id,
            attachment_bot_id=selected_attachment_bot if attachment_file_id else None,
            now=timestamp,
        )
        delivery = enqueue_delivery(
            conn,
            event_key=f"abuse-report:{ticket_id}",
            bot_id=source_bot,
            chat_id=_actor_id(ctx),
            thread_id=None,
            purpose="abuse_report",
            resource_type="support_ticket",
            resource_id=ticket_id,
        )
    return {
        "ticket_id": ticket_id,
        "ticket_type": "abuse_report",
        "assigned_key": "root",
        "status": "unseen",
        "initial_message_id": message_id,
        "delivery": delivery,
    }


def list_abuse_reports(ctx: Any) -> list[dict]:
    """Root sees all reports; every other actor sees only reports they submitted."""
    actor_id = _actor_id(ctx)
    root = int(_is_root(ctx))
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM support_tickets WHERE ticket_type='abuse_report' "
            "AND (?=1 OR sender_tg_id=?) ORDER BY created_at DESC,ticket_id",
            (root, actor_id),
        ).fetchall()
        return [
            {
                key: row[key]
                for key in (
                    "ticket_id", "sender_tg_id", "bot_id", "ticket_type",
                    "assigned_key", "status", "created_at", "updated_at",
                )
            }
            for row in rows
        ]


def count_abuse_reports(ctx: Any) -> int:
    actor_id = _actor_id(ctx)
    root = int(_is_root(ctx))
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM support_tickets WHERE ticket_type='abuse_report' "
            "AND (?=1 OR sender_tg_id=?)",
            (root, actor_id),
        ).fetchone()
        return int(row[0])


def search_abuse_reports(ctx: Any, query: str) -> list[dict]:
    actor_id = _actor_id(ctx)
    root = int(_is_root(ctx))
    needle = str(query or "").strip()
    if not needle:
        return []
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM support_tickets WHERE ticket_type='abuse_report' "
            "AND (?=1 OR sender_tg_id=?) AND ticket_id LIKE ? "
            "ORDER BY created_at DESC",
            (root, actor_id, f"%{needle}%"),
        ).fetchall()
        return [
            {
                key: row[key]
                for key in (
                    "ticket_id", "sender_tg_id", "bot_id", "ticket_type",
                    "assigned_key", "status", "created_at", "updated_at",
                )
            }
            for row in rows
        ]


def list_abuse_messages(ctx: Any, ticket_id: str) -> list[dict]:
    exact = str(ticket_id or "").strip()
    actor_id = _actor_id(ctx)
    root = _is_root(ctx)
    with database.get_db() as conn:
        ticket = conn.execute(
            "SELECT * FROM support_tickets WHERE ticket_id=? AND ticket_type='abuse_report'",
            (exact,),
        ).fetchone()
        if ticket is None:
            raise AbuseError("abuse report does not exist")
        sender = int(ticket["sender_tg_id"]) == actor_id
        if not root and not sender:
            raise PermissionError("actor cannot view this abuse report")
        rows = conn.execute(
            "SELECT * FROM support_messages WHERE ticket_id=? "
            + ("" if root else "AND (actor_tg_id=? OR visibility='customer') ")
            + "ORDER BY id",
            (exact,) if root else (exact, actor_id),
        ).fetchall()
        return [_decode(row, int(ticket["bot_id"])) for row in rows]


def export_abuse_report(ctx: Any, ticket_id: str) -> dict:
    exact = str(ticket_id or "").strip()
    actor_id = _actor_id(ctx)
    root = _is_root(ctx)
    with database.get_db() as conn:
        ticket = conn.execute(
            "SELECT * FROM support_tickets WHERE ticket_id=? AND ticket_type='abuse_report'",
            (exact,),
        ).fetchone()
        if ticket is None:
            raise AbuseError("abuse report does not exist")
        if not root and int(ticket["sender_tg_id"]) != actor_id:
            raise PermissionError("actor cannot export this abuse report")
        data = {
            key: ticket[key]
            for key in (
                "ticket_id", "sender_tg_id", "bot_id", "ticket_type",
                "assigned_key", "status", "created_at", "updated_at",
            )
        }
    return {"ticket": data, "messages": list_abuse_messages(ctx, exact)}


def reply_abuse_report(
    ctx: Any, ticket_id: str, body: str, *,
    visibility: str = "customer", now: int | None = None,
) -> dict:
    if not _is_root(ctx):
        raise PermissionError("only the configured root may respond to abuse reports")
    selected = str(visibility or "").strip().lower()
    if selected not in {"customer", "root_only"}:
        raise AbuseError("abuse replies must be customer or root_only")
    timestamp = _timestamp(now)
    exact = str(ticket_id or "").strip()
    actor_id = _actor_id(ctx)
    with database.get_db() as conn, support._atomic(conn):
        ticket = conn.execute(
            "SELECT * FROM support_tickets WHERE ticket_id=? AND ticket_type='abuse_report'",
            (exact,),
        ).fetchone()
        if ticket is None:
            raise AbuseError("abuse report does not exist")
        message_id = support._insert_message(
            conn, ticket_id=exact, actor_tg_id=actor_id,
            visibility=selected, body=body,
            attachment_file_id=None, attachment_bot_id=None, now=timestamp,
        )
        conn.execute(
            "UPDATE support_tickets SET updated_at=? WHERE ticket_id=?",
            (timestamp, exact),
        )
        delivery = None
        if selected == "customer":
            delivery = enqueue_delivery(
                conn,
                event_key=f"abuse-customer-reply:{exact}:{message_id}",
                bot_id=int(ticket["bot_id"]),
                chat_id=int(ticket["sender_tg_id"]),
                thread_id=None,
                purpose="support_reply",
                resource_type="support_message",
                resource_id=str(message_id),
            )
    return {"message_id": message_id, "visibility": selected, "delivery": delivery}


__all__ = [
    "AbuseError", "create_abuse_report", "list_abuse_reports",
    "count_abuse_reports", "search_abuse_reports", "list_abuse_messages",
    "export_abuse_report", "reply_abuse_report",
]
