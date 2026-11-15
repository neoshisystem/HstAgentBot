"""v5.2 digital-goods catalogue and idempotent manual-delivery pages.

This module renders UI and delegates accepted orders to the established quote,
order and goods-fulfillment services. It never creates a second order or stock
allocation while rendering a resend action.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
import time
import uuid
from typing import Any, Callable

import config
import database
from ui_v52 import button, render_error, render_page


def _value(obj: Any, key: str, default=None):
    return obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)


def _positive(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if result <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return result


def _money(value: Any, label: str = "amount_toman") -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a non-negative integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a non-negative integer") from exc
    if result < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return result

def _actor(ctx: Any) -> int:
    return _positive(_value(ctx, "actor_tg_id", _value(ctx, "telegram_id")), "actor_tg_id")


def _bot_id(ctx: Any) -> int | None:
    bot = _value(ctx, "bot")
    raw = _value(ctx, "bot_id", _value(bot, "id"))
    try:
        return _positive(raw, "bot_id")
    except ValueError:
        return None


def _root(ctx: Any) -> bool:
    try:
        root_id = _positive(getattr(config, "ADMIN_TELEGRAM_ID", 0) or 0, "ADMIN_TELEGRAM_ID")
        return _actor(ctx) == root_id
    except (TypeError, ValueError):
        return False


def _persona(ctx: Any) -> str:
    raw = str(_value(ctx, "persona", "user") or "user").lower()
    if _root(ctx) or raw in {"root", "admin"}:
        return "admin"
    return "rep" if raw in {"rep", "representative"} else "user"


def _merchant(ctx: Any) -> str:
    explicit = _value(ctx, "merchant_key", _value(ctx, "seller_key"))
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    rep_id = _value(ctx, "rep_id")
    try:
        return f"rep:{_positive(rep_id, 'rep_id')}"
    except ValueError:
        return "root" if _root(ctx) else "unknown"


def _in_scope(ctx: Any, product: Mapping[str, Any]) -> bool:
    """True when the product belongs to the actor's sales route.

    ``digital_products`` has no owner column, so a row loaded from the database
    carries neither ``seller_key`` nor ``merchant_key``. Such a row is a
    platform-wide good and must be visible on every route; only a product that
    explicitly names a seller (or the platform ``root``) is restricted to that
    route. The previous version defaulted a missing seller to ``"root"`` and
    compared it to ``_merchant(ctx)``, which resolves to ``"unknown"`` for a
    plain user and ``"rep:<id>"`` for a representative — so every database row
    was filtered out and the shop always rendered its empty message.
    """
    if _root(ctx):
        return True
    explicit = product.get("seller_key", product.get("merchant_key"))
    seller = str(explicit).strip() if explicit is not None else ""
    if not seller or seller == "root":
        return True
    return seller == _merchant(ctx)


def _stock(product: Mapping[str, Any], stock_count: int | None = None) -> int | None:
    raw = stock_count
    if raw is None:
        raw = product.get("available_count", product.get("available_stock", product.get("stock")))
    if raw is None:
        return None
    if isinstance(raw, bool):
        raise ValueError("stock count must be a non-negative integer")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("stock count must be a non-negative integer") from exc
    if value < 0:
        raise ValueError("stock count must be a non-negative integer")
    return value


def _price(product: Mapping[str, Any], persona: str) -> int:
    field = "user_price" if persona == "user" else "rep_price"
    return _money(product.get(field, product.get("price_toman", 0)), field)


def show_catalog(
    ctx: Any,
    products: Sequence[Mapping[str, Any]],
    *,
    stock_counts: Mapping[int, int] | None = None,
    parent_callback: str | None = None,
):
    """Render active products with persona-specific price and truthful stock."""
    try:
        actor = _actor(ctx)
        persona = _persona(ctx)
        visible = []
        rows = []
        lines = [f"شناسه مشتری: {actor}", "قیمت بر اساس مسیر فروش معتبر انتخاب شده است."]
        for product in products:
            if not isinstance(product, Mapping) or not bool(product.get("is_active", True)):
                continue
            if not _in_scope(ctx, product):
                continue
            product_id = _positive(product.get("id", product.get("product_id")), "product_id")
            delivery = str(product.get("delivery_type") or "")
            if delivery not in {"auto_inventory", "file_document", "manual_fulfill"}:
                continue
            count = _stock(product, (stock_counts or {}).get(product_id))
            title = str(product.get("title") or product.get("product_code") or f"کالا {product_id}")
            price = _price(product, persona)
            if price <= 0:
                # The add-product wizard encodes "allowed audience" in the price
                # fields: a zero price on this persona's side means the product is
                # not offered to that audience, so it is not listed for them.
                continue
            unavailable = delivery == "auto_inventory" and count == 0
            if delivery == "file_document" and product.get("file_bot_id") is not None:
                unavailable = _bot_id(ctx) != _positive(product["file_bot_id"], "file_bot_id")
            stock_label = "نامشخص" if count is None else str(count)
            lines.append(f"{title} | {price:,} تومان | موجودی: {stock_label} | {delivery}")
            if unavailable:
                lines.append(f"{product_id}: فعلاً قابل خرید نیست")
            else:
                rows.append([button(title[:32], f"v52:goods:{product_id}")])
            visible.append(product_id)
        if not visible:
            lines.append("در این مسیر فروش کالای فعالی در دسترس نیست.")
        return render_page("فروشگاه", "\n".join(lines), persona=persona,
                           page_kind="catalog", parent_callback=parent_callback, rows=rows)
    except (TypeError, ValueError) as exc:
        return render_error(str(exc), persona=_persona(ctx), parent_callback=parent_callback)


def preview_product(
    ctx: Any,
    product: Mapping[str, Any],
    *,
    quote: Mapping[str, Any] | None = None,
    stock_count: int | None = None,
    parent_callback: str | None = "v52:shop",
):
    """Preview one product against the frozen digital-product quote and same checkout engine."""
    try:
        if not isinstance(product, Mapping):
            raise ValueError("product snapshot is required")
        product_id = _positive(product.get("id", product.get("product_id")), "product_id")
        if not bool(product.get("is_active", True)):
            raise ValueError("محصول غیرفعال است.")
        if not _in_scope(ctx, product):
            raise PermissionError("این محصول به مسیر فروش این بات/نماینده تعلق ندارد.")
        delivery = str(product.get("delivery_type") or "")
        if delivery not in {"auto_inventory", "file_document", "manual_fulfill"}:
            raise ValueError("delivery_type پشتیبانی نمی‌شود.")
        count = _stock(product, stock_count)
        if delivery == "auto_inventory" and count == 0:
            raise ValueError("موجودی این کالا صفر است و خرید جدید پذیرفته نمی‌شود.")
        if delivery == "file_document":
            expected_bot = _positive(product.get("file_bot_id"), "file_bot_id")
            if _bot_id(ctx) != expected_bot:
                raise PermissionError("فایل فقط توسط همان بات ثبت‌کننده قابل تحویل است.")
        if quote is None or not isinstance(quote, Mapping):
            raise ValueError("quote نهایی محصول لازم است؛ قیمت از UI حدس زده نمی‌شود.")
        if quote.get("purpose") != "digital_product_buy":
            raise ValueError("quote برای خرید کالای دیجیتال صادر نشده است.")
        snapshot = quote.get("snapshot") or {}
        quote_product_id = quote.get("product_id")
        if quote_product_id is None and isinstance(snapshot, Mapping):
            quote_product_id = snapshot.get("product_id")
        if quote_product_id is not None and _positive(quote_product_id, "quote.product_id") != product_id:
            raise ValueError("quote به محصول دیگری تعلق دارد.")
        amount = _money(quote.get("amount_toman"))
        title = str(product.get("title") or product.get("product_code") or product_id)
        stock_text = "نامشخص" if count is None else str(count)
        callback = str(quote.get("checkout_callback") or "v52:q:" + str(quote.get("quote_nonce") or ""))
        if not re.fullmatch(r"v52:q:[A-Za-z0-9_-]{16,40}", callback):
            raise ValueError("checkout callback باید nonce معتبر و حداکثر ۶۴ بایت باشد.")
        return render_page(
            "پیش‌نمایش کالا",
            f"محصول: {title}\nنوع تحویل: {delivery}\nموجودی: {stock_text}\n"
            f"مبلغ قطعی quote: {amount:,} تومان\n"
            "تأیید این صفحه سفارش تازه یا تخصیص موجودی را تکراری ایجاد نمی‌کند.",
            persona=_persona(ctx), page_kind="preview", parent_callback=parent_callback,
            rows=[[button("ادامه به checkout همان quote", callback)]],
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona=_persona(ctx), parent_callback=parent_callback)


def prepare_manual_delivery(
    ctx: Any,
    order: Mapping[str, Any],
    *,
    secret_payload: str | None = None,
    claim: Mapping[str, Any] | None = None,
    fulfillment: Mapping[str, Any] | None = None,
    confirm: bool = False,
    claim_service: Callable[..., Any] | None = None,
    complete_service: Callable[..., Any] | None = None,
    resend_service: Callable[..., Any] | None = None,
    now: int | None = None,
    parent_callback: str | None = "v52:shop",
):
    """Claim once and store a manual payload; never display payload or claim token."""
    try:
        if not isinstance(order, Mapping):
            raise ValueError("order snapshot is required")
        order_id = str(order.get("order_id") or "").strip()
        if not order_id or len(order_id.encode("utf-8")) > 64:
            raise ValueError("order_id is invalid")
        if str(order.get("purpose") or "") != "digital_product_buy":
            raise ValueError("order is not a digital-goods purchase")
        if str(order.get("delivery_type") or "") != "manual_fulfill":
            raise ValueError("order is not a manual-fulfillment item")
        if str(order.get("status") or "") not in {"accepted", "completed"}:
            raise ValueError("manual delivery requires an accepted/paid order")
        actor = _actor(ctx)
        seller = str(order.get("merchant_key") or "root")
        is_payer = int(order.get("actor_tg_id", -1)) == actor
        is_assigned_operator = _root(ctx) or _merchant(ctx) == seller
        if not (is_payer or is_assigned_operator):
            raise PermissionError("only the payer or an authorized fulfillment operator may view this order")
        if not _root(ctx) and not is_payer and _merchant(ctx) != seller:
            raise PermissionError("manual fulfillment is restricted to the assigned seller/root")
        current = fulfillment or {}
        state = str(current.get("state") or "awaiting_manual")
        if state in {"ready", "delivered"}:
            if confirm:
                if resend_service is None:
                    raise ValueError("resend must use the existing order fulfillment service")
                resend_result = resend_service(order_id)
                resend_state = str(_value(resend_result, "state", state))
            else:
                resend_state = state
            return render_page(
                "تحویل آماده / ارسال مجدد",
                f"سفارش: {order_id}\nوضعیت تحویل: {resend_state}\n"
                "ارسال مجدد فقط به همان order و همان payload مجاز است؛ تخصیص تازه انجام نمی‌شود.",
                persona=_persona(ctx), page_kind="delivery",
                parent_callback=parent_callback,
                rows=[[button("ارسال مجدد همان سفارش", f"v52:fulfill:{order_id}")]],
            )
        if not confirm:
            return render_page(
                "پیش‌نمایش تحویل دستی",
                f"سفارش: {order_id}\nمحصول: {order.get('product_title', 'کالای دیجیتال')}\n"
                "ورود راز فقط در DM انجام می‌شود. پیش‌نمایش، claim یا ذخیره را اجرا نمی‌کند.",
                persona=_persona(ctx), page_kind="preview", parent_callback=parent_callback,
                rows=[[button("تأیید و ثبت تحویل دستی", f"v52:fulfill:{order_id}")]],
            )
        if not isinstance(secret_payload, str) or not secret_payload.strip():
            raise ValueError("payload must be provided in private DM after confirmation")
        if str(_value(ctx, "chat_type", "private")).lower() != "private":
            raise PermissionError("manual fulfillment payload must only be submitted in private DM")
        if len(secret_payload.encode("utf-8")) > 16_384:
            raise ValueError("manual fulfillment payload exceeds the size limit")
        selected_claim = claim
        if selected_claim is None:
            if claim_service is None:
                from goods_fulfillment import claim_manual_order
                claim_service = claim_manual_order
            selected_claim = claim_service(order_id, actor, now=now)
        if not isinstance(selected_claim, Mapping):
            raise ValueError("a unique active manual claim could not be acquired")
        token = selected_claim.get("claim_token")
        operator_id = _positive(selected_claim.get("operator_id", actor), "operator_id")
        if operator_id != actor or not isinstance(token, str) or not token:
            raise PermissionError("manual claim does not belong to this operator")
        if complete_service is None:
            from goods_fulfillment import complete_manual_order
            complete_service = complete_manual_order
        result = complete_service(order_id, actor, secret_payload, claim_token=token, now=now)
        result_state = str(_value(result, "state", "ready"))
        return render_page(
            "ثبت تحویل دستی",
            f"سفارش: {order_id}\nوضعیت: {result_state}\n"
            "محتوای تحویل رمزنگاری شده و در متن صفحه یا گزارش نمایش داده نمی‌شود.",
            persona=_persona(ctx), page_kind="result", parent_callback=parent_callback,
            rows=[[button("بازگشت به سفارش", f"v52:fulfill:{order_id}")]],
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona=_persona(ctx), parent_callback=parent_callback)


_DELIVERY_LABELS = {
    "auto_inventory": "تحویل خودکار از موجودی انبار / کد دیجیتال",
    "manual_fulfill": "تحویل دستی توسط ادمین",
    "file_document": "فایل / سند دیجیتال",
}
_AUDIENCE_LABELS = {"all": "همه", "rep": "فقط نمایندگان", "user": "فقط کاربران"}
_WIZARD_STEPS = ("title", "category", "delivery", "price", "description", "audience")


def _now() -> int:
    return int(time.time())


def _require_root(ctx: Any) -> None:
    if not _root(ctx):
        raise PermissionError("مدیریت فروشگاه فقط در اختیار مدیریت کل است.")


def _category_id(conn, title: str) -> int:
    """Return the id of a category by title, creating it when it does not exist."""
    selected = str(title).strip()
    if not selected or len(selected) > 120:
        raise ValueError("نام دسته‌بندی باید بین ۱ تا ۱۲۰ نویسه باشد.")
    row = conn.execute(
        "SELECT id FROM digital_categories WHERE title=? ORDER BY id LIMIT 1", (selected,)
    ).fetchone()
    if row is not None:
        return int(row["id"])
    cursor = conn.execute(
        "INSERT INTO digital_categories(title,parent_id,description,sort_order,is_active) "
        "VALUES(?,NULL,'',0,1)",
        (selected,),
    )
    return int(cursor.lastrowid)


def _available_stock(conn, product_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM digital_inventory WHERE product_id=? AND status='available'",
        (int(product_id),),
    ).fetchone()
    return int(row[0])


def _audience_prices(audience: str, base: int) -> tuple[int, int]:
    """Map the wizard's allowed-audience choice onto the two price columns.

    ``digital_products`` has no audience column, so the audience is encoded in the
    price pair: a zero price on one side means the product is not offered to that
    persona (``show_catalog`` skips a zero price for the viewing persona).
    """
    if audience == "rep":
        return base, 0
    if audience == "user":
        return 0, base
    return base, base


def admin_shop_page(ctx: Any, *, parent_callback: str | None = "v52:home:admin"):
    """Admin catalogue manager: every product plus the add-product entry.

    Returns ``(text, rows)``; the dispatcher wraps it in ``render_page``.
    """
    try:
        _require_root(ctx)
        with database.get_db() as conn:
            product_rows = conn.execute(
                "SELECT p.*, c.title AS category_title FROM digital_products p "
                "LEFT JOIN digital_categories c ON c.id=p.category_id "
                "ORDER BY p.sort_order, p.id"
            ).fetchall()
            stock_map = {
                int(row["product_id"]): int(row["count"])
                for row in conn.execute(
                    "SELECT product_id, COUNT(*) AS count FROM digital_inventory "
                    "WHERE status='available' GROUP BY product_id"
                ).fetchall()
            }
        lines = ["مدیریت کالاهای دیجیتال فروشگاه.", ""]
        buttons = [[button("➕ افزودن محصول / کالای جدید", "v52:shop:new")]]
        if not product_rows:
            lines.append("هنوز هیچ محصولی ثبت نشده است؛ با دکمهٔ بالا اولین کالا را بسازید.")
        else:
            lines.append(f"تعداد محصولات: {len(product_rows)}")
            for row in product_rows:
                product_id = int(row["id"])
                title = str(row["title"])
                category = str(row["category_title"] or "بدون دسته")
                delivery = str(row["delivery_type"])
                price = int(row["user_price"])
                stock = stock_map.get(product_id, 0)
                enabled = "فعال" if row["is_active"] else "غیرفعال"
                lines.append(
                    f"#{product_id} · {title} | دسته: {category} | "
                    f"{_DELIVERY_LABELS.get(delivery, delivery)} | قیمت: {price:,} تومان | "
                    f"موجودی: {stock} | {enabled}"
                )
                buttons.append([button(
                    f"#{product_id} {title[:24]} · {price:,}ت · موجودی {stock} · {enabled}",
                    f"v52:shop:product:{product_id}",
                )])
        buttons.append([button("🛒 مشاهده فروشگاه مشتریان", "v52:shop:catalog")])
        return "\n".join(lines), buttons
    except (TypeError, ValueError, PermissionError) as exc:
        return str(exc), [[button("بازگشت", parent_callback or "v52:home:admin")]]


def admin_product_detail(ctx: Any, product_id: int, *, parent_callback: str | None = "v52:shop"):
    """One product's management card. Returns ``(text, rows)``."""
    try:
        _require_root(ctx)
        pid = _positive(product_id, "product_id")
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT p.*, c.title AS category_title FROM digital_products p "
                "LEFT JOIN digital_categories c ON c.id=p.category_id WHERE p.id=?",
                (pid,),
            ).fetchone()
            if row is None:
                raise ValueError("محصول موردنظر یافت نشد.")
            stock = _available_stock(conn, pid)
        delivery = str(row["delivery_type"])
        lines = [
            f"شناسه: {pid}",
            f"نام: {row['title']}",
            f"کد: {row['product_code']}",
            f"دسته‌بندی: {row['category_title'] or 'بدون دسته'}",
            f"نوع تحویل: {_DELIVERY_LABELS.get(delivery, delivery)}",
            f"قیمت نماینده: {int(row['rep_price']):,} تومان",
            f"قیمت کاربر: {int(row['user_price']):,} تومان",
            f"موجودی قابل فروش: {stock}",
            f"وضعیت: {'فعال' if row['is_active'] else 'غیرفعال'}",
        ]
        buttons = [
            [button("📦 مدیریت موجودی و افزودن کد/آیتم به انبار محصول", f"v52:shop:stock:{pid}")],
            [button("✏️ ویرایش قیمت و وضعیت (فعال/غیرفعال)", f"v52:shop:edit:{pid}")],
            [button("🗑 حذف محصول", f"v52:shop:delete:{pid}")],
            [button("بازگشت به مدیریت فروشگاه", parent_callback or "v52:shop")],
        ]
        return "\n".join(lines), buttons
    except (TypeError, ValueError, PermissionError) as exc:
        return str(exc), [[button("بازگشت به مدیریت فروشگاه", parent_callback or "v52:shop")]]


def _wizard_summary(answers: Mapping[str, Any]) -> str:
    collected = []
    if answers.get("title"):
        collected.append(f"نام: {answers['title']}")
    if answers.get("category_title"):
        collected.append(f"دسته: {answers['category_title']}")
    if answers.get("delivery_type"):
        collected.append(
            f"تحویل: {_DELIVERY_LABELS.get(str(answers['delivery_type']), answers['delivery_type'])}")
    if answers.get("base_price") is not None:
        collected.append(f"قیمت: {int(answers['base_price']):,} تومان")
    if answers.get("description"):
        collected.append("توضیحات: ثبت شد")
    if answers.get("audience"):
        collected.append(f"مخاطب: {_AUDIENCE_LABELS.get(str(answers['audience']), answers['audience'])}")
    if not collected:
        return ""
    return "\n\nپاسخ‌های ثبت‌شده:\n" + "\n".join(collected)


def _render_wizard_step(ctx: Any, step: str, answers: Mapping[str, Any],
                        parent_callback: str | None):
    import v52_router
    payload = {"step": step, **dict(answers)}
    v52_router.DEFAULT_FSM.set(
        ctx, "goods_product_draft", payload=payload,
        parent_callback=parent_callback, now=_now())
    summary = _wizard_summary(answers)
    cancel = [[button("لغو", parent_callback or "v52:shop")]]
    if step == "title":
        return "گام ۱ از ۶ — نام محصول را در پیام بعدی ارسال کنید." + summary, cancel
    if step == "category":
        return ("گام ۲ از ۶ — نام دسته‌بندی را ارسال کنید (اگر نباشد ساخته می‌شود)."
                + summary, cancel)
    if step == "delivery":
        return ("گام ۳ از ۶ — نوع تحویل را انتخاب کنید." + summary, [
            [button("تحویل خودکار از موجودی انبار / کد دیجیتال",
                    "v52:shop:wiz:delivery:auto_inventory")],
            [button("تحویل دستی توسط ادمین", "v52:shop:wiz:delivery:manual_fulfill")],
            [button("لغو", parent_callback or "v52:shop")],
        ])
    if step == "price":
        return ("گام ۴ از ۶ — قیمت پایه به تومان را ارسال کنید (فقط عدد)."
                + summary, cancel)
    if step == "description":
        return "گام ۵ از ۶ — توضیحات محصول را ارسال کنید." + summary, cancel
    return ("گام ۶ از ۶ — گروه مخاطب مجاز را انتخاب کنید." + summary, [
        [button("همه", "v52:shop:wiz:audience:all")],
        [button("فقط نمایندگان", "v52:shop:wiz:audience:rep")],
        [button("فقط کاربران", "v52:shop:wiz:audience:user")],
        [button("لغو", parent_callback or "v52:shop")],
    ])


def _persist_wizard_product(ctx: Any, answers: Mapping[str, Any], parent_callback: str | None):
    import goods_service
    title = str(answers.get("title") or "").strip()
    category_title = str(answers.get("category_title") or "").strip()
    delivery = str(answers.get("delivery_type") or "")
    base = _money(answers.get("base_price", 0), "base_price")
    description = str(answers.get("description") or "")
    audience = str(answers.get("audience") or "all")
    if not title or not category_title or delivery not in {"auto_inventory", "manual_fulfill"}:
        raise ValueError("اطلاعات ویزارد ناقص است؛ لطفاً از ابتدا شروع کنید.")
    rep_price, user_price = _audience_prices(audience, base)
    with database.get_db() as conn:
        category_id = _category_id(conn, category_title)
        conn.commit()
    saved = goods_service.save_product({
        "product_code": "SKU-" + uuid.uuid4().hex[:12].upper(),
        "category_id": category_id,
        "title": title,
        "description": description,
        "delivery_type": delivery,
        "rep_price": rep_price,
        "user_price": user_price,
        "is_active": True,
    })
    pid = int(saved["id"])
    lines = [
        "✅ محصول ثبت شد.",
        f"شناسه: {pid}",
        f"نام: {title}",
        f"دسته‌بندی: {category_title}",
        f"نوع تحویل: {_DELIVERY_LABELS.get(delivery, delivery)}",
        f"گروه مخاطب: {_AUDIENCE_LABELS.get(audience, audience)}",
        f"قیمت پایه: {base:,} تومان",
    ]
    rows = [
        [button("📦 مشاهده و مدیریت محصول", f"v52:shop:product:{pid}")],
        [button("بازگشت به مدیریت فروشگاه", parent_callback or "v52:shop")],
    ]
    return "\n".join(lines), rows


def begin_product_wizard(ctx: Any, *, parent_callback: str | None = "v52:shop"):
    """Start the add-product wizard at its first step. Returns ``(text, rows)``."""
    try:
        _require_root(ctx)
        return build_product_wizard_step(
            ctx, {"step": "title"}, parent_callback=parent_callback)
    except (TypeError, ValueError, PermissionError) as exc:
        return str(exc), [[button("بازگشت به مدیریت فروشگاه", parent_callback or "v52:shop")]]


def build_product_wizard_step(ctx: Any, payload: Mapping[str, Any], *, value: Any = None,
                              parent_callback: str | None = "v52:shop"):
    """Advance the add-product wizard one answer and render the resulting step.

    ``payload`` carries the answers collected so far plus the current ``step``.
    ``value`` is the answer just supplied (text for text steps, the button value
    for the delivery/audience choice steps). When the final step is answered the
    product is persisted through ``goods_service.save_product`` and a result page
    is returned. Returns ``(text, rows)``.
    """
    try:
        _require_root(ctx)
        source = dict(payload or {})
        answers = {key: val for key, val in source.items() if key != "step"}
        current = str(source.get("step") or "title")
        if current not in _WIZARD_STEPS:
            raise ValueError("گام ویزارد نامعتبر است.")
        if value is not None:
            if current == "title":
                title = str(value).strip()
                if not title or len(title) > 200:
                    raise ValueError("نام محصول باید بین ۱ تا ۲۰۰ نویسه باشد.")
                answers["title"] = title
            elif current == "category":
                category = str(value).strip()
                if not category or len(category) > 120:
                    raise ValueError("نام دسته‌بندی باید بین ۱ تا ۱۲۰ نویسه باشد.")
                answers["category_title"] = category
            elif current == "delivery":
                if str(value) not in {"auto_inventory", "manual_fulfill"}:
                    raise ValueError("نوع تحویل نامعتبر است.")
                answers["delivery_type"] = str(value)
            elif current == "price":
                answers["base_price"] = _money(str(value).replace(",", "").strip(), "base_price")
            elif current == "description":
                answers["description"] = str(value).strip()[:1000]
            elif current == "audience":
                if str(value) not in _AUDIENCE_LABELS:
                    raise ValueError("گروه مخاطب نامعتبر است.")
                answers["audience"] = str(value)
            index = _WIZARD_STEPS.index(current)
            if index + 1 >= len(_WIZARD_STEPS):
                return _persist_wizard_product(ctx, answers, parent_callback)
            current = _WIZARD_STEPS[index + 1]
        return _render_wizard_step(ctx, current, answers, parent_callback)
    except (TypeError, ValueError, PermissionError) as exc:
        return str(exc), [[button("بازگشت به مدیریت فروشگاه", parent_callback or "v52:shop")]]


def handle_product_wizard_choice(ctx: Any, field: str, value: str, *,
                                 parent_callback: str | None = "v52:shop"):
    """Handle a delivery/audience button press inside the add-product wizard."""
    try:
        _require_root(ctx)
        import v52_router
        entry = v52_router.DEFAULT_FSM.peek(ctx, now=_now())
        if entry is None or entry.state != "goods_product_draft":
            raise ValueError("ویزارد افزودن محصول فعال نیست؛ از ابتدا شروع کنید.")
        payload = dict(entry.payload or {})
        if str(payload.get("step")) != str(field):
            raise ValueError("این گزینه با گام فعلی ویزارد هم‌خوان نیست.")
        return build_product_wizard_step(
            ctx, payload, value=value, parent_callback=parent_callback)
    except (TypeError, ValueError, PermissionError) as exc:
        return str(exc), [[button("بازگشت به مدیریت فروشگاه", parent_callback or "v52:shop")]]


def begin_stock_import(ctx: Any, product_id: int, *, parent_callback: str | None = "v52:shop"):
    """Arm the one-code-per-line inventory import for an auto-inventory product.

    The pasted batch is consumed by the existing ``inventory_digital_batch`` text
    route, which previews and then calls ``goods_service.import_inventory``.
    """
    try:
        _require_root(ctx)
        pid = _positive(product_id, "product_id")
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT id,title,delivery_type,is_active FROM digital_products WHERE id=?",
                (pid,),
            ).fetchone()
        if row is None:
            raise ValueError("محصول موردنظر یافت نشد.")
        if str(row["delivery_type"]) != "auto_inventory":
            raise ValueError("افزودن کد/آیتم فقط برای محصول با تحویل خودکار از انبار ممکن است.")
        if not row["is_active"]:
            raise ValueError("محصول غیرفعال است؛ ابتدا آن را فعال کنید.")
        if str(_value(ctx, "chat_type", "private")).lower() != "private":
            raise PermissionError("ورود کد/آیتم انبار فقط در پیوی مدیریت انجام می‌شود.")
        import v52_router
        v52_router.DEFAULT_FSM.set(
            ctx, "inventory_digital_batch", payload={"product_id": pid},
            parent_callback=parent_callback, now=_now())
        text = (
            f"محصول: {pid} · {row['title']}\n\n"
            "هر کد/آیتم را در یک خط جداگانه در پیام بعدی ارسال کنید. "
            "مقادیر رمزنگاری می‌شوند و در لاگ نمایش داده نمی‌شوند."
        )
        return text, [[button("بازگشت به محصول", f"v52:shop:product:{pid}")]]
    except (TypeError, ValueError, PermissionError) as exc:
        return str(exc), [[button("بازگشت به مدیریت فروشگاه", parent_callback or "v52:shop")]]


def begin_product_edit(ctx: Any, product_id: int, *, parent_callback: str | None = "v52:shop"):
    """Price/status editor for one product. Returns ``(text, rows)``."""
    try:
        _require_root(ctx)
        pid = _positive(product_id, "product_id")
        with database.get_db() as conn:
            row = conn.execute("SELECT * FROM digital_products WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise ValueError("محصول موردنظر یافت نشد.")
        lines = [
            f"ویرایش محصول #{pid} · {row['title']}",
            f"قیمت نماینده: {int(row['rep_price']):,} تومان",
            f"قیمت کاربر: {int(row['user_price']):,} تومان",
            f"وضعیت: {'فعال' if row['is_active'] else 'غیرفعال'}",
        ]
        rows = [
            [button("💵 تغییر قیمت پایه", f"v52:shop:price:{pid}")],
            [button("🔁 فعال/غیرفعال کردن", f"v52:shop:toggle:{pid}")],
            [button("بازگشت به محصول", f"v52:shop:product:{pid}")],
        ]
        return "\n".join(lines), rows
    except (TypeError, ValueError, PermissionError) as exc:
        return str(exc), [[button("بازگشت به مدیریت فروشگاه", parent_callback or "v52:shop")]]


def toggle_product_active(ctx: Any, product_id: int, *, parent_callback: str | None = "v52:shop"):
    """Flip a product's enabled flag and return its refreshed management card."""
    try:
        _require_root(ctx)
        pid = _positive(product_id, "product_id")
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT is_active FROM digital_products WHERE id=?", (pid,)).fetchone()
            if row is None:
                raise ValueError("محصول موردنظر یافت نشد.")
            new_state = 0 if row["is_active"] else 1
            conn.execute("UPDATE digital_products SET is_active=? WHERE id=?", (new_state, pid))
            conn.commit()
        return admin_product_detail(ctx, pid, parent_callback=parent_callback)
    except (TypeError, ValueError, PermissionError) as exc:
        return str(exc), [[button("بازگشت به مدیریت فروشگاه", parent_callback or "v52:shop")]]


def begin_product_price(ctx: Any, product_id: int, *, parent_callback: str | None = "v52:shop"):
    """Ask for a new base price (text step) for one product."""
    try:
        _require_root(ctx)
        pid = _positive(product_id, "product_id")
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT id,title FROM digital_products WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise ValueError("محصول موردنظر یافت نشد.")
        import v52_router
        v52_router.DEFAULT_FSM.set(
            ctx, "goods_product_price", payload={"product_id": pid},
            parent_callback=parent_callback, now=_now())
        text = (
            f"محصول #{pid} · {row['title']}\n"
            "قیمت پایهٔ جدید را به تومان در پیام بعدی ارسال کنید (فقط عدد)."
        )
        return text, [[button("بازگشت به محصول", f"v52:shop:product:{pid}")]]
    except (TypeError, ValueError, PermissionError) as exc:
        return str(exc), [[button("بازگشت به مدیریت فروشگاه", parent_callback or "v52:shop")]]


def apply_product_price(ctx: Any, payload: Mapping[str, Any], value: Any, *,
                        parent_callback: str | None = "v52:shop"):
    """Persist a new base price, preserving the audience shape of the price pair."""
    try:
        _require_root(ctx)
        pid = _positive((payload or {}).get("product_id"), "product_id")
        base = _money(str(value).replace(",", "").strip(), "base_price")
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT rep_price,user_price FROM digital_products WHERE id=?", (pid,)).fetchone()
            if row is None:
                raise ValueError("محصول موردنظر یافت نشد.")
            rep_price, user_price = int(row["rep_price"]), int(row["user_price"])
            new_rep = base if rep_price > 0 else 0
            new_user = base if user_price > 0 else 0
            if new_rep == 0 and new_user == 0:
                new_rep = new_user = base
            conn.execute(
                "UPDATE digital_products SET rep_price=?, user_price=? WHERE id=?",
                (new_rep, new_user, pid))
            conn.commit()
        return admin_product_detail(ctx, pid, parent_callback=parent_callback)
    except (TypeError, ValueError, PermissionError) as exc:
        return str(exc), [[button("بازگشت به مدیریت فروشگاه", parent_callback or "v52:shop")]]


def preview_product_delete(ctx: Any, product_id: int, *, parent_callback: str | None = "v52:shop"):
    """First of the two delete steps: show what will be removed and ask to confirm."""
    try:
        _require_root(ctx)
        pid = _positive(product_id, "product_id")
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT id,title FROM digital_products WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise ValueError("محصول موردنظر یافت نشد.")
        text = (
            f"حذف محصول #{pid} · {row['title']}\n"
            "این عملیات بازگشت‌پذیر نیست. برای تأیید نهایی دکمهٔ زیر را بزنید."
        )
        rows = [
            [button("✅ تأیید حذف نهایی", f"v52:shop:delete:{pid}:confirm")],
            [button("لغو و بازگشت", f"v52:shop:product:{pid}")],
        ]
        return text, rows
    except (TypeError, ValueError, PermissionError) as exc:
        return str(exc), [[button("بازگشت به مدیریت فروشگاه", parent_callback or "v52:shop")]]


def confirm_product_delete(ctx: Any, product_id: int, *, parent_callback: str | None = "v52:shop"):
    """Second delete step: remove the product, refusing when inventory still references it."""
    try:
        _require_root(ctx)
        pid = _positive(product_id, "product_id")
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT id,title FROM digital_products WHERE id=?", (pid,)).fetchone()
            if row is None:
                raise ValueError("محصول موردنظر یافت نشد.")
            stock = _available_stock(conn, pid)
            total_stock = conn.execute(
                "SELECT COUNT(*) FROM digital_inventory WHERE product_id=?", (pid,)).fetchone()[0]
            if int(total_stock) > 0:
                raise ValueError(
                    "این محصول رکورد انبار دارد و حذف آن مجاز نیست؛ ابتدا آن را غیرفعال کنید.")
            conn.execute("DELETE FROM digital_products WHERE id=?", (pid,))
            conn.commit()
        text = (f"🗑 محصول #{pid} · {row['title']} حذف شد.\n"
                f"موجودی باقی‌ماندهٔ حذف‌شده: {stock}")
        rows = [[button("بازگشت به مدیریت فروشگاه", parent_callback or "v52:shop")]]
        return text, rows
    except (TypeError, ValueError, PermissionError) as exc:
        return str(exc), [[button("بازگشت به مدیریت فروشگاه", parent_callback or "v52:shop")]]


__all__ = ["show_catalog", "preview_product", "prepare_manual_delivery",
           "admin_shop_page", "admin_product_detail", "build_product_wizard_step",
           "begin_product_wizard", "handle_product_wizard_choice", "begin_stock_import",
           "begin_product_edit", "toggle_product_active", "begin_product_price",
           "apply_product_price", "preview_product_delete", "confirm_product_delete"]