"""Offline-safe activation workflow for already-paid reserved packages.

The caller obtains a prepared ticket, performs any panel request after this
function has committed, then calls finish_activation with the ticket and the
definitive external outcome. UNKNOWN deliberately keeps both persistent locks.
"""
from __future__ import annotations

import secrets
import sqlite3
import time
from typing import Any


PERSISTENT_LOCK_EXPIRY = 253402300799.0


class ReservationActivationError(ValueError):
    """A reservation, account binding, or lock state is not safe to activate."""


def _positive_id(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _timestamp(now: int | None) -> int:
    value = int(time.time()) if now is None else now
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("now must be a non-negative UTC epoch-seconds integer")
    return value


def _row_dict(row: Any) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        return dict(row)
    except (TypeError, ValueError):
        keys = getattr(row, "keys", None)
        if keys is None:
            raise TypeError("SQLite connection must use sqlite3.Row row_factory")
        return {key: row[key] for key in keys()}


def _normalized(value: Any) -> str:
    return str(value or "").strip().casefold()


def prepare_activation(
    conn: sqlite3.Connection,
    account_id: int,
    reservation_id: int,
    *,
    now: int | None = None,
) -> dict[str, Any] | None:
    """Claim account then reservation locks and re-read one pending reservation.

    A clean connection is required so the transaction is fully committed before
    the caller can dispatch a panel request. Existing reservation charges are
    authoritative; this workflow never writes a debit or a second ledger row.
    Returns None when another worker owns either lock or the reservation is no
    longer pending.
    """
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    account_id = _positive_id(account_id, "account_id")
    reservation_id = _positive_id(reservation_id, "reservation_id")
    timestamp = _timestamp(now)
    if conn.in_transaction:
        raise ReservationActivationError(
            "prepare_activation requires a clean connection; panel I/O must not run inside a transaction"
        )

    account_lock = f"account:{account_id}"
    reservation_lock = f"reservation-activation:{reservation_id}"
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "DELETE FROM operation_locks WHERE expires_at <= ?",
            (timestamp,),
        )

        # Keep the exact pending-row predicate from the frozen task contract.
        row = conn.execute(
            "SELECT * FROM reserved_packages WHERE id=? AND status='pending'",
            (reservation_id,),
        ).fetchone()
        reservation = _row_dict(row)
        if reservation is None:
            conn.commit()
            return None

        account = _row_dict(conn.execute(
            "SELECT id,account_code,email,deleted_at FROM accounts WHERE id=?",
            (account_id,),
        ).fetchone())
        if account is None or account.get("deleted_at") is not None:
            raise ReservationActivationError("active account binding is unavailable")
        if reservation.get("account_id") is not None and int(reservation["account_id"]) != account_id:
            raise ReservationActivationError("reservation is bound to a different account")
        if (
            _normalized(reservation.get("account_code")) != _normalized(account.get("account_code"))
            or _normalized(reservation.get("email")) != _normalized(account.get("email"))
        ):
            raise ReservationActivationError("reservation account identity does not match")

        # Both keys are checked before either is inserted; BEGIN IMMEDIATE makes
        # this pair acquisition serializable across competing scheduler workers.
        for key in (account_lock, reservation_lock):
            if conn.execute(
                "SELECT 1 FROM operation_locks WHERE lock_key=?",
                (key,),
            ).fetchone() is not None:
                conn.commit()
                return None

        account_token = secrets.token_hex(16)
        reservation_token = secrets.token_hex(16)
        conn.execute(
            "INSERT INTO operation_locks(lock_key,token,expires_at) VALUES(?,?,?)",
            (account_lock, account_token, PERSISTENT_LOCK_EXPIRY),
        )
        conn.execute(
            "INSERT INTO operation_locks(lock_key,token,expires_at) VALUES(?,?,?)",
            (reservation_lock, reservation_token, PERSISTENT_LOCK_EXPIRY),
        )
        conn.commit()
        return {
            "state": "prepared",
            "account_id": account_id,
            "reservation_id": reservation_id,
            "lock_order": [account_lock, reservation_lock],
            "locks": [
                {"key": account_lock, "token": account_token},
                {"key": reservation_lock, "token": reservation_token},
            ],
            "reservation": reservation,
            "legacy_charge_already_recorded": True,
            "network_must_run_after_commit": True,
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def finish_activation(
    conn: sqlite3.Connection,
    prepared: dict[str, Any],
    *,
    outcome: str,
) -> dict[str, Any]:
    """Finalize a prepared activation without charging again.

    success marks the package activated and releases both owned locks.
    definite_failure leaves it pending and releases both locks.
    unknown leaves it pending and retains both persistent locks for
    reconciliation. Replays are idempotent and cannot consume another worker's
    locks.
    """
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    if not isinstance(prepared, dict):
        raise TypeError("prepared must be the ticket returned by prepare_activation")
    account_id = _positive_id(prepared.get("account_id"), "account_id")
    reservation_id = _positive_id(prepared.get("reservation_id"), "reservation_id")
    normalized = str(outcome or "").strip().lower()
    if normalized not in {"success", "definite_failure", "unknown"}:
        raise ValueError("outcome must be success, definite_failure, or unknown")
    locks = prepared.get("locks")
    if not isinstance(locks, list) or len(locks) != 2:
        raise ValueError("prepared ticket must contain the ordered account and reservation locks")
    expected = [f"account:{account_id}", f"reservation-activation:{reservation_id}"]
    tokens: list[str] = []
    for index, item in enumerate(locks):
        if not isinstance(item, dict) or item.get("key") != expected[index]:
            raise ValueError("prepared lock order or key is invalid")
        token = item.get("token")
        if not isinstance(token, str) or len(token) < 16:
            raise ValueError("prepared lock token is invalid")
        tokens.append(token)
    if conn.in_transaction:
        raise ReservationActivationError("finish_activation requires a clean connection")

    conn.execute("BEGIN IMMEDIATE")
    try:
        current = _row_dict(conn.execute(
            "SELECT id,status FROM reserved_packages WHERE id=?",
            (reservation_id,),
        ).fetchone())
        if current is None:
            raise ReservationActivationError("reservation does not exist")

        owned = []
        present = []
        for key, token in zip(expected, tokens):
            row = conn.execute(
                "SELECT token FROM operation_locks WHERE lock_key=?",
                (key,),
            ).fetchone()
            present.append(row is not None)
            owned.append(row is not None and str(row[0]) == token)

        if not any(present):
            conn.commit()
            return {
                "state": "already_finalized",
                "reservation_id": reservation_id,
                "reservation_status": str(current["status"]),
                "idempotent_replay": True,
            }
        if not all(owned):
            raise ReservationActivationError("both exact persistent locks must still belong to this activation")
        if str(current["status"]) not in {"pending", "activated"}:
            raise ReservationActivationError("reservation is no longer pending or activated")

        if normalized == "unknown":
            conn.commit()
            return {
                "state": "unknown",
                "reservation_id": reservation_id,
                "reservation_status": str(current["status"]),
                "locks_retained": True,
                "ledger_debit_created": False,
            }

        if normalized == "success" and str(current["status"]) == "pending":
            cursor = conn.execute(
                "UPDATE reserved_packages SET status='activated',activated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status='pending'",
                (reservation_id,),
            )
            if cursor.rowcount != 1:
                raise ReservationActivationError("pending reservation changed during activation")
        elif normalized == "definite_failure" and str(current["status"]) != "pending":
            raise ReservationActivationError("a failed activation cannot reopen an activated reservation")

        for key, token in zip(expected, tokens):
            cursor = conn.execute(
                "DELETE FROM operation_locks WHERE lock_key=? AND token=?",
                (key, token),
            )
            if cursor.rowcount != 1:
                raise ReservationActivationError("could not release the exact activation lock pair")

        conn.commit()
        final_status = "activated" if normalized == "success" else "pending"
        return {
            "state": normalized,
            "reservation_id": reservation_id,
            "reservation_status": final_status,
            "locks_retained": False,
            "ledger_debit_created": False,
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
