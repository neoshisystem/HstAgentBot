"""Atomic capacity reservations for v5.2 panel sales."""

from __future__ import annotations

from contextlib import contextmanager
import sqlite3
from typing import Iterator


class CapacityUnavailable(RuntimeError):
    """Raised when capacity cannot be established safely."""


class CapacityFull(RuntimeError):
    """Raised when no sellable capacity remains."""


class CapacityConflict(RuntimeError):
    """Raised when an idempotency key is already bound to another reservation."""


def _positive_id(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _non_negative_int(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    return value


@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[None]:
    """Use BEGIN IMMEDIATE or a savepoint without committing a caller transaction."""
    if conn.in_transaction:
        conn.execute("SAVEPOINT w06_capacity")
        try:
            yield
            conn.execute("RELEASE SAVEPOINT w06_capacity")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT w06_capacity")
            conn.execute("RELEASE SAVEPOINT w06_capacity")
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


def reserve_capacity(
    conn: sqlite3.Connection,
    *,
    reservation_id: str,
    order_id: str,
    panel_id: int,
    snapshot_count: int | None,
    snapshot_at: int | None,
    now: int,
    max_snapshot_age: int = 300,
    account_id: int | None = None,
) -> dict:
    """Reserve one sell slot atomically using a valid, fresh external count.

    snapshot_count is the distinct active/on-hold client count already visible
    on the panel, including any manual-pool client already counted there.
    Active reservations with account_id=NULL are added as not-yet-visible work;
    reservations tied to an account are not counted twice.
    """
    key = str(reservation_id or "").strip()
    order = str(order_id or "").strip()
    if not key or not order:
        raise ValueError("reservation_id and order_id must be non-empty")
    panel_id = _positive_id(panel_id, "panel_id")
    now = _non_negative_int(now, "now")
    max_snapshot_age = _positive_id(max_snapshot_age, "max_snapshot_age")
    if snapshot_count is None or snapshot_at is None:
        raise CapacityUnavailable("capacity snapshot is unknown")
    snapshot_count = _non_negative_int(snapshot_count, "snapshot_count")
    snapshot_at = _non_negative_int(snapshot_at, "snapshot_at")
    if snapshot_at > now or now - snapshot_at > max_snapshot_age:
        raise CapacityUnavailable("capacity snapshot is stale or from the future")
    if account_id is not None:
        account_id = _positive_id(account_id, "account_id")

    with _atomic(conn):
        panel = conn.execute(
            "SELECT max_clients,lifecycle FROM panels WHERE id=?", (panel_id,)
        ).fetchone()
        if panel is None:
            raise CapacityUnavailable("panel does not exist")
        if str(panel["lifecycle"]) != "active":
            raise CapacityUnavailable("panel lifecycle does not accept new sales")
        limit = _non_negative_int(int(panel["max_clients"]), "max_clients")

        existing = conn.execute(
            "SELECT reservation_id,panel_id,state FROM capacity_reservations "
            "WHERE reservation_id=? OR order_id=?",
            (key, order),
        ).fetchone()
        if existing is not None:
            if (
                str(existing["reservation_id"]) == key
                and int(existing["panel_id"]) == panel_id
                and str(existing["state"]) == "active"
            ):
                return {
                    "reservation_id": key,
                    "state": "active",
                    "capacity_limit": limit,
                    "snapshot_count": snapshot_count,
                    "inflight_count": None,
                    "idempotent_replay": True,
                }
            raise CapacityConflict("reservation or order key is already bound")

        # Pool clients represented in snapshot_count are not counted again.
        inflight = int(conn.execute(
            "SELECT COUNT(*) FROM capacity_reservations "
            "WHERE panel_id=? AND state='active' AND account_id IS NULL",
            (panel_id,),
        ).fetchone()[0])
        if limit > 0 and snapshot_count + inflight >= limit:
            raise CapacityFull("panel capacity is full")

        conn.execute(
            "INSERT INTO capacity_reservations"
            "(reservation_id,order_id,panel_id,account_id,state,expires_at) "
            "VALUES(?,?,?,?, 'active', NULL)",
            (key, order, panel_id, account_id),
        )
        return {
            "reservation_id": key,
            "state": "active",
            "capacity_limit": limit,
            "snapshot_count": snapshot_count,
            "inflight_count": inflight + (1 if account_id is None else 0),
            "idempotent_replay": False,
        }


def release_capacity(
    conn: sqlite3.Connection,
    reservation_id: str,
    *,
    outcome: str,
    account_id: int | None = None,
) -> bool:
    """Release only after a definite terminal outcome; UNKNOWN locks retain stock."""
    key = str(reservation_id or "").strip()
    normalized = str(outcome or "").strip().lower()
    if not key:
        raise ValueError("reservation_id must be non-empty")
    if normalized in {"unknown", "needs_reconciliation", "timeout_unknown"}:
        return False
    if normalized not in {"definite_failure", "cancelled", "success", "allocated"}:
        raise ValueError("unsupported capacity reservation outcome")
    if account_id is not None:
        account_id = _positive_id(account_id, "account_id")
    with _atomic(conn):
        reservation = conn.execute(
            "SELECT order_id,state FROM capacity_reservations WHERE reservation_id=?",
            (key,),
        ).fetchone()
        if reservation is None or str(reservation["state"]) != "active":
            return False
        locked = conn.execute(
            "SELECT 1 FROM panel_operations WHERE order_id=? "
            "AND state IN ('prepared','dispatching','needs_reconciliation') LIMIT 1",
            (reservation["order_id"],),
        ).fetchone()
        if locked is not None:
            return False
        if normalized in {"success", "allocated"}:
            cursor = conn.execute(
                "UPDATE capacity_reservations SET state='allocated',account_id=COALESCE(?,account_id) "
                "WHERE reservation_id=? AND state='active'",
                (account_id, key),
            )
        else:
            cursor = conn.execute(
                "UPDATE capacity_reservations SET state='released' "
                "WHERE reservation_id=? AND state='active'",
                (key,),
            )
        return cursor.rowcount == 1


__all__ = [
    "CapacityUnavailable", "CapacityFull", "CapacityConflict",
    "reserve_capacity", "release_capacity",
]