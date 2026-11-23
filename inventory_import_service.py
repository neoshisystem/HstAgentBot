"""Staged, encrypted hand-off between an import preview and its confirmation.

Why this module exists
----------------------
The operator pastes a batch, sees a preview ("تعداد اقلام: N"), and presses
"تأیید ورود موجودی". The parsed items used to live only in a local variable of
the preview call, and ``v52_router.route_text`` *consumes* (pops) the FSM entry
before the preview is rendered. Nothing therefore carried the batch from the
preview to the confirmation, and the confirm callback was wired to the entry
handler — so the batch was silently dropped and the import inserted nothing.

This module is the missing bridge. The preview stages the encrypted batch under a
short random token, and the confirmation consumes that token with a
compare-and-swap, exactly like ``confirmation_service`` does for public
snapshots. The difference is deliberate: an inventory item IS a secret, so the
payload is encrypted with the column key rather than stored as canonical JSON.

The token is bound to actor, bot, kind and target, is single-use, and expires.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
from typing import Any

from crypto_store import decrypt_secret, encrypt_secret

_COLUMNS_LABEL = "hstagent/columns/v1"
_TOKEN_RE_LENGTH = 24
DEFAULT_TTL_SECONDS = 900
MAX_TTL_SECONDS = 3600
KINDS = ("digital_inventory", "manual_pool")

_STAGING_DDL = """
CREATE TABLE IF NOT EXISTS inventory_import_staging (
    token TEXT PRIMARY KEY,
    actor_tg_id INTEGER NOT NULL,
    bot_id INTEGER NOT NULL,
    kind TEXT NOT NULL,
    target_id INTEGER NOT NULL,
    payload_cipher TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    meta_json TEXT NOT NULL DEFAULT '{}',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    consumed_at INTEGER
)
"""

# Each kind owns its own callback prefix. A shared prefix would route a staged
# manual-pool batch into the digital-inventory confirmation, which then fails the
# kind check — the operator would see "not found" on a perfectly valid preview.
_CALLBACK_PREFIXES: dict[str, tuple[str, str]] = {
    "digital_inventory": ("v52:invok:", "v52:invno:"),
    "manual_pool": ("v52:poolok:", "v52:poolno:"),
}


class StagingError(ValueError):
    """Base error for a staged import hand-off."""


class StagingNotFound(StagingError):
    """The token does not exist, or belongs to another actor/bot/kind/target."""


class StagingExpired(StagingError):
    """The staged batch expired before it was confirmed."""


class StagingConsumed(StagingError):
    """The staged batch was already confirmed."""


def ensure_staging_table(conn: sqlite3.Connection) -> None:
    """Create the staging table on first use.

    ``init_db`` creates it too, but a database restored from a backup, a test
    fixture or an audit clone predates that call. Every accessor here guarantees
    it first, so a clone can never raise ``OperationalError: no such table``.
    """
    conn.execute(_STAGING_DDL)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_import_staging_expiry "
        "ON inventory_import_staging(expires_at)"
    )


def _positive(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise StagingError(f"{label} باید یک عدد صحیح مثبت باشد.")
    return value


def _timestamp(value: Any, label: str = "now") -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise StagingError(f"{label} باید یک زمان یونیکس نامنفی باشد.")
    return value


def _kind(value: Any) -> str:
    selected = str(value or "").strip().lower()
    if selected not in KINDS:
        raise StagingError("نوع ورود موجودی نامعتبر است.")
    return selected


def _items(values: Any) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)) or not values:
        raise StagingError("فهرست اقلام برای ذخیره‌سازی موقت خالی است.")
    out: list[str] = []
    for item in values:
        if not isinstance(item, str) or not item.strip():
            raise StagingError("هر قلم باید متن غیرخالی باشد.")
        out.append(item)
    return out


def _meta(payload: Any) -> str:
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise StagingError("اطلاعات همراه ورود موجودی باید یک دیکشنری باشد.")
    safe = {str(key)[:60]: value for key, value in payload.items()
            if isinstance(key, str) and key}
    return json.dumps(safe, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stage(
    conn: sqlite3.Connection,
    *,
    actor_tg_id: int,
    bot_id: int,
    kind: str,
    target_id: int,
    items: Any,
    meta: dict[str, Any] | None = None,
    now: int | None = None,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    encryptor=None,
) -> dict[str, Any]:
    """Persist an encrypted batch and return its one-use confirmation callbacks."""
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    actor = _positive(actor_tg_id, "actor_tg_id")
    bot = _positive(bot_id, "bot_id")
    selected_kind = _kind(kind)
    target = _positive(target_id, "target_id")
    prepared = _items(items)
    timestamp = _timestamp(int(time.time()) if now is None else now)
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) \
            or not 1 <= ttl_seconds <= MAX_TTL_SECONDS:
        raise StagingError(
            f"مدت اعتبار پیش‌نمایش باید بین ۱ و {MAX_TTL_SECONDS} ثانیه باشد."
        )
    cipher_fn = encryptor or encrypt_secret
    ensure_staging_table(conn)
    cipher = cipher_fn(
        json.dumps(prepared, ensure_ascii=False, separators=(",", ":")),
        _COLUMNS_LABEL,
    )
    expires_at = timestamp + ttl_seconds
    confirm_prefix, cancel_prefix = _CALLBACK_PREFIXES[selected_kind]
    for _ in range(5):
        token = secrets.token_urlsafe(18)
        if len(token) != _TOKEN_RE_LENGTH:
            continue
        callbacks = {
            "confirm": f"{confirm_prefix}{token}",
            "cancel": f"{cancel_prefix}{token}",
        }
        if max(len(value.encode("utf-8")) for value in callbacks.values()) >= 64:
            raise AssertionError(
                "staged-import callback exceeds Telegram's 64-byte limit"
            )
        try:
            conn.execute(
                "INSERT INTO inventory_import_staging("
                "token,actor_tg_id,bot_id,kind,target_id,payload_cipher,item_count,"
                "meta_json,created_at,expires_at,consumed_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                (token, actor, bot, selected_kind, target, cipher, len(prepared),
                 _meta(meta), timestamp, expires_at),
            )
        except sqlite3.IntegrityError:
            continue
        return {
            "token": token,
            "callback_confirm": callbacks["confirm"],
            "callback_cancel": callbacks["cancel"],
            "item_count": len(prepared),
            "expires_at": expires_at,
            "kind": selected_kind,
            "target_id": target,
        }
    raise StagingError("تولید شناسه یکتا برای پیش‌نمایش ممکن نشد.")


def consume(
    conn: sqlite3.Connection,
    *,
    token: str,
    actor_tg_id: int,
    bot_id: int,
    kind: str,
    target_id: int,
    now: int | None = None,
    decryptor=None,
) -> dict[str, Any]:
    """Compare-and-swap the staged batch and return its decrypted items.

    A replay of the same token after a successful consume raises
    :class:`StagingConsumed` rather than inserting the batch twice.
    """
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    actor = _positive(actor_tg_id, "actor_tg_id")
    bot = _positive(bot_id, "bot_id")
    selected_kind = _kind(kind)
    target = _positive(target_id, "target_id")
    timestamp = _timestamp(int(time.time()) if now is None else now)
    if not isinstance(token, str) or len(token) != _TOKEN_RE_LENGTH:
        raise StagingNotFound("پیش‌نمایش ورود موجودی یافت نشد.")
    cipher_fn = decryptor or decrypt_secret
    ensure_staging_table(conn)
    row = conn.execute(
        "SELECT actor_tg_id,bot_id,kind,target_id,payload_cipher,item_count,"
        "meta_json,expires_at,consumed_at FROM inventory_import_staging WHERE token=?",
        (token,),
    ).fetchone()
    if row is None:
        raise StagingNotFound("پیش‌نمایش ورود موجودی یافت نشد یا منقضی شده است.")
    if (int(row[0]) != actor or int(row[1]) != bot
            or str(row[2]) != selected_kind or int(row[3]) != target):
        raise StagingNotFound("این پیش‌نمایش به این حساب یا محصول تعلق ندارد.")
    if row[8] is not None:
        raise StagingConsumed("این پیش‌نمایش قبلاً تأیید شده است.")
    if int(row[7]) <= timestamp:
        raise StagingExpired("اعتبار پیش‌نمایش به پایان رسیده است؛ لطفاً دوباره ارسال کنید.")
    changed = conn.execute(
        "UPDATE inventory_import_staging SET consumed_at=? "
        "WHERE token=? AND consumed_at IS NULL AND expires_at>?",
        (timestamp, token, timestamp),
    )
    if changed.rowcount != 1:
        raise StagingConsumed("این پیش‌نمایش هم‌اکنون توسط درخواست دیگری تأیید شد.")
    try:
        items = json.loads(cipher_fn(str(row[4]), _COLUMNS_LABEL))
    except Exception as exc:  # noqa: BLE001 - surfaced as a domain error
        raise StagingError("محتوای ذخیره‌شدهٔ این پیش‌نمایش قابل خواندن نیست.") from exc
    if not isinstance(items, list) or not items:
        raise StagingError("محتوای ذخیره‌شدهٔ این پیش‌نمایش خالی است.")
    try:
        meta = json.loads(str(row[6]) or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        meta = {}
    return {
        "token": token,
        "items": [str(item) for item in items],
        "item_count": int(row[5]),
        "meta": meta if isinstance(meta, dict) else {},
        # The caller must re-drive the insert against the target the batch was
        # staged for. It is returned here rather than re-read by the caller so the
        # value can never disagree with the row that was just compare-and-swapped
        # — reading it separately is what made the confirmation path raise
        # ``KeyError: 'target_id'`` and drop the batch (``BUG-SHOP-002``).
        "kind": selected_kind,
        "target_id": target,
    }


def cancel(
    conn: sqlite3.Connection,
    *,
    token: str,
    actor_tg_id: int,
    bot_id: int,
    now: int | None = None,
) -> dict[str, Any]:
    """Drop a staged batch without importing it. Idempotent for the owner."""
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    actor = _positive(actor_tg_id, "actor_tg_id")
    bot = _positive(bot_id, "bot_id")
    timestamp = _timestamp(int(time.time()) if now is None else now)
    if not isinstance(token, str) or len(token) != _TOKEN_RE_LENGTH:
        raise StagingNotFound("پیش‌نمایش ورود موجودی یافت نشد.")
    ensure_staging_table(conn)
    row = conn.execute(
        "SELECT actor_tg_id,bot_id,consumed_at FROM inventory_import_staging WHERE token=?",
        (token,),
    ).fetchone()
    if row is None or int(row[0]) != actor or int(row[1]) != bot:
        raise StagingNotFound("پیش‌نمایش ورود موجودی یافت نشد.")
    if row[2] is not None:
        return {"token": token, "cancelled": False, "already_consumed": True}
    conn.execute(
        "UPDATE inventory_import_staging SET consumed_at=? "
        "WHERE token=? AND consumed_at IS NULL",
        (timestamp, token),
    )
    return {"token": token, "cancelled": True, "already_consumed": False}


def purge_expired(conn: sqlite3.Connection, *, now: int | None = None) -> int:
    """Delete expired, never-confirmed batches. Returns the number removed."""
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    timestamp = _timestamp(int(time.time()) if now is None else now)
    ensure_staging_table(conn)
    cursor = conn.execute(
        "DELETE FROM inventory_import_staging WHERE expires_at<=? AND consumed_at IS NULL",
        (timestamp,),
    )
    return int(cursor.rowcount or 0)


__all__ = [
    "DEFAULT_TTL_SECONDS",
    "KINDS",
    "MAX_TTL_SECONDS",
    "StagingConsumed",
    "StagingError",
    "StagingExpired",
    "StagingNotFound",
    "cancel",
    "consume",
    "ensure_staging_table",
    "purge_expired",
    "stage",
]
