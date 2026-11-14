"""Private support, referral and abuse-report pages for v5.2."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

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


def _actor(ctx: Any) -> int:
    return _positive(_value(ctx, "actor_tg_id", _value(ctx, "telegram_id")), "actor_tg_id")


def _root(ctx: Any) -> bool:
    try:
        return _actor(ctx) == _positive(getattr(config, "ADMIN_TELEGRAM_ID", 0) or 0, "ADMIN_TELEGRAM_ID")
    except (TypeError, ValueError):
        return False


def _persona(ctx: Any) -> str:
    role = str(_value(ctx, "persona", "user") or "user").lower()
    return "admin" if _root(ctx) or role in {"admin", "root"} else "rep" if role in {"rep", "representative"} else "user"


_TICKET_GROUPS = (("open", "🟡 باز"), ("answered", "🟢 پاسخ‌داده"),
                  ("closed", "⚪️ بسته"), ("all", "📚 همه"))


def tickets_page(
    ctx: Any,
    *,
    group: str = "open",
    service=None,
    parent_callback: str | None = None,
):
    """Render the support queue with an explicit new-ticket entry and status filters."""
    try:
        if service is None:
            import support_service as service
        selected = str(group or "open").strip().lower()
        if selected not in {key for key, _ in _TICKET_GROUPS}:
            selected = "open"
        tickets = service.list_tickets(ctx, status=selected)
        rows = [[button(f"{str(item['ticket_id'])[:8]} · {item['status']}",
                        "v52:ticket:" + str(item["ticket_id"]))]
                for item in tickets[:12]]
        rows.append([button("➕ ارسال تیکت جدید", "v52:newticket")])
        marks = [(("• " if key == selected else "") + label, f"v52:tickets:{key}")
                 for key, label in _TICKET_GROUPS]
        rows.append([button(label, callback) for label, callback in marks[:2]])
        rows.append([button(label, callback) for label, callback in marks[2:]])
        body = (
            f"وضعیت نمایش: {selected}\n"
            f"تعداد تیکت‌های قابل مشاهده: {len(tickets)}\n"
            "گزارش‌های تخلف مسیر جداگانه‌ای دارند و در این صف نمایش داده نمی‌شوند."
        )
        return render_page("تیکت‌های پشتیبانی", body, persona=_persona(ctx), rows=rows,
                           parent_callback=parent_callback)
    except (TypeError, ValueError, PermissionError, KeyError) as exc:
        return render_error(str(exc), persona=_persona(ctx), parent_callback=parent_callback)


def begin_ticket(
    ctx: Any,
    account_id: int = 0,
    *,
    body: str = "",
    ticket_type: str = "disorder",
    submit: bool = False,
    attachment_file_id: str | None = None,
    panel_reader=None,
    service=None,
    now: int | None = None,
    parent_callback: str | None = None,
):
    """Prepare a seller-scoped ticket; an unavailable panel snapshot never blocks it."""
    try:
        actor = _actor(ctx)
        aid = int(account_id)
        if aid < 0:
            raise ValueError("account_id must be zero or positive")
        selected = str(ticket_type or "").lower()
        if selected not in {"general", "finance", "disorder", "delete_request"}:
            raise ValueError("unsupported ticket type")
        if not submit:
            detail = f"ثبت تیکت پشتیبانی\nشناسه کاربر: {actor}\nحساب: {aid or 'بدون حساب'}\nنوع درخواست: {selected}"
            if selected == "delete_request":
                detail += "\nاین درخواست فقط تیکت می‌سازد؛ حذف خودکار حساب انجام نمی‌شود."
            return render_page("پشتیبانی", detail, persona=_persona(ctx), page_kind="form",
                               parent_callback=parent_callback,
                               rows=[[button("🚨 ارسال گزارش تخلف", "v52:abuse")]])
        if aid == 0 and selected == "delete_request":
            raise ValueError("درخواست حذف حساب به یک حساب مشخص نیاز دارد.")
        if service is None:
            import support_service as service
        result = service.create_ticket(
            ctx, aid, str(body or ""), ticket_type=selected,
            attachment_file_id=attachment_file_id, panel_reader=panel_reader, now=now,
        )
        snapshot = result.get("snapshot") if isinstance(result, Mapping) else None
        unavailable = isinstance(snapshot, Mapping) and snapshot.get("unavailable") is True
        state_line = "وضعیت پنل: unavailable؛ تیکت با این وضعیت صریح ثبت شد." if unavailable else "snapshot ثبت شد."
        return render_page(
            "تیکت ثبت شد",
            f"شناسه تیکت: {result['ticket_id']}\nارجاع: {result['assigned_key']}\n"
            f"وضعیت: {result['status']}\n{state_line}",
            persona=_persona(ctx), page_kind="result", parent_callback=parent_callback,
            rows=[[button("مشاهده تیکت", f"v52:ticket:{result['ticket_id']}")]],
        )
    except (TypeError, ValueError, PermissionError, KeyError) as exc:
        return render_error(str(exc), persona=_persona(ctx), parent_callback=parent_callback)


def begin_abuse(
    ctx: Any,
    body: str = "",
    *,
    attachment_file_id: str | None = None,
    service=None,
    chat_type: str | None = None,
    now: int | None = None,
    parent_callback: str | None = None,
):
    """Create the root-DM-only abuse report; never route it to a representative or forum."""
    try:
        _actor(ctx)
        selected_chat = str(chat_type or _value(ctx, "chat_type", "private")).lower()
        if selected_chat != "private":
            return render_page(
                "گزارش محرمانه تخلف",
                "برای حفظ محرمانگی، ارسال گزارش تخلف فقط در گفت‌وگوی خصوصی بات اصلی انجام می‌شود؛ هیچ نسخه‌ای در گروه یا برای نماینده ارسال نمی‌شود.",
                persona=_persona(ctx), page_kind="redirect", parent_callback=parent_callback,
                rows=[[button("بازگشت به گفت‌وگوی خصوصی", "v52:abuse")]],
            )
        if not str(body or "").strip() and not attachment_file_id:
            return render_page(
                "گزارش محرمانه تخلف",
                "شرح اتفاق را در همین گفت‌وگوی خصوصی ارسال کنید. گزارش و پیوست فقط به مسیر خصوصی root می‌رود.",
                persona=_persona(ctx), page_kind="form", parent_callback=parent_callback,
            )
        if service is None:
            import abuse_service as service
        result = service.create_abuse_report(
            ctx, str(body or ""), attachment_file_id=attachment_file_id, now=now,
        )
        return render_page(
            "گزارش محرمانه ثبت شد",
            f"شناسه گزارش: {result['ticket_id']}\nمقصد: root خصوصی\nوضعیت: {result['status']}\n"
            "گزارش در گروه/تاپیک یا صندوق نماینده کپی نمی‌شود.",
            persona=_persona(ctx), page_kind="result", parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError, KeyError) as exc:
        return render_error(str(exc), persona=_persona(ctx), parent_callback=parent_callback)


def reply_or_escalate(
    ctx: Any,
    ticket_id: str | None = None,
    *,
    account_id: int | None = None,
    action: str = "reply",
    body: str = "",
    visibility: str = "customer",
    confirm: bool = False,
    service=None,
    now: int | None = None,
    reason: str | None = None,
    parent_callback: str | None = None,
):
    """Reply or escalate an assigned ticket; transfer/suspend are preview-only until a backend API exists."""
    try:
        actor = _actor(ctx)
        exact = str(ticket_id or "").strip()
        mode = str(action or "reply").lower()
        if mode == "transfer" and account_id is not None:
            aid = _positive(account_id, "account_id")
            if not _root(ctx):
                raise PermissionError("account transfer preview is root-only")
            if not reason or len(str(reason).strip()) < 8:
                raise ValueError("a reason of at least eight characters is required")
            return render_page(
                "پیش‌نمایش انتقال حساب",
                f"حساب: {aid}\nاقدام: انتقال مسئولیت پشتیبانی\nدلیل: {str(reason).strip()}\n"
                "این مرحله فقط پیش‌نمایش است؛ تغییری در مالکیت یا تخصیص انجام نمی‌شود.",
                persona="admin", page_kind="preview", parent_callback=parent_callback,
                rows=[[button("بازگشت", f"v52:support:{aid}")]],
            )
        if not exact or len(exact.encode("utf-8")) > 64:
            raise ValueError("ticket_id is invalid")
        if mode not in {"reply", "escalate", "transfer", "suspend", "close"}:
            raise ValueError("unsupported support action")
        if mode == "close":
            if service is None:
                import support_service as service
            result = service.close_ticket(ctx, exact, reason=reason, now=now)
            return render_page(
                "تیکت بسته شد",
                f"تیکت: {exact}\nوضعیت: {result['status']}\n"
                "پاسخ‌های بعدی این تیکت وضعیت آن را بازنمی‌گردانند.",
                persona=_persona(ctx), page_kind="result", parent_callback=parent_callback,
            )
        if mode in {"transfer", "suspend"}:
            if not _root(ctx):
                raise PermissionError("transfer/suspend preview is root-only")
            if not reason or len(str(reason).strip()) < 8:
                raise ValueError("a reason of at least eight characters is required")
            return render_page(
                "پیش‌نمایش اقدام مدیریتی",
                f"تیکت: {exact}\nاقدام: {mode}\nدلیل: {str(reason).strip()}\n"
                "این نشست فقط پیش‌نمایش است؛ سرویس مجاز انتقال/تعلیق در این لایه فراخوانی نمی‌شود.",
                persona="admin", page_kind="preview", parent_callback=parent_callback,
                rows=[[button("تأیید بیشتر / بازگشت", f"v52:ticket:{exact}")]],
            )
        if not confirm:
            return render_page(
                "پیش‌نمایش پاسخ / ارجاع",
                f"تیکت: {exact}\nاقدام: {mode}\nنمایش پیام: {visibility}\n"
                "پاسخ داخلی با پاسخ مشتری جدا است؛ ارسال پس از تأیید انجام می‌شود.",
                persona=_persona(ctx), page_kind="preview", parent_callback=parent_callback,
                rows=[[button("تأیید", f"v52:ticket:{exact}")]],
            )
        if service is None:
            import support_service as service
        if mode == "escalate":
            result = service.escalate_ticket(ctx, exact, now=now)
            return render_page(
                "تیکت به root ارجاع شد",
                f"تیکت: {exact}\nوضعیت: {result['status']}\nمسئول: root",
                persona=_persona(ctx), page_kind="result", parent_callback=parent_callback,
            )
        if not str(body or "").strip():
            raise ValueError("reply body is required")
        result = service.reply_ticket(ctx, exact, str(body), visibility=visibility, now=now)
        return render_page(
            "پاسخ ثبت شد",
            f"تیکت: {exact}\nشناسه پیام: {result['message_id']}\nقابلیت مشاهده: {result['visibility']}",
            persona=_persona(ctx), page_kind="result", parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError, KeyError) as exc:
        return render_error(str(exc), persona=_persona(ctx), parent_callback=parent_callback)


__all__ = ["begin_ticket", "begin_abuse", "reply_or_escalate", "tickets_page"]