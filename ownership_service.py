"""Root-only, preview-bound account ownership transfer with immutable financial history."""
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
from confirmation_service import (
    ConfirmationError, ConfirmationExpired, ConfirmationMismatch,
    ConfirmationStale, consume_confirmation, create_confirmation,
)
from crypto_store import encrypt_secret

_LABEL = "hstagent/columns/v1"
_ACTION = "account.ownership.transfer"
_TARGET = "account"


class OwnershipError(ValueError):
    """An ownership transfer cannot be completed safely."""


class OwnershipPermissionError(OwnershipError):
    """Only the configured root may transfer account ownership."""


class OwnershipConflict(OwnershipError):
    """The account, pending operation, or preview changed before transfer."""


def _value(ctx: Any, name: str, default=None):
    if isinstance(ctx, Mapping):
        return ctx.get(name, default)
    return getattr(ctx, name, default)


def _positive(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise OwnershipError(f"{name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise OwnershipError(f"{name} must be a positive integer") from None
    if result <= 0:
        raise OwnershipError(f"{name} must be a positive integer")
    return result


def _actor_id(ctx: Any) -> int:
    return _positive(_value(ctx, "actor_tg_id", _value(ctx, "telegram_id")), "actor_tg_id")


def _root(ctx: Any) -> int:
    actor_id = _actor_id(ctx)
    configured = getattr(config, "ADMIN_TELEGRAM_ID", 0)
    if isinstance(configured, bool):
        configured = 0
    try:
        configured = int(configured)
    except (TypeError, ValueError):
        configured = 0
    if configured <= 0 or actor_id != configured:
        raise OwnershipPermissionError("only the configured root may transfer ownership")
    return actor_id


def _bot_id(ctx: Any, explicit: int | None = None) -> int:
    return _positive(explicit if explicit is not None else _value(ctx, "bot_id"), "bot_id")


def _timestamp(now: int | None = None) -> int:
    result = int(time.time()) if now is None else now
    if isinstance(result, bool) or not isinstance(result, int) or result < 0:
        raise OwnershipError("now must be a non-negative UTC epoch-seconds integer")
    return result


@contextmanager
def _atomic(conn: sqlite3.Connection):
    if conn.in_transaction:
        point = "w14_owner_" + secrets.token_hex(5)
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
        raise OwnershipError("the selected Telegram bot is not registered and active")


def _assert_no_unknown_mutation(conn: sqlite3.Connection, account_id: int) -> None:
    row = conn.execute(
        "SELECT operation_id,state FROM panel_operations WHERE account_id=? "
        "AND state IN('dispatching','needs_reconciliation') LIMIT 1",
        (account_id,),
    ).fetchone()
    if row is not None:
        raise OwnershipConflict(
            "ownership transfer is blocked by an unresolved panel mutation"
        )


def _reason(value: str) -> str:
    if not isinstance(value, str):
        raise OwnershipError("a transfer reason is required")
    result = value.strip()
    if not 3 <= len(result) <= 1000:
        raise OwnershipError("transfer reason must contain 3 to 1000 characters")
    return result


def preview_transfer(
    actor_ctx: Any, account_id: int, new_owner_tg_id: int, *,
    reason: str, bot_id: int | None = None, seller_mode: str = "preserve",
    now: int | None = None,
) -> dict:
    """Persist a 300-second root/bot/revision-bound confirmation without changing the account."""
    actor_id = _root(actor_ctx)
    selected_account = _positive(account_id, "account_id")
    target_owner = _positive(new_owner_tg_id, "new_owner_tg_id")
    selected_bot = _bot_id(actor_ctx, bot_id)
    timestamp = _timestamp(now)
    selected_reason = _reason(reason)
    mode = str(seller_mode or "").strip().lower()
    if mode not in {"preserve", "root_direct"}:
        raise OwnershipError("seller_mode must be preserve or root_direct")

    with database.get_db() as conn, _atomic(conn):
        _validate_bot(conn, selected_bot)
        _assert_no_unknown_mutation(conn, selected_account)
        account = conn.execute(
            "SELECT id,owner_tg_id,seller_key,rep_id,revision,acl_revision,"
            "account_code,email,deleted_at FROM accounts WHERE id=?",
            (selected_account,),
        ).fetchone()
        if account is None or account["deleted_at"] is not None:
            raise OwnershipError("account does not exist or is deleted")
        owner = conn.execute(
            "SELECT telegram_id,is_blocked FROM bot_users WHERE telegram_id=?",
            (target_owner,),
        ).fetchone()
        if owner is None or bool(owner["is_blocked"]):
            raise OwnershipError("new owner must be an existing, unblocked Telegram user")

        revision = int(account["revision"])
        before = {
            "owner_tg_id": int(account["owner_tg_id"]) if account["owner_tg_id"] is not None else None,
            "seller_key": str(account["seller_key"] or "root"),
            "rep_id": int(account["rep_id"]) if account["rep_id"] is not None else None,
            "revision": revision,
            "acl_revision": int(account["acl_revision"]),
        }
        if mode == "root_direct":
            after_seller, after_rep = "root", None
        else:
            after_seller = before["seller_key"]
            after_rep = before["rep_id"]
        after = {
            "owner_tg_id": target_owner,
            "seller_key": after_seller,
            "rep_id": after_rep,
            "revision": revision + 1,
            "acl_revision": before["acl_revision"] + 1,
        }
        payload = {
            "account_id": selected_account,
            "before": before,
            "after": after,
            "reason": selected_reason,
            "seller_mode": mode,
        }
        confirmation = create_confirmation(
            conn,
            actor_tg_id=actor_id,
            bot_id=selected_bot,
            action=_ACTION,
            target_type=_TARGET,
            target_id=selected_account,
            expected_revision=revision,
            payload=payload,
            now=timestamp,
            ttl_seconds=300,
        )
    return {
        "account_id": selected_account,
        "before": before,
        "after": after,
        "reason": selected_reason,
        "seller_mode": mode,
        "nonce": confirmation["nonce"],
        "callback_ok": confirmation["callback_ok"],
        "callback_no": confirmation["callback_no"],
        "expires_at": confirmation["expires_at"],
        "expected_revision": revision,
        "replayed": False,
    }


def execute_transfer(
    actor_ctx: Any, account_id: int, new_owner_tg_id: int, nonce: str, *,
    bot_id: int | None = None, now: int | None = None,
) -> dict:
    """Consume the preview once, CAS-update ownership, revoke grants, and audit without touching finance."""
    actor_id = _root(actor_ctx)
    selected_account = _positive(account_id, "account_id")
    target_owner = _positive(new_owner_tg_id, "new_owner_tg_id")
    selected_bot = _bot_id(actor_ctx, bot_id)
    timestamp = _timestamp(now)

    try:
        with database.get_db() as conn, _atomic(conn):
            _validate_bot(conn, selected_bot)
            confirmation = conn.execute(
                "SELECT expected_revision,payload_json FROM action_confirmations WHERE nonce=?",
                (str(nonce),),
            ).fetchone()
            if confirmation is None:
                raise OwnershipError("ownership preview does not exist")
            expected_revision = int(confirmation["expected_revision"])
            payload = json.loads(str(confirmation["payload_json"]))
            if (
                int(payload.get("account_id", 0)) != selected_account
                or int(payload.get("after", {}).get("owner_tg_id", 0)) != target_owner
            ):
                raise OwnershipConflict("ownership preview does not match the requested transfer")

            account = conn.execute(
                "SELECT id,owner_tg_id,seller_key,rep_id,revision,acl_revision,"
                "account_code,email,deleted_at FROM accounts WHERE id=?",
                (selected_account,),
            ).fetchone()
            if account is None or account["deleted_at"] is not None:
                raise OwnershipError("account does not exist or is deleted")

            consumed = consume_confirmation(
                conn,
                nonce=str(nonce),
                actor_tg_id=actor_id,
                bot_id=selected_bot,
                action=_ACTION,
                target_type=_TARGET,
                target_id=selected_account,
                expected_revision=expected_revision,
                now=timestamp,
                operation_id=f"ownership-transfer:{nonce}",
            )
            if consumed["replayed"]:
                if (
                    account["owner_tg_id"] is not None
                    and int(account["owner_tg_id"]) == target_owner
                    and str(account["seller_key"] or "root") == str(payload["after"]["seller_key"])
                    and (
                        int(account["rep_id"]) if account["rep_id"] is not None else None
                    ) == payload["after"]["rep_id"]
                ):
                    return {
                        "account_id": selected_account,
                        "owner_tg_id": target_owner,
                        "seller_key": str(account["seller_key"] or "root"),
                        "rep_id": int(account["rep_id"]) if account["rep_id"] is not None else None,
                        "revoked_grants": 0,
                        "credentials_rotated": False,
                        "operation_id": consumed["operation_id"],
                        "replayed": True,
                    }
                raise OwnershipConflict("consumed ownership operation does not match current account state")

            _assert_no_unknown_mutation(conn, selected_account)
            if int(account["revision"]) != expected_revision:
                raise ConfirmationStale("account changed after ownership preview")
            before = payload["before"]
            if (
                (int(account["owner_tg_id"]) if account["owner_tg_id"] is not None else None) != before["owner_tg_id"]
                or str(account["seller_key"] or "root") != before["seller_key"]
                or (int(account["rep_id"]) if account["rep_id"] is not None else None) != before["rep_id"]
            ):
                raise ConfirmationStale("account ownership changed after preview")

            after = payload["after"]
            changed = conn.execute(
                "UPDATE accounts SET owner_tg_id=?,seller_key=?,rep_id=?,"
                "acl_revision=acl_revision+1,revision=revision+1 "
                "WHERE id=? AND revision=? AND deleted_at IS NULL",
                (
                    target_owner, str(after["seller_key"]), after["rep_id"],
                    selected_account, expected_revision,
                ),
            ).rowcount
            if changed != 1:
                raise OwnershipConflict("account ownership compare-and-swap failed")

            revoked = conn.execute(
                "UPDATE account_access_grants SET revoked_at=?,revision=revision+1 "
                "WHERE account_id=? AND revoked_at IS NULL",
                (timestamp, selected_account),
            ).rowcount

            audit_details = {
                "operation_id": consumed["operation_id"],
                "nonce": str(nonce),
                "reason": str(payload["reason"]),
                "before": before,
                "after": after,
                "revoked_grants": int(revoked),
                "credentials_rotated": False,
                "membership_rows_preserved": True,
            }
            details_cipher = encrypt_secret(
                json.dumps(audit_details, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                _LABEL,
            )
            conn.execute(
                "INSERT INTO audit_logs(rep_id,account_code,email,action,actor_id,actor_name,"
                "details_json,result_status,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    before["rep_id"], str(account["account_code"]), str(account["email"]),
                    "ownership_transfer", actor_id, "root", details_cipher,
                    "success", timestamp,
                ),
            )
            return {
                "account_id": selected_account,
                "owner_tg_id": target_owner,
                "seller_key": str(after["seller_key"]),
                "rep_id": after["rep_id"],
                "revoked_grants": int(revoked),
                "credentials_rotated": False,
                "operation_id": consumed["operation_id"],
                "replayed": False,
            }
    except (ConfirmationError, sqlite3.IntegrityError) as exc:
        if isinstance(exc, ConfirmationStale):
            raise
        raise OwnershipConflict("ownership confirmation or database constraint failed") from exc


__all__ = [
    "OwnershipError", "OwnershipPermissionError", "OwnershipConflict",
    "preview_transfer", "execute_transfer",
]
