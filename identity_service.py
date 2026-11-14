"""Offline identity and persona resolution for HstAgentBot v5.2."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import sqlite3

import config


class IdentityError(RuntimeError):
    """Raised when the configured root identity cannot be established safely."""


class UnknownActorError(LookupError):
    """Raised when an actor has no persisted Telegram identity."""


@dataclass(frozen=True)
class ActorContext:
    actor_tg_id: int
    persona: str
    is_root: bool
    is_blocked: bool
    rep_id: int | None = None


def _telegram_id(value: int) -> int:
    if isinstance(value, bool):
        raise TypeError("Telegram ID must be a positive integer")
    try:
        actor_id = int(value)
    except (TypeError, ValueError) as exc:
        raise TypeError("Telegram ID must be a positive integer") from exc
    if actor_id <= 0:
        raise ValueError("Telegram ID must be a positive integer")
    return actor_id


def _configured_root_id() -> int:
    value = getattr(config, "ADMIN_TELEGRAM_ID", 0)
    if isinstance(value, bool):
        return 0
    try:
        root_id = int(value)
    except (TypeError, ValueError):
        return 0
    return root_id if root_id > 0 else 0


@contextmanager
def _atomic(conn: sqlite3.Connection):
    """Keep identity creation atomic without committing a caller transaction."""
    if conn.in_transaction:
        conn.execute("SAVEPOINT v52_identity")
        try:
            yield
            conn.execute("RELEASE SAVEPOINT v52_identity")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT v52_identity")
            conn.execute("RELEASE SAVEPOINT v52_identity")
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


def ensure_personal_identity(
    conn: sqlite3.Connection,
    actor_tg_id: int,
) -> dict[str, int]:
    """Create the configured root's personal records once, preserving all existing data."""
    actor_id = _telegram_id(actor_tg_id)
    root_id = _configured_root_id()
    if root_id == 0 or actor_id != root_id:
        raise PermissionError("only config.ADMIN_TELEGRAM_ID can initialize root identity")

    with _atomic(conn):
        conn.execute(
            "INSERT INTO representatives(rep_code,name,telegram_id,scope_filter) "
            "VALUES('root_admin_rep','Root personal',?,'') "
            "ON CONFLICT(rep_code) DO NOTHING",
            (actor_id,),
        )
        root_rep = conn.execute(
            "SELECT id,telegram_id FROM representatives WHERE rep_code='root_admin_rep'"
        ).fetchone()
        if (
            root_rep is None
            or root_rep["telegram_id"] is None
            or int(root_rep["telegram_id"]) != actor_id
        ):
            raise IdentityError("root_admin_rep conflicts with config.ADMIN_TELEGRAM_ID")

        # Persisted role/persona is deliberately not used as the root authority.
        conn.execute(
            "INSERT INTO bot_users(telegram_id,role) VALUES(?,'end_user') "
            "ON CONFLICT(telegram_id) DO NOTHING",
            (actor_id,),
        )
        conn.execute(
            "INSERT INTO user_wallets(user_id,balance_toman) VALUES(?,0) "
            "ON CONFLICT(user_id) DO NOTHING",
            (actor_id,),
        )
        try:
            conn.execute(
                "INSERT INTO user_settings(user_id,first_connect_mode,active_persona) "
                "VALUES(?,0,'end_user') ON CONFLICT(user_id) DO NOTHING",
                (actor_id,),
            )
        except sqlite3.OperationalError as exc:
            raise IdentityError(
                "user_settings.active_persona is unavailable; apply migration 007_access"
            ) from exc

    return {
        "root_tg_id": actor_id,
        "root_rep_id": int(root_rep["id"]),
    }


def resolve_actor(
    conn: sqlite3.Connection,
    actor_tg_id: int,
) -> ActorContext:
    """Resolve a Telegram actor; root is derived only from ADMIN_TELEGRAM_ID.

    The selected persona is accepted only when its corresponding membership
    exists. An invalid or stale representative persona safely falls back to
    the user's personal end-user workspace.
    """
    actor_id = _telegram_id(actor_tg_id)
    row = conn.execute(
        "SELECT telegram_id,role,rep_id,is_blocked "
        "FROM bot_users WHERE telegram_id=?",
        (actor_id,),
    ).fetchone()
    if row is None:
        raise UnknownActorError(f"Telegram actor {actor_id} has no bot_users row")

    settings = conn.execute(
        "SELECT active_persona FROM user_settings WHERE user_id=?",
        (actor_id,),
    ).fetchone()
    requested = (
        str(settings["active_persona"]).strip().lower()
        if settings is not None and settings["active_persona"] is not None
        else "end_user"
    )

    persona = "end_user"
    rep_id = None
    if requested in {"rep", "representative"}:
        memberships = conn.execute(
            "SELECT id FROM representatives "
            "WHERE telegram_id=? AND is_active=1 AND deleted_at IS NULL "
            "ORDER BY id",
            (actor_id,),
        ).fetchall()
        if len(memberships) == 1:
            persona = "rep"
            rep_id = int(memberships[0]["id"])

    root_id = _configured_root_id()
    return ActorContext(
        actor_tg_id=actor_id,
        persona=persona,
        is_root=(root_id > 0 and actor_id == root_id),
        is_blocked=bool(row["is_blocked"]),
        rep_id=rep_id,
    )
