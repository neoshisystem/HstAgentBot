"""Offline eligibility rules for v5.2 panel and plan sales."""

from __future__ import annotations

import sqlite3

_ALLOWED_ACTOR_TYPES = {"end_user", "rep"}


def _actor_type(value: str) -> str:
    value = str(value or "").strip().lower()
    if value in {"representative", "reseller"}:
        value = "rep"
    if value not in _ALLOWED_ACTOR_TYPES:
        raise ValueError("actor_type must be end_user or rep")
    return value


def _panel(conn: sqlite3.Connection, panel_id: int):
    if isinstance(panel_id, bool) or not isinstance(panel_id, int) or panel_id <= 0:
        raise ValueError("panel_id must be a positive integer")
    return conn.execute("SELECT * FROM panels WHERE id=?", (panel_id,)).fetchone()


def _tier_rank(conn, actor_type, *, rep_id, actor_tier, tier_rank):
    if tier_rank is not None:
        if isinstance(tier_rank, bool) or not isinstance(tier_rank, int) or tier_rank < 0:
            raise ValueError("tier_rank must be a non-negative integer")
        return tier_rank
    tier = actor_tier
    if tier is None and actor_type == "rep" and rep_id is not None:
        row = conn.execute("SELECT tier FROM representatives WHERE id=?", (rep_id,)).fetchone()
        if row is None:
            return -1
        tier = row["tier"]
    if tier is None:
        return 0
    row = conn.execute(
        "SELECT rank FROM tier_policies WHERE actor_type=? AND tier_code=?",
        (actor_type, str(tier)),
    ).fetchone()
    return int(row["rank"]) if row is not None else -1


def _rep_assignment(conn, panel_id: int, rep_id: int | None) -> tuple[bool, bool]:
    """Return (denied, allowed) after applying ancestor assignments."""
    if rep_id is None:
        return False, False
    chain: list[tuple[int, bool]] = []
    seen: set[int] = set()
    current: int | None = rep_id
    direct = True
    while current is not None:
        if current in seen:
            return True, False
        seen.add(current)
        row = conn.execute(
            "SELECT id,parent_rep_id FROM representatives WHERE id=?", (current,)
        ).fetchone()
        if row is None:
            return True, False
        chain.append((current, direct))
        parent = row["parent_rep_id"]
        current = int(parent) if parent is not None else None
        direct = False
    denied = False
    allowed = False
    for ancestor_id, is_direct in chain:
        assignment = conn.execute(
            "SELECT effect,include_sub_reps FROM panel_rep_assignments "
            "WHERE panel_id=? AND rep_id=?",
            (panel_id, ancestor_id),
        ).fetchone()
        if assignment is None:
            continue
        if not (is_direct or bool(assignment["include_sub_reps"])):
            continue
        if str(assignment["effect"]) == "deny":
            denied = True
        elif str(assignment["effect"]) == "allow":
            allowed = True
    return denied, allowed


def _capability_allowed(
    conn, panel, actor_type, *, rep_id, actor_tier, tier_rank
) -> bool:
    if panel is None:
        return False
    capability = "for_reps" if actor_type == "rep" else "for_end_users"
    if not bool(panel[capability]):
        return False
    access_type = str(panel["access_type"] or "public")
    if access_type == "tier_restricted":
        rank = _tier_rank(
            conn, actor_type, rep_id=rep_id, actor_tier=actor_tier, tier_rank=tier_rank
        )
        minimum = int(panel["min_rep_rank"] if actor_type == "rep" else panel["min_user_rank"])
        if rank < minimum:
            return False
    elif access_type not in {"public", "private"}:
        return False
    return True


def can_sell_new(
    conn: sqlite3.Connection,
    *,
    panel_id: int,
    plan_code: str,
    actor_type: str,
    rep_id: int | None = None,
    actor_tier: str | None = None,
    tier_rank: int | None = None,
    is_root: bool = False,
) -> bool:
    """Check new-sale eligibility; deny assignments override descendant allows."""
    actor_type = _actor_type(actor_type)
    panel = _panel(conn, panel_id)
    if panel is None or str(panel["lifecycle"]) != "active":
        return False
    if not _capability_allowed(
        conn, panel, actor_type, rep_id=rep_id, actor_tier=actor_tier, tier_rank=tier_rank
    ):
        return False
    plan = conn.execute(
        "SELECT sale_status,panel_scope FROM pricing_plans WHERE plan_code=?",
        (str(plan_code),),
    ).fetchone()
    if plan is None or str(plan["sale_status"]) != "active":
        return False
    assignment_denied, assignment_allowed = _rep_assignment(conn, panel_id, rep_id)
    if not is_root and assignment_denied:
        return False
    if not is_root and str(panel["access_type"] or "public") == "private" and not assignment_allowed:
        return False
    mapping = conn.execute(
        "SELECT allowed FROM plan_panels WHERE plan_code=? AND panel_id=?",
        (str(plan_code), panel_id),
    ).fetchone()
    scope = str(plan["panel_scope"] or "selected")
    if mapping is not None:
        return bool(mapping["allowed"])
    return scope == "all"


def can_renew_existing(
    conn: sqlite3.Connection,
    *,
    panel_id: int,
    actor_type: str,
    rep_id: int | None = None,
    actor_tier: str | None = None,
    tier_rank: int | None = None,
    is_root: bool = False,
    on_hold: bool = False,
) -> bool:
    """Check renewal independent of current sale visibility and assignments."""
    actor_type = _actor_type(actor_type)
    panel = _panel(conn, panel_id)
    if panel is None or on_hold:
        return False
    if str(panel["lifecycle"]) not in {"active", "capacity"}:
        return False
    # Existing ownership is not revoked by sale deny or hidden/sold-out state.
    # Audience and tier capability plus lifecycle guards still apply to root.
    return _capability_allowed(
        conn, panel, actor_type, rep_id=rep_id, actor_tier=actor_tier, tier_rank=tier_rank
    )


__all__ = ["can_sell_new", "can_renew_existing"]