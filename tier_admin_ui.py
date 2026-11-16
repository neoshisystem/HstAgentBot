"""Root-only management surface for the tier catalog and its live pricing effect.

``tier_policies`` drives three separate behaviours — the discount applied by
:mod:`pricing_service`, the daily test quota enforced by :mod:`tier_service`, and
the validity check :mod:`rep_tree_service` runs before assigning a tier — but
until now nothing in the product could create, edit or retire a row. This module
is that missing half: a thin, testable page layer over the writers in
:mod:`tier_service`.

Every title, row label and hint on these pages is Persian. A raw column name
(``discount_bps``, ``min_monthly_sales_toman``, …) is never shown to the operator;
the technical code is only ever echoed inside a code span where it is the actual
identifier being edited.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ui_v52 import button, render_error, render_page

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
                f"   • {row['title_fa']} — کد <code>{row['tier_code']}</code> · "
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
            "🎖 سطوح کاربری و قیمت‌گذاری (Tiers)", _tier_lines(rows),
            persona="admin", rows=buttons, parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


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
            "➕ سطح جدید", NEW_FORMAT, persona="admin", page_kind="form",
            rows=[[button("بازگشت به فهرست سطوح", parent_callback or "v52:tiers")]],
            parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


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
        return render_page(
            "✅ سطح جدید ثبت شد",
            f"{created['title_fa']} — کد <code>{created['tier_code']}</code>\n"
            f"نقش هدف: {service.ACTOR_TYPE_FA.get(created['actor_type'], created['actor_type'])}\n"
            f"رتبه: {created['rank']}\n"
            f"تخفیف تعرفه‌ها: {_percent(created['discount_bps'])}\n"
            f"کش‌بک: {_percent(created['cashback_bps'])}\n"
            f"سهمیه تست روزانه: {created['daily_test_quota']}\n\n"
            "از این پس می‌توانید این سطح را به کاربران یا نمایندگان اختصاص دهید.",
            persona="admin", page_kind="result",
            rows=[[button("بازگشت به فهرست سطوح", parent_callback or "v52:tiers")]],
            parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


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
            f"کد: <code>{current['tier_code']}</code>\n"
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
            "✏️ ویرایش سطح", body, persona="admin", page_kind="form",
            rows=[[button("بازگشت به فهرست سطوح", parent_callback or "v52:tiers")]],
            parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


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
        return render_page(
            "✅ سطح به‌روز شد",
            f"{updated['title_fa']} — کد <code>{updated['tier_code']}</code>\n"
            f"رتبه: {updated['rank']}\n"
            f"تخفیف تعرفه‌ها: {_percent(updated['discount_bps'])}\n"
            f"کش‌بک: {_percent(updated['cashback_bps'])}\n"
            f"سهمیه تست روزانه: {updated['daily_test_quota']}\n"
            f"سقف اعتبار پیش‌فرض: {updated['default_credit_limit']:,} تومان\n"
            f"حداقل فروش ماهانه: {updated['min_monthly_sales_toman']:,} تومان\n\n"
            "تخفیف جدید از همین لحظه روی قیمت‌گذاری اعمال می‌شود.",
            persona="admin", page_kind="result",
            rows=[[button("بازگشت به فهرست سطوح", parent_callback or "v52:tiers")]],
            parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def delete_tier(ctx: Any, actor_type: str, code: str, *,
                parent_callback: str | None = "v52:tiers"):
    """Retire one tier policy, refusing while an actor still sits on it."""
    try:
        _require_root(ctx)
        with _db() as conn:
            result = _service().delete_tier(conn, actor_type, code)
            conn.commit()
        return render_page(
            "🗑 سطح حذف شد",
            f"سطح <code>{result['tier_code']}</code> برای نقش "
            f"{_service().ACTOR_TYPE_FA.get(result['actor_type'], result['actor_type'])} حذف شد.",
            persona="admin", page_kind="result",
            rows=[[button("بازگشت به فهرست سطوح", parent_callback or "v52:tiers")]],
            parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


__all__ = [
    "EDIT_FORMAT",
    "NEW_FORMAT",
    "apply_tier_edit",
    "apply_tier_new",
    "begin_tier_edit",
    "begin_tier_new",
    "delete_tier",
    "list_tiers",
]
