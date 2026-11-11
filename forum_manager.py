"""Idempotent Telegram forum-topic provisioning with timeout-safe repair state."""
from __future__ import annotations

import sqlite3
from typing import Any, Iterable

TOPIC_TITLES = {
    "buyreport": "گزارش خرید",
    "otherservice": "سایر خدمات",
    "pendingreceipts": "فیش‌های در انتظار",
    "paymentreport": "گزارش پرداخت",
    "reporttest": "گزارش آزمایشی",
    "supportreport": "پشتیبانی",
    "errorreport": "خطاها",
    "reportcron": "گزارش زمان‌بندی",
    "reportnight": "گزارش شبانه",
    "backupfile": "فایل‌های پشتیبان",
}


class ForumError(ValueError):
    """Forum registration or provisioning cannot be completed safely."""


def _chat_id(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ForumError(f"{name} must be a non-zero Telegram chat integer")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ForumError(f"{name} must be a non-zero Telegram chat integer") from None
    if result == 0:
        raise ForumError(f"{name} must be a non-zero Telegram chat integer")
    return result


def _field(value: Any, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _positive(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ForumError(f"{name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ForumError(f"{name} must be a positive integer") from None
    if result <= 0:
        raise ForumError(f"{name} must be a positive integer")
    return result


def check_forum_permissions(api: Any, group_chat_id: int, *, bot_id: int | None = None) -> dict[str, Any]:
    """Verify forum and bot rights; supergroups do not require can_post_messages."""
    chat_id = _chat_id(group_chat_id, "group_chat_id")
    try:
        chat = api.get_chat(chat_id=chat_id)
        if bot_id is None:
            me = api.get_me()
            bot_id = _positive(_field(_field(me, "result", me), "id"), "bot_id")
        else:
            bot_id = _positive(bot_id, "bot_id")
        member = api.get_chat_member(chat_id=chat_id, user_id=bot_id)
    except Exception as exc:
        return {"allowed": False, "reason": "telegram_permission_check_failed",
                "error_type": type(exc).__name__}

    chat_type = str(_field(chat, "type", ""))
    if not bool(_field(chat, "is_forum", False)):
        return {"allowed": False, "reason": "chat_is_not_a_forum"}
    if chat_type not in {"supergroup", "channel"}:
        return {"allowed": False, "reason": "unsupported_forum_chat_type"}
    status = str(_field(member, "status", ""))
    if status not in {"administrator", "creator"}:
        return {"allowed": False, "reason": "bot_is_not_forum_administrator"}
    if not bool(_field(member, "can_manage_topics", False)):
        return {"allowed": False, "reason": "can_manage_topics_required"}
    if not bool(_field(member, "can_pin_messages", False)):
        return {"allowed": False, "reason": "can_pin_messages_required"}
    # Telegram exposes can_post_messages for channels. It is not a supergroup
    # permission and therefore must not be treated as mandatory there.
    if chat_type == "channel" and not bool(_field(member, "can_post_messages", False)):
        return {"allowed": False, "reason": "can_post_messages_required_for_channel"}
    return {"allowed": True, "reason": None, "bot_id": bot_id,
            "chat_id": chat_id, "chat_type": chat_type}


def _topic_thread_id(result: Any) -> int:
    value = _field(_field(result, "result", result), "message_thread_id")
    return _positive(value, "message_thread_id")


def sync_report_topics(
    conn: sqlite3.Connection,
    api: Any,
    *,
    bot_id: int,
    group_chat_id: int,
    now: int,
    owner_key: str = "root",
    generation: int = 1,
    topic_keys: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    """Create missing topics once; ambiguous creates become UNKNOWN/manual repair.

    The creating intent is committed before Telegram is called.  This function
    never lists all topics and never creates a second topic for UNKNOWN/creating
    rows.  Existing known threads are edited in place.
    """
    selected_bot = _positive(bot_id, "bot_id")
    chat_id = _chat_id(group_chat_id, "group_chat_id")
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise ForumError("now must be a non-negative epoch-seconds integer")
    if not isinstance(owner_key, str) or not owner_key.strip() or len(owner_key) > 120:
        raise ForumError("owner_key is invalid")
    if conn.in_transaction:
        raise ForumError("sync_report_topics requires an idle connection")
    permission = check_forum_permissions(api, chat_id, bot_id=selected_bot)
    if not permission["allowed"]:
        raise ForumError(str(permission["reason"]))

    keys = tuple(TOPIC_TITLES) if topic_keys is None else tuple(topic_keys)
    outcomes: list[dict[str, Any]] = []
    for key in keys:
        if key not in TOPIC_TITLES:
            raise ForumError(f"unknown report topic key: {key}")
        title = TOPIC_TITLES[key]
        cursor = conn.execute(
            "INSERT INTO forum_topics(owner_key,bot_id,group_chat_id,generation,"
            "topic_key,title_fa,thread_id,state,updated_at) "
            "VALUES(?,?,?,?,?,?,NULL,'creating',?) "
            "ON CONFLICT(owner_key,bot_id,group_chat_id,generation,topic_key) DO NOTHING",
            (owner_key, selected_bot, chat_id, int(generation), key, title, now),
        )
        inserted = cursor.rowcount == 1
        conn.commit()  # Durable intent before the external API request.
        row = conn.execute(
            "SELECT id,thread_id,state FROM forum_topics WHERE owner_key=? AND bot_id=? "
            "AND group_chat_id=? AND generation=? AND topic_key=?",
            (owner_key, selected_bot, chat_id, int(generation), key),
        ).fetchone()
        if row is None:
            raise ForumError("forum topic intent disappeared")
        thread_id = row["thread_id"]
        state = str(row["state"])

        if thread_id is not None and state == "active":
            try:
                api.edit_forum_topic(chat_id=chat_id, message_thread_id=int(thread_id), name=title)
                outcomes.append({"topic_key": key, "state": "active",
                                 "thread_id": int(thread_id), "action": "edited"})
            except Exception as exc:
                outcomes.append({"topic_key": key, "state": "active",
                                 "thread_id": int(thread_id), "action": "edit_failed",
                                 "error_type": type(exc).__name__})
            continue

        if not inserted:
            # A process may have died after persisting "creating".  It is
            # ambiguous whether Telegram created the remote topic; never retry.
            if state == "creating":
                conn.execute(
                    "UPDATE forum_topics SET state='unknown',updated_at=? WHERE id=? AND state='creating'",
                    (now, int(row["id"])),
                )
                conn.commit()
                state = "unknown"
            outcomes.append({"topic_key": key, "state": state,
                             "action": "manual_repair_required"})
            continue

        try:
            response = api.create_forum_topic(chat_id=chat_id, name=title)
            created_thread_id = _topic_thread_id(response)
        except Exception as exc:
            conn.execute(
                "UPDATE forum_topics SET state='unknown',updated_at=? WHERE id=? AND state='creating'",
                (now, int(row["id"])),
            )
            conn.commit()
            outcomes.append({"topic_key": key, "state": "unknown",
                             "action": "manual_repair_required",
                             "error_type": type(exc).__name__})
            continue

        conn.execute(
            "UPDATE forum_topics SET thread_id=?,state='active',updated_at=? "
            "WHERE id=? AND state='creating' AND thread_id IS NULL",
            (created_thread_id, now, int(row["id"])),
        )
        conn.commit()
        outcomes.append({"topic_key": key, "state": "active",
                         "thread_id": created_thread_id, "action": "created"})
    return outcomes


__all__ = ["ForumError", "TOPIC_TITLES", "check_forum_permissions", "sync_report_topics"]
