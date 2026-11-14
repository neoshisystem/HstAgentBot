"""Per-edge v5.2 pricing rules; all money values are integer toman."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
import sqlite3
import time
from typing import Any


POLICY_VERSION = "v1.0"


class PricingError(ValueError):
    """A price, tenant edge, or approved pricing policy is invalid."""


def round_toman(value: Any) -> int:
    """Round a non-negative price to integer toman using Decimal HALF_UP."""
    if isinstance(value, bool):
        raise PricingError("boolean is not a money value")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise PricingError("invalid toman amount") from exc
    if not amount.is_finite() or amount < 0:
        raise PricingError("toman amount must be finite and non-negative")
    return int(amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _positive_id(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PricingError(f"{label} must be a positive integer")
    return value


def _bps(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10000:
        raise PricingError(f"{label} must be an integer in [0,10000]")
    return value


def _tier_discount(conn: sqlite3.Connection, actor_type: str, tier: str) -> int:
    row = conn.execute(
        "SELECT discount_bps FROM tier_policies WHERE actor_type=? AND tier_code=?",
        (actor_type, str(tier or "normal").strip().lower()),
    ).fetchone()
    return 0 if row is None else _bps(row[0], "tier discount")


def _plan(conn: sqlite3.Connection, plan_code: str) -> dict:
    if not isinstance(plan_code, str) or not plan_code.strip():
        raise PricingError("plan_code is required")
    row = conn.execute(
        "SELECT plan_code,rep_price,user_price,revision FROM pricing_plans WHERE plan_code=?",
        (plan_code.strip(),),
    ).fetchone()
    if row is None:
        raise PricingError("pricing plan does not exist")
    return {
        "plan_code": str(row[0]),
        "rep_price": round_toman(row[1]),
        "user_price": round_toman(row[2]),
        "revision": int(row[3]),
    }


def _custom_price(conn: sqlite3.Connection, seller_key: str, buyer_rep_id: int, plan_code: str):
    row = conn.execute(
        "SELECT price_toman,revision FROM rep_custom_prices "
        "WHERE seller_key=? AND buyer_rep_id=? AND plan_code=?",
        (seller_key, buyer_rep_id, plan_code),
    ).fetchone()
    if row is None:
        return None
    return round_toman(row[0]), int(row[1])


def _rep_cost(conn: sqlite3.Connection, rep_id: int, plan: dict, seen: set[int]):
    """Resolve one representative's price from its direct parent."""
    rep_id = _positive_id(rep_id, "rep_id")
    if rep_id in seen or len(seen) >= 4:
        raise PricingError("representative ancestry is cyclic or too deep")
    seen.add(rep_id)
    rep = conn.execute(
        "SELECT id,parent_rep_id,tier,markup_bps,is_active,deleted_at "
        "FROM representatives WHERE id=?",
        (rep_id,),
    ).fetchone()
    if rep is None or not bool(rep[4]) or rep[5] is not None:
        raise PricingError("representative is unavailable")
    edges = conn.execute(
        "SELECT id,parent_key,billing_mode,active FROM rep_edges "
        "WHERE child_rep_id=? AND active=1 ORDER BY id",
        (rep_id,),
    ).fetchall()
    if len(edges) != 1:
        raise PricingError("representative must have exactly one active parent edge")
    edge = edges[0]
    expected_parent = "root" if rep[1] is None else f"rep:{int(rep[1])}"
    seller_key = str(edge[1])
    if seller_key != expected_parent:
        raise PricingError("representative hierarchy and active edge disagree")

    markup = 0
    upstream_edges = []
    if seller_key == "root":
        cost = plan["rep_price"]
        revision = plan["revision"]
    else:
        match = re.fullmatch(r"rep:([1-9][0-9]*)", seller_key)
        if match is None:
            raise PricingError("unsupported parent seller key")
        parent_id = int(match.group(1))
        parent_cost, upstream_edges = _rep_cost(conn, parent_id, plan, seen)
        parent = conn.execute(
            "SELECT markup_bps FROM representatives WHERE id=?",
            (parent_id,),
        ).fetchone()
        if parent is None:
            raise PricingError("parent representative is unavailable")
        markup = _bps(parent[0], "parent markup")
        cost = round_toman(Decimal(parent_cost) * Decimal(10000 + markup) / Decimal(10000))
        revision = plan["revision"]

    custom = _custom_price(conn, seller_key, rep_id, plan["plan_code"])
    custom_used = custom is not None
    if custom_used:
        cost, revision = custom

    discount = _tier_discount(conn, "rep", str(rep[2] or "normal"))
    final_price = round_toman(Decimal(cost) * Decimal(10000 - discount) / Decimal(10000))
    edge_item = {
        "edge_id": int(edge[0]),
        "seller_key": seller_key,
        "buyer_key": f"rep:{rep_id}",
        "source_key": f"edge:{int(edge[0])}",
        "cost_before_tier_toman": cost,
        "discount_bps": discount,
        "markup_bps": 0 if custom_used else markup,
        "price_toman": final_price,
        "revision": int(revision),
        "custom": custom_used,
        "plan_code": plan["plan_code"],
    }
    return final_price, [*upstream_edges, edge_item]


def price_chain(
    conn: sqlite3.Connection,
    *,
    plan_code: str,
    seller_key: str,
    buyer_rep_id: int | None = None,
    buyer_tg_id: int | None = None,
    purpose: str = "sale",
    coupon_code: str | None = None,
    now: int | None = None,
) -> dict:
    """Calculate a direct seller-to-buyer price and its per-edge price snapshot."""
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    if not isinstance(seller_key, str) or (
        seller_key != "root" and re.fullmatch(r"rep:[1-9][0-9]*", seller_key) is None
    ):
        raise PricingError("seller_key must be root or rep:<id>")
    if (buyer_rep_id is None) == (buyer_tg_id is None):
        raise PricingError("exactly one buyer identity is required")
    plan = _plan(conn, plan_code)
    purpose = str(purpose or "sale").strip().lower()
    timestamp = int(time.time()) if now is None else now
    if isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0:
        raise PricingError("now must be a non-negative UTC epoch-seconds integer")

    if purpose == "personal_use" and seller_key == "root":
        buyer_key = (
            f"rep:{_positive_id(buyer_rep_id, 'buyer_rep_id')}"
            if buyer_rep_id is not None
            else f"user:{_positive_id(buyer_tg_id, 'buyer_tg_id')}"
        )
        return {
            "plan_code": plan["plan_code"], "merchant_key": seller_key,
            "buyer_key": buyer_key, "purpose": purpose, "amount_toman": 0,
            "price_edges": [], "policy_version": POLICY_VERSION, "coupon": None,
        }

    upstream_cost = 0
    if buyer_rep_id is not None:
        buyer_rep_id = _positive_id(buyer_rep_id, "buyer_rep_id")
        buyer = conn.execute(
            "SELECT parent_rep_id FROM representatives WHERE id=?",
            (buyer_rep_id,),
        ).fetchone()
        if buyer is None:
            raise PricingError("buyer representative does not exist")
        expected = "root" if buyer[0] is None else f"rep:{int(buyer[0])}"
        if expected != seller_key:
            raise PricingError("seller is not the buyer representative's direct parent")
        amount, edges = _rep_cost(conn, buyer_rep_id, plan, set())
        buyer_key = f"rep:{buyer_rep_id}"
        if seller_key != "root":
            upstream_cost = _rep_cost(
                conn, int(seller_key.split(":", 1)[1]), plan, set()
            )[0]
    else:
        buyer_tg_id = _positive_id(buyer_tg_id, "buyer_tg_id")
        if seller_key == "root":
            base = plan["user_price"]
            edges = [{
                "edge_id": None, "seller_key": "root", "buyer_key": f"user:{buyer_tg_id}",
                "source_key": f"wallet:root:{buyer_tg_id}",
                "cost_before_tier_toman": base, "discount_bps": 0,
                "markup_bps": 0, "price_toman": base,
                "revision": plan["revision"], "custom": False,
                "plan_code": plan["plan_code"],
            }]
        else:
            seller_id = int(seller_key.split(":", 1)[1])
            seller_cost, edges = _rep_cost(conn, seller_id, plan, set())
            seller = conn.execute(
                "SELECT markup_bps,is_active,deleted_at FROM representatives WHERE id=?",
                (seller_id,),
            ).fetchone()
            if seller is None or not bool(seller[1]) or seller[2] is not None:
                raise PricingError("seller representative is unavailable")
            markup = _bps(seller[0], "seller markup")
            base = round_toman(
                Decimal(seller_cost) * Decimal(10000 + markup) / Decimal(10000)
            )
            upstream_cost = seller_cost
            edges = list(edges)
            edges.append({
                "edge_id": None, "seller_key": seller_key, "buyer_key": f"user:{buyer_tg_id}",
                "source_key": f"wallet:rep:{seller_id}:{buyer_tg_id}",
                "cost_before_tier_toman": base, "discount_bps": 0,
                "markup_bps": markup, "price_toman": base,
                "revision": plan["revision"], "custom": False,
                "plan_code": plan["plan_code"],
            })
        actor = conn.execute(
            "SELECT tier,is_blocked FROM bot_users WHERE telegram_id=?",
            (buyer_tg_id,),
        ).fetchone()
        if actor is None or bool(actor[1]):
            raise PricingError("buyer user identity is unavailable")
        discount = _tier_discount(conn, "end_user", str(actor[0] or "normal"))
        amount = round_toman(
            Decimal(base) * Decimal(10000 - discount) / Decimal(10000)
        )
        edges[-1] = {**edges[-1], "discount_bps": discount, "price_toman": amount}
        buyer_key = f"user:{buyer_tg_id}"

    coupon_result = None
    if coupon_code is not None:
        if not isinstance(coupon_code, str) or not coupon_code.strip():
            raise PricingError("coupon_code must be a non-empty string")
        coupon = conn.execute(
            "SELECT issuer_key,discount_toman,budget_toman,used_toman,max_uses,expires_at,is_active "
            "FROM coupons WHERE code=?",
            (coupon_code.strip(),),
        ).fetchone()
        if coupon is None or not bool(coupon[6]):
            raise PricingError("coupon is unavailable")
        if str(coupon[0]) != seller_key:
            raise PricingError("coupon must be funded by the selling merchant")
        uses = int(conn.execute(
            "SELECT COUNT(*) FROM coupon_uses WHERE code=? AND state IN ('held','used')",
            (coupon_code.strip(),),
        ).fetchone()[0])
        discount = round_toman(coupon[1])
        budget_left = int(coupon[2]) - int(coupon[3])
        issuer_margin = amount if seller_key == "root" else max(0, amount - upstream_cost)
        if timestamp >= int(coupon[5]) or uses >= int(coupon[4]):
            raise PricingError("coupon is expired or exhausted")
        if discount > budget_left or discount > issuer_margin:
            raise PricingError("coupon discount is not fully funded by issuer margin and budget")
        amount -= discount
        coupon_result = {
            "code": coupon_code.strip(), "issuer_key": seller_key,
            "discount_toman": discount, "budget_remaining_before_toman": budget_left,
        }

    return {
        "plan_code": plan["plan_code"], "merchant_key": seller_key,
        "buyer_key": buyer_key, "purpose": purpose, "amount_toman": amount,
        "price_edges": edges, "policy_version": POLICY_VERSION, "coupon": coupon_result,
    }
