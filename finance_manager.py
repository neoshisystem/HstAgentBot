"""
finance_manager.py - Modular Financial & Dual-Wallet Subsystem for HstAgentBot
Handles:
- Card-to-card payment requests and instructions
- Receipt (photo/text) submission and validation
- Super Admin inline verification cards ([✅ تایید واریز] / [❌ رد فیش])
- Dual-wallet accounting: Representative balance/settlement & End-User personal wallet
- Credit limit enforcement and financial audit logging
"""

import logging
import random
import string
import html
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

# State constants for FSM
STATE_AWAITING_RECEIPT_AMOUNT = "AWAITING_RECEIPT_AMOUNT"
STATE_AWAITING_RECEIPT_PHOTO = "AWAITING_RECEIPT_PHOTO"
STATE_AWAITING_REJECT_REASON = "AWAITING_REJECT_REASON"


def generate_tracking_code(prefix: str = "RCP") -> str:
    """Generates a collision-resistant human-readable tracking code."""
    random_digits = "".join(random.choices(string.digits, k=6))
    return f"{prefix}-{random_digits}"


def build_card_payment_keyboard(tracking_code: str) -> InlineKeyboardMarkup:
    """Builds inline keyboard displayed with card-to-card instructions."""
    keyboard = [
        [
            InlineKeyboardButton("📸 ارسال تصویر فیش واریزی", callback_data=f"send_receipt_{tracking_code}"),
        ],
        [
            InlineKeyboardButton("❌ انصراف", callback_data=f"cancel_payment:{tracking_code}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def build_admin_receipt_review_keyboard(receipt_id: int, user_id: int | None = None) -> InlineKeyboardMarkup:
    """Builds Super Admin inline keyboard for receipt verification.

    The payer's profile is reachable from the very first delivery, before any
    decision: verifying a transfer routinely requires looking at the account
    first, and hiding the button until after the decision forced the operator to
    either guess or hunt the user down in the user list.
    """
    keyboard = [
        [
            InlineKeyboardButton("✅ تایید فیش", callback_data=f"admin_approve_rcp_{receipt_id}"),
            InlineKeyboardButton("❌ رد فیش", callback_data=f"admin_reject_rcp_{receipt_id}"),
        ],
        [
            InlineKeyboardButton("👁 مشاهده جزئیات", callback_data=f"admin_detail_rcp_{receipt_id}"),
        ]
    ]
    if user_id:
        keyboard.append([InlineKeyboardButton(
            "👤 مشاهده پروفایل کاربر",
            callback_data=f"admin:user_detail:{int(user_id)}:rcp:{int(receipt_id)}")])
    return InlineKeyboardMarkup(keyboard)


def build_admin_reviewed_keyboard(status: str, user_id: int | None = None,
                                  receipt_id: int | None = None) -> InlineKeyboardMarkup:
    """Keyboard shown after an admin settles the receipt.

    The status chip is inert: pressing it must never re-run the review or query
    the panel again, it only reports that the decision is already final. The
    payer's profile and the way back to the admin desk stay reachable, and the
    profile remembers the receipt it was opened from so its own back button
    returns there instead of dumping the operator in the user list.
    """
    text = "✅ تایید شده" if status == "approved" else "❌ رد شده"
    rows = [[InlineKeyboardButton(text, callback_data="noop")]]
    if user_id:
        origin = f":rcp:{int(receipt_id)}" if receipt_id else ""
        rows.append([InlineKeyboardButton(
            "👤 مشاهده پروفایل کاربر",
            callback_data=f"admin:user_detail:{int(user_id)}{origin}")])
    rows.append([InlineKeyboardButton("👑 بازگشت به میز مدیر کل", callback_data="admin:main")])
    return InlineKeyboardMarkup(rows)


async def reply_with_receipt_detail(message, receipt: dict, text: str, reply_markup=None):
    """Show the persisted receipt image when available, with a text fallback."""
    photo_id = receipt.get("receipt_photo_id")
    if photo_id:
        try:
            return await message.reply_photo(
                photo=photo_id,
                caption=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
        except Exception:
            logger.exception(
                "Could not replay stored receipt image for receipt %s; using text fallback",
                receipt.get("id") or receipt.get("tracking_code"),
            )
    return await message.reply_text(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )


def build_wallet_menu_keyboard(balance_toman: int, is_rep: bool = False) -> InlineKeyboardMarkup:
    """Builds interactive wallet management keyboard."""
    if is_rep:
        buttons = [
            [InlineKeyboardButton("💳 تسویه حساب / ثبت فیش", callback_data="rep_deposit_start")],
            [InlineKeyboardButton("📊 ریز تراکنش‌های مالی", callback_data="rep_ledger_history")],
            [InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_menu")]
        ]
    else:
        buttons = [
            [
                InlineKeyboardButton("💳 شارژ کیف پول (کارت به کارت)", callback_data="wallet_charge_start"),
            ],
            [
                InlineKeyboardButton("🔄 استعلام وضعیت سرویس من", callback_data="user_my_status"),
            ],
            [InlineKeyboardButton("🔙 بازگشت", callback_data="back_to_menu")]
        ]
    return InlineKeyboardMarkup(buttons)


def format_card_instructions(amount_toman: int, tracking_code: str, card_info: dict) -> str:
    """Formats clear, RTL card-to-card payment guidance."""
    card_number = card_info.get("card_number", "0000-0000-0000-0000")
    card_holder = card_info.get("card_holder", "مدیریت")
    bank_name = card_info.get("bank_name", "بانک")

    return (
        f"💳 **دستورالعمل پرداخت کارت‌به‌کارت**\n\n"
        f"• مبلغ قابل پرداخت: **{amount_toman:,} تومان**\n"
        f"• شناسه رهگیری: `{tracking_code}`\n\n"
        f"📋 **اطلاعات حساب مقصد:**\n"
        f"• شماره کارت: `{card_number}`\n"
        f"• بنام: **{card_holder}**\n"
        f"• بانک: **{bank_name}**\n\n"
        f"⚠️ **نکات مهم:**\n"
        f"۱. لطفاً پس از واریز، تصویر فیش پرداختی یا شماره پیگیری تراکنش را از طریق دکمه زیر ارسال فرمایید.\n"
        f"۲. پس از تایید توسط سوپر ادمین، مبلغ به صورت آنی در سیستم ثبت و اعمال خواهد شد."
    )


RECEIPT_STATUS_FA = {
    "pending": "⏳ در انتظار بررسی",
    "approved": "✅ تایید شده",
    "rejected": "❌ رد شده",
}


def receipt_status_label(status) -> str:
    """Persian label for a receipt status; an unknown value is never blank."""
    return RECEIPT_STATUS_FA.get(str(status or "pending"), "⏳ در انتظار بررسی")


def format_admin_receipt_card(receipt: dict, user_full_name: str = "", username: str = "",
                              *, decided_by: str = "", decided_at: str = "") -> str:
    """Formats the verification card delivered to Super Admin.

    The card carries the current status so the SAME renderer can redraw the
    caption after a decision: approving a receipt must actually change the text
    from «⏳ در انتظار بررسی» to «✅ تایید شده», not only its keyboard.
    """
    role_fa = "نماینده فروش" if receipt.get("role") == "rep" else "کاربر نهایی"
    username_display = f"@{username}" if username else "ندارد"
    note = receipt.get("receipt_note") or "بدون توضیحات"
    status = str(receipt.get("status") or "pending")
    status_line = f"• وضعیت فعلی: **{receipt_status_label(status)}**"
    if status in ("approved", "rejected") and decided_by:
        status_line += f" (توسط {decided_by}" + (f" در {decided_at}" if decided_at else "") + ")"
    header = ("🔔 **فیش واریزی جدید جهت بررسی و تایید**" if status == "pending"
              else "🧾 **فیش واریزی (تعیین‌تکلیف‌شده)**")
    footer = ("جهت تایید آنی یا رد فیش، یکی از گزینه‌های زیر را انتخاب کنید:"
              if status == "pending" else "این فیش تعیین تکلیف شده است.")

    return (
        f"{header}\n\n"
        f"• کد پیگیری: `{receipt.get('tracking_code')}`\n"
        f"• نقش واریزکننده: **{role_fa}**\n"
        f"• شناسه عددی تلگرام: `{receipt.get('user_id')}`\n"
        f"• نام فرستنده: **{user_full_name}** ({username_display})\n"
        f"• مبلغ واریزی: **{receipt.get('amount_toman', 0):,} تومان**\n"
        f"• توضیحات فرستنده: _{html.escape(note)}_\n"
        f"• تاریخ ثبت: `{receipt.get('created_at', '')[:19]}`\n"
        f"{status_line}\n\n"
        f"{footer}"
    )
