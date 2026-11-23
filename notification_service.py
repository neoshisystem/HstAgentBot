"""Expiry and usage notices from explicit latest validated account snapshots."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping
from typing import Any

from outbox_service import enqueue_delivery


class NotificationError(ValueError):
    """An alert cannot be planned with trustworthy account state."""


def _integer(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise NotificationError(f"{label} must be a non-negative integer")
    return value


def _setting(conn: sqlite3.Connection, key: str) -> float | None:
    row = conn.execute(
        "SELECT value FROM system_settings WHERE key=?", (key,)
    ).fetchone()
    if row is None:
        return None
    try:
        value = float(row["value"])
    except (TypeError, ValueError, KeyError, IndexError):
        return None
    return value if value >= 0 else None


def _recipients(conn: sqlite3.Connection, account_id: int, owner_tg_id: int | None) -> list[int]:
    ids: set[int] = set()
    if owner_tg_id is not None and int(owner_tg_id) > 0:
        ids.add(int(owner_tg_id))
    rows = conn.execute(
        "SELECT grantee_tg_id FROM account_access_grants "
        "WHERE account_id=? AND revoked_at IS NULL AND access_level IN ('viewer','manager') "
        "ORDER BY grantee_tg_id",
        (account_id,),
    ).fetchall()
    ids.update(int(row["grantee_tg_id"]) for row in rows if int(row["grantee_tg_id"]) > 0)
    return sorted(ids)


def _snapshot_is_usable(snapshot: Mapping[str, Any], now: int) -> bool:
    """Require explicit validation/latest markers and a positive observation timestamp."""
    if snapshot.get("validated") is not True or snapshot.get("latest") is not True:
        return False
    if snapshot.get("state") != "available" or snapshot.get("unavailable") is True:
        return False
    observed_at = snapshot.get("observed_at")
    if isinstance(observed_at, bool) or not isinstance(observed_at, int):
        return False
    return 0 <= observed_at <= now


def plan_expiry_notifications(
    conn: sqlite3.Connection,
    snapshots: Iterable[Mapping[str, Any]],
    *,
    now: int,
) -> list[dict[str, Any]]:
    """Queue one idempotent alert per account/version/threshold/recipient.

    This function accepts only caller-provided latest validated snapshots. It
    performs no panel or Telegram network calls and stores only an encrypted
    account reference in the existing delivery outbox.
    """
    current = _integer(now, "now")
    if conn.in_transaction:
        raise NotificationError("notification planning requires an idle connection")
    expiry_days = _setting(conn, "critical_audit_days")
    usage_gb = _setting(conn, "critical_audit_gb")
    if expiry_days is None or usage_gb is None:
        return []

    primary = conn.execute(
        "SELECT bot_id FROM bot_registry WHERE is_primary=1 AND status='active' "
        "ORDER BY bot_id LIMIT 1"
    ).fetchone()
    if primary is None:
        return []
    bot_id = int(primary["bot_id"])
    outcomes: list[dict[str, Any]] = []

    for snapshot in snapshots:
        if not isinstance(snapshot, Mapping) or not _snapshot_is_usable(snapshot, current):
            continue
        try:
            account_id = _integer(snapshot.get("account_id"), "account_id")
        except NotificationError:
            continue
        if account_id <= 0:
            continue
        account = conn.execute(
            "SELECT id,owner_tg_id,revision,expiry_time,deleted_at "
            "FROM accounts WHERE id=?",
            (account_id,),
        ).fetchone()
        if account is None or account["deleted_at"] is not None:
            continue

        expiry_at = snapshot.get("expires_at")
        expiry_at = expiry_at if isinstance(expiry_at, int) and not isinstance(expiry_at, bool) and expiry_at >= 0 else None
        expiry_version = str(snapshot.get("expiry_version") or
                             f"{int(account['revision'] or 1)}:{int(account['expiry_time'] or 0)}")
        notices: list[tuple[str, str, str]] = []
        if expiry_at is not None and expiry_at <= current + int(expiry_days * 86400):
            days_label = format(expiry_days, ".8g")
            notices.append((
                "expiry_notice",
                f"account:{account_id}:expiry:{days_label}:{expiry_version}",
                "expiry",
            ))

        quota_mode = str(snapshot.get("quota_mode") or "unknown")
        quota_bytes = snapshot.get("quota_bytes")
        used_bytes = snapshot.get("used_bytes")
        if (
            quota_mode == "finite"
            and isinstance(quota_bytes, int) and not isinstance(quota_bytes, bool) and quota_bytes >= 0
            and isinstance(used_bytes, int) and not isinstance(used_bytes, bool) and used_bytes >= 0
        ):
            remaining_gb = max(quota_bytes - used_bytes, 0) / (1024 ** 3)
            if remaining_gb <= usage_gb:
                threshold_label = format(usage_gb, ".8g")
                notices.append((
                    "usage_notice",
                    f"account:{account_id}:usage:{threshold_label}:{expiry_version}",
                    "usage",
                ))

        recipients = _recipients(
            conn, account_id,
            int(account["owner_tg_id"]) if account["owner_tg_id"] is not None else None,
        )
        for purpose, event_key, kind in notices:
            for recipient in recipients:
                delivery = enqueue_delivery(
                    conn,
                    event_key=event_key,
                    bot_id=bot_id,
                    chat_id=recipient,
                    thread_id=None,
                    purpose=purpose,
                    resource_type="account",
                    resource_id=str(account_id),
                )
                outcomes.append({
                    "account_id": account_id,
                    "recipient_tg_id": recipient,
                    "kind": kind,
                    "event_key": event_key,
                    "delivery_id": int(delivery["delivery_id"]),
                    "replayed": bool(delivery.get("replayed", False)),
                })
    return outcomes


__all__ = ["NotificationError", "plan_expiry_notifications"]
