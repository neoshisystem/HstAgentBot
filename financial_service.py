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


class LedgerConflict(FinancialError):
    """An idempotency key was reused for a different financial posting."""


def _require_transaction(conn: sqlite3.Connection) -> None:
    if not isinstance(conn, sqlite3.Connection) or not conn.in_transaction:
        raise FinancialError("financial posting must run inside the caller transaction")


def _edge_debt(conn: sqlite3.Connection, edge_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(CASE transaction_type "
        "WHEN 'debit' THEN amount WHEN 'credit' THEN -amount ELSE 0 END),0) "
        "FROM financial_ledger WHERE edge_id=?",
        (edge_id,),
    ).fetchone()
    return int(row[0])


def post_entry(
    conn: sqlite3.Connection,
    *,
    rep_id: int | None,
    user_id: int | None,
    transaction_type: str,
    amount: int,
    balance_after: int,
    description: str,
    actor_id: int,
    business_key: str,
    merchant_key: str,
    ledger_scope: str,
    edge_id: int | None = None,
    order_id: str | None = None,
    operation_id: str | None = None,
    policy_version: str = "v1.0",
    reversal_of: int | None = None,
) -> int:
    """Insert one immutable ledger posting, returning an identical prior row on replay."""
    _require_transaction(conn)
    exact = _amount(amount, allow_zero=False)
    if transaction_type not in {"debit", "credit"}:
        raise FinancialError("transaction_type must be debit or credit")
    if isinstance(balance_after, bool) or not isinstance(balance_after, int):
        raise FinancialError("balance_after must be an integer toman value")
    if isinstance(actor_id, bool) or not isinstance(actor_id, int) or actor_id <= 0:
        raise FinancialError("actor_id must be a positive integer")
    if not isinstance(business_key, str) or not business_key.strip():
        raise FinancialError("business_key is required")
    if not isinstance(merchant_key, str) or not merchant_key.strip():
        raise FinancialError("merchant_key is required")
    if ledger_scope not in {"wallet", "edge"}:
        raise FinancialError("ledger_scope must be wallet or edge")
    if not isinstance(description, str) or not description.strip():
        raise FinancialError("description is required")
    if order_id is not None and (not isinstance(order_id, str) or not order_id.strip()):
        raise FinancialError("order_id must be a non-empty string")
    if edge_id is not None and (isinstance(edge_id, bool) or not isinstance(edge_id, int) or edge_id <= 0):
        raise FinancialError("edge_id must be a positive integer")
    if rep_id is not None and (isinstance(rep_id, bool) or not isinstance(rep_id, int) or rep_id <= 0):
        raise FinancialError("rep_id must be a positive integer")
    if user_id is not None and (isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0):
        raise FinancialError("user_id must be a positive integer")

    prior = conn.execute(
        "SELECT id,rep_id,user_id,transaction_type,amount,balance_after,merchant_key,"
        "ledger_scope,edge_id,order_id,operation_id,reversal_of "
        "FROM financial_ledger WHERE business_key=?",
        (business_key,),
    ).fetchone()
    expected = (
        rep_id, user_id, transaction_type, exact, balance_after, merchant_key,
        ledger_scope, edge_id, order_id, operation_id, reversal_of,
    )
    if prior is not None:
        actual = tuple(prior[index] for index in range(1, 12))
        if actual != expected:
            raise LedgerConflict("business_key already belongs to a different ledger posting")
        return int(prior[0])

    if reversal_of is None:
        # Keep the W07.4 canonical posting contract exact for ordinary capture rows.
        cur = conn.execute(
            "INSERT INTO financial_ledger(rep_id,user_id,transaction_type,amount,balance_after,"
            "description,actor_id,business_key,merchant_key,ledger_scope,edge_id,order_id,"
            "operation_id,policy_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rep_id, user_id, transaction_type, exact, balance_after, description,
                actor_id, business_key, merchant_key, ledger_scope, edge_id, order_id,
                operation_id, policy_version,
            ),
        )
    else:
        # Reversal lineage is written atomically in the INSERT; ledger rows are append-only.
        cur = conn.execute(
            "INSERT INTO financial_ledger(rep_id,user_id,transaction_type,amount,balance_after,"
            "description,actor_id,business_key,merchant_key,ledger_scope,edge_id,order_id,"
            "operation_id,reversal_of,policy_version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                rep_id, user_id, transaction_type, exact, balance_after, description,
                actor_id, business_key, merchant_key, ledger_scope, edge_id, order_id,
                operation_id, reversal_of, policy_version,
            ),
        )
    return int(cur.lastrowid)


def _sync_projection_cache(
    conn: sqlite3.Connection,
    *,
    ledger_scope: str,
    merchant_key: str,
    user_id: int | None = None,
    edge_id: int | None = None,
) -> int:
    """Rebuild only the current projection from immutable ledger rows."""
    if ledger_scope == "wallet":
        if user_id is None:
            raise FinancialError("wallet projection requires user_id")
        balance = _wallet_balance(conn, merchant_key, user_id)
        conn.execute(
            "INSERT INTO user_wallets(user_id,balance_toman,updated_at) "
            "VALUES(?,?,CURRENT_TIMESTAMP) ON CONFLICT(user_id) DO UPDATE SET "
            "balance_toman=excluded.balance_toman,updated_at=CURRENT_TIMESTAMP",
            (user_id, balance),
        )
        return balance
    if ledger_scope == "edge":
        if edge_id is None:
            raise FinancialError("edge projection requires edge_id")
        debt = _edge_debt(conn, edge_id)
        row = conn.execute(
            "SELECT child_rep_id,parent_key FROM rep_edges WHERE id=?",
            (edge_id,),
        ).fetchone()
        if row is None:
            raise FinancialSourceError("edge disappeared while syncing its projection")
        conn.execute("UPDATE rep_edges SET debt_toman=? WHERE id=?", (debt, edge_id))
        if str(row[1]) == "root":
            conn.execute(
                "UPDATE representatives SET balance_toman=? WHERE id=?",
                (debt, int(row[0])),
            )
        return debt
    raise FinancialError("unsupported ledger projection")


def _available_excluding_own_hold(
    conn: sqlite3.Connection, source_key: str, order_id: str, amount: int
) -> int | None:
    parsed = _source(source_key)
    other_holds = int(conn.execute(
        "SELECT COALESCE(SUM(amount_toman),0) FROM financial_holds "
        "WHERE source_key=? AND state='active' AND order_id<>?",
        (source_key, order_id),
    ).fetchone()[0])
    if parsed[0] == "wallet":
        _, merchant_key, _, user_tg_id = parsed
        refund_holds = int(conn.execute(
            "SELECT COALESCE(SUM(amount_toman),0) FROM wallet_refund_holds "
            "WHERE merchant_key=? AND user_tg_id=? AND state='active'",
            (merchant_key, user_tg_id),
        ).fetchone()[0])
        return _wallet_balance(conn, merchant_key, user_tg_id) - other_holds - refund_holds

    edge_id = parsed[1]
    edge = conn.execute(
        "SELECT billing_mode,credit_limit_toman FROM rep_edges WHERE id=? AND active=1",
        (edge_id,),
    ).fetchone()
    if edge is None:
        raise FinancialSourceError("active representative edge does not exist")
    mode, limit = str(edge[0]), int(edge[1])
    debt = _edge_debt(conn, edge_id)
    if mode == "credit":
        return None if limit == 0 else limit - debt - other_holds
    if mode == "prepaid_wallet":
        return max(-debt, 0) - other_holds
    raise FinancialSourceError("unsupported edge billing mode")


def capture_holds(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    actor_id: int,
    now: int,
    policy_version: str = "v1.0",
) -> dict:
    """Capture every active order hold atomically; the caller owns BEGIN IMMEDIATE/commit."""
    _require_transaction(conn)
    if not isinstance(order_id, str) or not order_id.strip():
        raise FinancialError("order_id is required")
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise FinancialError("now must be a non-negative UTC epoch-seconds integer")
    order = conn.execute(
        "SELECT status FROM payment_orders WHERE order_id=?", (order_id,)
    ).fetchone()
    if order is None:
        raise FinancialError("payment order does not exist")
    if str(order[0]) not in {"accepted", "completed"}:
        raise FinancialError("only an accepted payment order can be captured")
    holds = conn.execute(
        "SELECT hold_id,source_key,amount_toman,state,captured_ledger_id "
        "FROM financial_holds WHERE order_id=? ORDER BY source_key",
        (order_id,),
    ).fetchall()
    if not holds:
        raise FinancialError("payment order has no financial holds")

    prepared = []
    for row in holds:
        hold_id, source_key, amount = str(row[0]), str(row[1]), int(row[2])
        state, captured_id = str(row[3]), row[4]
        if state == "captured":
            if captured_id is None:
                raise FinancialError("captured hold is missing its ledger reference")
            ledger = conn.execute(
                "SELECT business_key FROM financial_ledger WHERE id=?", (int(captured_id),)
            ).fetchone()
            if ledger is None or str(ledger[0]) != f"order:{order_id}:capture:{source_key}":
                raise FinancialError("captured hold ledger reference is inconsistent")
            prepared.append((hold_id, source_key, amount, state, int(captured_id), None))
            continue
        if state != "active":
            raise FinancialError("released holds cannot be captured")
        parsed = _source(source_key)
        available = _available_excluding_own_hold(conn, source_key, order_id, amount)
        if available is not None and available < amount:
            raise InsufficientFunds(
                f"held source is no longer fundable: {source_key}; "
                f"requested={amount}; available={available}"
            )
        if parsed[0] == "wallet":
            _, merchant_key, rep_id, user_id = parsed
            prepared.append((
                hold_id, source_key, amount, state, None,
                {
                    "rep_id": rep_id or None, "user_id": user_id,
                    "transaction_type": "debit", "merchant_key": merchant_key,
                    "ledger_scope": "wallet", "edge_id": None,
                },
            ))
        else:
            edge_id = parsed[1]
            edge = conn.execute(
                "SELECT child_rep_id,parent_key FROM rep_edges WHERE id=? AND active=1",
                (edge_id,),
            ).fetchone()
            if edge is None:
                raise FinancialSourceError("active representative edge does not exist")
            prepared.append((
                hold_id, source_key, amount, state, None,
                {
                    "rep_id": int(edge[0]), "user_id": None,
                    "transaction_type": "debit", "merchant_key": str(edge[1]),
                    "ledger_scope": "edge", "edge_id": edge_id,
                },
            ))

    captured_ids = []
    for hold_id, source_key, amount, state, prior_id, posting in prepared:
        if state == "captured":
            captured_ids.append(prior_id)
            continue
        merchant_key = posting["merchant_key"]
        if posting["ledger_scope"] == "wallet":
            current = _wallet_balance(conn, merchant_key, posting["user_id"])
            after = current - amount
        else:
            after = _edge_debt(conn, posting["edge_id"]) + amount
        ledger_id = post_entry(
            conn,
            rep_id=posting["rep_id"],
            user_id=posting["user_id"],
            transaction_type="debit",
            amount=amount,
            balance_after=after,
            description=f"Order {order_id} captured from {source_key}",
            actor_id=actor_id,
            business_key=f"order:{order_id}:capture:{source_key}",
            merchant_key=merchant_key,
            ledger_scope=posting["ledger_scope"],
            edge_id=posting["edge_id"],
            order_id=order_id,
            operation_id=f"capture:{order_id}",
            policy_version=policy_version,
        )
        _sync_projection_cache(
            conn,
            ledger_scope=posting["ledger_scope"],
            merchant_key=merchant_key,
            user_id=posting["user_id"],
            edge_id=posting["edge_id"],
        )
        conn.execute(
            "UPDATE financial_holds SET state='captured',captured_ledger_id=? "
            "WHERE hold_id=? AND state='active'",
            (ledger_id, hold_id),
        )
        captured_ids.append(ledger_id)

    conn.execute(
        "UPDATE payment_orders SET status='completed',revision=revision+1 "
        "WHERE order_id=? AND status='accepted'",
        (order_id,),
    )
    return {
        "order_id": order_id,
        "replayed": all(item[3] == "captured" for item in prepared),
        "ledger_ids": captured_ids,
    }


def release_holds(conn: sqlite3.Connection, *, order_id: str) -> int:
    """Release all still-active holds for an order without rewriting captured history."""
    _require_transaction(conn)
    cur = conn.execute(
        "UPDATE financial_holds SET state='released' WHERE order_id=? AND state='active'",
        (order_id,),
    )
    return int(cur.rowcount)


def _record_debt_claim(
    conn: sqlite3.Connection, *, order_id: str, ledger_id: int,
    merchant_key: str, user_id: int, amount: int, now: int,
) -> None:
    payload = {
        "order_id": order_id,
        "reversal_of": ledger_id,
        "merchant_key": merchant_key,
        "user_id": user_id,
        "amount_toman": amount,
        "reason": "wallet credit was spent or reserved before refund",
        "requires_operator_review": True,
    }
    conn.execute(
        "INSERT OR IGNORE INTO scheduled_jobs(job_key,kind,due_at,state,result_json) "
        "VALUES(?,?,?,?,?)",
        (
            f"debt-claim:order:{order_id}:ledger:{ledger_id}",
            "financial_debt_claim",
            now,
            "operator_review",
            __import__("json").dumps(payload, sort_keys=True, separators=(",", ":")),
        ),
    )


def reverse_order(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    actor_id: int,
    now: int,
    policy_version: str = "v1.0",
) -> dict:
    """Append immutable reversal entries; never mutate or delete original ledger rows."""
    _require_transaction(conn)
    if not isinstance(order_id, str) or not order_id.strip():
        raise FinancialError("order_id is required")
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise FinancialError("now must be a non-negative UTC epoch-seconds integer")
    order = conn.execute(
        "SELECT status FROM payment_orders WHERE order_id=?", (order_id,)
    ).fetchone()
    if order is None:
        return {"order_id": order_id, "status": "blocked", "reason": "order_missing"}
    status = str(order[0])
    if status == "closed_refunded":
        return {"order_id": order_id, "status": "replayed", "reversed_ids": []}
    if status == "exception":
        claims = int(conn.execute(
            "SELECT COUNT(*) FROM scheduled_jobs WHERE kind='financial_debt_claim' "
            "AND job_key LIKE ?",
            (f"debt-claim:order:{order_id}:ledger:%",),
        ).fetchone()[0])
        return {
            "order_id": order_id,
            "status": "operator_exception" if claims else "blocked",
            "debt_claims": claims,
            "reason": "existing_operator_exception",
        }
    if status != "completed":
        return {"order_id": order_id, "status": "blocked", "reason": "order_not_completed"}

    originals = conn.execute(
        "SELECT id,rep_id,user_id,transaction_type,amount,description,merchant_key,"
        "ledger_scope,edge_id,operation_id,policy_version,business_key "
        "FROM financial_ledger WHERE order_id=? AND ledger_scope IN ('wallet','edge') "
        "AND (business_key LIKE ? OR business_key LIKE ?) ORDER BY id",
        (order_id, f"order:{order_id}:capture:%", f"order:{order_id}:cashback:%"),
    ).fetchall()
    if not originals:
        raise FinancialError("completed order has no reversible ledger postings")

    reversed_ids = []
    debt_claims = []
    for row in originals:
        original_id = int(row[0])
        rep_id, user_id = row[1], row[2]
        original_type, original_amount = str(row[3]), int(row[4])
        merchant_key, scope, edge_id = str(row[6]), str(row[7]), row[8]
        business_key = f"order:{order_id}:reverse:{original_id}"
        existing = conn.execute(
            "SELECT id FROM financial_ledger WHERE business_key=?", (business_key,)
        ).fetchone()
        if existing is not None:
            reversed_ids.append(int(existing[0]))
            continue

        reverse_amount = original_amount
        reverse_type = "credit" if original_type == "debit" else "debit"
        if scope == "wallet" and reverse_type == "debit":
            if user_id is None:
                raise FinancialError("wallet credit reversal is missing user_id")
            prefix = "root" if merchant_key == "root" else merchant_key
            source_key = f"wallet:{prefix}:{int(user_id)}" if prefix == "root" else (
                f"wallet:{prefix}:{int(user_id)}"
            )
            available = available_balance(conn, source_key)
            if available is None:
                raise FinancialError("wallet reversal cannot use an unbounded source")
            reverse_amount = min(original_amount, max(int(available), 0))
            if reverse_amount == 0:
                _record_debt_claim(
                    conn, order_id=order_id, ledger_id=original_id,
                    merchant_key=merchant_key, user_id=int(user_id),
                    amount=original_amount, now=now,
                )
                debt_claims.append({"ledger_id": original_id, "amount_toman": original_amount})
                continue

        if scope == "wallet":
            current = _wallet_balance(conn, merchant_key, int(user_id))
            after = current + reverse_amount if reverse_type == "credit" else current - reverse_amount
        else:
            current = _edge_debt(conn, int(edge_id))
            after = current - reverse_amount if reverse_type == "credit" else current + reverse_amount

        reversal_id = post_entry(
            conn,
            rep_id=int(rep_id) if rep_id is not None else None,
            user_id=int(user_id) if user_id is not None else None,
            transaction_type=reverse_type,
            amount=reverse_amount,
            balance_after=after,
            description=f"Reversal of ledger entry {original_id} for order {order_id}",
            actor_id=actor_id,
            business_key=business_key,
            merchant_key=merchant_key,
            ledger_scope=scope,
            edge_id=int(edge_id) if edge_id is not None else None,
            order_id=order_id,
            operation_id=f"reverse:{order_id}:{original_id}",
            policy_version=policy_version,
            reversal_of=original_id,
        )
        _sync_projection_cache(
            conn,
            ledger_scope=scope,
            merchant_key=merchant_key,
            user_id=int(user_id) if user_id is not None else None,
            edge_id=int(edge_id) if edge_id is not None else None,
        )
        reversed_ids.append(reversal_id)
        if reverse_amount < original_amount:
            remainder = original_amount - reverse_amount
            _record_debt_claim(
                conn, order_id=order_id, ledger_id=original_id,
                merchant_key=merchant_key, user_id=int(user_id),
                amount=remainder, now=now,
            )
            debt_claims.append({"ledger_id": original_id, "amount_toman": remainder})

    release_holds(conn, order_id=order_id)
    final_status = "exception" if debt_claims else "closed_refunded"
    conn.execute(
        "UPDATE payment_orders SET status=?,revision=revision+1 WHERE order_id=? AND status='completed'",
        (final_status, order_id),
    )
    return {
        "order_id": order_id,
        "status": "operator_exception" if debt_claims else "refunded",
        "reversed_ids": reversed_ids,
        "debt_claims": debt_claims,
    }
