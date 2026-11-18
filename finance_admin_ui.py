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


# --------------------------------------------------------------------------- #
# Unified tariff, category, coupon and refund administration
#
# The super-admin menu used to carry two disconnected tariff menus: ``v52:plans``
# only toggled panel bindings, while ``admin:plans`` had CRUD but ignored the
# category, the tariff kind, the audience and the panel bindings. Both routes now
# render the one hub below and reach the one plan card, so an operator can never
# again edit half of a plan from one menu and the other half from another.
#
# Every label on these pages is Persian. A tariff code or category key is shown
# only where it is the identifier actually being edited.
# --------------------------------------------------------------------------- #

CHECKBOX_ON = "✅"
CHECKBOX_OFF = "☑️"
MAX_CALLBACK_BYTES = 64

PLAN_CATEGORY_NEW_FORMAT = (
    "قالب ثبت دسته‌بندی جدید:\n"
    "کد | عنوان فارسی | نماد\n"
    "مثال:\n"
    "gaming | سرویس‌های گیمینگ | 🎮\n\n"
    "• کد با حرف کوچک لاتین شروع می‌شود و فقط حروف کوچک، رقم و زیرخط می‌پذیرد.\n"
    "• نماد اختیاری است و می‌تواند خالی بماند."
)

PLAN_CATEGORY_EDIT_FORMAT = (
    "قالب ویرایش:\n"
    "عنوان فارسی | نماد | ترتیب نمایش | وضعیت\n"
    "مثال:\n"
    "سرویس‌های ویژه | 💎 | 2 | 1\n\n"
    "هر بخش را می‌توانید خالی بگذارید تا تغییر نکند. وضعیت: ۱ فعال، ۰ غیرفعال."
)

COUPON_NEW_FORMAT = (
    "قالب ثبت کوپن جدید:\n"
    "کد | مبلغ تخفیف (تومان) | سقف بودجه (تومان) | حداکثر استفاده | اعتبار (روز)\n"
    "مثال:\n"
    "WELCOME10 | 10000 | 1000000 | 100 | 90"
)

COUPON_EDIT_FORMAT = (
    "قالب ویرایش کوپن:\n"
    "مبلغ تخفیف | سقف بودجه | حداکثر استفاده | اعتبار (روز)\n"
    "مثال:\n"
    "15000 | 2000000 | 200 | 60\n\n"
    "هر بخش را می‌توانید خالی بگذارید تا تغییر نکند."
)

REFUND_DESTINATION_FORMAT = (
    "شماره کارت یا شبای مقصد عودت را ارسال کنید.\n"
    "مثال:\n"
    "6037-9911-2233-4455"
)

#: Order states ``refund_service.request_bank_refund`` accepts as refundable.
REFUNDABLE_ORDER_STATES = ("accepted", "completed", "exception", "closed_refunded")


def _db():
    import database
    return database.get_db()


def _catalog():
    import pricing_service
    return pricing_service


def _promotions():
    import promotion_service
    return promotion_service


def _require_root(ctx) -> None:
    if not _root(ctx):
        raise PermissionError(
            "مدیریت تعرفه‌ها، دسته‌بندی‌ها، کوپن‌ها و عودت وجه فقط در اختیار مدیر کل است."
        )


def _private(ctx) -> bool:
    """True only in a private chat; the write forms never run in a group."""
    return str(_get(ctx, "chat_type", "private")).lower() == "private"


def _checkbox_callback(prefix: str, plan_code: str, suffix: str) -> str:
    """Build one checkbox callback, refusing an identifier that cannot fit.

    Telegram caps callback data at 64 bytes. A pathological tariff code would push
    a checkbox row past that limit and ``ui_v52.button`` raises, which would turn
    the whole card into an error page. Failing here with a Persian explanation
    keeps the rest of the card usable and names exactly what to shorten.

    ``prefix`` carries its own trailing colon, so every literal in this module is a
    registered callback prefix rather than a bare fragment.
    """
    data = f"{prefix}{plan_code}:{suffix}"
    if len(data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        raise ValueError(
            "کد تعرفه بیش از حد بلند است و دکمه‌های این کارت در تلگرام جا نمی‌شود؛ "
            "کد تعرفه را کوتاه‌تر کنید."
        )
    return data


def _money(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "۰"


def _checkbox_label(text: str, state: bool) -> str:
    return f"{CHECKBOX_ON if state else CHECKBOX_OFF} {str(text)[:48]}"


def _split(value) -> list[str]:
    return [chunk.strip() for chunk in str(value or "").split("|")]


def _optional_int(chunk: str, label: str):
    """Parse one optional field; an empty chunk means «leave unchanged»."""
    if not chunk.strip():
        return None
    try:
        return int(chunk.strip())
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} باید یک عدد صحیح باشد.") from exc


def tariff_hub(ctx, *, parent_callback: str | None = None):
    """The single tariff, category and panel centre both entry points render."""
    try:
        _require_root(ctx)
        catalog = _catalog()
        with _db() as conn:
            plans = catalog.plan_catalog(conn, active_only=False)
            categories = catalog.list_plan_categories(conn)
            panel_count = int(conn.execute(
                "SELECT COUNT(*) FROM panels WHERE lifecycle='active'").fetchone()[0])
            tier_count = int(conn.execute(
                "SELECT COUNT(*) FROM tier_policies").fetchone()[0])
        active = [plan for plan in plans if int(plan.get("is_active", 1) or 0)]
        lines = [
            f"• تعداد تعرفه‌ها: {len(plans)} (فعال: {len(active)})",
            f"• دسته‌بندی‌های تعرفه: {len(categories)}",
            f"• پنل‌های فعال: {panel_count}",
            f"• سطوح قیمت‌گذاری: {tier_count}",
            "",
            "از این مرکز، قیمت، نوع تعرفه، دسته‌بندی، پنل‌های مجاز و مخاطب هر تعرفه را "
            "یک‌جا مدیریت می‌کنید. تغییرات بلافاصله در پورتال کاربری و میزکار نمایندگی "
            "اعمال می‌شود و قیمت نهایی از همان مسیری محاسبه می‌شود که خرید از آن انجام می‌گیرد.",
            "",
            "برای ویرایش هر تعرفه روی آن بزنید:",
        ]
        rows = [[button("📂 مدیریت دسته‌بندی‌های تعرفه", "v52:plancats"),
                 button("🎖 سطوح و قیمت اختصاصی", "v52:tiers")]]
        for plan in plans:
            dot = "🟢" if int(plan.get("is_active", 1) or 0) else "🔴"
            emoji = str(plan.get("category_emoji") or "")
            label = (
                f"{dot} {emoji} {plan['name']} · "
                f"{float(plan['gb']):g} گیگ/{int(plan['days'])} روز · "
                f"کاربر {_money(plan['user_price'])} ت"
            )
            rows.append([button(label[:60], f"v52:plan:{plan['plan_code']}")])
        rows.append([button("➕ تعریف تعرفهٔ جدید", "admin:prompt_add_plan")])
        return render_page(
            "🏷 مدیریت تعرفه‌ها، دسته‌بندی‌ها و پنل‌ها", "\n".join(lines),
            persona="admin", rows=rows, parent_callback=parent_callback)
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def plan_detail(ctx, plan_code, *, parent_callback: str | None = None):
    """The plan card: prices, tariff kind, audience, categories and panels.

    Every one of the four multi-select groups is a checkbox row that toggles in
    place and re-renders this same card, so an operator sees the effect of a change
    immediately instead of walking back through a menu.
    """
    try:
        _require_root(ctx)
        catalog = _catalog()
        with _db() as conn:
            plan = catalog.require_plan(conn, plan_code)
            code = str(plan["plan_code"])
            links = catalog.plan_category_links(conn, code)
            linked = {str(link["category_key"]) for link in links}
            categories = catalog.list_plan_categories(conn)
            bindings = catalog.plan_panel_bindings(conn, code)
            audience = catalog.plan_audience(conn, code)
        kind = str(plan.get("plan_kind") or "fixed")
        kind_fa = catalog.PLAN_KIND_FA.get(kind, kind)
        allowed_panels = [b for b in bindings if int(b["allowed"])]
        lines = [
            f"• کد تعرفه: {code}",
            f"• حجم و مدت: {float(plan['gb']):g} گیگابایت / {int(plan['days'])} روز",
            f"• قیمت خرید نمایندگی: {_money(plan['rep_price'])} تومان",
            f"• قیمت فروش به کاربر: {_money(plan['user_price'])} تومان",
            f"• نوع تعرفه: {kind_fa}",
            f"• وضعیت فروش: {'🟢 فعال' if int(plan.get('is_active', 1) or 0) else '🔴 غیرفعال'}",
            f"• نمایش سریع در منو: {'⚡️ بله' if int(plan.get('is_quick') or 0) else '📦 خیر'}",
            f"• مخاطب مجاز: 👤 کاربران نهایی "
            f"{CHECKBOX_ON if audience['for_end_users'] else CHECKBOX_OFF} | 🧑‍💼 نمایندگان "
            f"{CHECKBOX_ON if audience['for_reps'] else CHECKBOX_OFF}",
            f"• دسته‌بندی‌های مشمول: "
            + (", ".join(f"{link['emoji']} {link['title_fa']}" for link in links)
               if links else "بدون دسته‌بندی"),
            f"• پنل‌های مجاز: "
            + (", ".join(str(item["name"]) for item in allowed_panels)
               if allowed_panels else "هیچ پنلی مجاز نیست"),
            "",
            "برای تغییر هر گزینه روی همان دکمه بزنید؛ تغییر بلافاصله ذخیره و همین کارت "
            "دوباره نمایش داده می‌شود.",
        ]
        rows: list[list] = [
            [button("📐 ویرایش حجم و زمان", f"admin:prompt_edit_plan_specs:{code}"),
             button("✏️ ویرایش قیمت‌ها", f"admin:prompt_edit_plan:{code}")],
            [button("⚡️ تغییر حالت سریع/عادی",
                    f"admin:set_plan_quick:{code}:{0 if int(plan.get('is_quick') or 0) else 1}"),
             button("🟢 فعال‌سازی تعرفه" if not int(plan.get("is_active", 1) or 0)
                    else "⏸ غیرفعال‌سازی موقت تعرفه",
                    f"admin:set_plan_active:{code}:{0 if int(plan.get('is_active', 1) or 0) else 1}")],
        ]
        rows.append([
            button(_checkbox_label(catalog.PLAN_KIND_FA[option], option == kind),
                   _checkbox_callback("v52:pk:", code, option))
            for option in catalog.PLAN_KINDS
        ])
        rows.append([
            button(_checkbox_label("👤 کاربران نهایی", audience["for_end_users"]),
                   _checkbox_callback("v52:pa:", code, "end_user")),
            button(_checkbox_label("🧑‍💼 نمایندگان", audience["for_reps"]),
                   _checkbox_callback("v52:pa:", code, "rep")),
        ])
        for category in categories:
            rows.append([button(
                _checkbox_label(f"{category['emoji']} {category['title_fa']}",
                                str(category["category_key"]) in linked),
                _checkbox_callback("v52:pc:", code, str(category["category_key"])),
            )])
        rows.append([button("📂 مدیریت دسته‌بندی‌های تعرفه", "v52:plancats")])
        for binding in bindings:
            rows.append([button(
                _checkbox_label(f"🖥 {binding['name']}", bool(int(binding["allowed"]))),
                _checkbox_callback("v52:pp:", code, str(int(binding["panel_id"]))),
            )])
        rows.append([button("🖥 مدیریت پنل‌ها", "v52:panels")])
        rows.append([button("🗑 حذف این تعرفه", f"admin:confirm_del_plan:{code}")])
        return render_page(
            f"🏷 کارت تعرفهٔ {plan['name']}", "\n".join(lines),
            persona="admin", rows=rows, parent_callback=parent_callback or "v52:plans")
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def plan_categories_page(ctx, *, parent_callback: str | None = None):
    """List every tariff category with its edit and delete controls."""
    try:
        _require_root(ctx)
        catalog = _catalog()
        with _db() as conn:
            categories = catalog.list_plan_categories(conn)
        lines = [
            f"• تعداد دسته‌بندی‌ها: {len(categories)}",
            "",
            "هر تعرفه می‌تواند به چند دسته‌بندی متصل باشد. دسته‌بندی «اصلی» هر تعرفه، "
            "گروهی است که آن تعرفه زیر آن در پورتال کاربری و میزکار نمایندگی نمایش داده "
            "می‌شود؛ تعرفه‌های بدون دسته‌بندی زیر «همه بسته‌ها» می‌آیند.",
            "",
            "برای ویرایش عنوان، نماد یا ترتیب هر دسته‌بندی روی آن بزنید:",
        ]
        rows: list[list] = [[button("➕ افزودن دسته‌بندی جدید", "v52:plancatnew")]]
        for category in categories:
            state = "🟢" if int(category["is_active"]) else "🔴"
            rows.append([
                button(f"{state} ✏️ {category['emoji']} {category['title_fa']}"[:60],
                       f"v52:plancatedit:{category['category_key']}"),
                button("🗑", f"v52:plancatdel:{category['category_key']}"),
            ])
        return render_page(
            "📂 مدیریت دسته‌بندی‌های تعرفه", "\n".join(lines),
            persona="admin", rows=rows, parent_callback=parent_callback or "v52:plans")
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def begin_plan_category_new(ctx, *, parent_callback: str | None = None):
    """Arm the text input for a brand-new tariff category."""
    try:
        _require_root(ctx)
        if not _private(ctx):
            raise PermissionError("تعریف دسته‌بندی جدید فقط در پیوی مدیریت انجام می‌شود.")
        import time
        import v52_router
        v52_router.DEFAULT_FSM.set(
            ctx, "plan_category_new", payload={},
            parent_callback=parent_callback or "v52:plancats", now=int(time.time()))
        return render_page(
            "➕ دسته‌بندی تعرفهٔ جدید", PLAN_CATEGORY_NEW_FORMAT,
            persona="admin", page_kind="form",
            rows=[[button("بازگشت به دسته‌بندی‌ها", parent_callback or "v52:plancats")]],
            parent_callback=parent_callback or "v52:plancats")
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def apply_plan_category_new(ctx, value, *, parent_callback: str | None = None):
    """Persist a new tariff category from the ``key | title | emoji`` line."""
    try:
        _require_root(ctx)
        parts = _split(value)
        if len(parts) < 2:
            raise ValueError(PLAN_CATEGORY_NEW_FORMAT)
        catalog = _catalog()
        with _db() as conn:
            created = catalog.create_plan_category(
                conn, category_key=parts[0], title_fa=parts[1],
                emoji=parts[2] if len(parts) > 2 else "")
            conn.commit()
        return render_page(
            "✅ دسته‌بندی ثبت شد",
            f"{created['emoji']} {created['title_fa']}\n"
            f"کد دسته‌بندی: {created['category_key']}\n\n"
            "از این پس می‌توانید این دسته‌بندی را روی کارت هر تعرفه فعال کنید.",
            persona="admin", page_kind="result",
            rows=[[button("بازگشت به دسته‌بندی‌ها", parent_callback or "v52:plancats")]],
            parent_callback=parent_callback or "v52:plancats")
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def begin_plan_category_edit(ctx, category_key, *, parent_callback: str | None = None):
    """Arm the text input for editing one tariff category."""
    try:
        _require_root(ctx)
        if not _private(ctx):
            raise PermissionError("ویرایش دسته‌بندی فقط در پیوی مدیریت انجام می‌شود.")
        catalog = _catalog()
        with _db() as conn:
            current = catalog.get_plan_category(conn, category_key)
            linked = int(conn.execute(
                "SELECT COUNT(*) FROM plan_category_links WHERE category_key=?",
                (str(category_key),)).fetchone()[0])
        if current is None:
            raise ValueError("دسته‌بندی موردنظر یافت نشد.")
        import time
        import v52_router
        v52_router.DEFAULT_FSM.set(
            ctx, "plan_category_edit",
            payload={"category_key": str(current["category_key"])},
            parent_callback=parent_callback or "v52:plancats", now=int(time.time()))
        body = (
            f"{current['emoji']} {current['title_fa']}\n"
            f"کد دسته‌بندی: {current['category_key']}\n"
            f"ترتیب نمایش: {current['sort_order']}\n"
            f"وضعیت: {'🟢 فعال' if int(current['is_active']) else '🔴 غیرفعال'}\n"
            f"تعداد تعرفه‌های متصل: {linked}\n\n" + PLAN_CATEGORY_EDIT_FORMAT
        )
        return render_page(
            "✏️ ویرایش دسته‌بندی تعرفه", body, persona="admin", page_kind="form",
            rows=[[button("بازگشت به دسته‌بندی‌ها", parent_callback or "v52:plancats")]],
            parent_callback=parent_callback or "v52:plancats")
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def apply_plan_category_edit(ctx, payload, value, *, parent_callback: str | None = None):
    """Persist edits from the ``title | emoji | sort | active`` line."""
    try:
        _require_root(ctx)
        key = str((payload or {}).get("category_key") or "").strip()
        parts = _split(value)
        while len(parts) < 4:
            parts.append("")
        title, emoji, order, active = parts[:4]
        fields: dict = {}
        if title:
            fields["title_fa"] = title
        if emoji:
            fields["emoji"] = emoji
        if order:
            fields["sort_order"] = _optional_int(order, "ترتیب نمایش")
        if active:
            parsed = _optional_int(active, "وضعیت")
            if parsed not in (0, 1):
                raise ValueError("وضعیت باید ۰ (غیرفعال) یا ۱ (فعال) باشد.")
            fields["is_active"] = bool(parsed)
        if not fields:
            raise ValueError("هیچ تغییری ارسال نشد.\n\n" + PLAN_CATEGORY_EDIT_FORMAT)
        catalog = _catalog()
        with _db() as conn:
            updated = catalog.update_plan_category(conn, key, **fields)
            conn.commit()
        return render_page(
            "✅ دسته‌بندی به‌روز شد",
            f"{updated['emoji']} {updated['title_fa']}\n"
            f"کد دسته‌بندی: {updated['category_key']}\n"
            f"ترتیب نمایش: {updated['sort_order']}\n"
            f"وضعیت: {'🟢 فعال' if int(updated['is_active']) else '🔴 غیرفعال'}",
            persona="admin", page_kind="result",
            rows=[[button("بازگشت به دسته‌بندی‌ها", parent_callback or "v52:plancats")]],
            parent_callback=parent_callback or "v52:plancats")
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def delete_plan_category(ctx, category_key, *, parent_callback: str | None = None):
    """Delete one tariff category and every plan link that pointed at it."""
    try:
        _require_root(ctx)
        catalog = _catalog()
        with _db() as conn:
            result = catalog.delete_plan_category(conn, category_key)
            conn.commit()
        unlinked = result.get("unlinked_plans") or []
        return render_page(
            "🗑 دسته‌بندی حذف شد",
            f"کد دسته‌بندی: {result['category_key']}\n"
            f"تعداد تعرفه‌های جدا‌شده: {len(unlinked)}\n\n"
            + ("تعرفه‌های زیر از این دسته‌بندی جدا شدند: " + ", ".join(unlinked)
               if unlinked else "هیچ تعرفه‌ای به این دسته‌بندی متصل نبود."),
            persona="admin", page_kind="result",
            rows=[[button("بازگشت به دسته‌بندی‌ها", parent_callback or "v52:plancats")]],
            parent_callback=parent_callback or "v52:plancats")
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def toggle_plan_category(ctx, plan_code, category_key, *, parent_callback: str | None = None):
    """Flip one plan/category checkbox and re-render the plan card."""
    try:
        _require_root(ctx)
        catalog = _catalog()
        with _db() as conn:
            catalog.toggle_plan_category(conn, plan_code, category_key)
            conn.commit()
        return plan_detail(ctx, plan_code, parent_callback=parent_callback)
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def toggle_plan_panel(ctx, plan_code, panel_id, *, parent_callback: str | None = None):
    """Flip one plan/panel checkbox and re-render the plan card."""
    try:
        _require_root(ctx)
        catalog = _catalog()
        with _db() as conn:
            current = catalog.plan_panel_bindings(conn, plan_code)
            allowed = next(
                (bool(int(item["allowed"])) for item in current
                 if int(item["panel_id"]) == int(panel_id)), False)
            catalog.set_plan_panel_allowed(conn, plan_code, panel_id, not allowed)
            conn.commit()
        return plan_detail(ctx, plan_code, parent_callback=parent_callback)
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def toggle_plan_audience(ctx, plan_code, audience, *, parent_callback: str | None = None):
    """Flip one audience checkbox and re-render the plan card."""
    try:
        _require_root(ctx)
        catalog = _catalog()
        selected = str(audience or "").strip().lower()
        field = {"end_user": "for_end_users", "rep": "for_reps"}.get(selected)
        if field is None:
            raise ValueError("مخاطب درخواستی معتبر نیست.")
        with _db() as conn:
            current = catalog.plan_audience(conn, plan_code)
            catalog.set_plan_audience(conn, plan_code, **{field: not current[field]})
            conn.commit()
        return plan_detail(ctx, plan_code, parent_callback=parent_callback)
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def apply_plan_kind(ctx, plan_code, kind, *, parent_callback: str | None = None):
    """Set one tariff kind and re-render the plan card."""
    try:
        _require_root(ctx)
        catalog = _catalog()
        with _db() as conn:
            catalog.set_plan_kind(conn, plan_code, kind)
            conn.commit()
        return plan_detail(ctx, plan_code, parent_callback=parent_callback)
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


# --------------------------------------------------------------------------- #
# Coupons
# --------------------------------------------------------------------------- #

def coupons_page(ctx, *, parent_callback: str | None = None):
    """List every coupon with its live spend, budget and usage counters."""
    try:
        _require_root(ctx)
        import time
        promotions = _promotions()
        with _db() as conn:
            coupons = promotions.list_coupons(conn)
        now = int(time.time())
        lines = [
            f"• تعداد کوپن‌ها: {len(coupons)}",
            "",
            "کوپن‌ها از بودجهٔ صادرکننده (مدیریت کل) پرداخت می‌شوند: تخفیف هر سفارش از "
            "سقف بودجه و از حاشیهٔ همان سفارش کسر می‌شود، بنابراین یک کوپن هرگز بیشتر "
            "از سود سفارش تخفیف نمی‌دهد.",
            "",
        ]
        rows: list[list] = [[button("➕ افزودن کوپن جدید", "v52:couponnew")]]
        if not coupons:
            lines.append("هنوز هیچ کوپنی ثبت نشده است.")
        for coupon in coupons:
            state = "🟢" if coupon["is_active"] else "🔴"
            expired = "⌛️ منقضی" if int(coupon["expires_at"]) <= now else ""
            lines.append(
                f"{state} {coupon['code']} — تخفیف {_money(coupon['discount_toman'])} تومان | "
                f"بودجه {_money(coupon['budget_toman'])} | "
                f"استفاده {coupon['used_uses']}/{coupon['max_uses']} "
                f"(در انتظار: {coupon['held_uses']}) {expired}"
            )
            rows.append([
                button(f"✏️ {coupon['code']}"[:60], f"v52:couponedit:{coupon['code']}"),
                button("⏸" if coupon["is_active"] else "🟢",
                       f"v52:coupontoggle:{coupon['code']}"),
                button("🗑", f"v52:coupondel:{coupon['code']}"),
            ])
        return render_page(
            "🎁 کدهای تخفیف و کوپن‌ها", "\n".join(lines),
            persona="admin", rows=rows, parent_callback=parent_callback or "v52:home:admin")
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def begin_coupon_new(ctx, *, parent_callback: str | None = None):
    """Arm the text input for a brand-new coupon."""
    try:
        _require_root(ctx)
        if not _private(ctx):
            raise PermissionError("تعریف کوپن جدید فقط در پیوی مدیریت انجام می‌شود.")
        import time
        import v52_router
        v52_router.DEFAULT_FSM.set(
            ctx, "coupon_new", payload={},
            parent_callback=parent_callback or "v52:coupons", now=int(time.time()))
        return render_page(
            "➕ کوپن جدید", COUPON_NEW_FORMAT, persona="admin", page_kind="form",
            rows=[[button("بازگشت به کوپن‌ها", parent_callback or "v52:coupons")]],
            parent_callback=parent_callback or "v52:coupons")
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def apply_coupon_new(ctx, value, *, parent_callback: str | None = None):
    """Persist a new coupon from the ``code | discount | budget | uses | days`` line."""
    try:
        _require_root(ctx)
        import time
        parts = _split(value)
        if len(parts) < 5:
            raise ValueError(COUPON_NEW_FORMAT)
        code, discount, budget, uses, days = parts[:5]
        promotions = _promotions()
        with _db() as conn:
            created = promotions.create_coupon(
                conn, code=code,
                discount_toman=_optional_int(discount, "مبلغ تخفیف"),
                budget_toman=_optional_int(budget, "سقف بودجه"),
                max_uses=_optional_int(uses, "حداکثر تعداد استفاده"),
                expires_at=int(time.time()) + max(_optional_int(days, "اعتبار") or 0, 1) * 86400,
            )
            conn.commit()
        return render_page(
            "✅ کوپن ثبت شد",
            f"کد کوپن: {created['code']}\n"
            f"مبلغ تخفیف: {_money(created['discount_toman'])} تومان\n"
            f"سقف بودجه: {_money(created['budget_toman'])} تومان\n"
            f"حداکثر استفاده: {created['max_uses']}\n"
            f"وضعیت: 🟢 فعال",
            persona="admin", page_kind="result",
            rows=[[button("بازگشت به کوپن‌ها", parent_callback or "v52:coupons")]],
            parent_callback=parent_callback or "v52:coupons")
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def begin_coupon_edit(ctx, code, *, parent_callback: str | None = None):
    """Arm the text input for editing one coupon."""
    try:
        _require_root(ctx)
        if not _private(ctx):
            raise PermissionError("ویرایش کوپن فقط در پیوی مدیریت انجام می‌شود.")
        import time
        promotions = _promotions()
        with _db() as conn:
            current = promotions.coupon_row(conn, code)
        if current is None:
            raise ValueError("کوپن موردنظر یافت نشد.")
        import v52_router
        v52_router.DEFAULT_FSM.set(
            ctx, "coupon_edit", payload={"code": current["code"]},
            parent_callback=parent_callback or "v52:coupons", now=int(time.time()))
        body = (
            f"کد کوپن: {current['code']}\n"
            f"مبلغ تخفیف فعلی: {_money(current['discount_toman'])} تومان\n"
            f"سقف بودجه فعلی: {_money(current['budget_toman'])} تومان\n"
            f"حداکثر استفاده: {current['max_uses']}\n"
            f"مبلغ استفاده‌شده: {_money(current['used_toman'])} تومان\n"
            f"وضعیت: {'🟢 فعال' if current['is_active'] else '🔴 غیرفعال'}\n\n"
            + COUPON_EDIT_FORMAT
        )
        return render_page(
            "✏️ ویرایش کوپن", body, persona="admin", page_kind="form",
            rows=[[button("بازگشت به کوپن‌ها", parent_callback or "v52:coupons")]],
            parent_callback=parent_callback or "v52:coupons")
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def apply_coupon_edit(ctx, payload, value, *, parent_callback: str | None = None):
    """Persist edits from the ``discount | budget | uses | days`` line."""
    try:
        _require_root(ctx)
        import time
        code = str((payload or {}).get("code") or "").strip()
        parts = _split(value)
        while len(parts) < 4:
            parts.append("")
        discount, budget, uses, days = parts[:4]
        fields: dict = {}
        if discount:
            fields["discount_toman"] = _optional_int(discount, "مبلغ تخفیف")
        if budget:
            fields["budget_toman"] = _optional_int(budget, "سقف بودجه")
        if uses:
            fields["max_uses"] = _optional_int(uses, "حداکثر تعداد استفاده")
        if days:
            fields["expires_at"] = int(time.time()) + max(
                _optional_int(days, "اعتبار") or 0, 1) * 86400
        if not fields:
            raise ValueError("هیچ تغییری ارسال نشد.\n\n" + COUPON_EDIT_FORMAT)
        promotions = _promotions()
        with _db() as conn:
            updated = promotions.update_coupon(conn, code, **fields)
            conn.commit()
        return render_page(
            "✅ کوپن به‌روز شد",
            f"کد کوپن: {updated['code']}\n"
            f"مبلغ تخفیف: {_money(updated['discount_toman'])} تومان\n"
            f"سقف بودجه: {_money(updated['budget_toman'])} تومان\n"
            f"حداکثر استفاده: {updated['max_uses']}\n"
            f"وضعیت: {'🟢 فعال' if updated['is_active'] else '🔴 غیرفعال'}",
            persona="admin", page_kind="result",
            rows=[[button("بازگشت به کوپن‌ها", parent_callback or "v52:coupons")]],
            parent_callback=parent_callback or "v52:coupons")
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def toggle_coupon(ctx, code, *, parent_callback: str | None = None):
    """Enable or retire one coupon and re-render the list."""
    try:
        _require_root(ctx)
        promotions = _promotions()
        with _db() as conn:
            current = promotions.coupon_row(conn, code)
            if current is None:
                raise ValueError("کوپن موردنظر یافت نشد.")
            promotions.set_coupon_active(conn, code, not current["is_active"])
            conn.commit()
        return coupons_page(ctx, parent_callback=parent_callback)
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def delete_coupon(ctx, code, *, parent_callback: str | None = None):
    """Delete one coupon that no order has held or used."""
    try:
        _require_root(ctx)
        promotions = _promotions()
        with _db() as conn:
            promotions.delete_coupon(conn, code)
            conn.commit()
        return coupons_page(ctx, parent_callback=parent_callback)
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


# --------------------------------------------------------------------------- #
# Admin-initiated order refunds
# --------------------------------------------------------------------------- #

def refunds_page(ctx, *, parent_callback: str | None = None):
    """List refundable orders and the refund requests already recorded."""
    try:
        _require_root(ctx)
        placeholders = ",".join("?" for _ in REFUNDABLE_ORDER_STATES)
        with _db() as conn:
            orders = conn.execute(
                "SELECT order_id,actor_tg_id,merchant_key,purpose,amount_toman,status,created_at "
                f"FROM payment_orders WHERE status IN ({placeholders}) "
                "ORDER BY created_at DESC,order_id DESC LIMIT 20",
                REFUNDABLE_ORDER_STATES,
            ).fetchall()
            requests = conn.execute(
                "SELECT refund_id,order_id,user_tg_id,amount_toman,state,created_at "
                "FROM refund_requests ORDER BY created_at DESC,refund_id DESC LIMIT 10"
            ).fetchall()
        lines = [
            "این صفحه عودت وجه سفارش‌های پرداخت‌شده را ثبت می‌کند. ثبت درخواست، مبلغ را "
            "بلافاصله از موجودی قابل‌خرج کاربر نگه می‌دارد و تا تسویهٔ بانکی به همان "
            "وضعیت «در حال پردازش» می‌ماند.",
            "",
            f"• سفارش‌های قابل عودت (آخرین {len(orders)} مورد):",
        ]
        rows: list[list] = []
        if not orders:
            lines.append("   موردی یافت نشد.")
        for order in orders:
            lines.append(
                f"   ◦ {order['order_id']} | کاربر {int(order['actor_tg_id'])} | "
                f"{_money(order['amount_toman'])} تومان | وضعیت {order['status']}"
            )
            rows.append([button(
                f"↩️ عودت {str(order['order_id'])[:24]} · {_money(order['amount_toman'])} ت"[:60],
                f"v52:refundorder:{order['order_id']}")])
        lines.append("")
        lines.append(f"• درخواست‌های عودت ثبت‌شده (آخرین {len(requests)} مورد):")
        if not requests:
            lines.append("   موردی ثبت نشده است.")
        for request in requests:
            lines.append(
                f"   ◦ {request['refund_id']} | کاربر {int(request['user_tg_id'])} | "
                f"{_money(request['amount_toman'])} تومان | وضعیت {request['state']}"
            )
        return render_page(
            "↩️ لغو و عودت وجه سفارش", "\n".join(lines),
            persona="admin", rows=rows, parent_callback=parent_callback or "v52:home:admin")
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def preview_order_refund(ctx, order_id, *, parent_callback: str | None = None):
    """Preview one order refund and offer the destination step."""
    try:
        _require_root(ctx)
        with _db() as conn:
            order = conn.execute(
                "SELECT order_id,actor_tg_id,merchant_key,purpose,amount_toman,status "
                "FROM payment_orders WHERE order_id=?", (str(order_id),)).fetchone()
            existing = conn.execute(
                "SELECT refund_id,state,amount_toman FROM refund_requests WHERE order_id=? "
                "ORDER BY created_at DESC", (str(order_id),)).fetchall()
        if order is None:
            raise ValueError("سفارش موردنظر یافت نشد.")
        if str(order["status"]) not in REFUNDABLE_ORDER_STATES:
            raise ValueError("این سفارش در وضعیت قابل عودت نیست.")
        lines = [
            f"• شناسه سفارش: {order['order_id']}",
            f"• پرداخت‌کننده: {int(order['actor_tg_id'])}",
            f"• فروشنده: {order['merchant_key']}",
            f"• مبلغ سفارش: {_money(order['amount_toman'])} تومان",
            f"• وضعیت سفارش: {order['status']}",
        ]
        if existing:
            lines.append("• درخواست‌های عودت قبلی این سفارش:")
            for row in existing:
                lines.append(
                    f"   ◦ {row['refund_id']} | {_money(row['amount_toman'])} تومان | {row['state']}")
        else:
            lines.append("• درخواست عودت قبلی: ندارد")
        lines.extend([
            "",
            "با زدن دکمهٔ زیر، مبلغ کامل سفارش از موجودی قابل‌خرج کاربر نگه داشته می‌شود و "
            "درخواست عودت بانکی ثبت می‌گردد. سپس شماره کارت یا شبای مقصد را ارسال می‌کنید.",
        ])
        return render_page(
            "↩️ پیش‌نمایش عودت وجه سفارش", "\n".join(lines),
            persona="admin",
            rows=[[button("ثبت درخواست عودت", f"v52:refundstart:{order['order_id']}")]],
            parent_callback=parent_callback or "v52:refunds")
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def begin_refund_destination(ctx, order_id, *, parent_callback: str | None = None):
    """Arm the text input for the refund destination bank account."""
    try:
        _require_root(ctx)
        if not _private(ctx):
            raise PermissionError("ثبت عودت وجه فقط در پیوی مدیریت انجام می‌شود.")
        with _db() as conn:
            order = conn.execute(
                "SELECT order_id,actor_tg_id,merchant_key,amount_toman,status "
                "FROM payment_orders WHERE order_id=?", (str(order_id),)).fetchone()
        if order is None:
            raise ValueError("سفارش موردنظر یافت نشد.")
        if str(order["status"]) not in REFUNDABLE_ORDER_STATES:
            raise ValueError("این سفارش در وضعیت قابل عودت نیست.")
        import time
        import v52_router
        v52_router.DEFAULT_FSM.set(
            ctx, "refund_destination",
            payload={"order_id": str(order["order_id"]),
                     "user_tg_id": int(order["actor_tg_id"]),
                     "merchant_key": str(order["merchant_key"]),
                     "amount_toman": int(order["amount_toman"])},
            parent_callback=parent_callback or "v52:refunds", now=int(time.time()))
        return render_page(
            "↩️ مقصد عودت وجه", REFUND_DESTINATION_FORMAT,
            persona="admin", page_kind="form",
            rows=[[button("بازگشت به سفارش", f"v52:refundorder:{order['order_id']}")]],
            parent_callback=parent_callback or "v52:refunds")
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def apply_refund_destination(ctx, payload, value, *, parent_callback: str | None = None):
    """Create the refund request and its wallet hold from the typed destination."""
    try:
        _require_root(ctx)
        import refund_service
        import time
        state = dict(payload or {})
        order_id = str(state.get("order_id") or "").strip()
        destination = str(value or "").strip()
        if not order_id:
            raise ValueError("شناسه سفارش در وضعیت فرم یافت نشد؛ از فهرست عودت‌ها دوباره شروع کنید.")
        if not destination:
            raise ValueError("مقصد عودت نمی‌تواند خالی باشد.")
        if len(destination) > 120:
            raise ValueError("مقصد عودت بیش از حد بلند است.")
        with _db() as conn:
            result = refund_service.request_bank_refund(
                conn,
                refund_id=f"admin-refund:{order_id}",
                merchant_key=str(state.get("merchant_key") or ""),
                user_tg_id=int(state.get("user_tg_id") or 0),
                requester_tg_id=_actor(ctx),
                amount_toman=int(state.get("amount_toman") or 0),
                bank_details={"destination": destination, "channel": "admin_panel"},
                now=int(time.time()),
                order_id=order_id,
            )
        return render_page(
            "✅ درخواست عودت ثبت شد",
            f"• شناسه درخواست: {result['refund_id']}\n"
            f"• مبلغ: {_money(result.get('amount_toman', 0))} تومان\n"
            f"• وضعیت: {result['state']}\n"
            + ("• این درخواست قبلاً ثبت شده بود و دوباره اجرا نشد."
               if result.get("replayed") else "")
            + "\n\nمبلغ تا تسویهٔ بانکی از موجودی قابل‌خرج کاربر نگه داشته شده است.",
            persona="admin", page_kind="result",
            rows=[[button("بازگشت به فهرست عودت‌ها", parent_callback or "v52:refunds")]],
            parent_callback=parent_callback or "v52:refunds")
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


__all__ = [
    "CHECKBOX_OFF", "CHECKBOX_ON", "COUPON_EDIT_FORMAT", "COUPON_NEW_FORMAT",
    "PLAN_CATEGORY_EDIT_FORMAT", "PLAN_CATEGORY_NEW_FORMAT", "REFUND_DESTINATION_FORMAT",
    "REFUNDABLE_ORDER_STATES", "apply_coupon_edit", "apply_coupon_new",
    "apply_plan_category_edit", "apply_plan_category_new", "apply_plan_kind",
    "apply_refund_destination", "begin_coupon_edit", "begin_coupon_new",
    "begin_plan_category_edit", "begin_plan_category_new", "begin_refund_destination",
    "coupons_page", "delete_coupon", "delete_plan_category", "edit_bank_card",
    "edit_plan", "plan_categories_page", "plan_detail", "preview_order_refund",
    "preview_settlement", "refunds_page", "tariff_hub", "toggle_coupon",
    "toggle_plan_audience", "toggle_plan_category", "toggle_plan_panel",
]
