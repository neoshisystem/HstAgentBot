"""
user_portal.py - End-User Interactive Portal for HstAgentBot
Provides a 100% inline keyboard-driven interface for End-Users:
1. [📱 سرویس‌های من] (مشاهده وضعیت، تمدید با کیف پول، لینک، QR، بازنشانی و حذف اتصال)
2. [🛒 خرید سرویس جدید] (انتخاب تعرفه و خرید خودکار از موجودی کیف پول)
3. [🔗 اتصال اکانت قبلی به تلگرام] (اتصال بدون نیاز به تایپ کامند)
4. [💳 کیف پول من و شارژ حساب] (موجودی، افزایش موجودی کارت‌به‌کارت و سوابق)
5. [📋 تعرفه‌ها و قیمت‌ها]
"""

import logging
import asyncio
import os
import sys
import time
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import database
import nlp_matcher
import finance_manager
import sub_manager

from server_integrations import load_integration
xui_manager = load_integration("xui_manager")

logger = logging.getLogger("UserPortal")


def build_user_portal_keyboard() -> InlineKeyboardMarkup:
    """Builds the main interactive portal keyboard for an end-user."""
    keyboard = [
        [
            InlineKeyboardButton("📱 سرویس‌های من", callback_data="user:services"),
            InlineKeyboardButton("🛒 خرید سرویس جدید", callback_data="user:buy_service"),
        ],
        [
            InlineKeyboardButton("💳 کیف پول و شارژ حساب", callback_data="user:wallet"),
            InlineKeyboardButton("🔗 اتصال اکانت به تلگرام", callback_data="user:prompt_link"),
        ],
        [
            InlineKeyboardButton("📋 تعرفه‌ها و قیمت‌ها", callback_data="user:pricing"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def build_back_to_user_keyboard(extra_buttons=None, back_callback="user:main") -> InlineKeyboardMarkup:
    """Builds two-tier navigation back button returning to previous step or user portal."""
    buttons = []
    if extra_buttons:
        for row in extra_buttons:
            buttons.append(row)
    if back_callback and back_callback != "user:main":
        buttons.append([
            InlineKeyboardButton("↩️ بازگشت به مرحله قبل", callback_data=back_callback),
            InlineKeyboardButton("🏠 منوی اصلی کاربری", callback_data="user:main")
        ])
    else:
        buttons.append([InlineKeyboardButton("🏠 بازگشت به پورتال کاربری", callback_data="user:main")])
    return InlineKeyboardMarkup(buttons)


async def show_user_portal(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message=False):
    """Renders or edits the end-user interactive portal."""
    user = update.effective_user
    database.ensure_bot_user(user.id, user.username or "", user.full_name or "", role="end_user")

    if database.is_user_blocked(user.id):
        msg = "⛔️ دسترسی حساب شما به این بات توسط مدیریت مسدود شده است."
        if update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        elif update.message:
            await update.message.reply_text(msg)
        return

    wallet_bal = database.get_user_wallet_balance(user.id)
    linked_accs = database.get_user_linked_accounts(user.id)

    text = (
        f"👋 **سلام {user.first_name} گرامی، به پورتال هوشمند خوش آمدید.**\n\n"
        f"• موجودی کیف پول شما: **{wallet_bal:,} تومان**\n"
        f"• تعداد سرویس‌های متصل: **{len(linked_accs)} سرویس**\n\n"
        "👇 لطفاً بخش مورد نظر خود را انتخاب نمایید:"
    )
    keyboard = build_user_portal_keyboard()

    if edit_message and update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        except BadRequest:
            pass
    elif update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


async def handle_user_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Routes and handles all end-user interactive callbacks (data starting with user:)."""
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat

    if database.is_user_blocked(user.id):
        await query.answer("⛔️ دسترسی شما به ربات مسدود شده است.", show_alert=True)
        return

    data = query.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "main"

    # Every service-specific user action must be re-authorized against the exact DB owner.
    account_actions = {
        "view_acc", "get_sub_link", "get_qr", "reset_sub",
        "unlink_confirm", "do_unlink", "renew_plans"
    }
    owned_account = None
    if action == "do_renew":
        intent_id = parts[2] if len(parts) > 2 else ""
        renew_intent = database.get_user_action_intent(intent_id, user.id)
        if not renew_intent or renew_intent["action_type"] != "renew":
            await query.answer("⛔️ درخواست تمدید معتبر نیست یا متعلق به شما نیست.", show_alert=True)
            return
        owned_account = database.get_user_owned_account(
            user.id, renew_intent.get("account_code") or renew_intent.get("account_email") or ""
        )
        if (
            not owned_account
            or str(owned_account["account_code"]).lower() != str(renew_intent.get("account_code") or "").lower()
            or str(owned_account["email"]).lower() != str(renew_intent.get("account_email") or "").lower()
        ):
            await query.answer("⛔️ مالکیت سرویس برای این درخواست قابل تأیید نیست.", show_alert=True)
            return
        # Retain the opaque token while normalizing the verified account target.
        parts = [parts[0], parts[1], owned_account["email"], renew_intent["plan_code"], intent_id]
    elif action in account_actions:
        requested_identifier = parts[2] if len(parts) > 2 else ""
        owned_account = database.get_user_owned_account(user.id, requested_identifier)
        if not owned_account:
            await query.answer("⛔️ این سرویس به حساب تلگرام شما متصل نیست.", show_alert=True)
            return
        parts[2] = owned_account["email"]

    # 1. Main Portal
    if action == "main":
        await show_user_portal(update, context, edit_message=True)
        await query.answer("پورتال کاربری")
        return

    # 2. My Services List
    elif action == "services":
        accs = database.get_user_linked_accounts(user.id)
        if not accs:
            lines = [
                "📱 **سرویس‌های من:**\n",
                "در حال حاضر هیچ سرویسی به تلگرام شما متصل نیست!\n",
                "• اگر قبلاً سرویسی خریده‌اید، با دکمه «اتصال اکانت به تلگرام» آن را اضافه کنید.",
                "• اگر سرویس جدید می‌خواهید، از دکمه «خرید سرویس جدید» استفاده نمایید."
            ]
            extra_btns = [
                [
                    InlineKeyboardButton("🛒 خرید سرویس جدید", callback_data="user:buy_service"),
                    InlineKeyboardButton("🔗 اتصال اکانت به تلگرام", callback_data="user:prompt_link")
                ]
            ]
            keyboard = build_back_to_user_keyboard(extra_btns)
            await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer()
            return

        lines = [
            f"📱 **سرویس‌های فعال شما ({len(accs)} سرویس):**\n",
            "جهت مشاهده وضعیت، تمدید، دریافت لینک یا QR، روی سرویس مورد نظر کلیک فرمایید:\n"
        ]
        extra_btns = []
        row = []
        for a in accs:
            c_name = f" ({a['customer_name']})" if a.get("customer_name") else ""
            status_dot = "🟢" if a.get("enable") else "🛑"
            row.append(InlineKeyboardButton(f"{status_dot} {a['account_code']}{c_name}", callback_data=f"user:view_acc:{a['account_code']}"))
            if len(row) == 2:
                extra_btns.append(row)
                row = []
        if row:
            extra_btns.append(row)

        keyboard = build_back_to_user_keyboard(extra_btns)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("لیست سرویس‌ها")

    # 2.1 View Specific Service
    elif action == "view_acc":
        acc_code = parts[2] if len(parts) > 2 else ""
        insp = await asyncio.to_thread(xui_manager.inspect_client, acc_code, scope="all", by_code_only=True)
        if insp.get("status") == "ok":
            client_info = insp["data"]
            report_text = xui_manager.format_status_report(client_info, is_admin=False)
            
            # Custom end-user service keyboard
            keyboard_btns = [
                [
                    InlineKeyboardButton("📊 بروزرسانی وضعیت", callback_data=f"user:view_acc:{acc_code}"),
                    InlineKeyboardButton("⚡️ تمدید با کیف پول", callback_data=f"user:renew_plans:{acc_code}")
                ],
                [
                    InlineKeyboardButton("🔗 دریافت لینک ساب", callback_data=f"user:get_sub_link:{acc_code}"),
                    InlineKeyboardButton("📱 QR کد ساب", callback_data=f"user:get_qr:{acc_code}:sub")
                ],
                [
                    InlineKeyboardButton("🔄 بازنشانی لینک ساب", callback_data=f"user:reset_sub:{acc_code}"),
                    InlineKeyboardButton("❌ قطع اتصال از تلگرام", callback_data=f"user:unlink_confirm:{acc_code}")
                ],
                [
                    InlineKeyboardButton("↩️ بازگشت به سرویس‌های من", callback_data="user:services"),
                    InlineKeyboardButton("🏠 منوی اصلی کاربری", callback_data="user:main")
                ]
            ]
            await query.edit_message_text(report_text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(keyboard_btns))
            await query.answer()
        else:
            await query.answer("⚠️ اطلاعات این سرویس در دسترس نیست.", show_alert=True)

    # 2.2 Sub Link Delivery
    elif action == "get_sub_link":
        acc_code = parts[2] if len(parts) > 2 else ""
        insp = await asyncio.to_thread(xui_manager.inspect_client, acc_code, scope="all", by_code_only=True)
        if insp.get("status") == "ok":
            sub_link = insp["data"].get("sub_link") or sub_manager.get_subscription_url(insp["data"].get("sub_id", ""))
            if not sub_link:
                await query.answer("لینک اشتراک در تنظیمات دامنه تعریف نشده است.", show_alert=True)
                return
            msg = (
                f"🔗 **لینک اشتراک سابسکریپشن اکانت {acc_code}:**\n\n"
                f"`{sub_link}`\n\n"
                "💡 این لینک را کپی کرده و در نرم‌افزار اتصال خود (v2rayNG, Streisand, v2box, Hiddify) وارد فرمایید."
            )
            await query.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
            await query.answer("لینک ارسال شد.")
        else:
            await query.answer("❌ خطا در بازیابی لینک.", show_alert=True)

    # 2.3 QR Delivery
    elif action == "get_qr":
        acc_code = parts[2] if len(parts) > 2 else ""
        qr_type = parts[3] if len(parts) > 3 else "sub"
        insp = await asyncio.to_thread(xui_manager.inspect_client, acc_code, scope="all", by_code_only=True)
        if insp.get("status") == "ok":
            client_info = insp["data"]
            sub_url = client_info.get("sub_link") or sub_manager.get_subscription_url(client_info.get("sub_id", ""))
            if not sub_url:
                await query.answer("دامنه سابسکریپشن در تنظیمات سامانه تعریف نشده است.", show_alert=True)
                return
            qr_bytes = sub_manager.generate_qr_code(sub_url)
            caption = f"📱 **QR کد اشتراک اکانت {acc_code}**\n\nلینک: `{sub_url}`"
            await query.message.reply_photo(photo=qr_bytes, caption=caption, parse_mode=ParseMode.MARKDOWN)
            await query.answer("QR ارسال شد.")
        else:
            await query.answer("❌ خطا در تولید QR.", show_alert=True)

    # 2.4 Reset Sub
    elif action == "reset_sub":
        acc_code = parts[2] if len(parts) > 2 else ""
        res = await asyncio.to_thread(
            sub_manager.reset_client_subscription, xui_manager, acc_code, scope="all"
        )

        # A partial result means the external panel already rotated the subId,
        # but a public URL could not be constructed. Persist the new ID anyway
        # so SQLite never continues to advertise the revoked subscription token.
        if res.get("status") in ("ok", "partial") and res.get("new_sub_id"):
            try:
                database.upsert_account_record(
                    rep_id=owned_account.get("rep_id"),
                    email=owned_account["email"],
                    account_code=owned_account["account_code"],
                    sub_id=res["new_sub_id"],
                )
            except Exception:
                logger.exception(
                    "3X-UI rotated subscription id for %s but local persistence failed",
                    owned_account.get("email"),
                )
                await query.answer(
                    "⚠️ پنل لینک قبلی را باطل و شناسه جدید ایجاد کرده، اما ثبت محلی کامل نشد؛ موضوع نیازمند تطبیق مدیریتی است.",
                    show_alert=True,
                )
                return

            if res.get("status") == "ok" and res.get("new_sub_url"):
                msg = (
                    f"🔄 **لینک اشتراک اکانت {acc_code} با موفقیت بازنشانی شد!**\n\n"
                    "لینک قبلی باطل گردید. لینک جدید را جایگزین کنید:\n\n"
                    f"{res['new_sub_url']}"
                )
                await query.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
                await query.answer("لینک ساب بازنشانی شد.")
            else:
                await query.answer(
                    "⚠️ لینک قبلی باطل شد و شناسه جدید در دیتابیس ثبت گردید، اما لینک عمومی ساخته نشد. تنظیمات دامنه سابسکریپشن را بررسی کنید.",
                    show_alert=True,
                )
        else:
            await query.answer(
                f"❌ خطا: {res.get('message', 'بازنشانی انجام نشد')}",
                show_alert=True,
            )

    # 2.5 Unlink Confirm
    elif action == "unlink_confirm":
        acc_code = parts[2] if len(parts) > 2 else ""
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ بله، قطع اتصال شود", callback_data=f"user:do_unlink:{acc_code}"),
                InlineKeyboardButton("❌ انصراف", callback_data=f"user:view_acc:{acc_code}")
            ]
        ])
        await query.edit_message_text(
            f"⚠️ **آیا مطمئن هستید که می‌خواهید اتصال اکانت `{acc_code}` از تلگرام شما حذف شود؟**\n\n"
            "(این عمل سرویس شما را متوقف نمی‌کند، فقط اتصال آن از این بات قطع می‌شود.)",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )
        await query.answer()

    elif action == "do_unlink":
        acc_code = parts[2] if len(parts) > 2 else ""
        unlinked = database.unlink_account_owner(acc_code, user.id)
        if not unlinked:
            await query.answer("⛔️ سرویس در اختیار شما نیست یا قبلاً قطع شده است.", show_alert=True)
            return
        await query.answer("✅ اتصال اکانت با موفقیت قطع شد.", show_alert=True)
        await show_user_portal(update, context, edit_message=True)

    # 3. Buy Service (Self-Service)
    elif action == "buy_service":
        plans = database.get_pricing_plans(active_only=True)
        wallet_bal = database.get_user_wallet_balance(user.id)
        lines = [
            "🛒 **خرید سرویس اینترنت آزاد:**\n",
            f"• موجودی کیف پول شما: **{wallet_bal:,} تومان**\n",
            "یکی از بسته‌های زیر را جهت فعال‌سازی آنی انتخاب نمایید:\n"
        ]
        extra_btns = []
        row = []
        for p in plans:
            intent_id = database.create_user_action_intent(
                user_id=user.id, action_type="buy",
                plan_code=p["plan_code"], plan_name=p["name"],
                gb=p["gb"], days=p["days"], amount=p["user_price"],
            )
            row.append(InlineKeyboardButton(
                f"{p['name']} ({p['user_price']:,} ت)",
                callback_data=f"user:confirm_buy:{intent_id}"
            ))
            if len(row) == 2:
                extra_btns.append(row)
                row = []
        if row:
            extra_btns.append(row)

        keyboard = build_back_to_user_keyboard(extra_btns)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("خرید سرویس")

    # 3.1 Confirm Buy
    elif action == "confirm_buy":
        intent_id = parts[2] if len(parts) > 2 else ""
        intent = database.get_user_action_intent(intent_id, user.id)
        if (
            not intent or intent["action_type"] != "buy"
            or intent["status"] != "pending"
            or float(intent["expires_at"]) <= time.time()
        ):
            await query.answer("⛔️ این درخواست خرید منقضی شده یا قبلاً استفاده شده است؛ منوی خرید را دوباره باز کنید.", show_alert=True)
            return

        wallet_bal = database.get_user_wallet_balance(user.id)
        price = int(intent["amount"])
        if wallet_bal < price:
            deficit = price - wallet_bal
            lines = [
                "⚠️ **موجودی کیف پول شما کافی نیست!**\n",
                f"• هزینه پلن انتخابی ({intent['plan_name']}): **{price:,} تومان**",
                f"• موجودی فعلی شما: **{wallet_bal:,} تومان**",
                f"• کسری موجودی: **{deficit:,} تومان**\n",
                "لطفاً ابتدا کیف پول خود را شارژ نمایید:"
            ]
            keyboard = build_back_to_user_keyboard([
                [InlineKeyboardButton("➕ شارژ کیف پول (کارت‌به‌کارت)", callback_data="user_deposit_start")]
            ])
            await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer()
            return

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    f"✅ تایید و خرید ({price:,} تومان)",
                    callback_data=f"user:do_buy:{intent_id}"
                ),
                InlineKeyboardButton("❌ انصراف", callback_data="user:buy_service")
            ]
        ])
        await query.edit_message_text(
            f"🛒 **تایید نهایی خرید:**\n\n"
            f"• بسته: **{intent['plan_name']}** ({intent['gb']} گیگابایت / {intent['days']} روز)\n"
            f"• مبلغ کسر از کیف پول: **{price:,} تومان**\n"
            f"• موجودی پس از خرید: **{wallet_bal - price:,} تومان**\n\n"
            "آیا مایل به تکمیل خرید هستید؟",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )
        await query.answer()


    # 3.2 Execute Buy — durable intent token prevents a callback replay from charging twice.
    elif action == "do_buy":
        intent_id = parts[2] if len(parts) > 2 else ""
        try:
            started = database.begin_user_action_intent(intent_id, user.id, "buy")
        except Exception:
            logger.exception("Could not claim user purchase intent for user %s", user.id)
            await query.answer("❌ شروع تراکنش خرید ممکن نشد؛ لطفاً دوباره منوی خرید را باز کنید.", show_alert=True)
            return

        if started["status"] != "started":
            messages = {
                "not_found": "⛔️ درخواست خرید پیدا نشد یا متعلق به شما نیست.",
                "wrong_action": "⛔️ نوع درخواست خرید معتبر نیست.",
                "already_processed": "⛔️ این درخواست قبلاً اجرا یا برای بررسی ثبت شده است؛ برداشت مجدد انجام نمی‌شود.",
                "expired": "⌛️ درخواست خرید منقضی شده است؛ منوی خرید را دوباره باز کنید.",
                "plan_changed": "⚠️ تعرفه پس از نمایش منو تغییر کرده است؛ لطفاً منوی خرید را تازه کنید.",
                "busy": "⏳ درخواست دیگری در حال پردازش است؛ پس از پایان آن منوی خرید را دوباره باز کنید.",
                "not_owned": "⛔️ مالکیت سرویس برای این درخواست قابل تأیید نیست.",
            }
            if started["status"] == "insufficient_funds":
                msg = f"❌ موجودی کافی نیست (موجودی: {started['balance']:,} ت | موردنیاز: {started['required']:,} ت)."
            else:
                msg = messages.get(started["status"], "❌ درخواست خرید قابل اجرا نیست.")
            await query.answer(msg, show_alert=True)
            return

        intent = started["intent"]
        price = int(intent["amount"])
        gb = float(intent["gb"])
        days = int(intent["days"])
        lock_key = started["lock_key"]
        lock_token = started["lock_token"]
        new_bal = started["new_balance"]
        comment = f"User_{user.id}_{user.first_name or ''}"[:30]
        try:
            alloc_res = await asyncio.to_thread(
                xui_manager.allocate_pool_client, gb, days, comment=comment, scope="all"
            )
        except Exception:
            logger.exception("User purchase outcome is unknown for user %s", user.id)
            try:
                database.mark_user_action_intent_for_reconciliation(
                    intent_id, user.id, lock_key, lock_token,
                    "X-UI allocation raised an exception; external outcome must be reconciled",
                )
            except Exception:
                logger.exception("Could not mark user purchase intent for reconciliation (%s)", intent_id)
            await query.answer(
                "⚠️ پاسخ پنل نامشخص است. مبلغ محفوظ مانده و درخواست برای تطبیق مدیریتی قفل شده است.",
                show_alert=True
            )
            return

        if isinstance(alloc_res, dict) and alloc_res.get("status") == "success":
            email = alloc_res.get("email")
            sub_id = alloc_res.get("sub_id")
            if not email:
                logger.error("X-UI reports success but has no email for user purchase %s: %r", user.id, alloc_res)
                try:
                    database.mark_user_action_intent_for_reconciliation(
                        intent_id, user.id, lock_key, lock_token,
                        "X-UI reported allocation success without an account email",
                    )
                except Exception:
                    logger.exception("Could not mark incomplete purchase result for reconciliation (%s)", intent_id)
                await query.answer("⚠️ پنل موفقیت را گزارش کرد اما شناسه اکانت ناقص است؛ مبلغ محفوظ و عملیات قفل شد.", show_alert=True)
                return

            code = xui_manager.extract_account_identifier(email) or email
            try:
                database.upsert_account_record(
                    rep_id=None,
                    email=email,
                    account_code=code,
                    customer_name=user.full_name or "EndUser",
                    sub_id=sub_id,
                    enable=1,
                    total_gb=gb,
                    expiry_time=alloc_res.get("data", {}).get("expiry_time", 0),
                    owner_tg_id=user.id
                )
            except Exception:
                logger.exception("X-UI allocated account %s but local persistence failed", email)
                try:
                    database.mark_user_action_intent_for_reconciliation(
                        intent_id, user.id, lock_key, lock_token,
                        f"X-UI created account {email}, but local account persistence failed",
                    )
                except Exception:
                    logger.exception("Could not mark local persistence failure for reconciliation (%s)", intent_id)
                await query.answer(
                    "⚠️ اکانت در پنل ایجاد شده اما ثبت محلی کامل نشد؛ مبلغ محفوظ و عملیات قفل شد.",
                    show_alert=True
                )
                return

            try:
                database.complete_user_action_intent(
                    intent_id, user.id, lock_key, lock_token,
                    result_note=f"Allocated account {code}",
                )
            except Exception:
                logger.exception("Could not finalize successful purchase intent %s", intent_id)
                try:
                    database.mark_user_action_intent_for_reconciliation(
                        intent_id, user.id, lock_key, lock_token,
                        f"X-UI account {code} and local record exist, but intent finalization failed",
                    )
                except Exception:
                    logger.exception("Could not mark unfinalized successful purchase for reconciliation (%s)", intent_id)
                await query.answer("⚠️ اکانت ثبت شد اما نهایی‌سازی تراکنش کامل نشد؛ عملیات برای تطبیق مدیریتی قفل باقی ماند.", show_alert=True)
                return

            sub_url = alloc_res.get("data", {}).get("sub_link") or sub_manager.get_subscription_url(sub_id or "")
            delivered_link = (
                f"🔗 **لینک اختصاصی شما:**\n{sub_url}\n\n"
                if sub_url else
                "⚠️ لینک عمومی سابسکریپشن در تنظیمات سرور تعریف نشده است. برای دریافت لینک با پشتیبانی تماس بگیرید.\n\n"
            )
            deliv_text = (
                f"🎉 **سرویس شما با موفقیت فعال و متصل شد!**\n\n"
                f"• کد شناسه سرویس: {code}\n"
                f"• حجم: **{int(gb)} گیگابایت** | مدت: **{days} روز**\n"
                f"• کسر از کیف پول: {price:,} تومان | مانده کیف پول: {new_bal:,} تومان\n\n"
                f"{delivered_link}"
                "💡 شناسه سرویس را برای پیگیری‌های بعدی نگهداری کنید."
            )
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except BadRequest:
                pass
            if sub_url:
                try:
                    qr_bytes = sub_manager.generate_qr_code(sub_url)
                    await query.message.reply_photo(
                        photo=qr_bytes, caption=deliv_text,
                        parse_mode=ParseMode.MARKDOWN,
                        reply_markup=build_user_portal_keyboard()
                    )
                except Exception:
                    logger.exception("Could not deliver purchase QR for %s; sending text fallback", code)
                    await query.message.reply_text(
                        deliv_text, parse_mode=ParseMode.MARKDOWN,
                        reply_markup=build_user_portal_keyboard()
                    )
            else:
                await query.message.reply_text(
                    deliv_text, parse_mode=ParseMode.MARKDOWN,
                    reply_markup=build_user_portal_keyboard()
                )
            await query.answer("🎉 خرید با موفقیت انجام شد!")
        elif isinstance(alloc_res, dict) and (
            alloc_res.get("status") in ("error", "failed", "failure")
            or alloc_res.get("success") is False
        ):
            try:
                database.refund_user_wallet_and_release_operation_lock(
                    user_id=user.id,
                    amount=price,
                    description="استرداد وجه به دلیل شکست قطعی تخصیص اکانت",
                    actor_id=user.id,
                    actor_name="SystemRefund",
                    lock_key=lock_key,
                    token=lock_token,
                    intent_id=intent_id,
                )
            except Exception:
                logger.exception(
                    "User purchase refund failed; persistent operation lock retained for reconciliation (user=%s)",
                    user.id,
                )
                await query.answer(
                    "⚠️ صدور ناموفق بود اما استرداد در دیتابیس ثبت نشد؛ قفل عملیات برای بررسی مدیریت محفوظ مانده است.",
                    show_alert=True,
                )
                return
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except BadRequest:
                pass
            await query.message.reply_text(
                f"❌ صدور سرویس توسط پنل رد شد. مبلغ {price:,} تومان به کیف پول بازگشت داده شد."
            )
            await query.answer("صدور سرویس ناموفق بود.", show_alert=True)
        else:
            logger.error("Ambiguous X-UI allocation response for user %s: %r", user.id, alloc_res)
            try:
                database.mark_user_action_intent_for_reconciliation(
                    intent_id, user.id, lock_key, lock_token,
                    f"Ambiguous X-UI allocation response: {alloc_res!r}"[:500],
                )
            except Exception:
                logger.exception("Could not mark ambiguous purchase intent for reconciliation (%s)", intent_id)
            await query.answer(
                "⚠️ پاسخ پنل برای تایید یا رد خرید کافی نیست؛ مبلغ محفوظ و عملیات قفل شده است.",
                show_alert=True
            )

    # 4. User Wallet View
    elif action == "wallet":
        bal = database.get_user_wallet_balance(user.id)
        card_info = database.get_card_info()
        lines = [
            f"💳 **کیف پول من: {user.first_name}**\n",
            f"• موجودی فعلی: **{bal:,} تومان**\n",
            "با شارژ کیف پول می‌توانید در هر زمان به سادگی اقدام به خرید سرویس جدید یا تمدید سرویس‌های فعلی خود نمایید.",
        ]
        extra_btns = [
            [
                InlineKeyboardButton("➕ افزایش موجودی (کارت‌به‌کارت)", callback_data="user_deposit_start"),
                InlineKeyboardButton("📑 سوابق پرداخت من", callback_data="user:receipts")
            ]
        ]
        keyboard = build_back_to_user_keyboard(extra_btns)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("کیف پول")

    # 4.1 User Receipts
    elif action == "receipts":
        rcps = database.get_user_receipts(user.id, limit=10)
        lines = ["📑 **سوابق پرداخت‌ها و فیش‌های واریزی شما:**\n"]
        if not rcps:
            lines.append("• تاکنون هیچ تراکنش واریزی ثبت نکرده‌اید.")
        else:
            for r in rcps:
                status_icon = "⏳ در انتظار تایید" if r["status"] == "pending" else ("✅ تایید شده" if r["status"] == "approved" else "❌ رد شده")
                lines.append(
                    f"• `{r['tracking_code']}` | **{r['amount_toman']:,} ت** | {status_icon}\n"
                    f"  تاریخ: `{r['created_at'][:16]}`"
                )
                if r.get("rejection_reason") and r["status"] == "rejected":
                    lines.append(f"  علت رد: _{r['rejection_reason']}_")

        keyboard = build_back_to_user_keyboard([[InlineKeyboardButton("➕ شارژ مجدد کیف پول", callback_data="user_deposit_start")]], back_callback="user:wallet")
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("سوابق پرداخت")

    # 5. Link Account Prompt
    elif action == "prompt_link":
        context.user_data["user_state"] = {
            "action": "awaiting_link_code",
            "user_id": user.id,
            "ts": time.time()
        }
        keyboard = build_back_to_user_keyboard()
        msg = (
            "🔗 **اتصال اکانت به حساب کاربری تلگرام شما:**\n\n"
            "لطفاً **کد ۴ رقمی اکانت** یا **نام ایمیل سرویس** خود را ارسال فرمایید تا این سرویس به منوی شما متصل گردد:\n\n"
            "💡 (مثال: `1001` یا `Hst-sm-1001`)"
        )
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer()

    # 6. Pricing Plans for Users
    elif action == "pricing":
        plans = database.get_pricing_plans(active_only=True)
        lines = ["📋 **لیست تعرفه‌های اینترنت پرسرعت:**\n"]
        for p in plans:
            lines.append(
                f"• **{p['name']}**:\n"
                f"   حجم: `{int(p['gb'])} گیگابایت` | مدت: `{int(p['days'])} روز` | قیمت: **{p['user_price']:,} تومان**\n"
            )
        keyboard = build_back_to_user_keyboard([
            [InlineKeyboardButton("🛒 خرید سرویس", callback_data="user:buy_service")]
        ])
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("تعرفه‌ها")

    # 7. Renew Account Via Wallet Selection
    elif action == "renew_plans":
        acc_code = owned_account["account_code"]
        plans = database.get_pricing_plans(active_only=True)
        wallet_bal = database.get_user_wallet_balance(user.id)
        lines = [
            f"⚡️ **تمدید سرویس {acc_code} با کیف پول:**\n",
            f"• موجودی کیف پول: **{wallet_bal:,} تومان**\n",
            "پلن تمدید مورد نظر را انتخاب فرمایید:\n"
        ]
        extra_btns = []
        row = []
        for p in plans:
            intent_id = database.create_user_action_intent(
                user_id=user.id,
                action_type="renew",
                plan_code=p["plan_code"],
                plan_name=p["name"],
                gb=p["gb"],
                days=p["days"],
                amount=p["user_price"],
                account_code=owned_account["account_code"],
                account_email=owned_account["email"],
            )
            row.append(InlineKeyboardButton(
                f"{p['name']} ({p['user_price']:,} ت)",
                callback_data=f"user:do_renew:{intent_id}"
            ))
            if len(row) == 2:
                extra_btns.append(row)
                row = []
        if row:
            extra_btns.append(row)

        keyboard = build_back_to_user_keyboard(extra_btns)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer()

    # 7.1 Execute Renew with Wallet — intent token resolves the canonical owned account.
    elif action == "do_renew":
        intent_id = parts[4] if len(parts) > 4 else ""
        acc_code = owned_account["email"]
        try:
            started = database.begin_user_action_intent(intent_id, user.id, "renew")
        except Exception:
            logger.exception("Could not claim user renewal intent for %s", intent_id)
            await query.answer("❌ شروع تراکنش تمدید ممکن نشد؛ لطفاً منوی تمدید را دوباره باز کنید.", show_alert=True)
            return

        if started["status"] != "started":
            messages = {
                "not_found": "⛔️ درخواست تمدید پیدا نشد یا متعلق به شما نیست.",
                "wrong_action": "⛔️ نوع درخواست تمدید معتبر نیست.",
                "already_processed": "⛔️ این درخواست تمدید قبلاً اجرا یا برای بررسی ثبت شده است؛ برداشت مجدد انجام نمی‌شود.",
                "expired": "⌛️ درخواست تمدید منقضی شده است؛ منوی تمدید را دوباره باز کنید.",
                "plan_changed": "⚠️ تعرفه پس از نمایش منو تغییر کرده است؛ لطفاً منوی تمدید را تازه کنید.",
                "busy": "⏳ عملیات دیگری روی این سرویس در جریان است؛ منوی تمدید را پس از پایان آن دوباره باز کنید.",
                "not_owned": "⛔️ مالکیت سرویس برای این درخواست قابل تأیید نیست.",
            }
            if started["status"] == "insufficient_funds":
                msg = f"❌ موجودی کافی نیست (موجودی: {started['balance']:,} ت | موردنیاز: {started['required']:,} ت)."
            else:
                msg = messages.get(started["status"], "❌ درخواست تمدید قابل اجرا نیست.")
            await query.answer(msg, show_alert=True)
            return

        intent = started["intent"]
        plan_name = intent["plan_name"]
        price = int(intent["amount"])
        lock_key = started["lock_key"]
        lock_token = started["lock_token"]
        new_bal = started["new_balance"]

        try:
            renew_res = await asyncio.to_thread(
                xui_manager.renew_client,
                acc_code, float(intent["gb"]), int(intent["days"]),
                comment=None, rollover=True, countdown="auto", scope="all", by_code_only=True
            )
        except Exception:
            logger.exception("User renewal outcome is unknown for %s", acc_code)
            try:
                database.mark_user_action_intent_for_reconciliation(
                    intent_id, user.id, lock_key, lock_token,
                    "X-UI renewal raised an exception; external outcome must be reconciled",
                )
            except Exception:
                logger.exception("Could not mark renewal intent for reconciliation (%s)", intent_id)
            await query.answer(
                "⚠️ پاسخ پنل نامشخص است؛ مبلغ محفوظ و عملیات برای تطبیق مدیریتی قفل شده است.",
                show_alert=True
            )
            return

        if isinstance(renew_res, dict) and (
            renew_res.get("status") in ("success", "ok") or renew_res.get("success") is True
        ):
            try:
                database.complete_user_action_intent(
                    intent_id, user.id, lock_key, lock_token,
                    result_note=f"Renewed account {intent['account_code']}",
                )
            except Exception:
                logger.exception("Could not finalize successful renewal intent %s", intent_id)
                try:
                    database.mark_user_action_intent_for_reconciliation(
                        intent_id, user.id, lock_key, lock_token,
                        f"X-UI renewal succeeded for {intent['account_code']}, but intent finalization failed",
                    )
                except Exception:
                    logger.exception("Could not mark unfinalized renewal for reconciliation (%s)", intent_id)
                await query.answer("⚠️ تمدید در پنل انجام شد اما نهایی‌سازی تراکنش کامل نشد؛ عملیات برای بررسی قفل باقی ماند.", show_alert=True)
                return
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except BadRequest:
                pass
            await query.message.reply_text(
                f"✅ **سرویس {acc_code} با موفقیت تمدید شد!**\n\n"
                f"• بسته: **{plan_name}**\n"
                f"• کسر از کیف پول: {price:,} تومان\n"
                f"• موجودی باقیمانده: {new_bal:,} تومان",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_user_portal_keyboard()
            )
            await query.answer("سرویس با موفقیت تمدید شد.")
        elif isinstance(renew_res, dict) and (
            renew_res.get("status") in ("error", "failed", "failure")
            or renew_res.get("success") is False
        ):
            try:
                database.refund_user_wallet_and_release_operation_lock(
                    user_id=user.id,
                    amount=price,
                    description=f"استرداد به دلیل شکست قطعی تمدید {acc_code}",
                    actor_id=user.id,
                    actor_name="RenewRefund",
                    lock_key=lock_key,
                    token=lock_token,
                    intent_id=intent_id,
                )
            except Exception:
                logger.exception(
                    "User renewal refund failed; persistent operation lock retained for reconciliation (account=%s)",
                    acc_code,
                )
                await query.answer(
                    "⚠️ تمدید ناموفق بود اما استرداد در دیتابیس ثبت نشد؛ قفل عملیات برای بررسی مدیریت محفوظ مانده است.",
                    show_alert=True,
                )
                return
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except BadRequest:
                pass
            await query.answer("❌ پنل تمدید را رد کرد؛ مبلغ به کیف پول بازگشت داده شد.", show_alert=True)
        else:
            logger.error("Ambiguous X-UI renewal response for %s: %r", acc_code, renew_res)
            try:
                database.mark_user_action_intent_for_reconciliation(
                    intent_id, user.id, lock_key, lock_token,
                    f"Ambiguous X-UI renewal response: {renew_res!r}"[:500],
                )
            except Exception:
                logger.exception("Could not mark ambiguous renewal intent for reconciliation (%s)", intent_id)
            await query.answer(
                "⚠️ پاسخ پنل برای تایید یا رد تمدید کافی نیست؛ مبلغ محفوظ و عملیات قفل شده است.",
                show_alert=True
            )



async def process_user_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Processes FSM inputs initiated from end-user portal."""
    user = update.effective_user
    user_state = context.user_data.get("user_state")
    if not user_state:
        return False

    if time.time() - user_state.get("ts", 0) > 90:
        context.user_data.pop("user_state", None)
        return False

    msg_text = update.message.text.strip()
    action = user_state.get("action")

    if msg_text.lower() in ("cancel", "انصراف", "لغو"):
        context.user_data.pop("user_state", None)
        await update.message.reply_text("❌ عملیات لغو شد.", reply_markup=build_user_portal_keyboard())
        return True

    if action == "awaiting_link_code":
        context.user_data.pop("user_state", None)
        linked = database.link_user_to_account(msg_text, user.id, user.username or "", user.full_name or "")
        if isinstance(linked, dict) and linked.get("status") == "ok":
            await update.message.reply_text(
                f"✅ **اکانت `{msg_text}` با موفقیت به حساب تلگرام شما متصل گردید.**\n\n"
                "از این پس می‌توانید وضعیت مصرف این سرویس را در بخش «📱 سرویس‌های من» مشاهده و تمدید نمایید.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_user_portal_keyboard()
            )
        else:
            await update.message.reply_text(
                f"⚠️ اکانتی با مشخصه `{msg_text}` در سیستم یافت نشد.\n"
                "لطفاً مطمئن شوید کد یا نام ایمیل را به درستی وارد کرده‌اید.",
                reply_markup=build_user_portal_keyboard()
            )
        return True

    return False
