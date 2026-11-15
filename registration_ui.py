"""Registration, referral and approval surfaces for the v5.2 bot.

Every page here is pure rendering: it reads state, returns text plus a keyboard,
and never mutates anything or performs network I/O. The handlers in ``rep_bot``,
``admin_panel`` and ``rep_panel`` own the decisions.
"""
from __future__ import annotations

from typing import Any, Mapping, Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import registration_service

_STATUS_LABELS = {
    registration_service.STATUS_APPROVED: "✅ تاییدشده",
    registration_service.STATUS_PENDING: "⏳ در انتظار تایید مدیریت",
    registration_service.STATUS_AWAITING_REFERRAL: "🎁 در انتظار کد معرف",
    registration_service.STATUS_REJECTED: "❌ ردشده",
}


def _value(obj: Any, key: str, default=None):
    return obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)


def _display_name(row: Mapping[str, Any]) -> str:
    name = str(_value(row, "full_name") or "").strip()
    username = str(_value(row, "username") or "").strip().lstrip("@")
    if name and username:
        return f"{name} (@{username})"
    return name or (f"@{username}" if username else "بدون نام")


def build_registration_waiting_page(
    status: str, *, has_referrer: bool = False
) -> tuple[str, InlineKeyboardMarkup]:
    """The respectful holding page a not-yet-approved user sees instead of the portal."""
    if status == registration_service.STATUS_AWAITING_REFERRAL:
        text = (
            "🔒 **عضویت در این ربات نیازمند کد معرف است.**\n\n"
            "برای ورود، کد معرف خود را ارسال کنید یا از دوستی که شما را دعوت کرده "
            "بخواهید لینک دعوتش را برایتان بفرستد.\n\n"
            "پس از ارسال کد معتبر، درخواست شما بررسی می‌شود."
        )
    else:
        text = (
            "⏳ **حساب کاربری شما ثبت شد و در انتظار تایید مدیریت است.**\n\n"
            "به‌محض تایید، دسترسی شما فعال می‌شود و پیام تبریک برایتان ارسال می‌گردد.\n"
            "می‌توانید وضعیت خود را بررسی کنید یا کد معرف خود را ثبت نمایید."
        )
    rows = [
        [InlineKeyboardButton("🔄 بررسی وضعیت تایید", callback_data="user:reg_status")],
        [InlineKeyboardButton("🎁 ثبت کد معرف", callback_data="user:referral_code")],
    ]
    if has_referrer:
        rows.append([InlineKeyboardButton("🎁 دعوت از دوستان", callback_data="user:referral")])
    return text, InlineKeyboardMarkup(rows)


def build_registration_rejected_page() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "❌ **درخواست عضویت شما تایید نشد.**\n\n"
        "اگر فکر می‌کنید اشتباهی رخ داده است، می‌توانید وضعیت خود را دوباره بررسی کنید "
        "یا با پشتیبانی تماس بگیرید."
    )
    return text, InlineKeyboardMarkup([
        [InlineKeyboardButton("🔄 بررسی وضعیت تایید", callback_data="user:reg_status")],
    ])


def build_registration_status_page(status: str) -> tuple[str, InlineKeyboardMarkup]:
    label = _STATUS_LABELS.get(status, status)
    if status == registration_service.STATUS_APPROVED:
        text = f"✅ **وضعیت عضویت شما: {label}**\n\nدسترسی شما فعال است."
        rows = [[InlineKeyboardButton("🏠 منوی اصلی کاربری", callback_data="user:main")]]
    elif status == registration_service.STATUS_REJECTED:
        return build_registration_rejected_page()
    else:
        text, markup = build_registration_waiting_page(status)
        return f"🔄 **بررسی وضعیت:** {label}\n\n{text}", markup
    return text, InlineKeyboardMarkup(rows)


def build_referral_page(
    *, code: str, link: str, stats: Mapping[str, Any], reward_plan: Mapping[str, Any]
) -> tuple[str, InlineKeyboardMarkup]:
    """The invite page: the actor's own code, deep link, totals and reward rules."""
    lines = [
        "🎁 **دعوت از دوستان (کد معرف)**\n",
        f"• کد معرف اختصاصی شما: `{code}`",
    ]
    if link:
        lines.append(f"• لینک دعوت مستقیم:\n{link}")
    lines += [
        "",
        f"• تعداد دعوت‌شدگان: **{int(stats.get('invited') or 0)}**",
        f"• تاییدشده: **{int(stats.get('approved') or 0)}**",
        f"• مجموع پاداش دریافتی: **{int(stats.get('reward_total') or 0):,} تومان**",
        "",
        "💡 **قوانین پاداش این ربات:**",
    ]
    signup = int(reward_plan.get("signup_bonus") or 0)
    first = int(reward_plan.get("first_purchase_bonus") or 0)
    percent = int(reward_plan.get("purchase_percent") or 0)
    if signup:
        lines.append(f"• هدیه تایید عضویت زیرمجموعه: **{signup:,} تومان**")
    if first:
        lines.append(f"• هدیه اولین خرید زیرمجموعه: **{first:,} تومان**")
    if percent:
        lines.append(f"• پاداش درصدی اولین خرید: **{percent}%**")
    if not any((signup, first, percent)):
        lines.append("• در حال حاضر پاداش نقدی برای این ربات فعال نیست؛ دعوت شما همچنان ثبت می‌شود.")
    rows = [
        [InlineKeyboardButton("📋 لیست دوستان دعوت‌شده", callback_data="user:referral_list")],
    ]
    if link:
        rows.append([InlineKeyboardButton("📤 اشتراک‌گذاری لینک دعوت", url=link)])
    rows.append([InlineKeyboardButton("🏠 منوی اصلی کاربری", callback_data="user:main")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def build_referral_list_page(referrals: Sequence[Mapping[str, Any]]) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["📋 **دوستان دعوت‌شده توسط شما:**\n"]
    if not referrals:
        lines.append("• هنوز کسی را دعوت نکرده‌اید. لینک دعوت خود را برای دوستانتان بفرستید.")
    else:
        for item in referrals:
            status = _STATUS_LABELS.get(
                str(_value(item, "registration_status") or ""), "نامشخص")
            joined = str(_value(item, "created_at") or "")[:16]
            lines.append(f"• {_display_name(item)} — {status} | `{joined}`")
    return "\n".join(lines), InlineKeyboardMarkup([
        [InlineKeyboardButton("↩️ بازگشت به صفحه دعوت", callback_data="user:referral")],
        [InlineKeyboardButton("🏠 منوی اصلی کاربری", callback_data="user:main")],
    ])


def build_approval_card(user: Mapping[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """The owner notification card for one pending registration."""
    user_id = int(_value(user, "telegram_id") or 0)
    referrer = str(_value(user, "referred_by_code") or "").strip()
    text = (
        "🆕 **درخواست عضویت جدید**\n\n"
        f"• نام: {_display_name(user)}\n"
        f"• شناسه عددی: `{user_id}`\n"
        f"• بات ورودی: `{_value(user, 'origin_bot_id') or '—'}`\n"
        f"• کد معرف: `{referrer or 'ندارد'}`\n"
        f"• زمان: `{str(_value(user, 'created_at') or '')[:19]}`"
    )
    rows = [
        [InlineKeyboardButton("✅ تایید عضویت", callback_data=f"admin:reg_approve:{user_id}"),
         InlineKeyboardButton("❌ رد درخواست", callback_data=f"admin:reg_reject:{user_id}")],
        [InlineKeyboardButton("👤 مشاهده پروفایل کاربر", callback_data=f"admin:user_detail:{user_id}")],
    ]
    return text, InlineKeyboardMarkup(rows)


def build_rep_approval_card(user: Mapping[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    """The representative-scoped variant so a rep bot never routes to the root queue."""
    text, _markup = build_approval_card(user)
    user_id = int(_value(user, "telegram_id") or 0)
    rows = [
        [InlineKeyboardButton("✅ تایید عضویت", callback_data=f"rep:reg_approve:{user_id}"),
         InlineKeyboardButton("❌ رد درخواست", callback_data=f"rep:reg_reject:{user_id}")],
        [InlineKeyboardButton("↩️ بازگشت به درخواست‌های عضویت", callback_data="rep:pending_reg")],
    ]
    return text, InlineKeyboardMarkup(rows)


def build_pending_registrations_page(
    pending: Sequence[Mapping[str, Any]], *, scope: str = "admin"
) -> tuple[str, InlineKeyboardMarkup]:
    """Queue listing with per-row approve/reject and a bulk-approve action."""
    lines = ["⏳ **درخواست‌های عضویت در انتظار:**\n"]
    rows: list[list[InlineKeyboardButton]] = []
    if not pending:
        lines.append("✅ در حال حاضر هیچ درخواست عضویت معلقی وجود ندارد.")
    else:
        lines.append(f"• تعداد: **{len(pending)}**\n")
        prefix = "admin" if scope == "admin" else "rep"
        for item in pending:
            user_id = int(_value(item, "telegram_id") or 0)
            status = _STATUS_LABELS.get(
                str(_value(item, "registration_status") or ""), "نامشخص")
            lines.append(f"• `{user_id}` — {_display_name(item)} | {status}")
            rows.append([
                InlineKeyboardButton(f"✅ تایید {user_id}", callback_data=f"{prefix}:reg_approve:{user_id}"),
                InlineKeyboardButton(f"❌ رد {user_id}", callback_data=f"{prefix}:reg_reject:{user_id}"),
            ])
        rows.append([InlineKeyboardButton(
            f"✅ تایید دسته‌جمعی همه ({len(pending)})", callback_data=f"{prefix}:reg_approve_all")])
    return "\n".join(lines), InlineKeyboardMarkup(rows)


def build_referral_code_prompt() -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "🎁 **ثبت کد معرف**\n\n"
        "کد معرف خود را ارسال کنید (مثال: `AB12CD34`).\n\n"
        "⏱ مهلت ارسال: ۶۰ ثانیه (یا بنویسید `cancel`)"
    )
    return text, InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ انصراف", callback_data="user:main")],
    ])


def build_registration_mode_keyboard(scope: str) -> InlineKeyboardMarkup:
    """Mode picker for the settings category and the representative workspace."""
    prefix = "admin" if scope == "admin" else "rep"
    labels = {
        "open": "🟢 ثبت‌نام باز",
        "approval_required": "🟡 نیازمند تایید مدیریت",
        "referral_only": "🔵 فقط با کد معرف",
    }
    rows = [
        [InlineKeyboardButton(label, callback_data=f"{prefix}:reg_mode:{mode}")]
        for mode, label in labels.items()
    ]
    rows.append([InlineKeyboardButton("↩️ بازگشت", callback_data=f"{prefix}:reg_settings")])
    return InlineKeyboardMarkup(rows)


def build_registration_settings_page(
    *, mode: str, requires_approval: bool, pending: int, scope: str = "admin"
) -> tuple[str, InlineKeyboardMarkup]:
    prefix = "admin" if scope == "admin" else "rep"
    mode_label = {
        "open": "🟢 ثبت‌نام باز",
        "approval_required": "🟡 نیازمند تایید مدیریت",
        "referral_only": "🔵 فقط با کد معرف",
    }.get(mode, mode)
    text = (
        "🔐 **سیاست‌های ثبت‌نام، عضویت و کد معرف**\n\n"
        f"• حالت فعلی: **{mode_label}**\n"
        f"• تایید مدیریت پس از کد معرف: **{'فعال' if requires_approval else 'غیرفعال'}**\n"
        f"• درخواست‌های در انتظار: **{pending}**\n\n"
        "برای تغییر حالت، یکی از گزینه‌های زیر را انتخاب کنید. "
        "این تنظیم برای همین بات/نماینده اعمال می‌شود."
    )
    rows = [
        [InlineKeyboardButton("🟢 ثبت‌نام باز", callback_data=f"{prefix}:reg_mode:open")],
        [InlineKeyboardButton("🟡 نیازمند تایید مدیریت", callback_data=f"{prefix}:reg_mode:approval_required")],
        [InlineKeyboardButton("🔵 فقط با کد معرف", callback_data=f"{prefix}:reg_mode:referral_only")],
        [InlineKeyboardButton(
            f"🔁 تایید مدیریت پس از کد معرف: {'فعال' if requires_approval else 'غیرفعال'}",
            callback_data=f"{prefix}:reg_toggle_approval")],
        [InlineKeyboardButton(
            f"⏳ درخواست‌های عضویت در انتظار ({pending})",
            callback_data=f"{prefix}:pending_reg")],
    ]
    # Standard navigation so this submenu is never a dead end: back to the parent
    # settings centre and straight home. The representative workspace has no
    # settings centre, so it only gets the home row.
    if scope == "admin":
        rows.append([InlineKeyboardButton(
            "🔙 بازگشت به تنظیمات پیشرفته", callback_data="admin:settings")])
        rows.append([InlineKeyboardButton(
            "🏠 بازگشت به منوی اصلی", callback_data="admin:main")])
    else:
        rows.append([InlineKeyboardButton(
            "🏠 بازگشت به منوی اصلی", callback_data="rep:main")])
    return text, InlineKeyboardMarkup(rows)


__all__ = [
    "build_registration_waiting_page", "build_registration_rejected_page",
    "build_registration_status_page", "build_referral_page", "build_referral_list_page",
    "build_approval_card", "build_rep_approval_card", "build_pending_registrations_page",
    "build_referral_code_prompt", "build_registration_mode_keyboard",
    "build_registration_settings_page",
]
