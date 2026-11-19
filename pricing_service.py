"""Per-edge v5.2 pricing rules; all money values are integer toman."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
import sqlite3
import time
from typing import Any


POLICY_VERSION = "v1.0"


def _table_present(conn: sqlite3.Connection, table: str) -> bool:
    """Return whether a SQLite table exists in this connection's schema."""
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _ensure_catalogue(conn: sqlite3.Connection) -> None:
    """Provision the v5.2 catalogue tables on a connection that predates them.

    ``init_db`` creates them on a restart, but a restored backup and an audit clone
    never run ``init_db`` — and the readers below are the only thing standing
    between such a database and an ``OperationalError`` page. Both helpers are
    idempotent, so this is two ``sqlite_master`` lookups once the schema exists.
    A connection that cannot be written to (a read-only clone) is left alone; the
    callers then degrade to "no categories" instead of raising.
    """
    if _table_present(conn, "plan_categories") and _table_present(conn, "tier_plan_prices"):
        return
    try:
        import database
        database.ensure_plan_catalog_schema(conn)
        database.ensure_tier_plan_prices_table(conn)
    except sqlite3.Error:
        return


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


# --------------------------------------------------------------------------- #
# Plan catalogue: tariff kind, audience and categories
#
# ``price_chain`` prices one order. The catalogue below answers the different
# question the operator and the buyer both ask *before* an order exists: which
# tariff is this, who may buy it, which category does it belong to, and what does
# this exact actor pay for it. Keeping the tariff-kind registry here means the
# validator and the menu can never disagree about which kinds exist.
# --------------------------------------------------------------------------- #

#: A tariff is a fixed volume/duration package, an unlimited one, or a
#: stepped/arbitrary one whose bounds the operator sets per plan.
PLAN_KINDS: tuple[str, ...] = ("fixed", "unlimited", "custom")
PLAN_KIND_FA: dict[str, str] = {
    "fixed": "عادی / حجمی",
    "unlimited": "حجم یا زمان نامحدود",
    "custom": "پلکانی / دلخواه",
}
AUDIENCE_FA: dict[str, str] = {"end_user": "کاربران نهایی", "rep": "نمایندگان"}
AUDIENCES: tuple[str, ...] = ("end_user", "rep")
CATEGORY_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,29}$")
MAX_CATEGORY_TITLE = 40
MAX_CATEGORY_EMOJI = 4


class PlanCatalogError(PricingError):
    """A plan-category, tariff-kind or audience value is invalid."""


def _non_negative(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise PlanCatalogError(f"{label} باید یک عدد صحیح نامنفی باشد.")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise PlanCatalogError(f"{label} باید یک عدد صحیح نامنفی باشد.") from exc
    if number < 0:
        raise PlanCatalogError(f"{label} نمی‌تواند منفی باشد.")
    return number


def _clean_plan_kind(value: Any) -> str:
    kind = str(value or "").strip().lower()
    if kind not in PLAN_KINDS:
        raise PlanCatalogError(
            "نوع تعرفه باید یکی از «عادی/حجمی»، «نامحدود» یا «پلکانی/دلخواه» باشد."
        )
    return kind


def _clean_audience(value: Any) -> str:
    audience = str(value or "").strip().lower()
    if audience not in AUDIENCES:
        raise PlanCatalogError("مخاطب تعرفه باید «کاربران نهایی» یا «نمایندگان» باشد.")
    return audience


def _clean_category_key(value: Any) -> str:
    key = str(value or "").strip().lower()
    if not CATEGORY_KEY_RE.fullmatch(key):
        raise PlanCatalogError(
            "کد دسته‌بندی باید با حرف کوچک لاتین شروع شود و فقط شامل حروف کوچک، "
            "رقم و زیرخط باشد (۲ تا ۳۰ کاراکتر)."
        )
    return key


def _clean_category_title(value: Any) -> str:
    title = str(value or "").strip()
    if not title:
        raise PlanCatalogError("عنوان فارسی دسته‌بندی نمی‌تواند خالی باشد.")
    if len(title) > MAX_CATEGORY_TITLE:
        raise PlanCatalogError(f"عنوان دسته‌بندی حداکثر {MAX_CATEGORY_TITLE} کاراکتر است.")
    return title


def _clean_emoji(value: Any) -> str:
    emoji = str(value or "").strip()
    if len(emoji) > MAX_CATEGORY_EMOJI:
        raise PlanCatalogError("نماد دسته‌بندی حداکثر ۴ نویسه است.")
    return emoji


def _plan_row(conn: sqlite3.Connection, plan_code: Any) -> dict | None:
    """One raw ``pricing_plans`` row, or ``None``; never raises for a missing plan."""
    if not isinstance(plan_code, str) or not plan_code.strip():
        raise PricingError("plan_code is required")
    row = conn.execute(
        "SELECT * FROM pricing_plans WHERE plan_code=?", (plan_code.strip(),)
    ).fetchone()
    return None if row is None else dict(row)


def require_plan(conn: sqlite3.Connection, plan_code: Any) -> dict:
    """One raw plan row, or ``PricingError`` when it does not exist."""
    _ensure_catalogue(conn)
    plan = _plan_row(conn, plan_code)
    if plan is None:
        raise PricingError("pricing plan does not exist")
    return plan


def list_plan_categories(conn: sqlite3.Connection, *, active_only: bool = False) -> list[dict]:
    """Every plan category in display order."""
    _ensure_catalogue(conn)
    clause = " WHERE is_active=1" if active_only else ""
    rows = conn.execute(
        "SELECT category_key,title_fa,emoji,sort_order,is_active FROM plan_categories"
        + clause + " ORDER BY sort_order,category_key"
    ).fetchall()
    return [dict(row) for row in rows]


def get_plan_category(conn: sqlite3.Connection, category_key: Any) -> dict | None:
    _ensure_catalogue(conn)
    row = conn.execute(
        "SELECT category_key,title_fa,emoji,sort_order,is_active FROM plan_categories "
        "WHERE category_key=?",
        (_clean_category_key(category_key),),
    ).fetchone()
    return None if row is None else dict(row)


def create_plan_category(
    conn: sqlite3.Connection, *, category_key: Any, title_fa: Any,
    emoji: Any = "", sort_order: Any = 0,
) -> dict:
    """Add one plan category; the key is unique and lower-case."""
    key = _clean_category_key(category_key)
    if get_plan_category(conn, key) is not None:
        raise PlanCatalogError(f"دسته‌بندی «{key}» از قبل وجود دارد.")
    conn.execute(
        "INSERT INTO plan_categories(category_key,title_fa,emoji,sort_order,is_active) "
        "VALUES(?,?,?,?,1)",
        (key, _clean_category_title(title_fa), _clean_emoji(emoji),
         _non_negative(sort_order, "ترتیب نمایش")),
    )
    return get_plan_category(conn, key)


def update_plan_category(conn: sqlite3.Connection, category_key: Any, **fields) -> dict:
    """Edit one plan category in place; only the supplied fields change."""
    key = _clean_category_key(category_key)
    if get_plan_category(conn, key) is None:
        raise PlanCatalogError(f"دسته‌بندی «{key}» یافت نشد.")
    assignments: list[str] = []
    params: list[Any] = []
    if fields.get("title_fa") is not None:
        assignments.append("title_fa=?")
        params.append(_clean_category_title(fields["title_fa"]))
    if fields.get("emoji") is not None:
        assignments.append("emoji=?")
        params.append(_clean_emoji(fields["emoji"]))
    if fields.get("sort_order") is not None:
        assignments.append("sort_order=?")
        params.append(_non_negative(fields["sort_order"], "ترتیب نمایش"))
    if fields.get("is_active") is not None:
        assignments.append("is_active=?")
        params.append(1 if fields["is_active"] else 0)
    unknown = sorted(set(fields) - {"title_fa", "emoji", "sort_order", "is_active"})
    if unknown:
        raise PlanCatalogError("فیلد ناشناخته: " + ", ".join(unknown))
    if not assignments:
        return get_plan_category(conn, key)
    params.append(key)
    conn.execute(
        "UPDATE plan_categories SET " + ",".join(assignments) + " WHERE category_key=?",
        params,
    )
    return get_plan_category(conn, key)


def delete_plan_category(conn: sqlite3.Connection, category_key: Any) -> dict:
    """Delete one category and every plan link that pointed at it.

    A deleted category must not leave a dangling ``pricing_plans.category_key``,
    so the primary pointer of every affected plan is re-synced from the links that
    remain.
    """
    key = _clean_category_key(category_key)
    if get_plan_category(conn, key) is None:
        raise PlanCatalogError(f"دسته‌بندی «{key}» یافت نشد.")
    affected = [str(row["plan_code"]) for row in conn.execute(
        "SELECT plan_code FROM plan_category_links WHERE category_key=?", (key,)
    ).fetchall()]
    conn.execute("DELETE FROM plan_category_links WHERE category_key=?", (key,))
    conn.execute("DELETE FROM plan_categories WHERE category_key=?", (key,))
    for plan_code in affected:
        _sync_primary_category(conn, plan_code)
    return {"category_key": key, "deleted": True, "unlinked_plans": affected}


def plan_category_links(conn: sqlite3.Connection, plan_code: Any) -> list[dict]:
    """Every category linked to one plan, primary first."""
    plan = require_plan(conn, plan_code)
    rows = conn.execute(
        "SELECT l.category_key,l.is_primary,c.title_fa,c.emoji,c.sort_order,c.is_active "
        "FROM plan_category_links l JOIN plan_categories c ON c.category_key=l.category_key "
        "WHERE l.plan_code=? ORDER BY l.is_primary DESC, c.sort_order, c.category_key",
        (str(plan["plan_code"]),),
    ).fetchall()
    return [dict(row) for row in rows]


def _sync_primary_category(conn: sqlite3.Connection, plan_code: str) -> str:
    """Point ``pricing_plans.category_key`` at the plan's primary linked category."""
    primary = conn.execute(
        "SELECT l.category_key FROM plan_category_links l "
        "JOIN plan_categories c ON c.category_key=l.category_key "
        "WHERE l.plan_code=? ORDER BY l.is_primary DESC, c.sort_order, l.category_key LIMIT 1",
        (str(plan_code),),
    ).fetchone()
    key = "" if primary is None else str(primary["category_key"])
    conn.execute(
        "UPDATE pricing_plans SET category_key=? WHERE plan_code=?", (key, str(plan_code)))
    return key


def set_plan_categories(
    conn: sqlite3.Connection, plan_code: Any, category_keys, *, primary: Any = None,
) -> dict:
    """Replace a plan's category links atomically and re-sync its primary pointer."""
    plan = require_plan(conn, plan_code)
    code = str(plan["plan_code"])
    wanted: list[str] = []
    for raw in category_keys or ():
        key = _clean_category_key(raw)
        if get_plan_category(conn, key) is None:
            raise PlanCatalogError(f"دسته‌بندی «{key}» یافت نشد.")
        if key not in wanted:
            wanted.append(key)
    chosen = _clean_category_key(primary) if primary else (wanted[0] if wanted else "")
    if chosen and chosen not in wanted:
        raise PlanCatalogError("دسته‌بندی اصلی باید یکی از دسته‌بندی‌های انتخاب‌شده باشد.")
    conn.execute("DELETE FROM plan_category_links WHERE plan_code=?", (code,))
    for key in wanted:
        conn.execute(
            "INSERT INTO plan_category_links(plan_code,category_key,is_primary) VALUES(?,?,?)",
            (code, key, 1 if key == chosen else 0),
        )
    return {"plan_code": code, "categories": wanted,
            "primary_category": _sync_primary_category(conn, code)}


def toggle_plan_category(conn: sqlite3.Connection, plan_code: Any, category_key: Any) -> dict:
    """Add or remove one plan/category link — the plan card's checkbox action."""
    plan = require_plan(conn, plan_code)
    code = str(plan["plan_code"])
    key = _clean_category_key(category_key)
    if get_plan_category(conn, key) is None:
        raise PlanCatalogError(f"دسته‌بندی «{key}» یافت نشد.")
    existing = conn.execute(
        "SELECT is_primary FROM plan_category_links WHERE plan_code=? AND category_key=?",
        (code, key),
    ).fetchone()
    if existing is None:
        has_primary = conn.execute(
            "SELECT 1 FROM plan_category_links WHERE plan_code=? AND is_primary=1", (code,)
        ).fetchone()
        conn.execute(
            "INSERT INTO plan_category_links(plan_code,category_key,is_primary) VALUES(?,?,?)",
            (code, key, 0 if has_primary is not None else 1),
        )
        linked = True
    else:
        conn.execute(
            "DELETE FROM plan_category_links WHERE plan_code=? AND category_key=?", (code, key))
        linked = False
    return {"plan_code": code, "category_key": key, "linked": linked,
            "primary_category": _sync_primary_category(conn, code)}


def set_plan_primary_category(
    conn: sqlite3.Connection, plan_code: Any, category_key: Any,
) -> dict:
    """Promote one linked category to the plan's primary category."""
    plan = require_plan(conn, plan_code)
    code = str(plan["plan_code"])
    key = _clean_category_key(category_key)
    if conn.execute(
        "SELECT 1 FROM plan_category_links WHERE plan_code=? AND category_key=?", (code, key)
    ).fetchone() is None:
        raise PlanCatalogError("این دسته‌بندی به تعرفه متصل نیست.")
    conn.execute("UPDATE plan_category_links SET is_primary=0 WHERE plan_code=?", (code,))
    conn.execute(
        "UPDATE plan_category_links SET is_primary=1 WHERE plan_code=? AND category_key=?",
        (code, key),
    )
    return {"plan_code": code, "primary_category": _sync_primary_category(conn, code)}


def plan_audience(conn: sqlite3.Connection, plan_code: Any) -> dict:
    """Which audiences may buy this plan, read from the plan's own columns."""
    plan = require_plan(conn, plan_code)
    return {
        "plan_code": str(plan["plan_code"]),
        "for_end_users": bool(int(plan.get("for_end_users", 1) or 0)),
        "for_reps": bool(int(plan.get("for_reps", 1) or 0)),
    }


def set_plan_audience(
    conn: sqlite3.Connection, plan_code: Any, *,
    for_end_users: Any = None, for_reps: Any = None,
) -> dict:
    """Enable or disable one plan for end users and/or representatives.

    A plan that is enabled for nobody can never be sold, which is a configuration
    mistake rather than a valid state, so the write is refused instead of leaving
    an invisible tariff behind.
    """
    plan = require_plan(conn, plan_code)
    code = str(plan["plan_code"])
    users = bool(int(plan.get("for_end_users", 1) or 0)) if for_end_users is None else bool(for_end_users)
    reps = bool(int(plan.get("for_reps", 1) or 0)) if for_reps is None else bool(for_reps)
    if not users and not reps:
        raise PlanCatalogError(
            "تعرفه باید حداقل برای یکی از دو مخاطب (کاربران نهایی یا نمایندگان) فعال بماند."
        )
    conn.execute(
        "UPDATE pricing_plans SET for_end_users=?,for_reps=? WHERE plan_code=?",
        (1 if users else 0, 1 if reps else 0, code),
    )
    return plan_audience(conn, code)


def set_plan_kind(conn: sqlite3.Connection, plan_code: Any, kind: Any) -> dict:
    """Set the tariff kind of one plan."""
    plan = require_plan(conn, plan_code)
    code = str(plan["plan_code"])
    selected = _clean_plan_kind(kind)
    conn.execute("UPDATE pricing_plans SET plan_kind=? WHERE plan_code=?", (selected, code))
    return {"plan_code": code, "plan_kind": selected}


def set_plan_panel_allowed(
    conn: sqlite3.Connection, plan_code: Any, panel_id: Any, allowed: Any,
) -> dict:
    """Allow or deny one panel for one plan — the plan card's panel checkbox."""
    plan = require_plan(conn, plan_code)
    code = str(plan["plan_code"])
    selected_panel = _positive_id(panel_id, "panel_id")
    if conn.execute("SELECT 1 FROM panels WHERE id=?", (selected_panel,)).fetchone() is None:
        raise PlanCatalogError("پنل موردنظر وجود ندارد.")
    flag = 1 if allowed else 0
    conn.execute(
        "INSERT INTO plan_panels(plan_code,panel_id,allowed) VALUES(?,?,?) "
        "ON CONFLICT(plan_code,panel_id) DO UPDATE SET allowed=excluded.allowed",
        (code, selected_panel, flag),
    )
    return {"plan_code": code, "panel_id": selected_panel, "allowed": bool(flag)}


def plan_panel_bindings(conn: sqlite3.Connection, plan_code: Any) -> list[dict]:
    """Every panel with this plan's explicit allow/deny state, in display order."""
    plan = require_plan(conn, plan_code)
    rows = conn.execute(
        "SELECT p.id AS panel_id,p.name,p.lifecycle,p.is_default,"
        "COALESCE(pp.allowed,0) AS allowed,"
        "CASE WHEN pp.plan_code IS NULL THEN 0 ELSE 1 END AS explicit "
        "FROM panels p LEFT JOIN plan_panels pp "
        "ON pp.panel_id=p.id AND pp.plan_code=? "
        "ORDER BY p.is_default DESC,p.id",
        (str(plan["plan_code"]),),
    ).fetchall()
    return [dict(row) for row in rows]


def plan_catalog(
    conn: sqlite3.Connection, *, audience: Any = None, active_only: bool = True,
) -> list[dict]:
    """Sellable plans for one audience, each with its primary category attached."""
    _ensure_catalogue(conn)
    clauses: list[str] = []
    params: list[Any] = []
    if active_only:
        clauses.append("p.is_active=1")
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(pricing_plans)")}
    if audience is not None:
        selected = _clean_audience(audience)
        # A legacy schema predates the audience columns, where every plan is on sale
        # to both audiences; only filter when the column the filter reads exists.
        column = "for_reps" if selected == "rep" else "for_end_users"
        if column in columns:
            clauses.append(f"p.{column}=1")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    if not _table_present(conn, "plan_categories"):
        # Unit/legacy fixtures may predate the optional category catalogue; keep
        # those callers functional until ``init_db`` provisions the real tables.
        rows = conn.execute("SELECT p.* FROM pricing_plans p" + where +
                            " ORDER BY p.sort_order,p.plan_code", params).fetchall()
        return [dict(row, category_title="دسته‌بندی نشده", category_emoji="📦",
                     category_key=str(dict(row).get("category_key") or ""))
                for row in rows]
    rows = conn.execute(
        "SELECT p.*,c.title_fa AS category_title,c.emoji AS category_emoji "
        "FROM pricing_plans p LEFT JOIN plan_categories c ON c.category_key=p.category_key "
        + where + " ORDER BY p.sort_order,p.plan_code",
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def plans_grouped_by_category(
    conn: sqlite3.Connection, *, audience: Any = None, active_only: bool = True,
) -> list[dict]:
    """Sellable plans grouped by their primary category, for the buyer menus.

    An uncategorised plan is never dropped: it lands in a trailing group whose
    ``category`` is ``None``, which the caller renders as «همه بسته‌ها».
    """
    return group_plans_by_category(
        conn, plan_catalog(conn, audience=audience, active_only=active_only))


def group_plans_by_category(conn: sqlite3.Connection, plans: list[dict]) -> list[dict]:
    """Group an already-loaded list of plan rows by their primary category.

    The buyer menus load their plans through their own module accessor (which the
    surrounding tests and callers already patch), so grouping has to accept that
    list rather than re-reading the table. A plan whose category is unknown — or a
    database without the optional catalogue at all — lands in one flat group whose
    ``category`` is ``None``.
    """
    order: list[str] = []
    buckets: dict[str, list[dict]] = {}
    for plan in plans:
        key = str(plan.get("category_key") or "")
        if key not in buckets:
            buckets[key] = []
            order.append(key)
        buckets[key].append(plan)
    groups: list[dict] = []
    for key in order:
        if not key:
            groups.append({"category": None, "plans": buckets[key]})
            continue
        category = get_plan_category(conn, key)
        groups.append({"category": category, "plans": buckets[key]})
    return groups


# --------------------------------------------------------------------------- #
# Effective price for one actor
# --------------------------------------------------------------------------- #

def _actor_tier_and_scope(
    conn: sqlite3.Connection, *, user_id: int | None, rep_id: int | None,
) -> tuple[str, str]:
    """``(role_scope, tier_code)`` for the paying actor, defaulting to ``normal``."""
    if rep_id is not None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(representatives)")}
        if "tier" not in columns:
            return "rep", "normal"
        row = conn.execute(
            "SELECT tier FROM representatives WHERE id=?", (int(rep_id),)).fetchone()
        return "rep", str(row["tier"] if row is not None else "normal").strip().lower()
    if user_id is not None:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(bot_users)")}
        if "tier" not in columns:
            return "end_user", "normal"
        row = conn.execute(
            "SELECT tier FROM bot_users WHERE telegram_id=?", (int(user_id),)).fetchone()
        return "end_user", str(row["tier"] if row is not None else "normal").strip().lower()
    return "end_user", "normal"


def _tier_policy(conn: sqlite3.Connection, role_scope: str, tier_code: str) -> dict | None:
    if not _table_present(conn, "tier_policies"):
        return None
    row = conn.execute(
        "SELECT title_fa,rank,discount_bps FROM tier_policies "
        "WHERE actor_type=? AND tier_code=?",
        (role_scope, tier_code),
    ).fetchone()
    return None if row is None else dict(row)


def _tier_custom_price(
    conn: sqlite3.Connection, role_scope: str, tier_code: str, plan_code: str,
) -> int | None:
    row = conn.execute(
        "SELECT custom_price_toman FROM tier_plan_prices "
        "WHERE tier_key=? AND role_scope=? AND plan_code=?",
        (tier_code, role_scope, plan_code),
    ).fetchone()
    return None if row is None else round_toman(row["custom_price_toman"])


def resolve_tier_plan_price(
    conn: sqlite3.Connection, *, role_scope: str, tier_code: str, plan_code: Any,
) -> dict:
    """The price one *explicit* tier pays for one plan, and the reason for it.

    This is the primitive the tier administration matrix renders: it answers for a
    named tier without needing an actor id, so the operator sees the exact number
    the tier will be charged. :func:`resolve_effective_plan_price` resolves the
    actor's own tier and then delegates here, which keeps one implementation of
    the priority order.
    """
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    scope = str(role_scope or "").strip().lower()
    if scope not in AUDIENCES:
        raise PlanCatalogError("نقش هدف باید «rep» (نمایندگی) یا «end_user» (کاربری) باشد.")
    tier = str(tier_code or "").strip().lower()
    if not tier:
        raise PlanCatalogError("کد سطح نمی‌تواند خالی باشد.")
    plan = require_plan(conn, plan_code)
    base = round_toman(plan["rep_price"] if scope == "rep" else plan["user_price"])
    policy = _tier_policy(conn, scope, tier)
    discount_bps = _bps(policy["discount_bps"], "tier discount") if policy else 0
    custom = _tier_custom_price(conn, scope, tier, str(plan["plan_code"])) if _table_present(
        conn, "tier_plan_prices") else None
    if custom is not None:
        effective = custom
    else:
        effective = round_toman(
            Decimal(base) * Decimal(10000 - discount_bps) / Decimal(10000))
    return {
        "plan_code": str(plan["plan_code"]),
        "plan_name": str(plan["name"]),
        "gb": float(plan["gb"]),
        "days": int(plan["days"]),
        "plan_kind": str(plan.get("plan_kind") or "fixed"),
        "role_scope": scope,
        "tier_key": tier,
        "tier_title_fa": str(policy["title_fa"]) if policy else "",
        "base_price": base,
        "effective_price": effective,
        "discount_bps": discount_bps,
        "discount_percent": discount_bps / 100.0,
        "custom_price_toman": custom,
        "is_custom_price": custom is not None,
        "saving_toman": max(base - effective, 0),
    }


def _resolve_effective_plan_price(
    conn: sqlite3.Connection, plan_code: Any, *,
    user_id: int | None, rep_id: int | None,
) -> dict:
    role_scope, tier_code = _actor_tier_and_scope(conn, user_id=user_id, rep_id=rep_id)
    return resolve_tier_plan_price(
        conn, role_scope=role_scope, tier_code=tier_code, plan_code=plan_code)


def resolve_effective_plan_price(
    plan_code: str, *, user_id: int | None = None, rep_id: int | None = None,
    conn: sqlite3.Connection | None = None,
) -> dict:
    """The exact price one actor pays for one plan, and the reason for it.

    Resolution order, highest priority first:

    1. the actor tier's own fixed price in ``tier_plan_prices``;
    2. the actor tier's ``discount_bps`` applied to the base price;
    3. the base price (``rep_price`` for a representative, ``user_price`` for an
       end user).

    ``price_chain`` stays the authoritative engine for an order that already has a
    quote; this helper answers the same question for a menu, a confirmation card
    and a wallet debit, so a VIP user or a gold representative sees and pays the
    tier price instead of the untiered list price.

    ``conn`` is optional: without it the helper opens its own connection, which is
    what the UI call sites need, while a caller already inside a transaction (the
    intent guard) passes its own connection.
    """
    if user_id is not None and rep_id is not None:
        raise PricingError("exactly one actor identity is required")
    if user_id is not None:
        _positive_id(user_id, "user_id")
    if rep_id is not None:
        _positive_id(rep_id, "rep_id")
    if conn is None:
        import database
        with database.get_db() as opened:
            return _resolve_effective_plan_price(
                opened, plan_code, user_id=user_id, rep_id=rep_id)
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    return _resolve_effective_plan_price(
        conn, plan_code, user_id=user_id, rep_id=rep_id)


def audience_allows(plan: dict, audience: str) -> bool:
    """True when ``plan`` may be sold to ``audience`` (``end_user`` or ``rep``)."""
    selected = _clean_audience(audience)
    column = "for_reps" if selected == "rep" else "for_end_users"
    return bool(int(plan.get(column, 1) or 0))
