"""Root-controlled dedicated Telegram bot registry."""
from __future__ import annotations
import hmac
import inspect
import sqlite3
from contextlib import contextmanager
from typing import Any, Callable

import config
from confirmation_service import ConfirmationError, consume_confirmation, create_confirmation
from crypto_store import CryptoStoreError, decrypt_secret, encrypt_secret


class BotRegistryError(RuntimeError):
    """Unsafe or invalid registry operation."""
class BotRegistryPermissionError(BotRegistryError):
    """Only the configured root may manage registrations."""
class BotVerificationError(BotRegistryError):
    """Telegram getMe did not verify the declared identity."""
class BotRegistryConflict(BotRegistryError):
    """A unique bot, token, primary slot or representative assignment conflicts."""


def _positive(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise BotRegistryError(f"{label} must be a positive integer")
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise BotRegistryError(f"{label} must be a positive integer") from None
    if value <= 0:
        raise BotRegistryError(f"{label} must be a positive integer")
    return value


def _now(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise BotRegistryError("now must be a non-negative epoch-seconds integer")
    return value


def _root(actor_tg_id: int) -> int:
    actor = _positive(actor_tg_id, "actor_tg_id")
    try:
        root = int(getattr(config, "ADMIN_TELEGRAM_ID", 0))
    except (TypeError, ValueError):
        root = 0
    if isinstance(getattr(config, "ADMIN_TELEGRAM_ID", 0), bool) or root <= 0 or actor != root:
        raise BotRegistryPermissionError("only the configured root may manage bot registrations")
    return actor


@contextmanager
def _atomic(conn: sqlite3.Connection):
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    if conn.in_transaction:
        conn.execute("SAVEPOINT bot_registry_service")
        try:
            yield
            conn.execute("RELEASE SAVEPOINT bot_registry_service")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT bot_registry_service")
            conn.execute("RELEASE SAVEPOINT bot_registry_service")
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


def _get_me(result: Any) -> tuple[int, str]:
    if isinstance(result, dict):
        value = result.get("result", result)
        actual_id = value.get("id") if isinstance(value, dict) else None
        username = value.get("username", "") if isinstance(value, dict) else ""
    else:
        value = getattr(result, "result", result)
        actual_id = getattr(value, "id", None)
        username = getattr(value, "username", "")
    try:
        return _positive(actual_id, "getMe.id"), str(username or "").strip()
    except BotRegistryError:
        raise BotVerificationError("Telegram getMe returned no valid bot ID") from None


def _assert_token_unused(conn: sqlite3.Connection, token: str) -> None:
    for row in conn.execute(
        "SELECT token_cipher FROM bot_registry WHERE token_cipher IS NOT NULL"
    ).fetchall():
        try:
            existing = decrypt_secret(str(row["token_cipher"]), "hstagent/columns/v1")
        except (CryptoStoreError, TypeError, ValueError):
            raise BotRegistryError("an existing bot credential cannot be verified safely") from None
        if hmac.compare_digest(existing, token):
            raise BotRegistryConflict("this Telegram bot token is already registered")


async def verify_bot_registration(
    conn: sqlite3.Connection, *, actor_tg_id: int, bot_id: int, token: str,
    get_me: Callable[[str], Any], now: int, rep_id: int | None = None,
    is_primary: bool = False, ttl_seconds: int = 300,
) -> dict[str, Any]:
    """Verify getMe outside SQL transaction; store encrypted pending identity and preview."""
    actor = _root(actor_tg_id)
    declared = _positive(bot_id, "bot_id")
    now = _now(now)
    if not isinstance(token, str) or not token.strip() or not callable(get_me):
        raise BotRegistryError("bot token and get_me callable are required")
    token = token.strip()
    if not isinstance(is_primary, bool):
        raise BotRegistryError("is_primary must be a boolean")
    if is_primary:
        if rep_id is not None:
            raise BotRegistryError("primary bot cannot be assigned to a representative")
        selected_rep = None
    else:
        selected_rep = _positive(rep_id, "rep_id")
    if conn.in_transaction:
        raise BotRegistryError("Telegram getMe must run outside the database transaction")
    try:
        response = get_me(token)
        if inspect.isawaitable(response):
            response = await response
    except Exception:
        raise BotVerificationError("Telegram getMe verification failed") from None
    actual_id, username = _get_me(response)
    if actual_id != declared:
        raise BotVerificationError("declared bot ID does not match Telegram getMe")
    try:
        cipher = encrypt_secret(token, "hstagent/columns/v1")
    except CryptoStoreError:
        raise BotRegistryError("bot credential encryption failed") from None

    try:
        with _atomic(conn):
            _assert_token_unused(conn, token)
            if conn.execute("SELECT 1 FROM bot_registry WHERE bot_id=?", (declared,)).fetchone():
                raise BotRegistryConflict("bot ID is already registered")
            if selected_rep is not None and conn.execute(
                "SELECT 1 FROM representatives WHERE id=? AND is_active=1 AND deleted_at IS NULL",
                (selected_rep,),
            ).fetchone() is None:
                raise BotRegistryConflict("representative is inactive or unavailable")
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision) "
                "VALUES(?,?,?,?,?,'pending',NULL,1)",
                (declared, selected_rep, int(is_primary), cipher, username),
            )
            confirmation = create_confirmation(
                conn, actor_tg_id=actor, bot_id=declared, action="bot.activate",
                target_type="bot_registry", target_id=declared, expected_revision=1,
                payload={"bot_id": declared, "rep_id": selected_rep,
                         "is_primary": is_primary, "username": username, "revision": 1},
                now=now, ttl_seconds=ttl_seconds,
            )
    except sqlite3.IntegrityError:
        raise BotRegistryConflict("primary slot or representative uniqueness was violated") from None
    except ConfirmationError as exc:
        raise BotRegistryError("activation preview could not be created") from exc
    return {
        "bot_id": declared, "rep_id": selected_rep, "is_primary": is_primary,
        "username": username, "status": "pending", "revision": 1,
        "confirmation": confirmation, "replayed": False,
    }


def activate_bot(conn: sqlite3.Connection, *, actor_tg_id: int, bot_id: int,
                 nonce: str, now: int) -> dict[str, Any]:
    """Consume root preview and activate one pending identity exactly once."""
    actor, selected, now = _root(actor_tg_id), _positive(bot_id, "bot_id"), _now(now)
    try:
        with _atomic(conn):
            row = conn.execute(
                "SELECT status,revision FROM bot_registry WHERE bot_id=?", (selected,)
            ).fetchone()
            if row is None:
                raise BotRegistryError("bot registration does not exist")
            status, revision = str(row["status"]), int(row["revision"])
            if status == "pending":
                expected = revision
            elif status == "active" and revision > 1:
                expected = revision - 1
            else:
                raise BotRegistryConflict("only a pending bot can be activated")
            consumed = consume_confirmation(
                conn, nonce=nonce, actor_tg_id=actor, bot_id=selected,
                action="bot.activate", target_type="bot_registry", target_id=selected,
                expected_revision=expected, now=now,
            )
            if status == "active":
                return {"bot_id": selected, "status": "active",
                        "revision": revision, "replayed": bool(consumed["replayed"])}
            # Contractual compare-and-set transition from pending to active.
            conn.execute(
                "UPDATE bot_registry SET status='active',verified_at=?,revision=revision+1 "
                "WHERE bot_id=? AND status='pending'", (now, selected),
            )
            return {"bot_id": selected, "status": "active",
                    "revision": revision + 1, "replayed": False}
    except sqlite3.IntegrityError:
        raise BotRegistryConflict("primary slot or active representative uniqueness was violated") from None
    except ConfirmationError as exc:
        raise BotRegistryError("activation confirmation is invalid, stale or expired") from exc


def prepare_bot_disable(conn: sqlite3.Connection, *, actor_tg_id: int, bot_id: int,
                        now: int, ttl_seconds: int = 300) -> dict[str, Any]:
    """Preview a disable operation while preserving orders and bot customer links."""
    actor, selected, now = _root(actor_tg_id), _positive(bot_id, "bot_id"), _now(now)
    try:
        with _atomic(conn):
            row = conn.execute(
                "SELECT status,revision,rep_id,is_primary FROM bot_registry WHERE bot_id=?",
                (selected,),
            ).fetchone()
            if row is None or row["status"] != "active":
                raise BotRegistryConflict("only an active bot can be disabled")
            pending = int(conn.execute(
                "SELECT COUNT(*) FROM payment_orders WHERE bot_id=? "
                "AND status NOT IN ('completed','cancelled','closed_refunded','expired','definite_failure')",
                (selected,),
            ).fetchone()[0])
            customers = int(conn.execute(
                "SELECT COUNT(*) FROM bot_customers WHERE bot_id=?", (selected,)
            ).fetchone()[0])
            revision = int(row["revision"])
            confirmation = create_confirmation(
                conn, actor_tg_id=actor, bot_id=selected, action="bot.disable",
                target_type="bot_registry", target_id=selected, expected_revision=revision,
                payload={"bot_id": selected, "rep_id": row["rep_id"],
                         "is_primary": bool(row["is_primary"]),
                         "pending_orders": pending, "linked_customers": customers,
                         "revision": revision},
                now=now, ttl_seconds=ttl_seconds,
            )
    except ConfirmationError as exc:
        raise BotRegistryError("disable preview could not be created") from exc
    return {"bot_id": selected, "status": "active", "revision": revision,
            "pending_orders": pending, "linked_customers": customers,
            "confirmation": confirmation}


def disable_bot(conn: sqlite3.Connection, *, actor_tg_id: int, bot_id: int,
                nonce: str, now: int) -> dict[str, Any]:
    """Disable active bot; do not delete, reassign or mutate pending orders."""
    actor, selected, now = _root(actor_tg_id), _positive(bot_id, "bot_id"), _now(now)
    try:
        with _atomic(conn):
            row = conn.execute(
                "SELECT status,revision FROM bot_registry WHERE bot_id=?", (selected,)
            ).fetchone()
            if row is None:
                raise BotRegistryError("bot registration does not exist")
            status, revision = str(row["status"]), int(row["revision"])
            if status == "active":
                expected = revision
            elif status == "disabled" and revision > 1:
                expected = revision - 1
            else:
                raise BotRegistryConflict("only an active bot can be disabled")
            consumed = consume_confirmation(
                conn, nonce=nonce, actor_tg_id=actor, bot_id=selected,
                action="bot.disable", target_type="bot_registry", target_id=selected,
                expected_revision=expected, now=now,
            )
            if status == "disabled":
                return {"bot_id": selected, "status": "disabled",
                        "revision": revision, "replayed": bool(consumed["replayed"])}
            conn.execute(
                "UPDATE bot_registry SET status='disabled',revision=revision+1 "
                "WHERE bot_id=? AND status='active'", (selected,),
            )
            return {"bot_id": selected, "status": "disabled",
                    "revision": revision + 1, "replayed": False}
    except ConfirmationError as exc:
        raise BotRegistryError("disable confirmation is invalid, stale or expired") from exc


__all__ = [
    "BotRegistryError", "BotRegistryPermissionError", "BotVerificationError",
    "BotRegistryConflict", "verify_bot_registration", "activate_bot",
    "prepare_bot_disable", "disable_bot",
]
