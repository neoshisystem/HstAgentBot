"""v5.2 digital-goods catalogue and idempotent manual-delivery pages.

This module renders UI and delegates accepted orders to the established quote,
order and goods-fulfillment services. It never creates a second order or stock
allocation while rendering a resend action.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any, Callable

import config
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
    if _root(ctx):
        return True
    seller = str(product.get("seller_key") or product.get("merchant_key") or "root")
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


__all__ = ["show_catalog", "preview_product", "prepare_manual_delivery"]