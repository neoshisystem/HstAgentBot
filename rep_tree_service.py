"""Transactional representative-tree and root-administration services."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager
import json
import sqlite3
import time

import config
import database


MAX_REP_DEPTH = 3
_ALLOWED_BILLING_MODES = frozenset({"credit", "prepaid_wallet"})


class RepTreeError(ValueError):
    """Base class for invalid representative-tree operations."""


class TreeValidationError(RepTreeError):
    """A proposed tree change violates the approved hierarchy."""


class RepQuotaExceeded(RepTreeError):
    """A parent has reached its configured child quota."""


class ReparentBlocked(RepTreeError):
    """An unsettled financial or unknown operation blocks reparenting."""


class RootApprovalRequired(PermissionError):
    """The operation requires the configured root Telegram identity."""


def _positive_id(value, label: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{label} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{label} must be a positive integer") from exc
    if result <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return result


def _timestamp(now=None) -> int:
    value = int(time.time()) if now is None else now
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError("now must be a non-negative UTC epoch-seconds integer")
    return value


def _actor(ctx) -> dict:
    get = ctx.get if isinstance(ctx, Mapping) else lambda key, default=None: getattr(ctx, key, default)
    actor_id = _positive_id(get("actor_tg_id", get("telegram_id", None)), "actor Telegram ID")
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


def _require_root(actor: dict) -> None:
    if not actor["root"]:
        raise RootApprovalRequired("only config.ADMIN_TELEGRAM_ID may approve this operation")


@contextmanager
def _atomic(conn: sqlite3.Connection):
    """Use a savepoint when nested; otherwise serialize writers with IMMEDIATE."""
    if conn.in_transaction:
        conn.execute("SAVEPOINT v52_rep_tree")
        try:
            yield
            conn.execute("RELEASE SAVEPOINT v52_rep_tree")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT v52_rep_tree")
            conn.execute("RELEASE SAVEPOINT v52_rep_tree")
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


def _rep_row(conn: sqlite3.Connection, rep_id: int):
    return conn.execute(
        "SELECT id,rep_code,name,telegram_id,parent_rep_id,is_active,deleted_at,"
        "can_create_sub_reps,max_sub_reps,tier,billing_mode,credit_limit_toman "
        "FROM representatives WHERE id=?",
        (rep_id,),
    ).fetchone()


def _depth(conn: sqlite3.Connection, rep_id: int) -> int:
    row = _rep_row(conn, rep_id)
    if row is None:
        raise TreeValidationError("representative does not exist")
    if str(row["rep_code"]) == "root_admin_rep":
        return 0
    chain = conn.execute(
        """
        WITH RECURSIVE chain(id,parent_rep_id,depth) AS (
            SELECT id,parent_rep_id,1 FROM representatives WHERE id=?
            UNION ALL
            SELECT r.id,r.parent_rep_id,c.depth+1
            FROM representatives r JOIN chain c ON r.id=c.parent_rep_id
            WHERE c.depth<4
        )
        SELECT id,parent_rep_id,depth FROM chain
        """,
        (rep_id,),
    ).fetchall()
    if not chain:
        raise TreeValidationError("representative ancestry is unavailable")
    if len({int(item["id"]) for item in chain}) != len(chain):
        raise TreeValidationError("existing representative ancestry contains a cycle")
    if len(chain) == 4:
        raise TreeValidationError("existing representative depth exceeds the supported tree")
    return len(chain)


def _subtree_ids(conn: sqlite3.Connection, rep_id: int) -> list[int]:
    rows = conn.execute(
        """
        WITH RECURSIVE subtree(id) AS (
            SELECT id FROM representatives WHERE id=?
            UNION
            SELECT r.id FROM representatives r JOIN subtree s ON r.parent_rep_id=s.id
        )
        SELECT id FROM subtree ORDER BY id
        """,
        (rep_id,),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _subtree_height(conn: sqlite3.Connection, rep_id: int) -> int:
    rows = conn.execute(
        """
        WITH RECURSIVE descendants(id,depth,path) AS (
            SELECT id,1,printf('/%d/',id)
            FROM representatives WHERE parent_rep_id=?
            UNION ALL
            SELECT r.id,d.depth+1,d.path||r.id||'/'
            FROM representatives r JOIN descendants d ON r.parent_rep_id=d.id
            WHERE d.depth<4 AND instr(d.path,printf('/%d/',r.id))=0
        )
        SELECT COALESCE(MAX(depth),0) AS height FROM descendants
        """,
        (rep_id,),
    ).fetchone()
    return int(rows["height"])


def _financial_state(conn: sqlite3.Connection, rep_id: int) -> dict:
    ids = _subtree_ids(conn, rep_id)
    edges = conn.execute(
        "SELECT id,debt_toman FROM rep_edges WHERE child_rep_id IN "
        f"({','.join('?' for _ in ids)}) AND active=1",
        tuple(ids),
    ).fetchall() if ids else []
    debt = sum(int(edge["debt_toman"] or 0) for edge in edges)

    scopes = [f"rep:{item}" for item in ids]
    scopes.extend(f"edge:{int(edge['id'])}" for edge in edges)
    if scopes:
        placeholders = ",".join("?" for _ in scopes)
        holds = int(conn.execute(
            "SELECT COUNT(*) FROM financial_holds WHERE state='active' "
            f"AND source_key IN ({placeholders})",
            tuple(scopes),
        ).fetchone()[0])
    else:
        holds = 0

    placeholders = ",".join("?" for _ in ids) if ids else "NULL"
    unknown = int(conn.execute(
        "SELECT COUNT(*) FROM panel_operations po JOIN accounts a ON a.id=po.account_id "
        f"WHERE a.rep_id IN ({placeholders}) AND po.state='needs_reconciliation'",
        tuple(ids),
    ).fetchone()[0])
    return {
        "edge_debt_toman": debt,
        "active_hold_count": holds,
        "unknown_operation_count": unknown,
        "requires_reconciliation": bool(debt or holds or unknown),
    }


def validate_parent_change(
    conn: sqlite3.Connection,
    parent_rep_id: int | None,
    child_rep_id: int | None = None,
) -> dict:
    """Validate a proposed parent (None is the virtual root) and its full subtree."""
    child_id = None if child_rep_id is None else _positive_id(child_rep_id, "child_rep_id")
    parent_id = None if parent_rep_id is None else _positive_id(parent_rep_id, "parent_rep_id")

    child = None
    if child_id is not None:
        child = _rep_row(conn, child_id)
        if child is None:
            raise TreeValidationError("child representative does not exist")

    parent = None
    if parent_id is not None:
        parent = _rep_row(conn, parent_id)
        if parent is None:
            raise TreeValidationError("parent representative does not exist")
        if str(parent["rep_code"]) == "root_admin_rep":
            parent_id = None
            parent = None
        elif not bool(parent["is_active"]) or parent["deleted_at"] is not None:
            raise TreeValidationError("inactive or deleted representatives cannot receive children")

    if child_id is not None and parent_id == child_id:
        raise TreeValidationError("a representative cannot be its own parent")

    if child_id is not None and parent_id is not None:
        descendants = set(_subtree_ids(conn, child_id))
        if parent_id in descendants:
            raise TreeValidationError("reparenting would create a cycle")

    parent_depth = 0 if parent_id is None else _depth(conn, parent_id)
    subtree_height = 0 if child_id is None else _subtree_height(conn, child_id)
    proposed_child_depth = parent_depth + 1
    if proposed_child_depth > MAX_REP_DEPTH or proposed_child_depth + subtree_height > MAX_REP_DEPTH:
        raise TreeValidationError("representative depth would exceed the approved three levels")

    if parent is not None:
        if not bool(parent["can_create_sub_reps"]):
            raise TreeValidationError("parent is not allowed to create sub-representatives")
        quota = int(parent["max_sub_reps"] or 0)
        if quota <= 0:
            raise RepQuotaExceeded("max_sub_reps=0 forbids creating children")
        count_sql = (
            "SELECT COUNT(*) FROM representatives WHERE parent_rep_id=? "
            "AND is_active=1 AND deleted_at IS NULL AND rep_code<>'root_admin_rep'"
        )
        params: tuple[int, ...] = (parent_id,)
        if child_id is not None:
            count_sql += " AND id<>?"
            params += (child_id,)
        current = int(conn.execute(count_sql, params).fetchone()[0])
        if current >= quota:
            raise RepQuotaExceeded("parent has reached max_sub_reps")

    if child is not None and parent_id != (
        None if child["parent_rep_id"] is None else int(child["parent_rep_id"])
    ):
        financial = _financial_state(conn, child_id)
        if financial["requires_reconciliation"]:
            raise ReparentBlocked(
                "reparenting is blocked until edge debt, active holds and unknown operations are resolved"
            )

    return {
        "parent_rep_id": parent_id,
        "child_rep_id": child_id,
        "depth": proposed_child_depth,
        "subtree_height": subtree_height,
        "max_depth": MAX_REP_DEPTH,
    }


def create_sub_rep(
    ctx,
    parent_rep_id: int | None,
    *,
    rep_code: str,
    name: str,
    telegram_id: int,
    billing_mode: str = "credit",
    can_create_sub_reps: bool = False,
    max_sub_reps: int = 0,
    credit_limit_toman: int = 0,
    tier: str = "normal",
    now: int | None = None,
) -> dict:
    """Create one child representative and its financial edge atomically."""
    actor = _actor(ctx)
    if actor["blocked"] and not actor["root"]:
        raise PermissionError("blocked actors cannot create representatives")
    parent_id = None if parent_rep_id is None else _positive_id(parent_rep_id, "parent_rep_id")
    if not actor["root"]:
        if parent_id is None or actor["rep_id"] is None or int(actor["rep_id"]) != parent_id:
            raise PermissionError("a representative may create children only under its own identity")
    code = str(rep_code).strip()
    title = str(name).strip()
    if not code or code == "root_admin_rep" or not title:
        raise ValueError("rep_code and name must be non-empty; root_admin_rep is reserved")
    telegram = _positive_id(telegram_id, "telegram_id")
    mode = str(billing_mode).strip().lower()
    if mode == "parent_shared":
        raise TreeValidationError("parent_shared billing is not supported")
    if mode not in _ALLOWED_BILLING_MODES:
        raise ValueError("billing_mode must be credit or prepaid_wallet")
    if isinstance(max_sub_reps, bool) or not isinstance(max_sub_reps, int) or max_sub_reps < 0:
        raise ValueError("max_sub_reps must be a non-negative integer; zero means forbidden")
    if isinstance(credit_limit_toman, bool) or not isinstance(credit_limit_toman, int) or credit_limit_toman < 0:
        raise ValueError("credit_limit_toman must be a non-negative integer")
    if not isinstance(can_create_sub_reps, bool):
        raise TypeError("can_create_sub_reps must be boolean")
    timestamp = _timestamp(now)

    with database.get_db() as conn:
        with _atomic(conn):
            checked = validate_parent_change(conn, parent_id)
            normalized_parent = checked["parent_rep_id"]
            cursor = conn.execute(
                """
                INSERT INTO representatives(
                    rep_code,name,telegram_id,scope_filter,balance_toman,credit_limit_toman,
                    is_active,parent_rep_id,tier,billing_mode,can_create_sub_reps,max_sub_reps
                ) VALUES(?,?,?,?,0,?,1,?,?,?,?,?)
                """,
                (
                    code, title, telegram, "", credit_limit_toman,
                    normalized_parent, str(tier).strip(), mode,
                    int(can_create_sub_reps), max_sub_reps,
                ),
            )
            child_id = int(cursor.lastrowid)
            parent_key = "root" if normalized_parent is None else f"rep:{normalized_parent}"
            edge_cursor = conn.execute(
                """
                INSERT INTO rep_edges(
                    parent_key,child_rep_id,billing_mode,debt_toman,
                    credit_limit_toman,active,started_at,ended_at
                ) VALUES(?,?,?,0,?,1,?,NULL)
                """,
                (parent_key, child_id, mode, credit_limit_toman, timestamp),
            )
            return {
                "rep_id": child_id,
                "rep_code": code,
                "parent_rep_id": normalized_parent,
                "parent_key": parent_key,
                "depth": checked["depth"],
                "edge_id": int(edge_cursor.lastrowid),
                "billing_mode": mode,
                "is_active": True,
            }


def _audit(conn, rep_id: int, actor_id: int, action: str, details: dict) -> None:
    conn.execute(
        "INSERT INTO audit_logs(rep_id,action,actor_id,actor_name,details_json,result_status) "
        "VALUES(?,?,?,?,?,'success')",
        (
            rep_id, action, actor_id, "hisystem",
            json.dumps(details, ensure_ascii=False, sort_keys=True),
        ),
    )


def soft_delete_rep(ctx, rep_id: int, *, now: int | None = None) -> dict:
    """Soft-delete a representative without detaching descendants or touching money."""
    actor = _actor(ctx)
    _require_root(actor)
    exact_id = _positive_id(rep_id, "rep_id")
    timestamp = _timestamp(now)

    with database.get_db() as conn:
        with _atomic(conn):
            row = _rep_row(conn, exact_id)
            if row is None:
                raise TreeValidationError("representative does not exist")
            if str(row["rep_code"]) == "root_admin_rep":
                raise TreeValidationError("root identity cannot be soft-deleted")
            if row["deleted_at"] is None:
                financial = _financial_state(conn, exact_id)
                children = int(conn.execute(
                    "SELECT COUNT(*) FROM representatives WHERE parent_rep_id=?",
                    (exact_id,),
                ).fetchone()[0])
                edges = int(conn.execute(
                    "SELECT COUNT(*) FROM rep_edges WHERE child_rep_id=? AND active=1",
                    (exact_id,),
                ).fetchone()[0])
                conn.execute(
                    "UPDATE representatives SET is_active=0,deleted_at=? "
                    "WHERE id=? AND deleted_at IS NULL",
                    (timestamp, exact_id),
                )
                _audit(
                    conn, exact_id, actor["id"], "representative_soft_deleted",
                    {
                        "deleted_at": timestamp,
                        "preserved_children": children,
                        "preserved_active_edges": edges,
                        "unresolved_financial": financial,
                    },
                )
            else:
                financial = _financial_state(conn, exact_id)
                children = int(conn.execute(
                    "SELECT COUNT(*) FROM representatives WHERE parent_rep_id=?",
                    (exact_id,),
                ).fetchone()[0])
                edges = int(conn.execute(
                    "SELECT COUNT(*) FROM rep_edges WHERE child_rep_id=? AND active=1",
                    (exact_id,),
                ).fetchone()[0])
            final = _rep_row(conn, exact_id)
            return {
                "rep_id": exact_id,
                "is_active": bool(final["is_active"]),
                "deleted_at": int(final["deleted_at"]),
                "blocked_new_operations": True,
                "preserved_children": children,
                "preserved_active_edges": edges,
                "unresolved_financial": financial,
            }


def set_actor_tier(ctx, rep_id: int, tier_code: str, *, now: int | None = None) -> dict:
    """Set an existing representative tier after root approval and audit the change."""
    actor = _actor(ctx)
    _require_root(actor)
    exact_id = _positive_id(rep_id, "rep_id")
    code = str(tier_code).strip().lower()
    timestamp = _timestamp(now)

    with database.get_db() as conn:
        with _atomic(conn):
            rep = _rep_row(conn, exact_id)
            if rep is None or str(rep["rep_code"]) == "root_admin_rep":
                raise TreeValidationError("representative does not exist or is not tier-manageable")
            if not bool(rep["is_active"]) or rep["deleted_at"] is not None:
                raise TreeValidationError("deleted or inactive representatives cannot change tier")
            policy = conn.execute(
                "SELECT 1 FROM tier_policies WHERE actor_type='rep' AND tier_code=?",
                (code,),
            ).fetchone()
            if policy is None:
                raise ValueError("tier_code is not present in tier_policies for representatives")
            previous = str(rep["tier"])
            if previous != code:
                conn.execute("UPDATE representatives SET tier=? WHERE id=?", (code, exact_id))
                _audit(
                    conn, exact_id, actor["id"], "representative_tier_changed",
                    {"from": previous, "to": code, "changed_at": timestamp},
                )
            return {
                "rep_id": exact_id,
                "previous_tier": previous,
                "tier": code,
                "changed": previous != code,
            }
