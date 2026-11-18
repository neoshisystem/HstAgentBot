"""Atomic issuer-funded coupon reservations and cashback wallet postings."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from financial_service import (
    FinancialError,
    InsufficientFunds,
    _edge_debt,
    _require_transaction,
    _sync_projection_cache,
    _wallet_balance,
    available_balance,
    post_entry,
)


class PromotionError(ValueError):
    """Base validation error for coupons and cashback."""


class PromotionBudgetExceeded(PromotionError):
    """Issuer-funded coupon or cashback budget is insufficient."""


class PromotionConflict(PromotionError):
    """An idempotency key or order already belongs to another promotion."""


def _positive(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PromotionError(f"{field} must be a positive integer toman value")
    return value


def _transaction(conn: sqlite3.Connection) -> bool:
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    if conn.in_transaction:
        _require_transaction(conn)
        return False
    conn.execute("BEGIN IMMEDIATE")
    return True


def _finish(conn: sqlite3.Connection, started: bool, error: bool = False) -> None:
    if not started:
        return
    if error:
        if conn.in_transaction:
            conn.rollback()
    elif conn.in_transaction:
        conn.commit()


def hold_coupon(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    code: str,
    price_toman: int,
    now: int,
) -> dict:
    """Reserve one issuer-funded coupon discount under a serialized SQLite write lock."""
    exact_price = _positive(price_toman, "price_toman")
    if not isinstance(order_id, str) or not order_id.strip():
        raise PromotionError("order_id is required")
    if not isinstance(code, str) or not code.strip():
        raise PromotionError("coupon code is required")
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise PromotionError("now must be a non-negative UTC epoch-seconds integer")
    started = _transaction(conn)
    try:
        order = conn.execute(
            "SELECT merchant_key,status FROM payment_orders WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if order is None or str(order[1]) not in {"awaiting_payment", "pending_review", "accepted"}:
            raise PromotionError("coupon requires an open payment order")
        coupon = conn.execute(
            "SELECT issuer_key,discount_toman,budget_toman,used_toman,max_uses,expires_at,is_active "
            "FROM coupons WHERE code=?",
            (code,),
        ).fetchone()
        if coupon is None or not bool(coupon[6]) or int(coupon[5]) <= now:
            raise PromotionError("coupon is missing, inactive or expired")
        issuer_key = str(coupon[0])
        value = min(int(coupon[1]), exact_price)
        budget, used, max_uses = int(coupon[2]), int(coupon[3]), int(coupon[4])
        existing = conn.execute(
            "SELECT code,amount_toman,state FROM coupon_uses WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if existing is not None:
            if str(existing[0]) != code:
                raise PromotionConflict("an order can hold at most one coupon")
            if str(existing[2]) in {"held", "used"}:
                if int(existing[1]) != value:
                    raise PromotionConflict("coupon replay changed its discount snapshot")
                _finish(conn, started)
                return {
                    "order_id": order_id, "code": code, "issuer_key": issuer_key,
                    "discount_toman": value, "state": str(existing[2]), "replayed": True,
                }

        held = int(conn.execute(
            "SELECT COALESCE(SUM(amount_toman),0) FROM coupon_uses WHERE code=? AND state='held'",
            (code,),
        ).fetchone()[0])
        uses = int(conn.execute(
            "SELECT COUNT(*) FROM coupon_uses WHERE code=? AND state IN ('held','used')",
            (code,),
        ).fetchone()[0])
        remaining = budget - used - held
        if value > remaining or uses >= max_uses:
            raise PromotionBudgetExceeded(
                f"coupon issuer budget or usage limit exhausted: requested={value}, remaining={max(remaining, 0)}"
            )
        if existing is None:
            conn.execute(
                "INSERT INTO coupon_uses(code,order_id,amount_toman,state) VALUES(?,?,?,'held')",
                (code, order_id, value),
            )
        else:
            conn.execute(
                "UPDATE coupon_uses SET amount_toman=?,state='held' WHERE order_id=? AND code=? AND state='released'",
                (value, order_id, code),
            )
        _finish(conn, started)
        return {
            "order_id": order_id, "code": code, "issuer_key": issuer_key,
            "discount_toman": value, "state": "held", "replayed": False,
        }
    except Exception:
        _finish(conn, started, error=True)
        raise


def finalize_coupon(conn: sqlite3.Connection, *, order_id: str, success: bool) -> dict:
    """Move a held coupon to used on success or release it on payment failure."""
    started = _transaction(conn)
    try:
        row = conn.execute(
            "SELECT code,amount_toman,state FROM coupon_uses WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if row is None:
            raise PromotionError("order has no coupon reservation")
        code, amount, state = str(row[0]), int(row[1]), str(row[2])
        target = "used" if success else "released"
        if state == target:
            _finish(conn, started)
            return {"order_id": order_id, "code": code, "state": target, "replayed": True}
        if state != "held":
            raise PromotionConflict("coupon reservation is no longer held")
        if success:
            conn.execute(
                "UPDATE coupons SET used_toman=used_toman+? WHERE code=?",
                (amount, code),
            )
        conn.execute(
            "UPDATE coupon_uses SET state=? WHERE order_id=? AND state='held'",
            (target, order_id),
        )
        _finish(conn, started)
        return {"order_id": order_id, "code": code, "state": target, "replayed": False}
    except Exception:
        _finish(conn, started, error=True)
        raise


def _issuer_source(source_key: str) -> tuple[str, int | None, int | None, int | None]:
    root = re.fullmatch(r"wallet:root:([1-9][0-9]*)", source_key)
    if root:
        return "root", None, int(root.group(1)), None
    rep = re.fullmatch(r"wallet:rep:([1-9][0-9]*):([1-9][0-9]*)", source_key)
    if rep:
        rep_id = int(rep.group(1))
        return f"rep:{rep_id}", rep_id, int(rep.group(2)), None
    edge = re.fullmatch(r"edge:([1-9][0-9]*)", source_key)
    if edge:
        return "", None, None, int(edge.group(1))
    raise PromotionError("unsupported issuer_source_key")


def post_cashback(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    amount_toman: int,
    issuer_source_key: str,
    actor_id: int,
    now: int,
    policy_version: str = "v1.0",
) -> dict:
    """Debit a finite issuer budget and credit the completed order's merchant wallet once."""
    amount = _positive(amount_toman, "amount_toman")
    if not isinstance(issuer_source_key, str) or not issuer_source_key:
        raise PromotionError("issuer_source_key is required")
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise PromotionError("now must be a non-negative UTC epoch-seconds integer")
    started = _transaction(conn)
    try:
        order = conn.execute(
            "SELECT actor_tg_id,merchant_key,status FROM payment_orders WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if order is None or str(order[2]) != "completed":
            raise PromotionError("cashback is only permitted for a completed order")
        user_id, merchant_key = int(order[0]), str(order[1])
        source_merchant, rep_id, source_user_id, edge_id = _issuer_source(issuer_source_key)
        if edge_id is not None:
            edge = conn.execute(
                "SELECT parent_key,child_rep_id,active FROM rep_edges WHERE id=?",
                (edge_id,),
            ).fetchone()
            if edge is None or not bool(edge[2]) or str(edge[0]) != merchant_key:
                raise PromotionError("issuer edge does not belong to the order merchant")
            rep_id = int(edge[1])
            source_merchant = str(edge[0])
        elif source_merchant != merchant_key:
            raise PromotionError("issuer wallet must belong to the order merchant")
        if source_user_id == user_id:
            raise PromotionError("buyer wallet cannot fund its own cashback")
        source_key = f"order:{order_id}:cashback:source:{issuer_source_key}"
        wallet_key = f"order:{order_id}:cashback:wallet:{merchant_key}:{user_id}"
        existing_source = conn.execute(
            "SELECT id,amount,merchant_key,ledger_scope,edge_id FROM financial_ledger WHERE business_key=?",
            (source_key,),
        ).fetchone()
        existing_wallet = conn.execute(
            "SELECT id,amount,merchant_key,ledger_scope,user_id FROM financial_ledger WHERE business_key=?",
            (wallet_key,),
        ).fetchone()
        if existing_source is not None or existing_wallet is not None:
            if existing_source is None or existing_wallet is None:
                raise PromotionConflict("cashback pair is partially persisted")
            if (
                int(existing_source[1]) != amount or str(existing_source[2]) != merchant_key
                or str(existing_source[3]) != ("edge" if edge_id is not None else "wallet")
                or int(existing_wallet[1]) != amount or str(existing_wallet[2]) != merchant_key
                or str(existing_wallet[3]) != "wallet" or int(existing_wallet[4]) != user_id
            ):
                raise PromotionConflict("cashback replay changed its immutable posting snapshot")
            _finish(conn, started)
            return {
                "order_id": order_id, "amount_toman": amount,
                "source_ledger_id": int(existing_source[0]),
                "wallet_ledger_id": int(existing_wallet[0]), "replayed": True,
            }

        available = available_balance(conn, issuer_source_key)
        if available is None or int(available) < amount:
            raise PromotionBudgetExceeded("issuer cashback budget is insufficient or unbounded")
        if edge_id is None:
            current_source = _wallet_balance(conn, merchant_key, int(source_user_id))
            source_after = current_source - amount
            source_scope = "wallet"
        else:
            current_source = _edge_debt(conn, edge_id)
            source_after = current_source + amount
            source_scope = "edge"
        source_id = post_entry(
            conn,
            rep_id=rep_id,
            user_id=source_user_id,
            transaction_type="debit",
            amount=amount,
            balance_after=source_after,
            description=f"Cashback funding for order {order_id}",
            actor_id=actor_id,
            business_key=source_key,
            merchant_key=merchant_key,
            ledger_scope=source_scope,
            edge_id=edge_id,
            order_id=order_id,
            operation_id=f"cashback:{order_id}",
            policy_version=policy_version,
        )
        wallet_after = _wallet_balance(conn, merchant_key, user_id) + amount
        wallet_id = post_entry(
            conn,
            rep_id=None,
            user_id=user_id,
            transaction_type="credit",
            amount=amount,
            balance_after=wallet_after,
            description=f"Cashback credited for order {order_id}",
            actor_id=actor_id,
            business_key=wallet_key,
            merchant_key=merchant_key,
            ledger_scope="wallet",
            edge_id=None,
            order_id=order_id,
            operation_id=f"cashback:{order_id}",
            policy_version=policy_version,
        )
        _sync_projection_cache(
            conn,
            ledger_scope=source_scope,
            merchant_key=merchant_key,
            user_id=source_user_id,
            edge_id=edge_id,
        )
        _sync_projection_cache(
            conn, ledger_scope="wallet", merchant_key=merchant_key, user_id=user_id
        )
        _finish(conn, started)
        return {
            "order_id": order_id, "amount_toman": amount,
            "source_ledger_id": source_id, "wallet_ledger_id": wallet_id,
            "replayed": False,
        }
    except Exception:
        _finish(conn, started, error=True)
        raise


# --------------------------------------------------------------------------- #
# Coupon catalogue administration
#
# ``hold_coupon``/``finalize_coupon`` spend a coupon; nothing could create, edit or
# retire one, so the catalogue was whatever a migration left behind. These writers
# are the missing half. Every reader resolves the coupon at call time, so an edit
# here takes effect on the next order.
# --------------------------------------------------------------------------- #

COUPON_CODE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$")
MAX_COUPON_DISCOUNT_TOMAN = 10 ** 12
MAX_COUPON_BUDGET_TOMAN = 10 ** 15
MAX_COUPON_USES = 1_000_000
MAX_COUPON_DAYS = 3650


def _clean_coupon_code(value: Any) -> str:
    code = str(value or "").strip().upper()
    if not COUPON_CODE_RE.fullmatch(code):
        raise PromotionError(
            "کد کوپن باید ۳ تا ۳۲ نویسه و فقط شامل حروف لاتین، رقم، نقطه، خط تیره و زیرخط باشد."
        )
    return code


def _clean_amount(value: Any, field_fa: str, maximum: int, *, minimum: int = 0) -> int:
    if isinstance(value, bool):
        raise PromotionError(f"{field_fa} باید یک عدد صحیح باشد.")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise PromotionError(f"{field_fa} باید یک عدد صحیح باشد.") from exc
    if number < minimum or number > maximum:
        raise PromotionError(f"{field_fa} باید بین {minimum} و {maximum} باشد.")
    return number


def coupon_row(conn: sqlite3.Connection, code: Any) -> dict | None:
    """One coupon row, or ``None``."""
    row = conn.execute(
        "SELECT code,issuer_key,discount_toman,budget_toman,used_toman,max_uses,"
        "expires_at,is_active FROM coupons WHERE code=?",
        (_clean_coupon_code(code),),
    ).fetchone()
    if row is None:
        return None
    return {
        "code": str(row[0]), "issuer_key": str(row[1]),
        "discount_toman": int(row[2]), "budget_toman": int(row[3]),
        "used_toman": int(row[4]), "max_uses": int(row[5]),
        "expires_at": int(row[6]), "is_active": bool(row[7]),
    }


def list_coupons(conn: sqlite3.Connection) -> list[dict]:
    """Every coupon with its live hold/use counters."""
    rows = conn.execute(
        "SELECT c.code,c.issuer_key,c.discount_toman,c.budget_toman,c.used_toman,"
        "c.max_uses,c.expires_at,c.is_active,"
        "(SELECT COUNT(*) FROM coupon_uses u WHERE u.code=c.code AND u.state='held') AS held_uses,"
        "(SELECT COUNT(*) FROM coupon_uses u WHERE u.code=c.code AND u.state='used') AS used_uses "
        "FROM coupons c ORDER BY c.is_active DESC,c.code"
    ).fetchall()
    return [
        {
            "code": str(row[0]), "issuer_key": str(row[1]),
            "discount_toman": int(row[2]), "budget_toman": int(row[3]),
            "used_toman": int(row[4]), "max_uses": int(row[5]),
            "expires_at": int(row[6]), "is_active": bool(row[7]),
            "held_uses": int(row[8]), "used_uses": int(row[9]),
        }
        for row in rows
    ]


def create_coupon(
    conn: sqlite3.Connection, *, code: Any, discount_toman: Any, budget_toman: Any,
    max_uses: Any, expires_at: Any, issuer_key: str = "root",
) -> dict:
    """Create one issuer-funded coupon; the code is unique and upper-case."""
    selected = _clean_coupon_code(code)
    if coupon_row(conn, selected) is not None:
        raise PromotionError(f"کوپن «{selected}» از قبل وجود دارد.")
    discount = _clean_amount(discount_toman, "مبلغ تخفیف", MAX_COUPON_DISCOUNT_TOMAN, minimum=1)
    budget = _clean_amount(budget_toman, "سقف بودجه", MAX_COUPON_BUDGET_TOMAN, minimum=discount)
    uses = _clean_amount(max_uses, "حداکثر تعداد استفاده", MAX_COUPON_USES, minimum=1)
    expires = _clean_amount(expires_at, "زمان انقضا", 2 ** 53)
    conn.execute(
        "INSERT INTO coupons(code,issuer_key,discount_toman,budget_toman,used_toman,"
        "max_uses,expires_at,is_active) VALUES(?,?,?,?,0,?,?,1)",
        (selected, str(issuer_key or "root"), discount, budget, uses, expires),
    )
    return coupon_row(conn, selected)


def update_coupon(conn: sqlite3.Connection, code: Any, **fields) -> dict:
    """Edit one coupon in place; only the supplied fields change."""
    selected = _clean_coupon_code(code)
    if coupon_row(conn, selected) is None:
        raise PromotionError(f"کوپن «{selected}» یافت نشد.")
    assignments: list[str] = []
    params: list[Any] = []
    if fields.get("discount_toman") is not None:
        assignments.append("discount_toman=?")
        params.append(_clean_amount(fields["discount_toman"], "مبلغ تخفیف",
                                    MAX_COUPON_DISCOUNT_TOMAN, minimum=1))
    if fields.get("budget_toman") is not None:
        assignments.append("budget_toman=?")
        params.append(_clean_amount(fields["budget_toman"], "سقف بودجه",
                                    MAX_COUPON_BUDGET_TOMAN, minimum=0))
    if fields.get("max_uses") is not None:
        assignments.append("max_uses=?")
        params.append(_clean_amount(fields["max_uses"], "حداکثر تعداد استفاده",
                                    MAX_COUPON_USES, minimum=1))
    if fields.get("expires_at") is not None:
        assignments.append("expires_at=?")
        params.append(_clean_amount(fields["expires_at"], "زمان انقضا", 2 ** 53))
    if fields.get("is_active") is not None:
        assignments.append("is_active=?")
        params.append(1 if fields["is_active"] else 0)
    unknown = sorted(set(fields) - {"discount_toman", "budget_toman", "max_uses",
                                    "expires_at", "is_active"})
    if unknown:
        raise PromotionError("فیلد ناشناخته: " + ", ".join(unknown))
    if not assignments:
        return coupon_row(conn, selected)
    params.append(selected)
    conn.execute(
        "UPDATE coupons SET " + ",".join(assignments) + " WHERE code=?", params)
    return coupon_row(conn, selected)


def set_coupon_active(conn: sqlite3.Connection, code: Any, active: Any) -> dict:
    """Enable or retire one coupon without deleting its spend history."""
    return update_coupon(conn, code, is_active=bool(active))


def delete_coupon(conn: sqlite3.Connection, code: Any) -> dict:
    """Delete a coupon only while no order holds or has used it.

    Removing a coupon that ``coupon_uses`` still references would either break the
    foreign key or orphan a discount an order is relying on, so the guard turns
    that into an explicit refusal instead of a silent data loss.
    """
    selected = _clean_coupon_code(code)
    if coupon_row(conn, selected) is None:
        raise PromotionError(f"کوپن «{selected}» یافت نشد.")
    used = int(conn.execute(
        "SELECT COUNT(*) FROM coupon_uses WHERE code=?", (selected,)).fetchone()[0])
    if used:
        raise PromotionError(
            f"این کوپن در {used} سفارش استفاده یا رزرو شده است؛ به‌جای حذف آن را غیرفعال کنید."
        )
    conn.execute("DELETE FROM coupons WHERE code=?", (selected,))
    return {"code": selected, "deleted": True}
