"""Operator-defined ticket statuses.

The four lifecycle states used to be a hard-coded dictionary, so the root could
not add a state, rename one, change its emoji or retire it. This module owns the
catalog: ``ticket_statuses`` holds one row per state, seeded with the four
historic codes so an existing deployment keeps behaving identically.

Every reader goes through :func:`label_for`, which falls back to the historic
static labels when the catalog is unavailable — a ticket whose status is missing
from the catalog must still render something readable rather than crash.
"""
from __future__ import annotations

import re
import sqlite3
import time
from typing import Any

CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,29}$")
GROUP_KEYS = ("open", "answered", "closed")
MAX_TITLE = 40
MAX_EMOJI = 8

# The historic labels, kept as the fallback for a row that predates the catalog
# or for a status code that was retired after a ticket was already written.
LEGACY_LABELS: dict[str, str] = {
    "unseen": "🟢 باز (Open)",
    "escalated": "🟢 باز (Open)",
    "answered": "🟡 در حال بررسی / پاسخ‌داده‌شده",
    "closed": "🔴 بسته شده (Closed)",
}


class TicketStatusError(ValueError):
    """Base error for the ticket-status catalog."""


class TicketStatusNotFound(TicketStatusError):
    """The requested status code does not exist."""


class TicketStatusConflict(TicketStatusError):
    """The code is taken, or a system row cannot be removed."""


def _db():
    import database
    return database.get_db()


def _ensure(conn) -> None:
    import database
    database.ensure_ticket_statuses_table(conn)


def _clean_title(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise TicketStatusError("عنوان فارسی وضعیت نمی‌تواند خالی باشد.")
    if len(text) > MAX_TITLE:
        raise TicketStatusError(f"عنوان وضعیت حداکثر {MAX_TITLE} کاراکتر است.")
    return text


def _clean_emoji(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) > MAX_EMOJI:
        raise TicketStatusError("ایموجی وضعیت بیش از حد بلند است.")
    return text


def _clean_code(value: Any) -> str:
    code = str(value or "").strip().lower()
    if not CODE_RE.fullmatch(code):
        raise TicketStatusError(
            "کد وضعیت باید با حرف کوچک لاتین شروع شود و فقط شامل حروف کوچک، "
            "رقم و زیرخط باشد (۲ تا ۳۰ کاراکتر)."
        )
    return code


def _clean_group(value: Any) -> str:
    group = str(value or "open").strip().lower()
    if group not in GROUP_KEYS:
        raise TicketStatusError("گروه وضعیت نامعتبر است.")
    return group


def _row_to_dict(row) -> dict[str, Any]:
    return {
        "code": str(row[0]),
        "title_fa": str(row[1]),
        "emoji": str(row[2] or ""),
        "group_key": str(row[3]),
        "sort_order": int(row[4]),
        "is_active": bool(row[5]),
        "is_system": bool(row[6]),
    }


def list_statuses(conn, *, include_inactive: bool = False) -> list[dict[str, Any]]:
    """Every catalog row, ordered for display."""
    _ensure(conn)
    clause = "" if include_inactive else " WHERE is_active=1"
    rows = conn.execute(
        "SELECT code,title_fa,emoji,group_key,sort_order,is_active,is_system "
        "FROM ticket_statuses" + clause + " ORDER BY sort_order, code"
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def get_status(conn, code: Any) -> dict[str, Any] | None:
    _ensure(conn)
    row = conn.execute(
        "SELECT code,title_fa,emoji,group_key,sort_order,is_active,is_system "
        "FROM ticket_statuses WHERE code=?",
        (str(code or "").strip().lower(),),
    ).fetchone()
    return None if row is None else _row_to_dict(row)


def label_for(conn, code: Any) -> str:
    """The emoji + Persian title for a status code, with a safe fallback."""
    text = str(code or "").strip().lower()
    if not text:
        return "نامشخص"
    try:
        row = get_status(conn, text)
    except sqlite3.Error:
        row = None
    if row is None:
        return LEGACY_LABELS.get(text, "نامشخص")
    return f"{row['emoji']} {row['title_fa']}".strip()


def group_codes(conn, group_key: str) -> tuple[str, ...]:
    """Active codes that belong to a queue group, for the status filter."""
    try:
        rows = list_statuses(conn)
    except sqlite3.Error:
        return ()
    return tuple(row["code"] for row in rows if row["group_key"] == group_key)


def create_status(
    conn,
    *,
    code: Any,
    title_fa: Any,
    emoji: Any = "",
    group_key: Any = "open",
    sort_order: int = 100,
    now: int | None = None,
) -> dict[str, Any]:
    """Add a new operator-defined status."""
    _ensure(conn)
    clean_code = _clean_code(code)
    if get_status(conn, clean_code) is not None:
        raise TicketStatusConflict(f"وضعیت «{clean_code}» از قبل وجود دارد.")
    if isinstance(sort_order, bool) or not isinstance(sort_order, int):
        raise TicketStatusError("ترتیب نمایش باید یک عدد صحیح باشد.")
    timestamp = int(time.time()) if now is None else int(now)
    conn.execute(
        "INSERT INTO ticket_statuses(code,title_fa,emoji,group_key,sort_order,"
        "is_active,is_system,created_at,updated_at) VALUES(?,?,?,?,?,1,0,?,?)",
        (clean_code, _clean_title(title_fa), _clean_emoji(emoji),
         _clean_group(group_key), sort_order, timestamp, timestamp),
    )
    return get_status(conn, clean_code)


def update_status(
    conn,
    code: Any,
    *,
    title_fa: Any = None,
    emoji: Any = None,
    group_key: Any = None,
    sort_order: Any = None,
    is_active: Any = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Edit one status in place; only the supplied fields change."""
    _ensure(conn)
    clean_code = _clean_code(code)
    current = get_status(conn, clean_code)
    if current is None:
        raise TicketStatusNotFound(f"وضعیت «{clean_code}» یافت نشد.")
    fields: list[str] = []
    params: list[Any] = []
    if title_fa is not None:
        fields.append("title_fa=?")
        params.append(_clean_title(title_fa))
    if emoji is not None:
        fields.append("emoji=?")
        params.append(_clean_emoji(emoji))
    if group_key is not None:
        fields.append("group_key=?")
        params.append(_clean_group(group_key))
    if sort_order is not None:
        if isinstance(sort_order, bool) or not isinstance(sort_order, int):
            raise TicketStatusError("ترتیب نمایش باید یک عدد صحیح باشد.")
        fields.append("sort_order=?")
        params.append(sort_order)
    if is_active is not None:
        fields.append("is_active=?")
        params.append(1 if is_active else 0)
    if not fields:
        return current
    fields.append("updated_at=?")
    params.append(int(time.time()) if now is None else int(now))
    params.append(clean_code)
    conn.execute(
        "UPDATE ticket_statuses SET " + ",".join(fields) + " WHERE code=?", params
    )
    return get_status(conn, clean_code)


def delete_status(conn, code: Any) -> dict[str, Any]:
    """Remove an operator-defined status; a system row is only deactivated.

    A system status is referenced by the historic lifecycle and by tickets that
    already exist, so it is disabled rather than deleted.
    """
    _ensure(conn)
    clean_code = _clean_code(code)
    current = get_status(conn, clean_code)
    if current is None:
        raise TicketStatusNotFound(f"وضعیت «{clean_code}» یافت نشد.")
    if current["is_system"]:
        update_status(conn, clean_code, is_active=False)
        return {"code": clean_code, "deleted": False, "deactivated": True}
    conn.execute("DELETE FROM ticket_statuses WHERE code=?", (clean_code,))
    return {"code": clean_code, "deleted": True, "deactivated": False}


def active_codes(conn) -> tuple[str, ...]:
    """Every active code, for validating a status change."""
    try:
        return tuple(row["code"] for row in list_statuses(conn))
    except sqlite3.Error:
        return tuple(LEGACY_LABELS)


def is_active_code(conn, code: Any) -> bool:
    text = str(code or "").strip().lower()
    if not text:
        return False
    try:
        row = get_status(conn, text)
    except sqlite3.Error:
        return text in LEGACY_LABELS
    if row is None:
        return text in LEGACY_LABELS
    return bool(row["is_active"])


__all__ = [
    "CODE_RE",
    "GROUP_KEYS",
    "LEGACY_LABELS",
    "MAX_EMOJI",
    "MAX_TITLE",
    "TicketStatusConflict",
    "TicketStatusError",
    "TicketStatusNotFound",
    "active_codes",
    "create_status",
    "delete_status",
    "get_status",
    "group_codes",
    "is_active_code",
    "label_for",
    "list_statuses",
    "update_status",
]
