"""Network-free root bot-registration and activation preview pages for v5.2."""
from __future__ import annotations

import re
from collections.abc import Mapping

import config
import service_errors_fa
from ui_v52 import button, render_error, render_page

#: A Telegram bot token already carries the bot id before its first colon, so the
#: operator only has to send the token. ``_LEGACY_CREDENTIALS`` keeps the older
#: ``<bot_id> <token>`` single line working for anyone following an older instruction.
_TOKEN_ONLY = re.compile(r"^([1-9][0-9]{0,17}):(\S+)$")
_LEGACY_CREDENTIALS = re.compile(r"^([1-9][0-9]{0,17})\s+(\S+)$")


def parse_bot_credentials(text) -> tuple[int, str]:
    """Split the operator's private answer into ``(declared_bot_id, token)``.

    Sending the token alone is enough: the id is read from the part before the first
    colon, which is exactly how Telegram builds a token. The older ``<bot_id> <token>``
    line is still accepted, so an operator who follows an older instruction is not stuck.
    """
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("bot token is required")
    legacy = _LEGACY_CREDENTIALS.match(raw)
    if legacy:
        return int(legacy.group(1)), legacy.group(2)
    token_only = _TOKEN_ONLY.match(raw)
    if token_only:
        return int(token_only.group(1)), raw
    raise ValueError("bot token must be in <bot_id>:<secret> form")


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
    text: str | None = None,
    is_primary: bool = False,
    chat_type: str | None = None,
    parent_callback: str | None = None,
):
    """Start a private root-only bot registration draft; never render the token.

    ``text`` is the operator's raw private-DM answer. When it is supplied and the two
    explicit fields are not, the bot id is read out of the token instead of being typed
    by hand.
    """
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
        # S59 — a token alone is a complete answer; the id lives inside it.
        if text is not None and (declared_bot_id is None or token is None):
            declared_bot_id, token = parse_bot_credentials(text)
        if declared_bot_id is None or token is None:
            return render_page(
                "ثبت بات جدید",
                "توکن بات را در همین گفت‌وگوی خصوصی ارسال کنید؛ قالب درست «شناسه:کلید» است "
                "و شناسهٔ بات از خود توکن خوانده می‌شود. "
                "اگر ترجیح می‌دهید هر دو را جدا بفرستید، «شناسه توکن» هم پذیرفته می‌شود. "
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
        return render_error(service_errors_fa.translate(exc), persona="admin", parent_callback=parent_callback)


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
            "وضعیت فعلی: در انتظار تأیید فعال‌سازی\n"
            "توکن در این تأیید نمایش داده نمی‌شود؛ فعال‌سازی فقط با کد یک‌بارمصرف انجام می‌شود."
        )
        return render_page("پیش‌نمایش فعال‌سازی بات", body, persona="admin",
                           page_kind="preview", parent_callback=parent_callback, rows=rows)
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(service_errors_fa.translate(exc), persona="admin", parent_callback=parent_callback)


async def begin_registration_from_text(
    ctx,
    text: str,
    *,
    verify,
    rep_id: int | None = None,
    is_primary: bool = False,
    chat_type: str | None = None,
    parent_callback: str | None = None,
):
    """Parse a private-DM answer, verify it, and render the activation preview.

    S59 — this closes the flow the operator actually walks: one message containing the
    token, a getMe check, then the ``preview_bot_activation`` confirmation card.

    ``verify`` is ``bot_registry_service.verify_bot_registration`` injected by the
    caller, already bound to its connection, actor and ``get_me`` callable. This module
    keeps its no-network, no-database contract: the getMe call and the encrypted write
    stay in the service that owns them, and the page logic stays testable offline.
    """
    try:
        if not _root(ctx):
            raise PermissionError("ثبت بات فقط در اختیار root است.")
        chat = str(chat_type or _get(ctx, "chat_type", "private")).lower()
        if chat != "private":
            raise PermissionError("توکن بات فقط باید در گفت‌وگوی خصوصی root دریافت شود.")
        if not isinstance(is_primary, bool):
            raise ValueError("is_primary must be boolean")
        if is_primary and rep_id is not None:
            raise ValueError("primary bot cannot be assigned to a representative")
        selected_rep = None if is_primary else _positive(rep_id, "rep_id")
        declared_bot_id, token = parse_bot_credentials(text)
        registration = await verify(bot_id=declared_bot_id, token=token,
                                    is_primary=is_primary, rep_id=selected_rep)
        return preview_bot_activation(ctx, registration, parent_callback=parent_callback)
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(service_errors_fa.translate(exc), persona="admin",
                            parent_callback=parent_callback)
    except Exception as exc:
        # A registry refusal (duplicate token, wrong id, disabled actor) is an operator
        # message, not a crash: translate it and keep the bot page reachable.
        from bot_registry_service import (BotRegistryConflict,
                                          BotRegistryPermissionError,
                                          BotVerificationError)
        if isinstance(exc, (BotRegistryConflict, BotRegistryPermissionError,
                            BotVerificationError)):
            return render_error(service_errors_fa.translate(exc), persona="admin",
                                parent_callback=parent_callback)
        raise


#: Operator-facing status wording, so a raw ``disabled`` token never reaches the screen.
_STATUS_FA = {
    "pending": "⏳ در انتظار تأیید",
    "active": "🟢 فعال",
    "disabled": "🔴 غیرفعال",
}


def _owner_label(bot) -> str:
    if bool(_get(bot, "is_primary")):
        return "root (بات اصلی)"
    rep_id = _get(bot, "rep_id")
    if rep_id is None:
        return "root"
    name = str(_get(bot, "rep_name") or "").strip()
    return f"نمایندهٔ {name} ({rep_id})" if name else f"نمایندهٔ {rep_id}"


def build_bots_page(ctx, bots, *, reps=(), parent_callback: str = "v52:bots"):
    """The bot-management hub: every registered bot, plus the registration entry points.

    Each row opens that bot's detail card, which is where the lifecycle buttons live.
    """
    try:
        if not _root(ctx):
            raise PermissionError("مدیریت بات‌ها فقط در اختیار root است.")
        rows = []
        for bot in bots or ():
            bot_id = _positive(_get(bot, "bot_id"), "bot_id")
            status = str(_get(bot, "status") or "").lower()
            label = _STATUS_FA.get(status, status)
            rows.append([button(f"{label} | {_owner_label(bot)} | {bot_id}",
                                f"v52:bot:show:{bot_id}")])
        rows.append([button("ثبت بات اصلی", "v52:bot:primary")])
        for rep in reps or ():
            rep_id = _positive(_get(rep, "id"), "rep_id")
            name = str(_get(rep, "name") or "").strip()
            rows.append([button(f"ثبت بات نماینده {name} ({rep_id})".strip(),
                                f"v52:bot:{rep_id}")])
        body = ("هر بات ثبت‌شده را از فهرست زیر باز کنید تا وضعیت، مالک و عملیات "
                "چرخهٔ عمر آن را ببینید. توکن بات هرگز در این صفحه‌ها نمایش داده نمی‌شود.")
        if not bots:
            body = ("هنوز هیچ باتی ثبت نشده است. " + body)
        return render_page("مدیریت بات‌ها", body, persona="admin",
                           rows=rows, parent_callback=parent_callback)
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(service_errors_fa.translate(exc), persona="admin",
                            parent_callback=parent_callback)


def build_bot_detail_page(ctx, bot, *, parent_callback: str = "v52:bots"):
    """One bot's card with the lifecycle actions its current status actually allows.

    A disabled bot offers «فعال‌سازی مجدد» (the counterpart ``disable_bot`` was missing),
    an active one offers «توقف موقت», and a non-primary bot can be reassigned to root
    or to another representative, or deleted once nothing depends on it.
    """
    try:
        if not _root(ctx):
            raise PermissionError("مدیریت بات‌ها فقط در اختیار root است.")
        bot_id = _positive(_get(bot, "bot_id"), "bot_id")
        status = str(_get(bot, "status") or "").lower()
        if status not in _STATUS_FA:
            raise ValueError("bot status is not recognised")
        primary = bool(_get(bot, "is_primary"))
        username = str(_get(bot, "username") or "").strip().lstrip("@")
        revision = int(_get(bot, "revision") or 1)
        rows = []
        if status == "disabled":
            rows.append([button("▶️ فعال‌سازی مجدد بات", f"v52:bot:enable:{bot_id}")])
        elif status == "active":
            rows.append([button("⏸ توقف موقت بات", f"v52:bot:disable:{bot_id}")])
        if not primary:
            rows.append([button("🔄 تغییر مالکیت / مصادره به مدیریت کل",
                                f"v52:bot:reassign:{bot_id}")])
            if status != "active":
                rows.append([button("🗑 حذف بات", f"v52:bot:delete:{bot_id}")])
        rows.append([button("↩️ بازگشت به فهرست بات‌ها", "v52:bots")])
        body = (
            f"شناسهٔ بات: {bot_id}\n"
            f"نام کاربری: {'@' + username if username else '—'}\n"
            f"مالک: {_owner_label(bot)}\n"
            f"وضعیت: {_STATUS_FA[status]}\n"
            f"نسخهٔ رکورد: {revision}\n\n"
            + ("بات اصلی سامانه است و نمی‌تواند منتقل یا حذف شود."
               if primary else
               "فعال‌سازی و توقف هرکدام با کد یک‌بارمصرف تأیید می‌شوند.")
        )
        return render_page(f"بات {bot_id}", body, persona="admin",
                           rows=rows, parent_callback=parent_callback)
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(service_errors_fa.translate(exc), persona="admin",
                            parent_callback=parent_callback)




#: Persian wording per lifecycle action, shared by the preview and the result page.
_BOT_ACTION_FA = {
    "bot.activate": "فعال‌سازی بات",
    "bot.disable": "توقف موقت بات",
    "bot.enable": "فعال‌سازی مجدد بات",
    "bot.reassign": "تغییر مالکیت بات",
    "bot.delete": "حذف بات",
}


def build_bot_action_preview(ctx, bot, prepared, *, action,
                             parent_callback: str = "v52:bots"):
    """The confirm page for a ``prepare_*`` result.

    ``prepared`` is the dict the service returned; its ``confirmation`` carries the
    one-shot ``callback_ok`` / ``callback_no`` the operator presses. Nothing has been
    changed yet — the transition runs when that nonce is consumed.
    """
    try:
        if not _root(ctx):
            raise PermissionError("مدیریت بات‌ها فقط در اختیار root است.")
        if not isinstance(prepared, Mapping):
            raise ValueError("prepared bot action is required")
        bot_id = _positive(prepared.get("bot_id") or _get(bot, "bot_id"), "bot_id")
        confirmation = prepared.get("confirmation")
        if not isinstance(confirmation, Mapping):
            raise ValueError("prepared bot action is required")
        callback_ok = str(confirmation.get("callback_ok") or "")
        callback_no = str(confirmation.get("callback_no") or "")
        if not callback_ok or not callback_no:
            raise ValueError("prepared bot action is required")
        title = _BOT_ACTION_FA.get(str(action), str(action))
        status = str(_get(bot, "status") or "").lower()
        lines = [
            f"عملیات: {title}",
            f"شناسهٔ بات: {bot_id}",
            f"مالک فعلی: {_owner_label(bot)}",
            f"وضعیت فعلی: {_STATUS_FA.get(status, '—')}",
        ]
        if str(action) == "bot.reassign":
            new_rep = prepared.get("new_rep_id")
            lines.append("مالک جدید: " + ("مدیریت کل (root)" if new_rep is None
                                           else f"نمایندهٔ {new_rep}"))
        if str(action) == "bot.disable":
            lines.append(f"سفارش‌های باز: {int(prepared.get('pending_orders') or 0)}")
            lines.append(f"کاربران متصل: {int(prepared.get('linked_customers') or 0)}")
        lines.append("")
        lines.append("هیچ تغییری تا زدن دکمهٔ تأیید اعمال نمی‌شود؛ "
                     "کد تأیید یک‌بارمصرف است و پس از مصرف باطل می‌شود.")
        return render_page(f"پیش‌نمایش {title}", "\n".join(lines), persona="admin",
                           page_kind="preview", parent_callback=parent_callback,
                           rows=[[button("تأیید", callback_ok)],
                                 [button("لغو", callback_no)],
                                 [button("بازگشت به کارت بات", f"v52:bot:view:{bot_id}")]])
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(service_errors_fa.translate(exc), persona="admin",
                            parent_callback=parent_callback)


def build_bot_action_result(ctx, result, *, action, parent_callback: str = "v52:bots"):
    """The outcome page once the transition has actually run."""
    try:
        if not _root(ctx):
            raise PermissionError("مدیریت بات‌ها فقط در اختیار root است.")
        if not isinstance(result, Mapping):
            raise ValueError("bot action result is required")
        bot_id = _positive(result.get("bot_id"), "bot_id")
        title = _BOT_ACTION_FA.get(str(action), str(action))
        replayed = bool(result.get("replayed"))
        notice = ("این تأیید قبلاً مصرف شده بود؛ تغییری دوباره اعمال نشد."
                  if replayed else f"{title} انجام شد.")
        lines = [notice, f"شناسهٔ بات: {bot_id}"]
        if "status" in result:
            lines.append("وضعیت جدید: " + _STATUS_FA.get(str(result["status"]).lower(),
                                                         str(result["status"])))
        if "rep_id" in result:
            new_rep = result["rep_id"]
            lines.append("مالک جدید: " + ("مدیریت کل (root)" if new_rep is None
                                           else f"نمایندهٔ {new_rep}"))
        lines.append(f"نسخهٔ رکورد: {int(result.get('revision') or 0)}")
        rows = [[button("بازگشت به فهرست بات‌ها", "v52:bots")]]
        if str(result.get("status")) != "deleted":
            rows.insert(0, [button("کارت این بات", f"v52:bot:view:{bot_id}")])
        return render_page("نتیجهٔ عملیات بات", "\n".join(lines), persona="admin",
                           page_kind="result", parent_callback=parent_callback, rows=rows)
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(service_errors_fa.translate(exc), persona="admin",
                            parent_callback=parent_callback)


def build_bot_reassign_picker(ctx, bot, reps, *, parent_callback: str = "v52:bots"):
    """Pick the new owner: root, or one of the active representatives."""
    try:
        if not _root(ctx):
            raise PermissionError("مدیریت بات‌ها فقط در اختیار root است.")
        bot_id = _positive(_get(bot, "bot_id"), "bot_id")
        rows = [[button("مصادره به مدیریت کل (root)", f"v52:bot:reassign:{bot_id}:root")]]
        for rep in reps or ():
            rep_id = _positive(_get(rep, "id"), "rep_id")
            name = str(_get(rep, "name") or "").strip()
            rows.append([button(f"انتقال به {name} ({rep_id})".strip(),
                                f"v52:bot:reassign:{bot_id}:{rep_id}")])
        rows.append([button("بازگشت به کارت بات", f"v52:bot:view:{bot_id}")])
        return render_page(
            "تغییر مالکیت بات",
            f"شناسهٔ بات: {bot_id}\nمالک فعلی: {_owner_label(bot)}\n\n"
            "مالک جدید را انتخاب کنید. برای قطع دسترسی یک نمایندهٔ متخلف، "
            "«مصادره به مدیریت کل» را بزنید.",
            persona="admin", parent_callback=parent_callback, rows=rows)
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(service_errors_fa.translate(exc), persona="admin",
                            parent_callback=parent_callback)


__all__ = ["begin_register_bot", "preview_bot_activation", "parse_bot_credentials",
           "begin_registration_from_text", "build_bots_page", "build_bot_detail_page",
           "build_bot_action_preview", "build_bot_action_result",
           "build_bot_reassign_picker"]
