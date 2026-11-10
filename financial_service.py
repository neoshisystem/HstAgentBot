"""Ledger-derived wallet/edge availability and atomic SQLite hold reservation."""
from __future__ import annotations

import re
import secrets
import sqlite3
from collections.abc import Mapping
from typing import Any


class FinancialError(ValueError):
    """Base financial source or reservation error."""


class FinancialSourceError(FinancialError):
    """A source key or ledger source is invalid or unavailable."""


class InsufficientFunds(FinancialError):
    """One or more required sources cannot fund the complete reservation."""


class HoldConflict(FinancialError):
    """An idempotent order replay supplied different hold amounts or sources."""


def _amount(value: Any, *, allow_zero: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FinancialError("hold amount must be an integer toman value")
    if value < 0 or (value == 0 and not allow_zero):
        raise FinancialError(
            "hold amount must be non-negative" if allow_zero else "hold amount must be positive"
        )
    return value


def _source(source_key: Any) -> tuple:
    if not isinstance(source_key, str):
        raise FinancialSourceError("source_key must be a string")
    match = re.fullmatch(r"wallet:root:([1-9][0-9]*)", source_key)
    if match:
        return ("wallet", "root", 0, int(match.group(1)))
    match = re.fullmatch(r"wallet:rep:([1-9][0-9]*):([1-9][0-9]*)", source_key)
    if match:
        return ("wallet", f"rep:{int(match.group(1))}", int(match.group(1)), int(match.group(2)))
    match = re.fullmatch(r"edge:([1-9][0-9]*)", source_key)
    if match:
        return ("edge", int(match.group(1)))
    raise FinancialSourceError("unsupported financial source_key")


def _active_holds(conn: sqlite3.Connection, source_key: str) -> int:
    return int(conn.execute(
        "SELECT COALESCE(SUM(amount_toman),0) FROM financial_holds "
        "WHERE source_key=? AND state='active'",
        (source_key,),
    ).fetchone()[0])


def _wallet_balance(conn: sqlite3.Connection, merchant_key: str, user_tg_id: int) -> int:
    return int(conn.execute(
        "SELECT COALESCE(SUM(CASE transaction_type "
        "WHEN 'credit' THEN amount WHEN 'debit' THEN -amount ELSE 0 END),0) "
        "FROM financial_ledger WHERE user_id=? AND merchant_key=? AND edge_id IS NULL",
        (user_tg_id, merchant_key),
    ).fetchone()[0])


def available_balance(conn: sqlite3.Connection, source_key: str) -> int | None:
    """Return spendable ledger balance; None means unbounded credit.

    Neither user_wallets.balance_toman nor representatives.balance_toman nor
    rep_edges.debt_toman is read. These are projections, not balance authority.
    """
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    parsed = _source(source_key)
    if parsed[0] == "wallet":
        _, merchant_key, rep_id, user_tg_id = parsed
        if rep_id:
            merchant = conn.execute(
                "SELECT is_active,deleted_at FROM representatives WHERE id=?",
                (rep_id,),
            ).fetchone()
            if merchant is None or not bool(merchant[0]) or merchant[1] is not None:
                raise FinancialSourceError("representative wallet merchant is unavailable")
        ledger_balance = _wallet_balance(conn, merchant_key, user_tg_id)
        refund_holds = int(conn.execute(
            "SELECT COALESCE(SUM(amount_toman),0) FROM wallet_refund_holds "
            "WHERE merchant_key=? AND user_tg_id=? AND state='active'",
            (merchant_key, user_tg_id),
        ).fetchone()[0])
        return max(ledger_balance - _active_holds(conn, source_key) - refund_holds, 0)

    edge_id = parsed[1]
    edge = conn.execute(
        "SELECT billing_mode,credit_limit_toman FROM rep_edges WHERE id=? AND active=1",
        (edge_id,),
    ).fetchone()
    if edge is None:
        raise FinancialSourceError("active representative edge does not exist")
    mode = str(edge[0])
    limit = int(edge[1])
    if limit < 0:
        raise FinancialSourceError("credit limit cannot be negative")
    debt = int(conn.execute(
        "SELECT COALESCE(SUM(CASE transaction_type "
        "WHEN 'debit' THEN amount WHEN 'credit' THEN -amount ELSE 0 END),0) "
        "FROM financial_ledger WHERE edge_id=?",
        (edge_id,),
    ).fetchone()[0])
    held = _active_holds(conn, source_key)
    if mode == "credit":
        if limit == 0:
            return None
        return max(limit - debt - held, 0)
    if mode == "prepaid_wallet":
        return max(max(-debt, 0) - held, 0)
    raise FinancialSourceError("unsupported edge billing mode")


def reserve_holds(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    holds: Mapping[str, int],
    now: int,
    expires_at: int | None = None,
) -> dict:
    """Reserve all non-zero sources under one BEGIN IMMEDIATE transaction.

    Source keys are processed lexically. Identical order replay is a no-op;
    differing sources or amounts are rejected without changing existing rows.
    """
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    if conn.in_transaction:
        raise FinancialError("reserve_holds requires a clean connection")
    if not isinstance(order_id, str) or not order_id.strip():
        raise FinancialError("order_id is required")
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise FinancialError("now must be a non-negative UTC epoch-seconds integer")
    if expires_at is not None and (
        isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at < now
    ):
        raise FinancialError("expires_at must be an epoch timestamp at or after now")
    if not isinstance(holds, Mapping):
        raise FinancialError("holds must map source_key to integer toman amount")

    requested = {}
    for source_key, amount in holds.items():
        _source(source_key)
        exact = _amount(amount)
        if exact:
            if source_key in requested:
                raise HoldConflict("source_key is repeated")
            requested[source_key] = exact
    requested = dict(sorted(requested.items()))

    conn.execute("BEGIN IMMEDIATE")
    try:
        order = conn.execute(
            "SELECT status FROM payment_orders WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if order is None:
            raise FinancialError("payment order does not exist")
        if str(order[0]) in {"completed", "cancelled", "expired", "closed_refunded"}:
            raise FinancialError("payment order is not eligible for new holds")

        existing_rows = conn.execute(
            "SELECT source_key,amount_toman,state FROM financial_holds "
            "WHERE order_id=? ORDER BY source_key",
            (order_id,),
        ).fetchall()
        if existing_rows:
            existing = {str(row[0]): int(row[1]) for row in existing_rows}
            if existing != requested:
                raise HoldConflict("order replay differs from its original source/amount set")
            conn.commit()
            return {
                "order_id": order_id,
                "replayed": True,
                "holds": [
                    {
                        "source_key": str(row[0]),
                        "amount_toman": int(row[1]),
                        "state": str(row[2]),
                    }
                    for row in existing_rows
                ],
            }

        for source_key, amount in requested.items():
            available = available_balance(conn, source_key)
            if available is not None and amount > available:
                raise InsufficientFunds(
                    f"insufficient funds for {source_key}: "
                    f"requested={amount}, available={available}"
                )
            conn.execute(
                "INSERT INTO financial_holds(hold_id,order_id,source_key,amount_toman,state,"
                "created_at,expires_at,captured_ledger_id) VALUES(?,?,?,?,'active',?,?,NULL)",
                (secrets.token_hex(16), order_id, source_key, amount, now, expires_at),
            )
        conn.commit()
        return {
            "order_id": order_id,
            "replayed": False,
            "holds": [
                {"source_key": key, "amount_toman": value, "state": "active"}
                for key, value in requested.items()
            ],
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
