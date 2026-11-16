"""Root-only settings page for the operator-defined ticket statuses.

The four lifecycle states used to be hard-coded, so the root could not add a
state, rename one, change its emoji or retire it. This page is the management
surface for the catalog owned by :mod:`ticket_status_service`.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ui_v52 import button, render_error, render_page

GROUP_FA = {
    "open": "باز (در صف بررسی)",
    "answered": "در حال بررسی / پاسخ‌داده‌شده",
    "closed": "بسته‌شده",
}
GROUP_HINT = (
    "گروه تعیین می‌کند این وضعیت در کدام صف تیکت‌ها دیده شود:\n"
    "• باز — تیکت‌های تازه و ارجاع‌شده\n"
    "• در حال بررسی — تیکت‌هایی که پاسخ گرفته‌اند\n"
    "• بسته‌شده — تیکت‌های پایان‌یافته"
)
NEW_FORMAT = (
    "قالب ثبت وضعیت جدید:\n"
    "<code>کد | عنوان فارسی | ایموجی | گروه</code>\n"
    "مثال:\n"
    "<code>vip_review | بررسی ویژه | ⭐ | answered</code>\n\n"
    "کد فقط با حرف کوچک لاتین شروع می‌شود و می‌تواند شامل حرف کوچک، رقم و زیرخط باشد. "
    "گروه یکی از open، answered یا closed است."
)
EDIT_FORMAT = (
    "قالب ویرایش:\n"
    "<code>عنوان فارسی | ایموجی | گروه | ترتیب | فعال</code>\n"
    "مثال:\n"
    "<code>در انتظار مشتری | 🟣 | answered | 35 | 1</code>\n\n"
    "هر بخش را می‌توانید خالی بگذارید تا تغییر نکند. "
    "مقدار «فعال» برابر 1 (روشن) یا 0 (خاموش) است."
)


def _value(obj: Any, key: str, default=None):
    return obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)


def _db():
    import database
    return database.get_db()


def _service():
    import ticket_status_service
    return ticket_status_service


def ticket_status_settings(ctx: Any, *, parent_callback: str | None = "v52:home:admin"):
    """List every status with its edit, toggle and delete controls."""
    try:
        if not _value(ctx, "is_root", False):
            raise PermissionError("تنظیمات وضعیت تیکت فقط در اختیار مدیر کل است.")
        with _db() as conn:
            rows = _service().list_statuses(conn, include_inactive=True)
        if not rows:
            body = "هیچ وضعیتی ثبت نشده است."
        else:
            lines = [
                "⚙️ وضعیت‌های تیکت",
                "",
                "هر وضعیت یک عنوان فارسی، یک ایموجی و یک گروه دارد. "
                "وضعیت فعال در فهرست انتخاب وضعیتِ تیکت دیده می‌شود.",
                "",
            ]
            for row in rows:
                state = "روشن" if row["is_active"] else "خاموش"
                kind = "سیستمی" if row["is_system"] else "سفارشی"
                lines.append(
                    f"{row['emoji']} {row['title_fa']} — کد <code>{row['code']}</code>\n"
                    f"   گروه: {GROUP_FA.get(row['group_key'], row['group_key'])} · "
                    f"ترتیب: {row['sort_order']} · {state} · {kind}"
                )
            body = "\n".join(lines)
        buttons = [[button("➕ افزودن وضعیت جدید", "v52:ticketstatus:new")]]
        for row in rows:
            mark = "🔴" if row["is_active"] else "🟢"
            label = f"{mark} {row['emoji']} {row['title_fa']}"
            buttons.append([
                button(label[:28], f"v52:ticketstatus:edit:{row['code']}"),
                button("روشن/خاموش", f"v52:ticketstatus:toggle:{row['code']}"),
                button("🗑", f"v52:ticketstatus:del:{row['code']}"),
            ])
        buttons.append([button("بازگشت به میز مدیر کل", "v52:home:admin")])
        return render_page("⚙️ تنظیمات تیکت و وضعیت‌ها", body, persona="admin",
                           rows=buttons, parent_callback=parent_callback)
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def begin_ticket_status_new(ctx: Any, *, parent_callback: str | None = "v52:ticketsettings"):
    """Arm the text input for a brand-new status."""
    try:
        if not _value(ctx, "is_root", False):
            raise PermissionError("افزودن وضعیت تیکت فقط در اختیار مدیر کل است.")
        if str(_value(ctx, "chat_type", "private")).lower() != "private":
            raise PermissionError("افزودن وضعیت تیکت فقط در پیوی مدیریت انجام می‌شود.")
        import v52_router
        import time
        v52_router.DEFAULT_FSM.set(
            ctx, "ticket_status_new", payload={}, parent_callback=parent_callback,
            now=int(time.time()))
        return render_page(
            "➕ وضعیت تیکت جدید",
            NEW_FORMAT + "\n\n" + GROUP_HINT,
            persona="admin", page_kind="form",
            rows=[[button("بازگشت به تنظیمات", parent_callback or "v52:ticketsettings")]],
            parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def begin_ticket_status_edit(ctx: Any, code: str, *,
                             parent_callback: str | None = "v52:ticketsettings"):
    """Arm the text input for editing one status."""
    try:
        if not _value(ctx, "is_root", False):
            raise PermissionError("ویرایش وضعیت تیکت فقط در اختیار مدیر کل است.")
        if str(_value(ctx, "chat_type", "private")).lower() != "private":
            raise PermissionError("ویرایش وضعیت تیکت فقط در پیوی مدیریت انجام می‌شود.")
        with _db() as conn:
            current = _service().get_status(conn, code)
        if current is None:
            raise ValueError("وضعیت موردنظر یافت نشد.")
        import v52_router
        import time
        v52_router.DEFAULT_FSM.set(
            ctx, "ticket_status_edit", payload={"code": current["code"]},
            parent_callback=parent_callback, now=int(time.time()))
        body = (
            f"ویرایش وضعیت {current['emoji']} {current['title_fa']}\n"
            f"کد: <code>{current['code']}</code>\n"
            f"گروه فعلی: {GROUP_FA.get(current['group_key'], current['group_key'])}\n"
            f"ترتیب فعلی: {current['sort_order']}\n"
            f"وضعیت فعلی: {'روشن' if current['is_active'] else 'خاموش'}\n\n"
            + EDIT_FORMAT + "\n\n" + GROUP_HINT
        )
        return render_page(
            "✏️ ویرایش وضعیت تیکت", body, persona="admin", page_kind="form",
            rows=[[button("بازگشت به تنظیمات", parent_callback or "v52:ticketsettings")]],
            parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def apply_ticket_status_new(ctx: Any, value: Any, *,
                            parent_callback: str | None = "v52:ticketsettings"):
    """Persist a new status from the ``code | title | emoji | group`` line."""
    try:
        if not _value(ctx, "is_root", False):
            raise PermissionError("افزودن وضعیت تیکت فقط در اختیار مدیر کل است.")
        parts = [chunk.strip() for chunk in str(value or "").split("|")]
        if len(parts) < 2:
            raise ValueError(NEW_FORMAT)
        code, title = parts[0], parts[1]
        emoji = parts[2] if len(parts) > 2 else ""
        group = parts[3] if len(parts) > 3 else "open"
        with _db() as conn:
            created = _service().create_status(
                conn, code=code, title_fa=title, emoji=emoji, group_key=group)
            conn.commit()
        return render_page(
            "✅ وضعیت تیکت ثبت شد",
            f"{created['emoji']} {created['title_fa']}\n"
            f"کد: <code>{created['code']}</code>\n"
            f"گروه: {GROUP_FA.get(created['group_key'], created['group_key'])}\n\n"
            "از این پس می‌توانید این وضعیت را روی هر تیکت انتخاب کنید.",
            persona="admin", page_kind="result",
            rows=[[button("بازگشت به تنظیمات", parent_callback or "v52:ticketsettings")]],
            parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def apply_ticket_status_edit(ctx: Any, payload: Mapping[str, Any], value: Any, *,
                             parent_callback: str | None = "v52:ticketsettings"):
    """Persist edits from the ``title | emoji | group | order | active`` line."""
    try:
        if not _value(ctx, "is_root", False):
            raise PermissionError("ویرایش وضعیت تیکت فقط در اختیار مدیر کل است.")
        code = str(payload.get("code") or "").strip()
        parts = [chunk.strip() for chunk in str(value or "").split("|")]
        while len(parts) < 5:
            parts.append("")
        title, emoji, group, order, active = parts[:5]
        kwargs: dict[str, Any] = {}
        if title:
            kwargs["title_fa"] = title
        if emoji:
            kwargs["emoji"] = emoji
        if group:
            kwargs["group_key"] = group
        if order:
            try:
                kwargs["sort_order"] = int(order)
            except (TypeError, ValueError) as exc:
                raise ValueError("ترتیب نمایش باید یک عدد صحیح باشد.") from exc
        if active:
            if active not in {"0", "1"}:
                raise ValueError("مقدار «فعال» باید 0 یا 1 باشد.")
            kwargs["is_active"] = active == "1"
        if not kwargs:
            raise ValueError("هیچ تغییری ارسال نشد.\n\n" + EDIT_FORMAT)
        with _db() as conn:
            updated = _service().update_status(conn, code, **kwargs)
            conn.commit()
        return render_page(
            "✅ وضعیت تیکت به‌روز شد",
            f"{updated['emoji']} {updated['title_fa']}\n"
            f"کد: <code>{updated['code']}</code>\n"
            f"گروه: {GROUP_FA.get(updated['group_key'], updated['group_key'])}\n"
            f"ترتیب: {updated['sort_order']}\n"
            f"وضعیت: {'روشن' if updated['is_active'] else 'خاموش'}",
            persona="admin", page_kind="result",
            rows=[[button("بازگشت به تنظیمات", parent_callback or "v52:ticketsettings")]],
            parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def toggle_ticket_status(ctx: Any, code: str, *,
                         parent_callback: str | None = "v52:ticketsettings"):
    """Flip one status between enabled and disabled."""
    try:
        if not _value(ctx, "is_root", False):
            raise PermissionError("تغییر وضعیت تیکت فقط در اختیار مدیر کل است.")
        with _db() as conn:
            current = _service().get_status(conn, code)
            if current is None:
                raise ValueError("وضعیت موردنظر یافت نشد.")
            _service().update_status(conn, code, is_active=not current["is_active"])
            conn.commit()
        return ticket_status_settings(ctx, parent_callback=parent_callback)
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def delete_ticket_status(ctx: Any, code: str, *,
                         parent_callback: str | None = "v52:ticketsettings"):
    """Delete a custom status, or disable a system one."""
    try:
        if not _value(ctx, "is_root", False):
            raise PermissionError("حذف وضعیت تیکت فقط در اختیار مدیر کل است.")
        with _db() as conn:
            result = _service().delete_status(conn, code)
            conn.commit()
        page = ticket_status_settings(ctx, parent_callback=parent_callback)
        note = (
            "وضعیت سیستمی حذف نمی‌شود و فقط خاموش شد تا تیکت‌های موجود سالم بمانند."
            if result["deactivated"] else "وضعیت سفارشی حذف شد."
        )
        return render_page("🗑 وضعیت تیکت", note, persona="admin", page_kind="result",
                           rows=[[button("بازگشت به تنظیمات", parent_callback or "v52:ticketsettings")]],
                           parent_callback=parent_callback)
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def status_picker_rows(ticket_id: str, *, current: str | None = None) -> list[list[Any]]:
    """One row per active status, so an operator changes the state in one tap."""
    try:
        with _db() as conn:
            rows = _service().list_statuses(conn)
    except Exception:  # noqa: BLE001 - a picker must never break the ticket page
        return []
    buttons = []
    for row in rows:
        mark = "• " if row["code"] == str(current or "").strip().lower() else ""
        label = f"{mark}{row['emoji']} {row['title_fa']}"
        buttons.append(button(label[:30], f"v52:ticketstatus:set:{ticket_id}:{row['code']}"))
    pairs = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
    return pairs


def set_ticket_status(ctx: Any, ticket_id: str, code: str, *,
                      parent_callback: str | None = None):
    """Change one ticket's status to an active catalog code."""
    try:
        import support_service
        with _db() as conn:
            if not _service().is_active_code(conn, code):
                raise ValueError("این وضعیت فعال نیست؛ ابتدا آن را در تنظیمات روشن کنید.")
            current = conn.execute(
                "SELECT status FROM support_tickets WHERE ticket_id=?", (str(ticket_id),)
            ).fetchone()
            if current is None:
                raise ValueError("تیکت موردنظر یافت نشد.")
            conn.execute(
                "UPDATE support_tickets SET status=?, updated_at=strftime('%s','now') "
                "WHERE ticket_id=?",
                (str(code).strip().lower(), str(ticket_id)),
            )
            conn.commit()
            label = _service().label_for(conn, code)
        return render_page(
            "✅ وضعیت تیکت تغییر کرد",
            f"تیکت {support_service.ticket_code_label(ticket_id)}\nوضعیت جدید: {label}",
            persona="admin", page_kind="result",
            rows=[[button("بازگشت به تیکت", f"v52:ticket:{ticket_id}")]],
            parent_callback=parent_callback or f"v52:ticket:{ticket_id}",
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


__all__ = [
    "GROUP_FA",
    "apply_ticket_status_edit",
    "apply_ticket_status_new",
    "begin_ticket_status_edit",
    "begin_ticket_status_new",
    "delete_ticket_status",
    "set_ticket_status",
    "status_picker_rows",
    "ticket_status_settings",
    "toggle_ticket_status",
]
