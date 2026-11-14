"""Privacy-first report routing from trusted Product registries.

Caller-supplied bot/chat/thread identifiers are intentionally ignored.  Every
route is resolved from bot_registry, representatives and forum_topics.
"""
from __future__ import annotations

from collections.abc import Mapping
import sqlite3
from typing import Any

_GROUP_TOPICS = frozenset({
    "buyreport", "otherservice", "pendingreceipts", "paymentreport",
    "reporttest", "supportreport", "errorreport", "reportcron",
    "reportnight", "backupfile",
})
_REP_TOPICS = {
    "rep_buy": "buyreport",
    "rep_renew": "buyreport",
    "rep_support": "supportreport",
    "rep_sub_settlement": "paymentreport",
}
_SAFE_PAYLOAD_FIELDS = frozenset({
    "event_id", "occurred_at", "period", "date", "count",
    "amount_toman", "status", "merchant_key",
})


class ReportRouteError(ValueError):
    """A report has no trusted, authorized destination."""


def _positive(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ReportRouteError(f"{label} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ReportRouteError(f"{label} must be a positive integer") from None
    if result <= 0:
        raise ReportRouteError(f"{label} must be a positive integer")
    return result


def _get(row: Any, key: str, default=None):
    try:
        return row[key]
    except (KeyError, IndexError, TypeError):
        return default


def _root_id(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM system_settings WHERE key='admin_telegram_id'"
    ).fetchone()
    value = _get(row, "value", 0)
    if not value:
        try:
            import config
            value = getattr(config, "ADMIN_TELEGRAM_ID", 0)
        except Exception:
            value = 0
    return _positive(value, "configured root Telegram ID")


def _safe_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    """Copy only explicitly safe scalar report metadata; never forward raw payloads."""
    result: dict[str, Any] = {}
    for key in _SAFE_PAYLOAD_FIELDS:
        value = event.get(key)
        if value is None or isinstance(value, (dict, list, tuple, bytes, bytearray)):
            continue
        if isinstance(value, str) and len(value) > 160:
            continue
        result[key] = value
    return result


def _route_event(conn: sqlite3.Connection, event: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(event, Mapping):
        raise ReportRouteError("event must be a mapping")
    key = str(event.get("logical_key", event.get("key", ""))).strip().lower()
    if not key:
        raise ReportRouteError("logical report key is required")

    if key in {"abusereport", "abuse_report"}:
        primary = conn.execute(
            "SELECT bot_id FROM bot_registry WHERE is_primary=1 AND status='active' "
            "ORDER BY bot_id LIMIT 1"
        ).fetchone()
        if primary is None:
            raise ReportRouteError("active primary bot is unavailable")
        return {
            "logical_key": "abusereport",
            "destination": "root_dm",
            "bot_id": int(_get(primary, "bot_id")),
            "chat_id": _root_id(conn),
            "thread_id": None,
            "payload": {},
        }

    if key in _GROUP_TOPICS:
        row = conn.execute(
            "SELECT f.bot_id,f.group_chat_id,f.thread_id "
            "FROM forum_topics f JOIN bot_registry b ON b.bot_id=f.bot_id "
            "WHERE f.owner_key='root' AND f.topic_key=? AND f.state='active' "
            "AND f.thread_id IS NOT NULL AND b.is_primary=1 AND b.status='active' "
            "ORDER BY f.generation DESC LIMIT 1",
            (key,),
        ).fetchone()
        if row is None:
            raise ReportRouteError("active root report topic is not registered")
        return {
            "logical_key": key,
            "destination": "group_topic",
            "bot_id": int(_get(row, "bot_id")),
            "chat_id": int(_get(row, "group_chat_id")),
            "thread_id": int(_get(row, "thread_id")),
            "payload": _safe_payload(event),
        }

    if key in _REP_TOPICS:
        rep_id = _positive(event.get("rep_id"), "rep_id")
        actor_id = _positive(event.get("actor_tg_id"), "actor_tg_id")
        rep = conn.execute(
            "SELECT id,telegram_id,group_id,topic_id FROM representatives "
            "WHERE id=? AND is_active=1 AND deleted_at IS NULL",
            (rep_id,),
        ).fetchone()
        if rep is None:
            raise ReportRouteError("representative is inactive or unavailable")
        if actor_id != int(_get(rep, "telegram_id", 0) or 0) and actor_id != _root_id(conn):
            raise PermissionError("representative report is outside the actor's scope")
        bot = conn.execute(
            "SELECT bot_id FROM bot_registry WHERE rep_id=? AND is_primary=0 "
            "AND status='active' ORDER BY bot_id LIMIT 1",
            (rep_id,),
        ).fetchone()
        group_id = _get(rep, "group_id")
        thread_id = _get(rep, "topic_id")
        if bot is None or group_id is None or int(group_id) == 0:
            raise ReportRouteError("representative bot/group is not registered")
        return {
            "logical_key": key,
            "destination": "representative_topic",
            "rep_id": rep_id,
            "bot_id": int(_get(bot, "bot_id")),
            "chat_id": int(group_id),
            "thread_id": int(thread_id) if thread_id else None,
            "payload": _safe_payload(event),
        }

    raise ReportRouteError("unsupported logical report key")


def route_event(event: Mapping[str, Any], *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Resolve one report destination using trusted database state only.

    A caller may pass an existing idle connection for deterministic tests or
    transaction composition.  Otherwise the Product database context is used.
    """
    if conn is not None:
        return _route_event(conn, event)
    import database
    with database.get_db() as managed:
        return _route_event(managed, event)


__all__ = ["ReportRouteError", "route_event"]
