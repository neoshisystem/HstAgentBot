"""Restartable, offline default-profile and personal-wallet backfill."""
from __future__ import annotations

from contextlib import contextmanager
import sqlite3
import time

from subscription_service import _timestamp, ensure_default_profile, get_primary_bot_id


@contextmanager
def _atomic(conn: sqlite3.Connection):
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    if conn.in_transaction:
        conn.execute("SAVEPOINT bootstrap_profiles")
        try:
            yield
            conn.execute("RELEASE SAVEPOINT bootstrap_profiles")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT bootstrap_profiles")
            conn.execute("RELEASE SAVEPOINT bootstrap_profiles")
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


def backfill_profiles(conn: sqlite3.Connection, *, now: int | None = None) -> dict[str, int]:
    """Backfill every Telegram principal without overwriting profile or wallet data."""
    timestamp = int(time.time()) if now is None else _timestamp(now)
    with _atomic(conn):
        primary_bot_id = get_primary_bot_id(conn)
        rows = conn.execute(
            "SELECT telegram_id FROM bot_users "
            "UNION SELECT telegram_id FROM representatives WHERE telegram_id IS NOT NULL "
            "UNION SELECT owner_tg_id FROM accounts WHERE owner_tg_id IS NOT NULL "
            "UNION SELECT grantee_tg_id FROM account_access_grants "
            "ORDER BY 1"
        ).fetchall()
        principals = sorted({
            int(row[0]) for row in rows if row[0] is not None and int(row[0]) > 0
        })
        profiles_created = 0
        wallets_created = 0
        for principal_id in principals:
            existing = conn.execute(
                "SELECT 1 FROM subscription_profiles "
                "WHERE owner_tg_id=? AND is_default=1 AND deleted_at IS NULL",
                (principal_id,),
            ).fetchone()
            result = ensure_default_profile(conn, principal_id, now=timestamp)
            if existing is None and result.created:
                profiles_created += 1
            cursor = conn.execute(
                "INSERT INTO user_wallets(user_id,balance_toman) VALUES(?,0) "
                "ON CONFLICT(user_id) DO NOTHING",
                (principal_id,),
            )
            wallets_created += max(0, cursor.rowcount)
        return {
            "primary_bot_id": primary_bot_id,
            "principal_count": len(principals),
            "profiles_created": profiles_created,
            "wallets_created": wallets_created,
        }


__all__ = ["backfill_profiles"]
