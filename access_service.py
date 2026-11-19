"""Account ACL and co-ownership grants for HstAgentBot v5.2."""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import time
from collections.abc import Mapping

import config
import database


_ACTIONS = frozenset({
    "view_config", "renew", "rotate", "grant", "traffic",
    "view_stats", "transfer",
})


def _actor(ctx) -> dict:
    if isinstance(ctx, Mapping):
        get = ctx.get
    else:
        get = lambda key, default=None: getattr(ctx, key, default)
    actor_id = get("actor_tg_id", get("telegram_id", None))
    if isinstance(actor_id, bool):
        raise TypeError("actor Telegram ID must be a positive integer")
    try:
        actor_id = int(actor_id)
    except (TypeError, ValueError) as exc:
        raise TypeError("actor Telegram ID must be a positive integer") from exc
    if actor_id <= 0:
        raise ValueError("actor Telegram ID must be a positive integer")
    configured_root = getattr(config, "ADMIN_TELEGRAM_ID", 0)
    try:
        configured_root = int(configured_root)
    except (TypeError, ValueError):
        configured_root = 0
    return {
        "id": actor_id,
        "root": configured_root > 0 and actor_id == configured_root,
        "blocked": bool(get("is_blocked", False)),
        "rep_id": get("rep_id", None),
    }


def _grant_level(conn: sqlite3.Connection, account_id: int, actor_id: int):
    row = conn.execute(
        "SELECT access_level FROM account_access_grants "
        "WHERE account_id=? AND grantee_tg_id=? AND revoked_at IS NULL",
        (account_id, actor_id),
    ).fetchone()
    return None if row is None else str(row["access_level"])


def _is_direct_seller(
    conn: sqlite3.Connection,
    account: sqlite3.Row,
    actor_id: int,
) -> bool:
    if account["rep_id"] is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM representatives "
        "WHERE id=? AND telegram_id=? AND is_active=1 AND deleted_at IS NULL",
        (int(account["rep_id"]), actor_id),
    ).fetchone()
    return row is not None


def _is_parent_of_seller(
    conn: sqlite3.Connection,
    account: sqlite3.Row,
    actor: dict,
) -> bool:
    if actor["rep_id"] is None or account["rep_id"] is None:
        return False
    try:
        rep_id = int(actor["rep_id"])
    except (TypeError, ValueError):
        return False
    row = conn.execute(
        "SELECT 1 FROM representatives child "
        "JOIN representatives parent ON parent.id=child.parent_rep_id "
        "WHERE child.id=? AND parent.id=? AND parent.telegram_id=? "
        "AND parent.is_active=1 AND parent.deleted_at IS NULL",
        (int(account["rep_id"]), rep_id, actor["id"]),
    ).fetchone()
    return row is not None


def _authorize_row(
    conn: sqlite3.Connection,
    actor: dict,
    account: sqlite3.Row | None,
    action: str,
) -> bool:
    if action not in _ACTIONS:
        raise ValueError(f"unsupported account action: {action}")
    if account is None:
        return False
    if actor["blocked"] and not actor["root"]:
        return False
    if actor["root"]:
        return True

    actor_id = actor["id"]
    owner = account["owner_tg_id"] is not None and int(account["owner_tg_id"]) == actor_id
    seller = _is_direct_seller(conn, account, actor_id)
    level = _grant_level(conn, int(account["id"]), actor_id)
    has_grant = level in {"viewer", "manager"}
    manager = level == "manager"

    if action == "view_stats":
        return owner or seller or has_grant or _is_parent_of_seller(conn, account, actor)
    if action == "view_config":
        return owner or seller or has_grant
    if action == "renew":
        return owner or seller or manager
    if action == "rotate":
        return owner or seller or manager
    if action == "grant":
        return owner or seller
    if action == "transfer":
        return owner
    # Traffic reset/adjustment is a root-only operation.
    return False


def authorize_account(ctx, account_id: int, action: str) -> bool:
    """Return whether the actor may perform an action on this exact account ID."""
    if isinstance(account_id, bool):
        raise TypeError("account_id must be a positive integer")
    try:
        exact_id = int(account_id)
    except (TypeError, ValueError) as exc:
        raise TypeError("account_id must be a positive integer") from exc
    if exact_id <= 0:
        raise ValueError("account_id must be a positive integer")
    actor = _actor(ctx)
    with database.get_db() as conn:
        account = conn.execute(
            "SELECT * FROM accounts WHERE id=? AND deleted_at IS NULL",
            (exact_id,),
        ).fetchone()
        return _authorize_row(conn, actor, account, action)


def list_visible_accounts(ctx) -> list[dict]:
    """List non-secret account summaries and aggregate-only child summaries."""
    actor = _actor(ctx)
    if actor["blocked"] and not actor["root"]:
        return []

    visible: list[dict] = []
    with database.get_db() as conn:
        accounts = conn.execute(
            "SELECT * FROM accounts WHERE deleted_at IS NULL ORDER BY id"
        ).fetchall()
        for account in accounts:
            if not _authorize_row(conn, actor, account, "view_config"):
                continue
            item = dict(account)
            # Secret material is delivered only through a separately authorized
            # DM flow; it never belongs in an account-list result.
            item.pop("sub_id", None)
            item.pop("sub_id_cipher", None)
            if actor["root"]:
                access = "root"
            elif account["owner_tg_id"] is not None and int(account["owner_tg_id"]) == actor["id"]:
                access = "owner"
            elif _is_direct_seller(conn, account, actor["id"]):
                access = "seller"
            else:
                access = _grant_level(conn, int(account["id"]), actor["id"])
            item["access_level"] = access
            visible.append(item)

        if actor["rep_id"] is not None and not actor["root"]:
            children = conn.execute(
                "SELECT child.id,COUNT(accounts.id) AS account_count "
                "FROM representatives child "
                "JOIN representatives parent ON parent.id=child.parent_rep_id "
                "LEFT JOIN accounts ON accounts.rep_id=child.id AND accounts.deleted_at IS NULL "
                "WHERE parent.id=? AND parent.telegram_id=? "
                "AND parent.is_active=1 AND parent.deleted_at IS NULL "
                "GROUP BY child.id ORDER BY child.id",
                (int(actor["rep_id"]), actor["id"]),
            ).fetchall()
            for child in children:
                visible.append({
                    "kind": "representative_summary",
                    "rep_id": int(child["id"]),
                    "account_count": int(child["account_count"]),
                })
    return visible


@contextmanager
def _atomic(conn: sqlite3.Connection):
    if conn.in_transaction:
        conn.execute("SAVEPOINT v52_acl")
        try:
            yield
            conn.execute("RELEASE SAVEPOINT v52_acl")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT v52_acl")
            conn.execute("RELEASE SAVEPOINT v52_acl")
            raise
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _epoch_seconds(now) -> int:
    value = int(time.time()) if now is None else now
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError("now must be a non-negative UTC epoch-seconds integer")
    return value


def set_grant(
    ctx,
    account_id: int,
    grantee_tg_id: int,
    access_level: str,
    *,
    now: int | None = None,
) -> dict:
    """Create or revise a viewer/manager grant without changing owner or balance."""
    actor = _actor(ctx)
    if isinstance(account_id, bool) or isinstance(grantee_tg_id, bool):
        raise TypeError("account and grantee IDs must be positive integers")
    account_id, grantee_tg_id = int(account_id), int(grantee_tg_id)
    if account_id <= 0 or grantee_tg_id <= 0:
        raise ValueError("account and grantee IDs must be positive integers")
    level = str(access_level).strip().lower()
    if level not in {"viewer", "manager"}:
        raise ValueError("access_level must be viewer or manager")
    if grantee_tg_id == actor["id"]:
        raise ValueError("self-grants are not supported")
    timestamp = _epoch_seconds(now)

    with database.get_db() as conn:
        with _atomic(conn):
            account = conn.execute(
                "SELECT * FROM accounts WHERE id=? AND deleted_at IS NULL",
                (account_id,),
            ).fetchone()
            if not _authorize_row(conn, actor, account, "grant"):
                raise PermissionError("actor cannot grant access to this account")
            grantee = conn.execute(
                "SELECT is_blocked FROM bot_users WHERE telegram_id=?",
                (grantee_tg_id,),
            ).fetchone()
            if grantee is None or bool(grantee["is_blocked"]):
                raise ValueError("grantee must be an existing, unblocked Telegram user")

            conn.execute(
                "INSERT INTO account_access_grants("
                "account_id,grantee_tg_id,access_level,granted_by_tg_id,created_at,revoked_at,revision"
                ") VALUES(?,?,?,?,?,NULL,1) "
                "ON CONFLICT(account_id,grantee_tg_id) DO UPDATE SET "
                "access_level=excluded.access_level,"
                "granted_by_tg_id=excluded.granted_by_tg_id,"
                "created_at=excluded.created_at,revoked_at=NULL,"
                "revision=account_access_grants.revision+1",
                (account_id, grantee_tg_id, level, actor["id"], timestamp),
            )
            conn.execute(
                "UPDATE accounts SET acl_revision=acl_revision+1 WHERE id=?",
                (account_id,),
            )
            row = conn.execute(
                "SELECT account_id,grantee_tg_id,access_level,granted_by_tg_id,"
                "created_at,revoked_at,revision FROM account_access_grants "
                "WHERE account_id=? AND grantee_tg_id=?",
                (account_id, grantee_tg_id),
            ).fetchone()
            return dict(row)


def revoke_grant(
    ctx,
    account_id: int,
    grantee_tg_id: int,
    *,
    now: int | None = None,
) -> bool:
    """Revoke a grant in place, retaining its history and incrementing ACL revisions."""
    actor = _actor(ctx)
    if isinstance(account_id, bool) or isinstance(grantee_tg_id, bool):
        raise TypeError("account and grantee IDs must be positive integers")
    account_id, grantee_tg_id = int(account_id), int(grantee_tg_id)
    if account_id <= 0 or grantee_tg_id <= 0:
        raise ValueError("account and grantee IDs must be positive integers")
    timestamp = _epoch_seconds(now)

    with database.get_db() as conn:
        with _atomic(conn):
            account = conn.execute(
                "SELECT * FROM accounts WHERE id=? AND deleted_at IS NULL",
                (account_id,),
            ).fetchone()
            if not _authorize_row(conn, actor, account, "grant"):
                raise PermissionError("actor cannot revoke access to this account")
            changed = conn.execute(
                "UPDATE account_access_grants "
                "SET revoked_at=?,revision=revision+1 "
                "WHERE account_id=? AND grantee_tg_id=? AND revoked_at IS NULL",
                (timestamp, account_id, grantee_tg_id),
            ).rowcount
            if changed:
                conn.execute(
                    "UPDATE accounts SET acl_revision=acl_revision+1 WHERE id=?",
                    (account_id,),
                )
            return bool(changed)
