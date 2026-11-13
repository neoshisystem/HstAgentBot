"""Internal outbox-backed Telegram reporting facade.

The facade intentionally exposes no BOT_TOKEN and never performs Telegram
network I/O. Messages and encrypted-backup references are queued for the
existing Product delivery worker.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from typing import Any

_TOKEN_PATTERN = re.compile(r"\b\d{5,}:[A-Za-z0-9_-]{20,}\b")


def _redact(value: Any) -> str:
    return _TOKEN_PATTERN.sub("[REDACTED_TOKEN]", str(value))


def _queue(logical_key: str, *, purpose: str, resource_type: str,
           resource_id: str, rep_id: int | None = None,
           actor_tg_id: int | None = None) -> dict[str, Any]:
    import database
    from report_router import route_event
    from outbox_service import enqueue_delivery

    event: dict[str, Any] = {"logical_key": logical_key}
    if rep_id is not None:
        actor = actor_tg_id
        if actor is None:
            with database.get_db() as conn:
                row = conn.execute(
                    "SELECT telegram_id FROM representatives WHERE id=? AND is_active=1 AND deleted_at IS NULL",
                    (int(rep_id),),
                ).fetchone()
            actor = int(row["telegram_id"]) if row and row["telegram_id"] is not None else 0
        event.update({"rep_id": int(rep_id), "actor_tg_id": int(actor)})
    with database.get_db() as conn:
        route = route_event(event, conn=conn)
        event_hash = hashlib.sha256(
            (logical_key + "\0" + str(route["chat_id"]) + "\0" + resource_id).encode("utf-8")
        ).hexdigest()
        queued = enqueue_delivery(
            conn,
            event_key=f"w19:{logical_key}:{event_hash}:{uuid.uuid4().hex[:12]}",
            bot_id=int(route["bot_id"]),
            chat_id=int(route["chat_id"]),
            thread_id=route["thread_id"],
            purpose=purpose,
            resource_type=resource_type,
            resource_id=resource_id,
        )
    return {
        "ok": True,
        "queued": True,
        "state": queued["state"],
        "delivery_id": queued["delivery_id"],
        "logical_key": logical_key,
    }


def send_telegram_message(target_topic, text=None, parse_mode=None, *,
                          logical_key: str | None = None, rep_id: int | None = None,
                          actor_tg_id: int | None = None, **_ignored):
    """Preserve the old signature and enqueue bounded text chunks only."""
    message = str(text if text is not None else target_topic)
    lower = message.casefold()
    if logical_key is None:
        if any(term in lower for term in ("abuse", "abusereport", "سوءاستفاده")):
            logical_key = "abusereport"
        else:
            logical_key = "rep_buy" if rep_id is not None else "errorreport"
    safe_message = _redact(message)
    # resource_id is capped at 240 characters by the outbox schema. Store the
    # JSON envelope for a bounded piece of text, never a truncated full report.
    safe_message = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", safe_message)
    chunks = [safe_message[index:index + 80] for index in range(0, len(safe_message), 80)] or [""]
    deliveries = []
    try:
        for index, chunk in enumerate(chunks):
            resource = json.dumps(
                {
                    "text": chunk,
                    "part": index + 1,
                    "parts": len(chunks),
                    "parse_mode": parse_mode if parse_mode in (None, "HTML", "Markdown", "MarkdownV2") else None,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if len(resource) > 240:
                raise ValueError("report chunk exceeds durable outbox reference limit")
            deliveries.append(_queue(
                logical_key,
                purpose="abusereport" if logical_key == "abusereport" else "admin_report",
                resource_type="telegram_message",
                resource_id=resource,
                rep_id=rep_id,
                actor_tg_id=actor_tg_id,
            ))
        return {
            "ok": all(item.get("ok") is True for item in deliveries),
            "queued": all(item.get("queued") is True for item in deliveries),
            "state": "pending" if deliveries else "unknown",
            "delivery_ids": [item.get("delivery_id") for item in deliveries],
            "logical_key": logical_key,
        }
    except Exception as exc:
        return {
            "ok": False,
            "queued": bool(deliveries),
            "state": "unknown",
            "delivery_ids": [item.get("delivery_id") for item in deliveries],
            "error": _redact(type(exc).__name__ + ": " + str(exc))[:240],
        }


def send_telegram_document(target_topic, document_path=None, caption=None, parse_mode=None, *,
                           logical_key: str = "backupfile", **_ignored):
    """Queue only an encrypted archive whose ciphertext and authenticated payload verify."""
    path = document_path if document_path is not None else target_topic
    if not isinstance(path, (str, os.PathLike)):
        return {"ok": False, "queued": False, "state": "definite_failure",
                "error": "encrypted backup archive path is required"}
    archive_path = os.path.abspath(os.fspath(path))
    if not archive_path.endswith(".tar.gz.enc") or not os.path.isfile(archive_path):
        return {"ok": False, "queued": False, "state": "definite_failure",
                "error": "only encrypted backup archives are eligible for document delivery"}
    try:
        import backup
        backup.verify_backup_archive(archive_path)
        # The outbox schema caps encrypted resource references at 240 chars.
        # Keep only the verified local archive path; the worker may derive a
        # standard caption, but no oversized caption is silently truncated.
        resource = json.dumps(
            {"path": archive_path},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(resource) > 240:
            return {"ok": False, "queued": False, "state": "definite_failure",
                    "error": "encrypted backup path exceeds durable outbox reference limit"}
        return _queue(
            logical_key,
            purpose="backup_document",
            resource_type="encrypted_backup",
            resource_id=resource,
        )
    except Exception as exc:
        return {
            "ok": False,
            "queued": False,
            "state": "unknown",
            "error": _redact(type(exc).__name__ + ": " + str(exc))[:240],
        }


__all__ = ["send_telegram_message", "send_telegram_document"]
