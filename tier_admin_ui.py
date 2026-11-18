"""Root-only management surface for the tier catalog and its live pricing effect.

``tier_policies`` drives three separate behaviours — the discount applied by
:mod:`pricing_service`, the daily test quota enforced by :mod:`tier_service`, and
the validity check :mod:`rep_tree_service` runs before assigning a tier — but
until now nothing in the product could create, edit or retire a row. This module
is that missing half: a thin, testable page layer over the writers in
:mod:`tier_service`.

It also owns the tier-specific plan price matrix: the tier's flat discount is one
percentage for every plan, while a negotiated wholesale rate is a fixed number for
one plan, so ``tier_plan_prices`` is edited here and resolved through
:func:`pricing_service.resolve_tier_plan_price` — the same resolver the buyer's
menu and the checkout guard use.

Every title, row label and hint on these pages is Persian. A raw column name
(``discount_bps``, ``min_monthly_sales_toman``, …) is never shown to the operator;
the technical code is only ever echoed inside a code span where it is the actual
identifier being edited.
"""
from __future__ import annotations

from collections.abc import Mapping
from html import escape as _html_escape
from typing import Any

from ui_v52 import button, render_error, render_page


def _code(value: Any) -> str:
    """Render one identifier as an escaped code span."""
    return f"<code>{_html_escape(str(value), quote=True)}</code>"


def _html_from_template(text: Any) -> str:
    """Escape a service message, then restore only the ``<code>`` spans it owns.

    ``render_page`` escapes a plain ``body``, so a ``<code>`` tag written into it
    reached the operator as the literal text ``&lt;code&gt;gold&lt;/code&gt;``. Passing
    the message as ``body_html`` after this transformation keeps every dynamic
    value escaped — a service exception can never inject markup — while the code
    spans the page intended to show are rendered as code.
    """
    escaped = _html_escape(str(text), quote=True)
    return escaped.replace("&lt;code&gt;", "<code>").replace("&lt;/code&gt;", "</code>")


#: The ladder is expressed in basis points so a 2.5% discount is exact. The
#: operator types a percentage and the service converts it, which keeps the
#: Persian label («درصد تخفیف») truthful. The constant itself lives in
#: :mod:`tier_service` so the two layers can never drift apart.
NEW_FORMAT = (
    "قالب ثبت سطح جدید:\n"
    "<code>نقش | کد | عنوان فارسی | رتبه | درصد تخفیف | درصد کش‌بک | "
    "سهمیه تست روزانه | سقف اعتبار | حداقل فروش ماهانه</code>\n"
    "مثال:\n"
    "<code>rep | gold | طلایی | 3 | 5 | 1 | 2 | 5000000 | 0</code>\n\n"
    "• نقش یکی از <code>rep</code> (نمایندگی) یا <code>end_user</code> (کاربری) است.\n"
    "• کد فقط با حرف کوچک لاتین شروع می‌شود.\n"
    "• درصد تخفیف و کش‌بک می‌توانند اعشاری باشند (مثلاً 2.5).\n"
    "• فیلدهای پایانی را می‌توانید حذف کنید تا مقدار پیش‌فرض بگیرند."
)

EDIT_FORMAT = (
    "قالب ویرایش:\n"
    "<code>عنوان فارسی | رتبه | درصد تخفیف | درصد کش‌بک | سهمیه تست | "
    "سقف اعتبار | حداقل فروش</code>\n"
    "مثال:\n"
    "<code>طلایی | 3 | 7.5 | 1 | 3 | 10000000 | 0</code>\n\n"
    "هر بخش را می‌توانید خالی بگذارید تا تغییر نکند."
)


def _value(obj: Any, key: str, default=None):
    return obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)


def _db():
    import database
    return database.get_db()


def _service():
    import tier_service
    return tier_service


def _root(ctx: Any) -> bool:
    return bool(_value(ctx, "is_root", False))


def _private(ctx: Any) -> bool:
    return str(_value(ctx, "chat_type", "private")).lower() == "private"


def _require_root(ctx: Any) -> None:
    if not _root(ctx):
        raise PermissionError("مدیریت سطوح و قیمت‌گذاری فقط در اختیار مدیر کل است.")


def _percent(bps: Any) -> str:
    """Render basis points as a trimmed Persian percentage."""
    try:
        value = int(bps) / _service().BPS_PER_PERCENT
    except (TypeError, ValueError):
        return "0%"
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{text}%"


def _split(value: Any) -> list[str]:
    return [chunk.strip() for chunk in str(value or "").split("|")]


def _optional_int(chunk: str, label: str, *, percent: bool = False):
    """Parse one optional field; an empty chunk means «leave unchanged»."""
    if not chunk.strip():
        return None
    service = _service()
    if percent:
        return service.parse_percent_bps(chunk, label)
    return service.parse_int(chunk, label)


def _tier_lines(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "هیچ سطحی ثبت نشده است."
    service = _service()
    lines = [
        "🎖 سطوح کاربری و قیمت‌گذاری",
        "",
        "تخفیف هر سطح بی‌درنگ روی محاسبهٔ قیمت خرید و تمدید همان سطح اعمال می‌شود.",
        "",
    ]
    for actor_type in service.ACTOR_TYPES:
        group = [row for row in rows if row["actor_type"] == actor_type]
        if not group:
            continue
        lines.append(f"▫️ سطوح {service.ACTOR_TYPE_FA.get(actor_type, actor_type)}:")
        for row in group:
            lines.append(
                f"   • {row['title_fa']} — کد {_code(row['tier_code'])} · "
                f"رتبه {row['rank']} · تخفیف {_percent(row['discount_bps'])} · "
                f"کش‌بک {_percent(row['cashback_bps'])} · "
                f"سهمیه تست روزانه {row['daily_test_quota']}"
            )
        lines.append("")
    return "\n".join(lines).rstrip()


def list_tiers(ctx: Any, *, parent_callback: str | None = "v52:home:admin"):
    """List every tier policy with its edit and retire controls."""
    try:
        _require_root(ctx)
        with _db() as conn:
            rows = _service().list_tiers(conn)
        buttons = [[button("➕ افزودن سطح جدید", "v52:tier:new")]]
        for row in rows:
            label = (
                f"✏️ {row['title_fa']} · "
                f"{_service().ACTOR_TYPE_FA.get(row['actor_type'], row['actor_type'])}"
            )
            buttons.append([
                button(label[:30], f"v52:tier:edit:{row['actor_type']}:{row['tier_code']}"),
                button("🗑", f"v52:tier:del:{row['actor_type']}:{row['tier_code']}"),
            ])
        buttons.append([button("بازگشت به میز مدیر کل", "v52:home:admin")])
        return render_page(
            "🎖 سطوح کاربری و قیمت‌گذاری (Tiers)", body_html=_tier_lines(rows),
            persona="admin", rows=buttons, parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback,
                            body_html=_html_from_template(exc))


def begin_tier_new(ctx: Any, *, parent_callback: str | None = "v52:tiers"):
    """Arm the text input for a brand-new tier policy."""
    try:
        _require_root(ctx)
        if not _private(ctx):
            raise PermissionError("تعریف سطح جدید فقط در پیوی مدیریت انجام می‌شود.")
        import time
        import v52_router
        v52_router.DEFAULT_FSM.set(
            ctx, "tier_new", payload={}, parent_callback=parent_callback,
            now=int(time.time()))
        return render_page(
            "➕ سطح جدید", body_html=_html_from_template(NEW_FORMAT),
            persona="admin", page_kind="form",
            rows=[[button("بازگشت به فهرست سطوح", parent_callback or "v52:tiers")]],
            parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback,
                            body_html=_html_from_template(exc))


def apply_tier_new(ctx: Any, value: Any, *, parent_callback: str | None = "v52:tiers"):
    """Persist a new tier from the ``role | code | title | …`` line."""
    try:
        _require_root(ctx)
        parts = _split(value)
        if len(parts) < 3:
            raise ValueError(NEW_FORMAT)
        actor_type, tier_code, title = parts[0], parts[1], parts[2]
        rest = parts[3:]
        while len(rest) < 6:
            rest.append("")
        rank, discount, cashback, quota, credit, min_sales = rest[:6]
        service = _service()
        with _db() as conn:
            created = service.create_tier(
                conn,
                actor_type=actor_type,
                tier_code=tier_code,
                title_fa=title,
                rank=rank or 0,
                discount_bps=_optional_int(discount, "درصد تخفیف", percent=True) or 0,
                cashback_bps=_optional_int(cashback, "درصد کش‌بک", percent=True) or 0,
                daily_test_quota=quota if quota.strip() else 1,
                default_credit_limit=credit or 0,
                min_monthly_sales_toman=min_sales or 0,
            )
            conn.commit()
        body = (
            f"{created['title_fa']} — کد {_code(created['tier_code'])}\n"
            f"نقش هدف: {service.ACTOR_TYPE_FA.get(created['actor_type'], created['actor_type'])}\n"
            f"رتبه: {created['rank']}\n"
            f"تخفیف تعرفه‌ها: {_percent(created['discount_bps'])}\n"
            f"کش‌بک: {_percent(created['cashback_bps'])}\n"
            f"سهمیه تست روزانه: {created['daily_test_quota']}\n\n"
            "از این پس می‌توانید این سطح را به کاربران یا نمایندگان اختصاص دهید."
        )
        return render_page(
            "✅ سطح جدید ثبت شد", body_html=_html_from_template(body),
            persona="admin", page_kind="result",
            rows=[[button("بازگشت به فهرست سطوح", parent_callback or "v52:tiers")]],
            parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback,
                            body_html=_html_from_template(exc))


def begin_tier_edit(ctx: Any, actor_type: str, code: str, *,
                    parent_callback: str | None = "v52:tiers"):
    """Arm the text input for editing one tier policy."""
    try:
        _require_root(ctx)
        if not _private(ctx):
            raise PermissionError("ویرایش سطح فقط در پیوی مدیریت انجام می‌شود.")
        service = _service()
        with _db() as conn:
            current = service.get_tier(conn, actor_type, code)
            usage = service.tier_usage(conn, actor_type, code) if current else None
        if current is None:
            raise ValueError("سطح موردنظر یافت نشد.")
        import time
        import v52_router
        v52_router.DEFAULT_FSM.set(
            ctx, "tier_edit",
            payload={"actor_type": current["actor_type"], "tier_code": current["tier_code"]},
            parent_callback=parent_callback, now=int(time.time()))
        body = (
            f"ویرایش سطح {current['title_fa']}\n"
            f"کد: {_code(current['tier_code'])}\n"
            f"نقش هدف: {service.ACTOR_TYPE_FA.get(current['actor_type'], current['actor_type'])}\n"
            f"رتبه فعلی: {current['rank']}\n"
            f"تخفیف فعلی: {_percent(current['discount_bps'])}\n"
            f"کش‌بک فعلی: {_percent(current['cashback_bps'])}\n"
            f"سهمیه تست روزانه: {current['daily_test_quota']}\n"
            f"سقف اعتبار پیش‌فرض: {current['default_credit_limit']:,} تومان\n"
            f"حداقل فروش ماهانه: {current['min_monthly_sales_toman']:,} تومان\n"
            f"تعداد استفاده‌کنندهٔ فعلی: {usage['total']}\n\n"
            + EDIT_FORMAT
        )
        return render_page(
            "✏️ ویرایش سطح", body_html=_html_from_template(body),
            persona="admin", page_kind="form",
            rows=[[button("🏷 تعیین قیمت اختصاصی تعرفه‌ها برای این سطح",
                          f"v52:tierprices:{current['actor_type']}:{current['tier_code']}")],
                  [button("بازگشت به فهرست سطوح", parent_callback or "v52:tiers")]],
            parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback,
                            body_html=_html_from_template(exc))


def apply_tier_edit(ctx: Any, payload: Mapping[str, Any], value: Any, *,
                    parent_callback: str | None = "v52:tiers"):
    """Persist edits from the ``title | rank | discount | …`` line."""
    try:
        _require_root(ctx)
        actor_type = str(payload.get("actor_type") or "").strip()
        code = str(payload.get("tier_code") or "").strip()
        parts = _split(value)
        while len(parts) < 7:
            parts.append("")
        title, rank, discount, cashback, quota, credit, min_sales = parts[:7]
        fields: dict[str, Any] = {}
        if title:
            fields["title_fa"] = title
        if rank:
            fields["rank"] = _optional_int(rank, "رتبه")
        if discount:
            fields["discount_bps"] = _optional_int(discount, "درصد تخفیف", percent=True)
        if cashback:
            fields["cashback_bps"] = _optional_int(cashback, "درصد کش‌بک", percent=True)
        if quota:
            fields["daily_test_quota"] = _optional_int(quota, "سهمیه تست روزانه")
        if credit:
            fields["default_credit_limit"] = _optional_int(credit, "سقف اعتبار پیش‌فرض")
        if min_sales:
            fields["min_monthly_sales_toman"] = _optional_int(min_sales, "حداقل فروش ماهانه")
        if not fields:
            raise ValueError("هیچ تغییری ارسال نشد.\n\n" + EDIT_FORMAT)
        service = _service()
        with _db() as conn:
            updated = service.update_tier(conn, actor_type, code, **fields)
            conn.commit()
        body = (
            f"{updated['title_fa']} — کد {_code(updated['tier_code'])}\n"
            f"رتبه: {updated['rank']}\n"
            f"تخفیف تعرفه‌ها: {_percent(updated['discount_bps'])}\n"
            f"کش‌بک: {_percent(updated['cashback_bps'])}\n"
            f"سهمیه تست روزانه: {updated['daily_test_quota']}\n"
            f"سقف اعتبار پیش‌فرض: {updated['default_credit_limit']:,} تومان\n"
            f"حداقل فروش ماهانه: {updated['min_monthly_sales_toman']:,} تومان\n\n"
            "تخفیف جدید از همین لحظه روی قیمت‌گذاری اعمال می‌شود."
        )
        return render_page(
            "✅ سطح به‌روز شد", body_html=_html_from_template(body),
            persona="admin", page_kind="result",
            rows=[[button("بازگشت به فهرست سطوح", parent_callback or "v52:tiers")]],
            parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback,
                            body_html=_html_from_template(exc))


def delete_tier(ctx: Any, actor_type: str, code: str, *,
                parent_callback: str | None = "v52:tiers"):
    """Retire one tier policy, refusing while an actor still sits on it."""
    try:
        _require_root(ctx)
        with _db() as conn:
            result = _service().delete_tier(conn, actor_type, code)
            conn.commit()
        body = (
            f"سطح {_code(result['tier_code'])} برای نقش "
            f"{_service().ACTOR_TYPE_FA.get(result['actor_type'], result['actor_type'])} حذف شد."
        )
        return render_page(
            "🗑 سطح حذف شد", body_html=_html_from_template(body),
            persona="admin", page_kind="result",
            rows=[[button("بازگشت به فهرست سطوح", parent_callback or "v52:tiers")]],
            parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback,
                            body_html=_html_from_template(exc))


# --------------------------------------------------------------------------- #
# Tier-specific plan price matrix
#
# The tier's flat discount is one percentage for every plan. A negotiated rate is
# not a percentage, so this page lists every plan with its base price, the price
# the tier's discount produces and the effective price the checkout will charge —
# all three resolved by :func:`pricing_service.resolve_tier_plan_price`, the same
# resolver the buyer's menu and the intent guard use.
# --------------------------------------------------------------------------- #

TIER_PRICE_FORMAT = (
    "قیمت اختصاصی این تعرفه برای این سطح را به تومان ارسال کنید.\n"
    "مثال:\n"
    "380000\n\n"
    "• عدد صفر یعنی این تعرفه برای این سطح رایگان محاسبه شود.\n"
    "• برای بازگرداندن تعرفه به تخفیف درصدی، از دکمهٔ «حذف قیمت اختصاصی» استفاده کنید."
)


def tier_price_matrix(ctx: Any, actor_type: str, code: str, *,
                      parent_callback: str | None = "v52:tiers"):
    """Every plan with this tier's base, discounted and effective price."""
    try:
        _require_root(ctx)
        service = _service()
        with _db() as conn:
            tier = service.get_tier(conn, actor_type, code)
            if tier is None:
                raise ValueError("سطح موردنظر یافت نشد.")
            matrix = service.tier_price_matrix(conn, tier["actor_type"], tier["tier_code"])
            custom = {row["plan_code"]: row["custom_price_toman"]
                      for row in service.list_tier_plan_prices(
                          conn, tier["actor_type"], tier["tier_code"])}
        lines = [
            f"سطح: {tier['title_fa']} · نقش "
            f"{service.ACTOR_TYPE_FA.get(tier['actor_type'], tier['actor_type'])}",
            f"کد سطح: {_code(tier['tier_code'])}",
            f"تخفیف پیش‌فرض این سطح: {_percent(tier['discount_bps'])}",
            f"تعداد قیمت‌های اختصاصی ثبت‌شده: {len(custom)}",
            "",
            "قیمت اختصاصی بر تخفیف درصدی اولویت دارد. اگر برای تعرفه‌ای قیمت اختصاصی ثبت "
            "نشود، همان تخفیف درصدی سطح روی قیمت پایه اعمال می‌شود. اعداد این صفحه همان "
            "اعدادی است که خریدار در منو می‌بیند و کیف پولش از آن کسر می‌شود.",
            "",
        ]
        rows = []
        for row in matrix:
            marker = "🏷" if row["is_custom_price"] else "•"
            lines.append(
                f"{marker} {row['plan_name']} — پایه {row['base_price']:,} ت | "
                f"قیمت مؤثر این سطح {row['effective_price']:,} ت"
                + ("" if not row["is_custom_price"]
                   else f" (اختصاصی: {row['custom_price_toman']:,} ت)")
            )
            row_buttons = [button(
                f"🏷 {row['plan_name']} · {row['effective_price']:,} ت"[:60],
                f"v52:tierprice:{tier['actor_type']}:{tier['tier_code']}:{row['plan_code']}")]
            if row["is_custom_price"]:
                row_buttons.append(button(
                    "🗑 حذف قیمت اختصاصی",
                    f"v52:tierpriceclear:{tier['actor_type']}:{tier['tier_code']}:{row['plan_code']}"))
            rows.append(row_buttons)
        rows.append([button("✏️ ویرایش مشخصات سطح",
                            f"v52:tier:edit:{tier['actor_type']}:{tier['tier_code']}")])
        rows.append([button("بازگشت به فهرست سطوح", parent_callback or "v52:tiers")])
        return render_page(
            "🏷 قیمت اختصاصی تعرفه‌ها برای این سطح",
            body_html=_html_from_template("\n".join(lines)),
            persona="admin", rows=rows, parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback,
                            body_html=_html_from_template(exc))


def begin_tier_plan_price(ctx: Any, actor_type: str, code: str, plan_code: str, *,
                          parent_callback: str | None = "v52:tiers"):
    """Arm the text input for one tier's fixed price on one plan."""
    try:
        _require_root(ctx)
        if not _private(ctx):
            raise PermissionError("تعیین قیمت اختصاصی فقط در پیوی مدیریت انجام می‌شود.")
        import pricing_service
        service = _service()
        with _db() as conn:
            tier = service.get_tier(conn, actor_type, code)
            if tier is None:
                raise ValueError("سطح موردنظر یافت نشد.")
            resolved = pricing_service.resolve_tier_plan_price(
                conn, role_scope=tier["actor_type"], tier_code=tier["tier_code"],
                plan_code=plan_code)
        import time
        import v52_router
        matrix_callback = f"v52:tierprices:{tier['actor_type']}:{tier['tier_code']}"
        v52_router.DEFAULT_FSM.set(
            ctx, "tier_plan_price",
            payload={"actor_type": tier["actor_type"], "tier_code": tier["tier_code"],
                     "plan_code": resolved["plan_code"]},
            parent_callback=matrix_callback, now=int(time.time()))
        body = (
            f"سطح: {tier['title_fa']}\n"
            f"تعرفه: {resolved['plan_name']}\n"
            f"قیمت پایه: {resolved['base_price']:,} تومان\n"
            f"قیمت مؤثر فعلی این سطح: {resolved['effective_price']:,} تومان\n"
            + (f"قیمت اختصاصی فعلی: {resolved['custom_price_toman']:,} تومان\n"
               if resolved["is_custom_price"] else
               f"قیمت اختصاصی فعلی: ثبت نشده (تخفیف {_percent(resolved['discount_bps'])} اعمال می‌شود)\n")
            + "\n" + TIER_PRICE_FORMAT
        )
        return render_page(
            "🏷 تعیین قیمت اختصاصی تعرفه", body_html=_html_from_template(body),
            persona="admin", page_kind="form",
            rows=[[button("بازگشت به ماتریس قیمت", matrix_callback)]],
            parent_callback=matrix_callback,
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback,
                            body_html=_html_from_template(exc))


def apply_tier_plan_price(ctx: Any, payload: Mapping[str, Any], value: Any, *,
                          parent_callback: str | None = "v52:tiers"):
    """Persist one tier's fixed price for one plan."""
    try:
        _require_root(ctx)
        service = _service()
        actor_type = str((payload or {}).get("actor_type") or "").strip()
        code = str((payload or {}).get("tier_code") or "").strip()
        plan_code = str((payload or {}).get("plan_code") or "").strip()
        price = service.parse_price_toman(value)
        with _db() as conn:
            result = service.set_tier_plan_price(
                conn, actor_type, code, plan_code, price)
            conn.commit()
        matrix_callback = f"v52:tierprices:{result['actor_type']}:{result['tier_code']}"
        body = (
            f"تعرفه {_code(result['plan_code'])} برای سطح "
            f"{_code(result['tier_code'])} با قیمت "
            f"{result['custom_price_toman']:,} تومان ثبت شد.\n\n"
            "این مبلغ از همین لحظه در پورتال کاربری و میزکار نمایندگی همان سطح اعمال می‌شود."
        )
        return render_page(
            "✅ قیمت اختصاصی ثبت شد", body_html=_html_from_template(body),
            persona="admin", page_kind="result",
            rows=[[button("بازگشت به ماتریس قیمت", matrix_callback)]],
            parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback,
                            body_html=_html_from_template(exc))


def clear_tier_plan_price(ctx: Any, actor_type: str, code: str, plan_code: str, *,
                          parent_callback: str | None = "v52:tiers"):
    """Remove one tier's fixed price and return the plan to the discount ladder."""
    try:
        _require_root(ctx)
        service = _service()
        with _db() as conn:
            service.clear_tier_plan_price(conn, actor_type, code, plan_code)
            conn.commit()
        return tier_price_matrix(ctx, actor_type, code, parent_callback=parent_callback)
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback,
                            body_html=_html_from_template(exc))


__all__ = [
    "EDIT_FORMAT",
    "NEW_FORMAT",
    "TIER_PRICE_FORMAT",
    "apply_tier_edit",
    "apply_tier_new",
    "apply_tier_plan_price",
    "begin_tier_edit",
    "begin_tier_new",
    "begin_tier_plan_price",
    "clear_tier_plan_price",
    "delete_tier",
    "list_tiers",
    "tier_price_matrix",
]
