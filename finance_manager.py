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
            InlineKeyboardButton("❌ انصراف", callback_data="cancel_payment"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def build_admin_receipt_review_keyboard(receipt_id: int) -> InlineKeyboardMarkup:
    """Builds Super Admin inline keyboard for receipt verification."""
    keyboard = [
        [
            InlineKeyboardButton("✅ تایید فیش", callback_data=f"admin_approve_rcp_{receipt_id}"),
            InlineKeyboardButton("❌ رد فیش", callback_data=f"admin_reject_rcp_{receipt_id}"),
        ],
        [
            InlineKeyboardButton("👁 مشاهده جزئیات", callback_data=f"admin_detail_rcp_{receipt_id}"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def build_admin_reviewed_keyboard(status: str) -> InlineKeyboardMarkup:
    """Keyboard shown after an admin settles the receipt."""
    text = "✅ تایید شده" if status == "approved" else "❌ رد شده"
    return InlineKeyboardMarkup([[InlineKeyboardButton(text, callback_data="noop")]])


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


def format_admin_receipt_card(receipt: dict, user_full_name: str = "", username: str = "") -> str:
    """Formats detailed verification card delivered to Super Admin."""
    role_fa = "نماینده فروش" if receipt.get("role") == "rep" else "کاربر نهایی"
    username_display = f"@{username}" if username else "ندارد"
    note = receipt.get("receipt_note") or "بدون توضیحات"

    return (
        f"🔔 **فیش واریزی جدید جهت بررسی و تایید**\n\n"
        f"• کد پیگیری: `{receipt.get('tracking_code')}`\n"
        f"• نقش واریزکننده: **{role_fa}**\n"
        f"• شناسه عددی تلگرام: `{receipt.get('user_id')}`\n"
        f"• نام فرستنده: **{user_full_name}** ({username_display})\n"
        f"• مبلغ واریزی: **{receipt.get('amount_toman', 0):,} تومان**\n"
        f"• توضیحات فرستنده: _{html.escape(note)}_\n"
        f"• تاریخ ثبت: `{receipt.get('created_at', '')[:19]}`\n\n"
        f"جهت تایید آنی یا رد فیش، یکی از گزینه‌های زیر را انتخاب کنید:"
    )
