"""Unambiguous, compare-and-swap account linking for HstAgentBot v5.2."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping

import config
import database


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
    }


def _active_grant(conn: sqlite3.Connection, account_id: int, actor_id: int):
    return conn.execute(
        "SELECT access_level FROM account_access_grants "
        "WHERE account_id=? AND grantee_tg_id=? AND revoked_at IS NULL",
        (account_id, actor_id),
    ).fetchone()


def _candidate_visible(conn, actor: dict, row: sqlite3.Row) -> bool:
    if row["owner_tg_id"] is None or actor["root"]:
        return True
    if int(row["owner_tg_id"]) == actor["id"]:
        return True
    return _active_grant(conn, int(row["id"]), actor["id"]) is not None


def resolve_link_candidates(ctx, identifier: str) -> dict:
    """Casefold code/email against all authorized matches; never choose LIMIT 1."""
    actor = _actor(ctx)
    if actor["blocked"] and not actor["root"]:
        return {"status": "not_found", "account_ids": [], "candidates": []}
    if not isinstance(identifier, str):
        raise TypeError("identifier must be text")
    needle = identifier.strip().casefold()
    if not needle:
        return {"status": "not_found", "account_ids": [], "candidates": []}

    candidates = []
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT id,account_code,email,owner_tg_id "
            "FROM accounts WHERE deleted_at IS NULL ORDER BY id"
        ).fetchall()
        for row in rows:
            code = str(row["account_code"] or "").strip().casefold()
            email = str(row["email"] or "").strip().casefold()
            if needle not in {code, email}:
                continue
            if not _candidate_visible(conn, actor, row):
                continue
            candidates.append({
                "account_id": int(row["id"]),
                "account_code": str(row["account_code"]),
                "email": str(row["email"]),
            })

    account_ids = [item["account_id"] for item in candidates]
    if not candidates:
        status = "not_found"
    elif len(candidates) == 1:
        status = "resolved"
    else:
        status = "ambiguous"
    return {
        "status": status,
        "account_ids": account_ids,
        "candidates": candidates,
    }


def claim_or_access(ctx, account_id: int) -> dict:
    """Claim an unowned account with CAS, or honor an existing owner/grant unchanged."""
    actor = _actor(ctx)
    if actor["blocked"] and not actor["root"]:
        return {"status": "denied", "account_id": int(account_id)}
    if isinstance(account_id, bool):
        raise TypeError("account_id must be a positive integer")
    try:
        exact_id = int(account_id)
    except (TypeError, ValueError) as exc:
        raise TypeError("account_id must be a positive integer") from exc
    if exact_id <= 0:
        raise ValueError("account_id must be a positive integer")

    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        account = conn.execute(
            "SELECT id,owner_tg_id,deleted_at FROM accounts WHERE id=?",
            (exact_id,),
        ).fetchone()
        if account is None or account["deleted_at"] is not None:
            return {"status": "not_found", "account_id": exact_id}

        if account["owner_tg_id"] is None:
            member = conn.execute(
                "SELECT is_blocked FROM bot_users WHERE telegram_id=?",
                (actor["id"],),
            ).fetchone()
            if member is None or (bool(member["is_blocked"]) and not actor["root"]):
                return {"status": "denied", "account_id": exact_id}
            changed = conn.execute(
                "UPDATE accounts SET owner_tg_id=?,acl_revision=acl_revision+1 "
                "WHERE id=? AND owner_tg_id IS NULL AND deleted_at IS NULL",
                (actor["id"], exact_id),
            ).rowcount
            if changed == 1:
                return {"status": "claimed", "account_id": exact_id, "owner_tg_id": actor["id"]}
            account = conn.execute(
                "SELECT id,owner_tg_id,deleted_at FROM accounts WHERE id=?",
                (exact_id,),
            ).fetchone()
            if account is None or account["deleted_at"] is not None:
                return {"status": "not_found", "account_id": exact_id}

        if int(account["owner_tg_id"]) == actor["id"]:
            return {"status": "owner", "account_id": exact_id, "owner_tg_id": actor["id"]}
        if actor["root"]:
            return {
                "status": "root_access",
                "account_id": exact_id,
                "owner_tg_id": int(account["owner_tg_id"]),
            }

        grant = _active_grant(conn, exact_id, actor["id"])
        if grant is not None:
            return {
                "status": "grant_access",
                "account_id": exact_id,
                "owner_tg_id": int(account["owner_tg_id"]),
                "access_level": str(grant["access_level"]),
            }
        return {"status": "denied", "account_id": exact_id}
