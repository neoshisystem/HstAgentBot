"""Transport-neutral checkout, receipt and order status pages for v5.2.

This module never performs network I/O or financial mutations. The approved
quote/order/payment services remain the only sources of executable state.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import service_errors_fa
from ui_v52 import button, render_error, render_page

#: Every timestamp on a checkout page is shown in Tehran wall time; the epoch
#: second is what the services store, never what the operator reads.
_TEHRAN = ZoneInfo("Asia/Tehran")

#: Internal state tokens -> the words the operator reads. A token that is not in
#: the map is reported as «نامشخص» rather than echoed: ARCH-UX-002 forbids showing
#: a raw English identifier, and an unknown token is exactly the case where the
#: operator must fall back to the support route.
_PAYMENT_STATES_FA = {
    "awaiting_payment": "در انتظار پرداخت",
    "pending": "در انتظار پرداخت",
    "paid": "پرداخت‌شده",
    "confirmed": "تأییدشده",
    "rejected": "رد شده",
    "late": "پرداخت با تأخیر",
    "refunded": "بازگشت‌خورده",
    "cancelled": "لغو شده",
    "canceled": "لغو شده",
    "failed": "ناموفق",
    "unknown": "نامشخص",
}
_OPERATION_STATES_FA = {
    "not_started": "شروع نشده",
    "pending": "در انتظار",
    "running": "در حال انجام",
    "in_progress": "در حال انجام",
    "succeeded": "موفق",
    "success": "موفق",
    "failed": "ناموفق",
    "cancelled": "لغو شده",
    "unknown": "نامشخص",
}
_DELIVERY_STATES_FA = {
    "not_started": "شروع نشده",
    "pending": "در انتظار",
    "in_progress": "در حال انجام",
    "delivered": "تحویل شده",
    "failed": "ناموفق",
    "cancelled": "لغو شده",
    "unknown": "نامشخص",
}
_RAILS_FA = {"wallet": "کیف پول", "card": "کارت‌به‌کارت", "exempt": "معاف از پرداخت"}
_ADDONS_FA = {"gib": "افزایش حجم (GiB)", "day": "افزایش زمان (روز)"}

_UNKNOWN_FA = "نامشخص"
_UNLIMITED_FA = "نامحدود"


def _get(value: Any, key: str, default=None):
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _label_fa(value: Any, labels: Mapping[str, str]) -> str:
    """The Persian label of an internal token; never the token itself."""
    return labels.get(str(value or "").strip().lower(), _UNKNOWN_FA)


def _digits(value: Any) -> str | None:
    """The plain digits of a byte/second count, or ``None`` when it is not a number."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return None


def _gib_fa(quota_bytes: int) -> str:
    """A byte count as a readable GiB amount, trimmed of trailing zeros."""
    gib = f"{quota_bytes / (1024 ** 3):.2f}".rstrip("0").rstrip(".")
    return f"{gib} گیگابایت"


def _volume_fa(snapshot: Mapping[str, Any]) -> str:
    """The Persian volume of one side of a renewal; never a mapping or a raw key."""
    if str(snapshot.get("quota_mode") or "").strip().lower() == "unlimited":
        return _UNLIMITED_FA
    quota = _digits(snapshot.get("quota_bytes"))
    if quota is None:
        return _UNKNOWN_FA
    return f"{quota} بایت ({_gib_fa(int(quota))})"


def _expiry_fa(snapshot: Mapping[str, Any]) -> str:
    """The Persian expiry of one side of a renewal, in Tehran wall time."""
    mode = str(snapshot.get("time_mode") or "").strip().lower()
    if mode == "unlimited":
        return _UNLIMITED_FA
    if mode == "on_hold":
        return "حساب معلق است و زمان‌بندی فعال ندارد"
    epoch = _digits(snapshot.get("expires_at"))
    if epoch is None:
        return _UNKNOWN_FA
    local = datetime.fromtimestamp(int(epoch), tz=timezone.utc).astimezone(_TEHRAN)
    return f"{local.strftime('%Y-%m-%d %H:%M')} به وقت تهران"


def _renewal_side_fa(label: str, snapshot: Mapping[str, Any]) -> list[str]:
    """The labelled volume/time block for one side of a frozen renewal snapshot.

    ``services.renewal_calculator`` speaks in ``quota_mode``/``quota_bytes``/
    ``used_bytes``/``time_mode``/``expires_at`` (and ``days`` on the target side).
    Those keys are internal: the operator gets labelled Persian values, and every
    value passes through a formatter, so no mapping can reach the page.
    """
    lines = [f"{label}:", f"• حجم: {_volume_fa(snapshot)}"]
    used = _digits(snapshot.get("used_bytes"))
    if used is not None:
        lines.append(f"• مصرف‌شده: {used} بایت")
    lines.append(f"• انقضا: {_expiry_fa(snapshot)}")
    remaining = _digits(snapshot.get("remaining_days"))
    if remaining is not None:
        lines.append(f"• روزهای باقی‌مانده: {remaining} روز")
    added = _digits(snapshot.get("days"))
    if added is not None:
        lines.append(f"• زمان افزوده‌شدهٔ این تمدید: {added} روز")
    return lines


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
        raise ValueError(f"{label} must be a non-negative integer toman amount")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a non-negative integer toman amount") from exc
    if result < 0:
        raise ValueError(f"{label} must be a non-negative integer toman amount")
    return result


def _actor(ctx: Any) -> int:
    return _positive(_get(ctx, "actor_tg_id", _get(ctx, "telegram_id")), "actor_tg_id")


def _persona(ctx: Any, fallback: str = "user") -> str:
    value = str(_get(ctx, "persona", fallback) or fallback).lower()
    return "admin" if value in {"admin", "root"} else "rep" if value in {"rep", "representative"} else "user"


def _quote_values(quote: Mapping[str, Any]) -> tuple[int, str, list, Mapping]:
    amount = _money(quote.get("amount_toman"))
    purpose = str(quote.get("purpose") or "")
    edges = quote.get("price_edges") or []
    snapshot = quote.get("snapshot") or {}
    if not isinstance(edges, list) or not isinstance(snapshot, Mapping):
        raise ValueError("quote snapshot is malformed")
    for edge in edges:
        if not isinstance(edge, Mapping):
            raise ValueError("quote price edge is malformed")
        if "price_toman" in edge:
            _money(edge["price_toman"], "price edge")
    if edges and _money(edges[-1].get("price_toman")) != amount:
        raise ValueError("quote final price edge does not match the frozen amount")
    return amount, purpose, edges, snapshot


def _quote_lines(amount: int, quote: Mapping[str, Any], edges: list) -> list[str]:
    lines = [f"مبلغ نهاییِ quote قفل‌شده: {amount:,} تومان"]
    lines.append(f"شناسهٔ quote: {quote.get('quote_id') or 'شناسه در جریان تسویه'}")
    if edges:
        lines.append("زنجیرهٔ قیمت:")
        for edge in edges:
            lines.append(
                f"{edge.get('seller_key', '—')} ← {edge.get('buyer_key', '—')}: "
                f"{_money(edge.get('price_toman', 0)):,} تومان"
            )
    coupon = quote.get("coupon") or (quote.get("spec") or {}).get("coupon")
    if isinstance(coupon, Mapping):
        # The frozen quote amount already includes the issuer-funded coupon edge.
        lines.append(f"کوپن ثبت‌شده در snapshot: {coupon.get('code', '—')}؛ تخفیف لحاظ‌شده در quote")
    return lines


def preview_buy(
    ctx: Any,
    plan_code: str,
    *,
    quote: Mapping[str, Any],
    parent_callback: str | None = None,
    persona: str | None = None,
):
    """Render the exact amount in an immutable quote; do not reprice or subtract coupons."""
    try:
        if not isinstance(quote, Mapping):
            raise ValueError("an immutable price quote is required")
        code = str(plan_code or "").strip()
        if not code:
            raise ValueError("plan_code is required")
        amount, purpose, edges, _snapshot = _quote_values(quote)
        if purpose and purpose not in {"vpn_buy", "sale", "personal_use"}:
            raise ValueError("quote purpose is not a purchase")
        if amount <= 0 and purpose != "personal_use":
            raise ValueError("paid purchase quote must be positive")
        lines = [f"پلن: {code}", *_quote_lines(amount, quote, edges)]
        rows = [[button("ادامه به انتخاب روش پرداخت", f"v52:order:{quote.get('order_id', quote.get('quote_id', code))}")]]
        return render_page("پیش‌نمایش خرید", "\n".join(lines),
                           persona=persona or _persona(ctx), page_kind="preview",
                           parent_callback=parent_callback, rows=rows)
    except (TypeError, ValueError) as exc:
        return render_error(service_errors_fa.translate(exc), persona=persona or _persona(ctx), parent_callback=parent_callback)


def preview_renew(
    ctx: Any,
    account_id: int,
    plan_code: str,
    *,
    quote: Mapping[str, Any],
    wallet_balance: int | None = None,
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
    redirect_callback: str | None = None,
    parent_callback: str | None = None,
    persona: str | None = None,
):
    """Show the before/after renewal values from the same frozen quote used by execution.

    When *wallet_balance* is provided the page appends a line comparing the
    quote amount against the wallet balance and replaces the default ``ادامه``
    button with a conditional deduction or top-up button.
    """
    try:
        aid = _positive(account_id, "account_id")
        code = str(plan_code or "").strip()
        if not code or not isinstance(quote, Mapping):
            raise ValueError("plan_code and immutable renewal quote are required")
        amount, purpose, edges, snapshot = _quote_values(quote)
        if purpose and purpose not in {"vpn_renew", "renewal"}:
            raise ValueError("quote purpose is not a renewal")
        spec = quote.get("spec")
        if not isinstance(spec, Mapping):
            spec = snapshot.get("spec", {})
        frozen = snapshot.get("before_after_renewal", {})
        if not isinstance(frozen, Mapping):
            frozen = {}
        selected_before = before if before is not None else frozen.get("before")
        selected_after = after if after is not None else frozen.get("after", frozen.get("renewal"))
        if not isinstance(selected_before, Mapping) or not isinstance(selected_after, Mapping):
            raise ValueError("renewal quote must contain before and after snapshots")
        if before is not None and frozen.get("before") is not None and dict(before) != dict(frozen["before"]):
            raise ValueError("before snapshot differs from the frozen quote")
        if after is not None and frozen.get("after") is not None and dict(after) != dict(frozen["after"]):
            raise ValueError("after snapshot differs from the frozen quote")
        lines = [f"حساب: {aid}", f"پلن: {code}", *_quote_lines(amount, quote, edges),
                 *_renewal_side_fa("وضعیت پیش از تمدید", selected_before),
                 *_renewal_side_fa("وضعیت پس از تمدید", selected_after)]
        callback = redirect_callback or f"v52:renew:{aid}:{code}"
        if wallet_balance is not None:
            balance = _money(wallet_balance, "wallet_balance")
            lines.append(f"مبلغ پلن: {amount:,} تومان | موجودی شما: {balance:,} تومان")
            if balance >= amount:
                rows = [[button("✅ کسر از کیف پول", callback)]]
            else:
                rows = [[button("💳 شارژ سریع کیف پول", callback)]]
        else:
            rows = [[button("ادامه", callback)]]
        return render_page("پیش‌نمایش تمدید", "\n".join(lines),
                           persona=persona or _persona(ctx), page_kind="preview",
                           parent_callback=parent_callback,
                           rows=rows)
    except (TypeError, ValueError) as exc:
        return render_error(service_errors_fa.translate(exc), persona=persona or _persona(ctx), parent_callback=parent_callback)


def begin_addon(
    ctx: Any,
    account_id: int,
    addon_type: str = "gib",
    value: int | float | None = None,
    *,
    on_hold: bool = False,
    parent_callback: str | None = None,
    persona: str | None = None,
):
    """Open an add-on draft; on-hold accounts may add quota but not days."""
    try:
        aid = _positive(account_id, "account_id")
        kind = str(addon_type or "").strip().lower()
        if kind not in {"gib", "day"}:
            raise ValueError("addon_type must be gib or day")
        if on_hold and kind == "day":
            raise ValueError("افزودن روز برای حساب معلق مجاز نیست؛ فقط افزایش حجم امکان‌پذیر است.")
        if value is None:
            rows = [[button("افزایش حجم (GiB)", f"v52:addon:{aid}:gib")]]
            if not on_hold:
                rows.append([button("افزایش زمان (روز)", f"v52:addon:{aid}:day")])
            return render_page(
                "افزونهٔ حساب",
                f"حساب: {aid}\nنوع افزونه را انتخاب کنید."
                + ("\nاین حساب معلق است؛ گزینهٔ زمان مسدود است." if on_hold else ""),
                persona=persona or _persona(ctx), page_kind="form",
                parent_callback=parent_callback, rows=rows,
            )
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError("مقدار افزونه باید مثبت باشد")
        if kind == "gib":
            if float(value) != int(value):
                raise ValueError("حجم افزونه باید تعداد صحیح GiB باشد")
            display = f"{int(value)} GiB"
        else:
            if float(value) != int(value):
                raise ValueError("زمان افزونه باید تعداد صحیح روز باشد")
            display = f"{int(value)} روز"
        return render_page(
            "پیش‌نمایش افزونه",
            f"حساب: {aid}\nنوع: {_label_fa(kind, _ADDONS_FA)}\nمقدار: {display}\n"
            "قیمت فقط از برآورد نهایی سرویس قیمت‌گذاری خوانده می‌شود.",
            persona=persona or _persona(ctx), page_kind="preview",
            parent_callback=parent_callback,
            rows=[[button("بازگشت به حساب", f"v52:account:{aid}")]],
        )
    except (TypeError, ValueError) as exc:
        return render_error(service_errors_fa.translate(exc), persona=persona or _persona(ctx), parent_callback=parent_callback)


def choose_rail(
    ctx: Any,
    order: Mapping[str, Any],
    rail: str,
    *,
    wallet_balance: int | None = None,
    parent_callback: str | None = None,
    persona: str | None = None,
):
    """Select exactly one supported rail; wallet requires full available coverage."""
    try:
        if not isinstance(order, Mapping):
            raise ValueError("order snapshot is required")
        actor = _actor(ctx)
        if int(order.get("actor_tg_id", actor)) != actor:
            raise PermissionError("order belongs to another actor")
        order_id = str(order.get("order_id") or "").strip()
        if not order_id:
            raise ValueError("order_id is required")
        amount = _money(order.get("amount_toman"))
        selected = str(rail or "").strip().lower()
        if selected in {"mixed", "split", "wallet+card", "wallet_card"}:
            raise ValueError("پرداخت ترکیبی مجاز نیست؛ یک روش پرداخت برای کل مبلغ انتخاب کنید.")
        if selected not in {"wallet", "card", "card_to_card", "exempt"}:
            raise ValueError("روش پرداخت پشتیبانی نمی‌شود")
        if selected == "wallet":
            if amount <= 0:
                raise ValueError("پرداخت کیف‌پولی فقط برای سفارش مبلغ‌دار معتبر است")
            if wallet_balance is None:
                raise ValueError("ماندهٔ قابل‌مصرف کیف‌پول باید از سرویس مالی خوانده شود؛ این صفحه خودش موجودی نمی‌سازد.")
            balance = _money(wallet_balance, "wallet_balance")
            if balance < amount:
                return render_page(
                    "موجودی ناکافی",
                    f"مبلغ سفارش: {amount:,} تومان\nماندهٔ قابل‌مصرف: {balance:,} تومان\n"
                    "برداشت جزئی یا ترکیب روش‌ها انجام نمی‌شود.",
                    persona=persona or _persona(ctx), page_kind="error",
                    parent_callback=parent_callback,
                    rows=[[button("انتخاب کارت‌به‌کارت", f"v52:pay:{order_id}:card")]],
                )
        if selected == "exempt" and amount != 0:
            raise ValueError("روش معاف فقط برای سفارش صفرمبلغ مجاز است")
        if selected in {"card", "card_to_card"} and amount <= 0:
            raise ValueError("پرداخت کارت‌به‌کارت به مبلغ مثبت نیاز دارد")
        normalized = "card" if selected == "card_to_card" else selected
        return render_page(
            "تأیید روش پرداخت",
            f"سفارش: {order_id}\nروش: {_label_fa(normalized, _RAILS_FA)}\nمبلغ کامل: {amount:,} تومان\n"
            "انتخاب روش به‌تنهایی پرداخت یا تحویل را انجام نمی‌دهد.",
            persona=persona or _persona(ctx), page_kind="preview",
            parent_callback=parent_callback,
            rows=[[button("ادامه", f"v52:pay:{order_id}:{'card' if normalized == 'card' else normalized}")]],
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(service_errors_fa.translate(exc), persona=persona or _persona(ctx), parent_callback=parent_callback)


def preview_receipt(
    ctx: Any,
    attempt: Mapping[str, Any],
    *,
    order: Mapping[str, Any],
    bank_card: Mapping[str, Any] | None = None,
    parent_callback: str | None = None,
    persona: str | None = None,
):
    """Show full card instructions only to the payer; never treat a photo as proof by itself."""
    try:
        if not isinstance(attempt, Mapping) or not isinstance(order, Mapping):
            raise ValueError("payment attempt and order snapshots are required")
        actor = _actor(ctx)
        if int(order.get("actor_tg_id", actor)) != actor:
            raise PermissionError("payment instructions are private to the payer")
        if str(order.get("status") or "awaiting_payment") != "awaiting_payment":
            raise ValueError("order is not awaiting payment")
        amount = _money(order.get("amount_toman"))
        attempt_amount = _money(attempt.get("amount_toman", amount))
        if attempt_amount != amount:
            raise ValueError("partial or mixed receipt amounts are not accepted")
        card = bank_card or attempt.get("card") or {}
        if not isinstance(card, Mapping):
            raise ValueError("assigned bank card snapshot is unavailable")
        number = str(card.get("card_number") or card.get("number") or "").strip()
        holder = str(card.get("card_holder") or card.get("holder") or "").strip()
        if not number or not holder:
            raise ValueError("payer card details are incomplete")
        bank = str(card.get("bank_name") or card.get("bank") or "—")
        lines = [
            f"سفارش: {order.get('order_id')}",
            f"مبلغ کامل: {amount:,} تومان",
            f"بانک: {bank}",
            f"شماره کارت مقصد: {number}",
            f"دارنده کارت: {holder}",
            "پس از انتقال، فقط یک تصویر رسید ارسال کنید؛ تصویر به‌تنهایی تأیید پرداخت یا تحویل نیست.",
        ]
        return render_page(
            "اطلاعات پرداخت کارت‌به‌کارت", "\n".join(lines),
            persona=persona or _persona(ctx), page_kind="payment",
            parent_callback=parent_callback,
            rows=[[button("ارسال رسید", f"v52:receipt:{attempt.get('attempt_id', '')}")]],
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(service_errors_fa.translate(exc), persona=persona or _persona(ctx), parent_callback=parent_callback)


def show_order(
    ctx: Any,
    order: Mapping[str, Any],
    *,
    payment: Mapping[str, Any] | None = None,
    operation: Mapping[str, Any] | None = None,
    delivery: Mapping[str, Any] | None = None,
    parent_callback: str | None = None,
    persona: str | None = None,
):
    """Render payment, operation and delivery as independent states; never auto-refund."""
    try:
        if not isinstance(order, Mapping):
            raise ValueError("order snapshot is required")
        actor = _actor(ctx)
        if int(order.get("actor_tg_id", actor)) != actor:
            raise PermissionError("order belongs to another actor")
        order_id = str(order.get("order_id") or "").strip()
        if not order_id:
            raise ValueError("order_id is required")
        payment_state = str((payment or {}).get("status") or order.get("payment_status") or order.get("status") or "unknown")
        operation_state = str((operation or {}).get("state") or "not_started")
        delivery_state = str((delivery or {}).get("state") or "not_started")
        lines = [
            f"سفارش: {order_id}",
            f"مبلغ: {_money(order.get('amount_toman')):,} تومان",
            f"وضعیت پرداخت: {_label_fa(payment_state, _PAYMENT_STATES_FA)}",
            f"وضعیت عملیات: {_label_fa(operation_state, _OPERATION_STATES_FA)}",
            f"وضعیت تحویل: {_label_fa(delivery_state, _DELIVERY_STATES_FA)}",
        ]
        if payment_state in {"pending", "awaiting_payment", "unknown", "late", "rejected"}:
            lines.append("پرداخت هنوز نهایی نشده است؛ هیچ تحویلی از این صفحه نتیجه‌گیری نمی‌شود.")
        if payment_state == "unknown":
            lines.append("وضعیت نامعلوم است؛ برای بررسی به مسیر پشتیبانی بازگردید. بازپرداخت خودکار انجام نمی‌شود.")
        if payment_state == "rejected":
            lines.append("رسید رد شده است؛ اصلاح یا ارسال مجدد باید از مسیر مجاز انجام شود.")
        return render_page(
            "وضعیت سفارش", "\n".join(lines),
            persona=persona or _persona(ctx), page_kind="order",
            parent_callback=parent_callback,
            rows=[[button("بازگشت به سفارش‌ها", "v52:orders")]],
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(service_errors_fa.translate(exc), persona=persona or _persona(ctx), parent_callback=parent_callback)


__all__ = [
    "preview_buy", "preview_renew", "begin_addon",
    "choose_rail", "preview_receipt", "show_order",
]
