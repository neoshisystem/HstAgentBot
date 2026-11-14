"""Scoped bank-card, plan and settlement administration pages for v5.2."""
from __future__ import annotations

from collections.abc import Mapping

import config
from ui_v52 import button, render_error, render_page


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _positive(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if result <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return result


def _actor(ctx):
    return _positive(_get(ctx, "actor_tg_id", _get(ctx, "telegram_id")), "actor_tg_id")


def _root(ctx):
    try:
        root_id = int(getattr(config, "ADMIN_TELEGRAM_ID", 0) or 0)
        return root_id > 0 and _actor(ctx) == root_id
    except (TypeError, ValueError):
        return False


def _rep_key(ctx):
    rep_id = _get(ctx, "rep_id")
    return f"rep:{_positive(rep_id, 'rep_id')}"


def _mask_card(value):
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    if len(digits) < 4:
        return "••••"
    return "•••• •••• •••• " + digits[-4:]


def _can_manage_merchant(ctx, merchant_key):
    if _root(ctx):
        return True
    try:
        return str(merchant_key or "") == _rep_key(ctx)
    except (TypeError, ValueError):
        return False


def edit_bank_card(
    ctx,
    card: Mapping | None = None,
    *,
    fields: Mapping | None = None,
    parent_callback: str | None = None,
):
    """Render card details/edit form within root or own merchant scope; mask list values."""
    try:
        if card is None:
            if not _root(ctx) and _get(ctx, "rep_id") is None:
                raise PermissionError("actor has no bank-card administration scope")
            return render_page(
                "مدیریت کارت بانکی",
                "شماره کارت، شبا، نام دارنده، بانک و scope را ثبت/ویرایش کنید. "
                "فهرست و پیش‌نمایش مدیریتی فقط چهار رقم پایانی را نشان می‌دهد.",
                persona="admin" if _root(ctx) else "rep", page_kind="form",
                parent_callback=parent_callback,
            )
        if not isinstance(card, Mapping):
            raise ValueError("bank card snapshot is required")
        merchant_key = str(card.get("merchant_key") or "")
        if not _can_manage_merchant(ctx, merchant_key):
            raise PermissionError("نماینده فقط کارت متعلق به merchant خود را می‌تواند ویرایش کند.")
        number = card.get("card_number") or card.get("number") or ""
        holder = str(card.get("card_holder") or card.get("holder") or "")
        bank = str(card.get("bank_name") or card.get("bank") or "")
        scope = str(card.get("target_scope") or card.get("scope") or "merchant")
        if scope not in {"merchant", "root", "rep", "all", "rep_settlement", "children"}:
            raise ValueError("bank-card target scope is invalid")
        text = (
            f"کارت: {card.get('id', 'جدید')}\nMerchant: {merchant_key}\n"
            f"شماره: {_mask_card(number)}\nدارنده: {holder}\nبانک: {bank}\nScope: {scope}\n"
            "نمایش کامل شماره کارت فقط در دستور پرداختِ payer مجاز است."
        )
        return render_page(
            "پیش‌نمایش ویرایش کارت", text,
            persona="admin" if _root(ctx) else "rep", page_kind="preview",
            parent_callback=parent_callback,
            rows=[[button("بازگشت به کارت‌ها", "v52:cards")]],
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin" if _root(ctx) else "rep", parent_callback=parent_callback)


def edit_plan(
    ctx,
    plan: Mapping,
    *,
    panel_scope: str | None = None,
    selected_panel_ids: list[int] | None = None,
    current_panels: list[Mapping] | None = None,
    allow_panels: list[str] | None = None,
    deny_panels: list[str] | None = None,
    sale_status: str | None = None,
    tier: str | None = None,
    custom_prices: Mapping | None = None,
    confirm_all_explicit: bool = False,
    parent_callback: str | None = None,
):
    """Preview normalized panel allow/deny, sale state, tier and seller-specific prices."""
    try:
        if not isinstance(plan, Mapping):
            raise ValueError("plan snapshot is required")
        seller_key = str(plan.get("seller_key") or plan.get("merchant_key") or "root")
        if not _can_manage_merchant(ctx, seller_key):
            raise PermissionError("actor cannot edit this plan scope")
        selected_scope = str(panel_scope or plan.get("panel_scope") or "legacy").strip().lower()
        if selected_scope not in {"legacy", "selected", "all_explicit"}:
            raise ValueError("panel_scope must be legacy, selected or all_explicit")
        allow = sorted({str(item).strip().lower() for item in (allow_panels or plan.get("allow_panels") or []) if str(item).strip()})
        deny = sorted({str(item).strip().lower() for item in (deny_panels or plan.get("deny_panels") or []) if str(item).strip()})
        if set(allow) & set(deny):
            raise ValueError("a panel cannot be both allowed and denied")
        if selected_scope == "selected":
            ids = selected_panel_ids or []
            if not ids or any(isinstance(item, bool) or not isinstance(item, int) or item <= 0 for item in ids):
                raise ValueError("selected panel scope needs positive panel IDs")
        elif selected_scope == "all_explicit":
            panels = current_panels or []
            if not panels:
                raise ValueError("all_explicit requires the current panel list")
            if not confirm_all_explicit:
                detail = ", ".join(str(p.get("name") or p.get("id")) for p in panels)
                return render_page(
                    "تأیید دامنهٔ همهٔ پنل‌ها",
                    f"پنل‌های فعلی که صریحاً انتخاب می‌شوند: {detail}\n"
                    "برای اعمال scope=all_explicit تأیید جداگانه لازم است.",
                    persona="admin" if _root(ctx) else "rep", page_kind="preview",
                    parent_callback=parent_callback,
                    rows=[[button("تأیید همهٔ پنل‌های نمایش‌داده‌شده", f"v52:plan:{plan.get('plan_code', '')}:all_explicit")]],
                )
        state = str(sale_status or plan.get("sale_status") or "active").lower()
        if state not in {"active", "paused", "hidden", "sold_out"}:
            raise ValueError("sale_status is invalid")
        selected_tier = str(tier or plan.get("tier") or "normal").lower()
        prices = dict(custom_prices or plan.get("custom_prices") or {})
        normalized_prices = {}
        for key, raw in prices.items():
            if not str(key).strip() or isinstance(raw, bool):
                raise ValueError("custom price keys and values are invalid")
            amount = int(raw)
            if amount < 0 or str(raw).strip() not in {str(amount), f"{amount}.0"}:
                raise ValueError("custom prices must be non-negative integer toman")
            normalized_prices[str(key).strip()] = amount
        text = (
            f"پلن: {plan.get('plan_code', '—')}\nMerchant: {seller_key}\n"
            f"Panel scope: {selected_scope}\nAllow: {allow or 'legacy/default'}\nDeny: {deny or 'none'}\n"
            f"Sale status: {state}\nTier: {selected_tier}\n"
            f"قیمت‌های سفارشی: {normalized_prices or 'بدون قیمت سفارشی'}\n"
            "قیمت نهایی سفارش‌های موجود از quote snapshot تغییر نمی‌کند."
        )
        return render_page("پیش‌نمایش تنظیم تعرفه", text,
                           persona="admin" if _root(ctx) else "rep",
                           page_kind="preview", parent_callback=parent_callback,
                           rows=[[button("بازگشت به تعرفه‌ها", "v52:plans")]])
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin" if _root(ctx) else "rep", parent_callback=parent_callback)


def preview_settlement(
    ctx,
    settlement: Mapping,
    *,
    parent_callback: str | None = None,
):
    """Preview only an explicit representative-edge settlement, never retail sales."""
    try:
        if not isinstance(settlement, Mapping):
            raise ValueError("settlement snapshot is required")
        purpose = str(settlement.get("purpose") or "").lower()
        if purpose != "rep_settlement":
            raise ValueError("settlement purpose must be explicitly rep_settlement")
        edge_id = _positive(settlement.get("edge_id"), "edge_id")
        merchant_key = str(settlement.get("merchant_key") or settlement.get("parent_key") or "")
        if not _can_manage_merchant(ctx, merchant_key):
            raise PermissionError("actor cannot settle this representative edge")
        debt = settlement.get("edge_debt_toman", settlement.get("debt_toman"))
        if isinstance(debt, bool) or not isinstance(debt, int) or debt < 0:
            raise ValueError("edge debt must be a non-negative ledger-derived integer")
        amount = settlement.get("amount_toman", debt)
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0 or amount > debt:
            raise ValueError("settlement amount must not exceed the explicit edge debt")
        text = (
            f"Edge: {edge_id}\nParent merchant: {merchant_key}\n"
            f"بدهی ثبت‌شدهٔ همین edge: {debt:,} تومان\n"
            f"مبلغ تسویهٔ پیشنهادی: {amount:,} تومان\n"
            "فروش مستقیم خرده‌فروشی در این تسویه دخالت ندارد؛ این صفحه هیچ posting مالی انجام نمی‌دهد."
        )
        return render_page(
            "پیش‌نمایش تسویه نماینده", text,
            persona="admin" if _root(ctx) else "rep", page_kind="preview",
            parent_callback=parent_callback,
            rows=[[button("بازگشت به امور مالی", "v52:finance")]],
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin" if _root(ctx) else "rep", parent_callback=parent_callback)


__all__ = ["edit_bank_card", "edit_plan", "preview_settlement"]
