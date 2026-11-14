"""Network-free root bot-registration and activation preview pages for v5.2."""
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


def _root(ctx):
    try:
        root_id = int(getattr(config, "ADMIN_TELEGRAM_ID", 0) or 0)
        actor = _positive(_get(ctx, "actor_tg_id", _get(ctx, "telegram_id")), "actor_tg_id")
        return root_id > 0 and actor == root_id
    except (TypeError, ValueError):
        return False


def begin_register_bot(
    ctx,
    rep_id: int | None = None,
    *,
    declared_bot_id: int | None = None,
    token: str | None = None,
    is_primary: bool = False,
    chat_type: str | None = None,
    parent_callback: str | None = None,
):
    """Start a private root-only bot registration draft; never render the token."""
    try:
        if not _root(ctx):
            raise PermissionError("ثبت بات فقط در اختیار root است.")
        chat = str(chat_type or _get(ctx, "chat_type", "private")).lower()
        if chat != "private":
            raise PermissionError("توکن بات فقط باید در گفت‌وگوی خصوصی root دریافت شود.")
        if not isinstance(is_primary, bool):
            raise ValueError("is_primary must be boolean")
        selected_rep = None if is_primary else _positive(rep_id, "rep_id")
        if is_primary and rep_id is not None:
            raise ValueError("primary bot cannot be assigned to a representative")
        if declared_bot_id is None or token is None:
            return render_page(
                "ثبت بات جدید",
                "شناسهٔ اعلام‌شدهٔ بات و token را در فرم خصوصی ارسال کنید. "
                "توکن در صفحهٔ تأیید، callback و گزارش‌ها نمایش داده نمی‌شود.",
                persona="admin", page_kind="form", parent_callback=parent_callback,
                rows=[[button("ثبت بات اصلی", "v52:bot:primary"),
                       button("ثبت بات نماینده", "v52:bot:rep")]],
            )
        _positive(declared_bot_id, "declared_bot_id")
        if not isinstance(token, str) or not token.strip():
            raise ValueError("bot token is required")
        # Verification is delegated to bot_registry_service.verify_bot_registration
        # by the confirmed private-DM handler; this UI function has no getMe call.
        return render_page(
            "دادهٔ ثبت بات دریافت شد",
            f"شناسهٔ اعلام‌شده: {int(declared_bot_id)}\n"
            f"نماینده: {selected_rep if selected_rep is not None else 'root'}\n"
            "اعتبارسنجی getMe و ذخیرهٔ رمزنگاری‌شده باید توسط handler/service انجام شود؛ "
            "این صفحه token را نگه نمی‌دارد یا نمایش نمی‌دهد.",
            persona="admin", page_kind="form", parent_callback=parent_callback,
            rows=[[button("بازگشت", "v52:bots")]],
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def preview_bot_activation(
    ctx,
    registration: Mapping,
    *,
    parent_callback: str | None = None,
):
    """Preview the verified pending identity; confirmation contains public ID only."""
    try:
        if not _root(ctx):
            raise PermissionError("فعال‌سازی بات فقط در اختیار root است.")
        if not isinstance(registration, Mapping):
            raise ValueError("verified registration snapshot is required")
        bot_id = _positive(registration.get("bot_id"), "bot_id")
        status = str(registration.get("status") or "pending").lower()
        if status != "pending":
            raise ValueError("only a pending verified bot can be activated")
        username = str(registration.get("username") or "").strip().lstrip("@")
        if not username:
            raise ValueError("verified bot username is required")
        rep_id = registration.get("rep_id")
        primary = bool(registration.get("is_primary", rep_id is None))
        if primary and rep_id is not None:
            raise ValueError("primary bot cannot be assigned to a representative")
        confirmation = registration.get("confirmation") or {}
        callback_ok = str(confirmation.get("callback_ok") or "") if isinstance(confirmation, Mapping) else ""
        callback_no = str(confirmation.get("callback_no") or "") if isinstance(confirmation, Mapping) else ""
        rows = []
        if callback_ok:
            rows.append([button("تأیید فعال‌سازی", callback_ok)])
        if callback_no:
            rows.append([button("لغو", callback_no)])
        if not rows:
            rows.append([button("بازگشت به بات‌ها", "v52:bots")])
        body = (
            f"شناسهٔ بات: {bot_id}\nنام کاربری: @{username}\n"
            f"مالک: {'root' if primary else f'نمایندهٔ {rep_id}'}\n"
            "وضعیت فعلی: pending\n"
            "توکن در این تأیید نمایش داده نمی‌شود؛ فعال‌سازی فقط با nonce یک‌بارمصرف انجام می‌شود."
        )
        return render_page("پیش‌نمایش فعال‌سازی بات", body, persona="admin",
                           page_kind="preview", parent_callback=parent_callback, rows=rows)
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


__all__ = ["begin_register_bot", "preview_bot_activation"]
