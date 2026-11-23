"""Deterministic, collision-safe username allocation for v5.2 panels."""

from __future__ import annotations

from contextlib import contextmanager
import re
import secrets
import sqlite3
from typing import Iterator


STRATEGIES = frozenset({
    "pool_preallocated", "rep_prefix_sequential", "rep_prefix_random",
    "custom_text_sequential", "numeric_id_random", "custom_username",
})
_CUSTOM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{2,63}$")


class UsernameAllocationError(RuntimeError):
    """Base error for safe username allocation failures."""


class UsernameCollision(UsernameAllocationError):
    """A generated or requested name is already owned by another client."""


class UsernameUnknown(UsernameAllocationError):
    """Panel outcome is unknown; do not allocate another username."""


def validate_custom_username(username: str, *, max_length: int = 64) -> str:
    """Validate a user-supplied name using the approved ASCII grammar."""
    if isinstance(max_length, bool) or not isinstance(max_length, int) or not 3 <= max_length <= 64:
        raise ValueError("max_length must be between 3 and 64")
    value = str(username or "").strip()
    if len(value) > max_length or _CUSTOM_RE.fullmatch(value) is None:
        raise ValueError("custom username does not match the approved ASCII format")
    return value


@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[None]:
    if conn.in_transaction:
        conn.execute("SAVEPOINT w06_naming")
        try:
            yield
            conn.execute("RELEASE SAVEPOINT w06_naming")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT w06_naming")
            conn.execute("RELEASE SAVEPOINT w06_naming")
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


def _positive_id(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _sanitize_prefix(value: str, *, max_length: int) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]", "", str(value or "").strip())
    cleaned = re.sub(r"^[^A-Za-z0-9]+", "", cleaned)
    cleaned = cleaned[:max_length]
    if not cleaned:
        raise ValueError("username prefix is empty after sanitization")
    return cleaned


def _account_collision(conn, panel_id, username, account_id):
    if account_id is None:
        row = conn.execute(
            "SELECT 1 FROM accounts WHERE panel_id=? AND email=? COLLATE NOCASE LIMIT 1",
            (panel_id, username),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT 1 FROM accounts WHERE panel_id=? AND email=? COLLATE NOCASE AND id<>? LIMIT 1",
            (panel_id, username, account_id),
        ).fetchone()
    return row is not None


def _local_collision(conn, panel_id, username, account_id):
    if _account_collision(conn, panel_id, username, account_id):
        return True
    return conn.execute(
        "SELECT 1 FROM manual_pool_clients WHERE panel_id=? "
        "AND external_identifier=? COLLATE NOCASE LIMIT 1",
        (panel_id, username),
    ).fetchone() is not None


def _random_digits(random_factory, width: int) -> str:
    value = str(random_factory(width) if random_factory is not None else secrets.randbelow(10 ** width)).strip()
    if not value.isdigit():
        raise ValueError("random_factory must return digits")
    if len(value) > width:
        value = value[-width:]
    return value.zfill(width)


def _next_sequence(conn, panel_id: int) -> int:
    conn.execute(
        "INSERT INTO panel_counters(panel_id,next_value) VALUES(?,1) "
        "ON CONFLICT(panel_id) DO NOTHING",
        (panel_id,),
    )
    row = conn.execute(
        "UPDATE panel_counters SET next_value=next_value+1 "
        "WHERE panel_id=? RETURNING next_value-1 AS allocated",
        (panel_id,),
    ).fetchone()
    if row is None:
        raise UsernameAllocationError("panel counter is unavailable")
    return int(row["allocated"])


def allocate_username(
    conn: sqlite3.Connection,
    *,
    panel_id: int,
    rep_id: int | None = None,
    account_id: int | None = None,
    operation_id: str | None = None,
    strategy: str | None = None,
    custom_username: str | None = None,
    adapter_max_length: int = 64,
    panel_state: str = "known",
    random_factory=None,
) -> dict:
    """Allocate a username locally and atomically; never retries after UNKNOWN."""
    panel_id = _positive_id(panel_id, "panel_id")
    if rep_id is not None:
        rep_id = _positive_id(rep_id, "rep_id")
    if account_id is not None:
        account_id = _positive_id(account_id, "account_id")
    if str(panel_state).strip().lower() in {"unknown", "needs_reconciliation", "timeout_unknown"}:
        raise UsernameUnknown("panel outcome is unknown; no new username may be allocated")
    panel = conn.execute(
        "SELECT id,username_strategy,custom_prefix FROM panels WHERE id=?", (panel_id,)
    ).fetchone()
    if panel is None:
        raise UsernameAllocationError("panel does not exist")
    selected = str(strategy or panel["username_strategy"] or "").strip().lower()
    if selected not in STRATEGIES:
        raise ValueError("unsupported username strategy")
    if isinstance(adapter_max_length, bool) or not isinstance(adapter_max_length, int):
        raise TypeError("adapter_max_length must be an integer")
    if not 3 <= adapter_max_length <= 64:
        raise ValueError("adapter_max_length must be between 3 and 64")

    if selected == "pool_preallocated":
        if account_id is None or not str(operation_id or "").strip():
            raise ValueError("pool_preallocated requires account_id and operation_id")
        op_key = str(operation_id).strip()
        with _atomic(conn):
            replay = conn.execute(
                "SELECT external_identifier,id FROM manual_pool_clients "
                "WHERE panel_id=? AND allocation_key=? AND account_id=? AND status='allocated'",
                (panel_id, op_key, account_id),
            ).fetchone()
            if replay is not None:
                return {
                    "username": str(replay["external_identifier"]),
                    "strategy": selected,
                    "pool_client_id": int(replay["id"]),
                    "idempotent_replay": True,
                }
            candidate = conn.execute(
                "SELECT id,external_identifier FROM manual_pool_clients "
                "WHERE panel_id=? AND status='available' AND account_id IS NULL ORDER BY id LIMIT 1",
                (panel_id,),
            ).fetchone()
            if candidate is None:
                raise UsernameAllocationError("preallocated pool is empty")
            username = str(candidate["external_identifier"])
            if _account_collision(conn, panel_id, username, account_id):
                raise UsernameCollision("preallocated username already belongs to another client")
            updated = conn.execute(
                "UPDATE manual_pool_clients SET status='allocated',account_id=?,"
                "allocation_key=?,revision=revision+1 "
                "WHERE id=? AND panel_id=? AND status='available' AND account_id IS NULL",
                (account_id, op_key, int(candidate["id"]), panel_id),
            )
            if updated.rowcount != 1:
                raise UsernameCollision("preallocated pool claim lost its compare-and-set")
            return {
                "username": username,
                "strategy": selected,
                "pool_client_id": int(candidate["id"]),
                "idempotent_replay": False,
            }

    if selected == "custom_username":
        username = validate_custom_username(
            custom_username or "", max_length=adapter_max_length
        )
        with _atomic(conn):
            if _local_collision(conn, panel_id, username, account_id):
                raise UsernameCollision("custom username is already owned")
            return {
                "username": username, "strategy": selected,
                "pool_client_id": None, "idempotent_replay": False,
            }

    if selected in {"rep_prefix_sequential", "rep_prefix_random"}:
        if rep_id is None:
            raise ValueError("representative prefix strategy requires rep_id")
        rep = conn.execute(
            "SELECT custom_prefix,rep_code FROM representatives WHERE id=?", (rep_id,)
        ).fetchone()
        if rep is None:
            raise UsernameAllocationError("representative does not exist")
        raw_prefix = rep["custom_prefix"] or rep["rep_code"]
    else:
        raw_prefix = panel["custom_prefix"]

    if selected in {"rep_prefix_sequential", "custom_text_sequential"}:
        prefix = _sanitize_prefix(raw_prefix, max_length=adapter_max_length - 1)
        with _atomic(conn):
            for _ in range(5):
                sequence = _next_sequence(conn, panel_id)
                username = f"{prefix}{sequence}"
                if len(username) > adapter_max_length:
                    raise UsernameAllocationError("generated username exceeds adapter limit")
                if not _local_collision(conn, panel_id, username, account_id):
                    return {
                        "username": username, "strategy": selected,
                        "pool_client_id": None, "idempotent_replay": False,
                    }
            raise UsernameCollision("five sequential candidates collided")

    if selected in {"rep_prefix_random", "numeric_id_random"}:
        prefix = (
            _sanitize_prefix(raw_prefix, max_length=adapter_max_length - 7)
            if selected == "rep_prefix_random"
            else ""
        )
        with _atomic(conn):
            for _ in range(5):
                suffix = _random_digits(random_factory, 6 if selected == "rep_prefix_random" else 10)
                username = f"{prefix}{suffix}"
                if len(username) > adapter_max_length:
                    raise UsernameAllocationError("generated username exceeds adapter limit")
                if not _local_collision(conn, panel_id, username, account_id):
                    return {
                        "username": username, "strategy": selected,
                        "pool_client_id": None, "idempotent_replay": False,
                    }
            raise UsernameCollision("five random candidates collided")

    raise ValueError("unsupported username strategy")


__all__ = [
    "STRATEGIES", "UsernameAllocationError", "UsernameCollision", "UsernameUnknown",
    "validate_custom_username", "allocate_username",
]