import os
import sys
import logging
import asyncio
from datetime import datetime, timezone

from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from config import (
    BOT_TOKEN,
    ADMIN_TELEGRAM_ID,
    MAIN_GROUP_ID,
    MY_PROJECT_GROUP_ID,
    OPERATIONAL_LOGS_TOPIC_ID,
    ADMIN_DM_LOGS_TOPIC_ID
)
import config
import database
import nlp_matcher
import admin_panel
import rep_panel
import user_portal
import finance_manager
import sub_manager
from watchdog_service import WatchdogService

from server_integrations import load_integration
xui_manager = load_integration("xui_manager")
backup_and_report = load_integration("backup_and_report")

# Set up logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger("HstAgentBot")


def _xui_operation_succeeded(result) -> bool:
    """Accept only an explicit successful response from the external panel."""
    return isinstance(result, dict) and (
        result.get("status") in ("success", "ok") or result.get("success") is True
    )

def _xui_operation_failed(result) -> bool:
    """Return True only for an explicit negative response, never for an ambiguous one."""
    return isinstance(result, dict) and (
        result.get("status") in ("error", "failed", "failure")
        or result.get("success") is False
    )


async def safe_edit_message(query, text: str, reply_markup=None, parse_mode=ParseMode.MARKDOWN):
    """
    Safely edits a message text or caption (for photo messages like QR codes),
    preventing BadRequest crashes when clicking buttons on photo cards.
    """
    try:
        if query.message and query.message.photo:
            return await query.edit_message_caption(caption=text, reply_markup=reply_markup, parse_mode=parse_mode)
        else:
            return await query.edit_message_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return None
        logger.warning(f"safe_edit_message fallback on error: {e}. Sending new message.")
        return await query.message.reply_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except Exception as e:
        logger.error(f"safe_edit_message unhandled error: {e}")

def send_admin_log(text: str, rep_id: int | None = None):
    """
    Sends operational event notifications:
    - Routes logs to the designated representative's log topic or the operational log topic.
    - Mirrors to Super Admin DM if configured in system settings.
    """
    try:
        import re
        import json
        import urllib.request
        clean_html = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        clean_html = re.sub(r"\*\*(.*?)\*\*", r"<b>\1</b>", clean_html)
        clean_html = re.sub(r"`(.*?)`", r"<code>\1</code>", clean_html)

        rep_topic = None
        if rep_id:
            rep = database.get_rep_by_id(rep_id)
            if rep and rep.get("topic_id"):
                rep_topic = rep["topic_id"]

        op_top = database.get_setting("operational_logs_topic_id", default=OPERATIONAL_LOGS_TOPIC_ID, cast_type=int)
        target_topic = rep_topic or op_top

        try:
            backup_and_report.send_telegram_message(target_topic, clean_html, parse_mode="HTML")
        except Exception:
            backup_and_report.send_telegram_message(target_topic, text, parse_mode=None)

        admin_dm_top = database.get_setting("admin_dm_topic_id", default=ADMIN_DM_LOGS_TOPIC_ID, cast_type=int)
        admin_tg_id = database.get_setting("admin_telegram_id", default=ADMIN_TELEGRAM_ID, cast_type=int)
        if admin_tg_id and admin_dm_top:
            try:
                url = f"https://api.telegram.org/bot{backup_and_report.BOT_TOKEN}/sendMessage"
                payload = {
                    "chat_id": admin_tg_id,
                    "message_thread_id": admin_dm_top,
                    "text": clean_html,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True
                }
                urllib.request.urlopen(urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"}
                ), timeout=10)
            except Exception as e:
                logger.error(f"Failed to mirror log to Admin DM: {e}")
    except Exception as e:
        logger.error(f"Failed to send admin log: {e}")

# --- Commands ---

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    thread_id = update.message.message_thread_id if update.message else None
    
    # Always clear any active FSM state on start/admin commands
    context.user_data.pop("admin_state", None)
    context.user_data.pop("rep_state", None)
    context.user_data.pop("user_state", None)
    context.user_data.pop("fsm_state", None)
    
    role, rep = database.get_user_role(user.id, chat.id, thread_id)
    if role == "unauthorized_group":
        return

    if database.is_user_blocked(user.id):
        await update.message.reply_text("⛔️ دسترسی شما به این بات توسط مدیریت مسدود شده است.")
        return

    if role == "super_admin":
        await admin_panel.show_admin_dashboard(update, context)
        return

    if role == "rep":
        await rep_panel.show_rep_dashboard(update, context, rep)
        return

    # End-User View
    await user_portal.show_user_portal(update, context)

async def link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    thread_id = update.message.message_thread_id if update.message else None
    role, rep = database.get_user_role(user.id, chat.id, thread_id)
    if role == "unauthorized_group":
        return

    # Allow /link in Private DM (chat.id > 0) AND in End-User Sandbox topic
    sandbox_topic_val = database.get_setting("end_user_sandbox_topic_id", "55")
    sandbox_topic_id = int(sandbox_topic_val) if sandbox_topic_val else 55
    sandbox_group_val = database.get_setting("end_user_sandbox_group_id", str(config.MY_PROJECT_GROUP_ID))
    sandbox_group_id = int(sandbox_group_val) if sandbox_group_val else int(config.MY_PROJECT_GROUP_ID)
    if int(chat.id) < 0 and not ((int(chat.id) == sandbox_group_id or int(chat.id) == int(config.MY_PROJECT_GROUP_ID)) and int(thread_id or 0) == sandbox_topic_id):
        return

    args = context.args
    if not args:
        await update.message.reply_text("⚠️ لطفاً کد اکانت را وارد کنید:\n`/link 3029`", parse_mode=ParseMode.MARKDOWN)
        return
    code = args[0].strip()
    u_name = getattr(user, "username", "") or ""
    u_full = getattr(user, "full_name", "") or ""
    res = database.link_user_to_account(code, user.id, u_name, u_full)
    if res.get("status") == "ok":
        await update.message.reply_text(
            f"✅ اکانت `{code}` با موفقیت به حساب تلگرام شما متصل گردید.\nاکنون می‌توانید با ارسال کد `{code}` وضعیت آن را استعلام کنید.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(f"❌ اکانتی با کد `{code}` یافت نشد.", parse_mode=ParseMode.MARKDOWN)

async def pricing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    thread_id = update.message.message_thread_id if update.message else None
    role, rep = database.get_user_role(user.id, chat.id, thread_id)
    if role == "unauthorized_group":
        return
        
    plans = database.get_pricing_plans()
    lines = ["📋 **جدول تعرفه‌های پلن‌های سرویس:**\n"]
    for p in plans:
        if role == "super_admin":
            lines.append(f"• **{p['name']}** (`{p['plan_code']}`):")
            lines.append(f"   خرید نماینده: **{p['rep_price']:,} تومان** | فروش کاربر: **{p['user_price']:,} تومان**")
        elif role == "rep":
            lines.append(f"• **{p['name']}**: **{p['rep_price']:,} تومان**")
        else:
            lines.append(f"• **{p['name']}**: **{p['user_price']:,} تومان**")
            
    if role == "super_admin":
        lines.append("\n✏️ *جهت تغییر قیمت: `/setprice <plan_code> <rep_toman> <user_toman>`*")
        
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def setprice_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_TELEGRAM_ID:
        return
    args = context.args
    if len(args) < 3:
        await update.message.reply_text("⚠️ فرمت دستور:\n`/setprice <plan_code> <rep_price> <user_price>`\nمثال:\n`/setprice 20gb_30d 75000 130000`", parse_mode=ParseMode.MARKDOWN)
        return
    plan_code, rep_p, user_p = args[0], int(args[1]), int(args[2])
    plan = database.update_plan_price(plan_code, rep_p, user_p)
    if plan:
        await update.message.reply_text(
            f"✅ نرخ پلن **{plan['name']}** (`{plan_code}`) بروزرسانی شد:\n"
            f"• قیمت نماینده: **{rep_p:,} تومان**\n"
            f"• قیمت کاربر: **{user_p:,} تومان**",
            parse_mode=ParseMode.MARKDOWN
        )
        send_admin_log(f"⚙️ **بروزرسانی نرخ تعرفه:** `{plan_code}` توسط ادمین -> نماینده: {rep_p:,} تومان | کاربر: {user_p:,} تومان")
    else:
        await update.message.reply_text(f"❌ پلن با شناسه `{plan_code}` یافت نشد.", parse_mode=ParseMode.MARKDOWN)

async def billing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    thread_id = update.message.message_thread_id if update.message else None
    role, rep = database.get_user_role(user.id, chat.id, thread_id)
    if role not in ("super_admin", "rep"):
        return

    # If Super Admin in private DM (without specific rep context), show aggregated summary of all reps
    if role == "super_admin" and rep is None:
        reps = database.get_all_active_reps()
        lines = ["📊 **گزارش تجمیعی صورت‌حساب کلیه نمایندگان:**\n"]
        total_debt_all = 0
        for r in reps:
            s = database.get_rep_financial_summary(r["id"])
            total_debt_all += s["balance_toman"]
            lines.append(
                f"👤 **{s['rep_name']}** (`{s['rep_code']}`):\n"
                f"   • مانده بدهی: **{s['balance_toman']:,} تومان** | تراکنش‌ها: {s['tx_count']}\n"
                f"   • کل بدهکار (فاکتورها): {s['total_debits']:,} تومان | کل بستانکار: {s['total_credits']:,} تومان\n"
            )
        lines.append(f"💰 **جمع کل مطالبات از تمام نمایندگان:** **{total_debt_all:,} تومان**")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        return

    rep_id = rep["id"]
    summary = database.get_rep_financial_summary(rep_id)
    recent = database.get_recent_ledger_entries(rep_id=rep_id, limit=5)

    lines = [
        f"📊 **صورت‌حساب مالی نمایندگی:** {summary['rep_name']}\n",
        f"• **مانده بدهی جاری:** **{summary['balance_toman']:,} تومان**",
        f"• کل فاکتورهای صادره (بدهکار): {summary['total_debits']:,} تومان",
        f"• کل واریزی و تسویه‌ها (بستانکار): {summary['total_credits']:,} تومان",
        f"• تعداد کل تراکنش‌ها: {summary['tx_count']}\n",
        "🕒 **آخرین ۵ تراکنش ثبت‌شده:**"
    ]
    if not recent:
        lines.append("• هیچ تراکنشی ثبت نشده است.")
    else:
        for tx in recent:
            sign = "🔺" if tx["transaction_type"] == "debit" else "🟢"
            lines.append(f"{sign} `{tx['created_at'][:16]}` | **{tx['amount']:,} ت** | {tx['description']} (مانده: {tx['balance_after']:,})")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

async def pay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_TELEGRAM_ID:
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("⚠️ فرمت دستور تسویه:\n`/pay <rep_code> <amount_toman> [توضیحات]`\nمثال:\n`/pay demo_rep 500000 واریز کارت به کارت`", parse_mode=ParseMode.MARKDOWN)
        return

    rep_code = args[0].lower()
    amount = int(args[1])
    if amount <= 0:
        await update.message.reply_text("❌ مبلغ تسویه باید عددی مثبت باشد.")
        return
    desc = " ".join(args[2:]) if len(args) > 2 else "تسویه حساب / واریزی نماینده"

    rep_info = database.get_rep_by_code(rep_code)
    if not rep_info:
        await update.message.reply_text(f"❌ نماینده `{rep_code}` یافت نشد.", parse_mode=ParseMode.MARKDOWN)
        return

    new_bal = database.register_rep_payment(
        rep_id=rep_info["id"],
        amount=amount,
        description=desc,
        actor_id=user.id,
        actor_name=user.full_name or "Admin"
    )

    await update.message.reply_text(
        f"✅ **واریزی نماینده با موفقیت ثبت شد.**\n\n"
        f"• نماینده: **{rep_info['name']}**\n"
        f"• مبلغ واریزی: **{amount:,} تومان**\n"
        f"• شرح: `{desc}`\n"
        f"• **مانده بدهی جدید:** **{new_bal:,} تومان**",
        parse_mode=ParseMode.MARKDOWN
    )
    await asyncio.to_thread(
        send_admin_log,
        f"🟢 **ثبت تسویه واریزی نماینده:** {rep_info['name']}\n• مبلغ: {amount:,} تومان\n• شرح: {desc}\n• مانده بدهی: {new_bal:,} تومان"
    )

async def wallet_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    thread_id = update.message.message_thread_id if update.message else None
    role, rep = database.get_user_role(user.id, chat.id, thread_id)
    if role == "unauthorized_group":
        return

    if role in ("super_admin", "rep") and rep:
        summary = database.get_rep_financial_summary(rep["id"])
        credit_limit = database.get_rep_credit_limit(rep["id"])
        limit_text = f"{credit_limit:,} تومان" if credit_limit > 0 else "نامحدود"
        msg = (
            f"💼 **کیف پول و تراز حساب نماینده: {rep['name']}**\n\n"
            f"• مانده بدهی فعلی: **{summary['balance_toman']:,} تومان**\n"
            f"• سقف اعتبار مجاز: **{limit_text}**\n"
            f"• مجموع فاکتورهای صادره: `{summary['total_debits']:,} تومان`\n"
            f"• مجموع واریزی‌های تاییدشده: `{summary['total_credits']:,} تومان`\n\n"
            "جهت تسویه بدهی یا ثبت فیش، از دکمه زیر استفاده فرمایید:"
        )
        keyboard = finance_manager.build_wallet_menu_keyboard(summary['balance_toman'], is_rep=True)
    else:
        balance = database.get_user_wallet_balance(user.id)
        msg = (
            f"💳 **کیف پول کاربری شما**\n\n"
            f"• موجودی فعال: **{balance:,} تومان**\n\n"
            "می‌توانید موجودی کیف پول خود را از طریق کارت‌به‌کارت افزایش دهید تا خریدها و تمدیدهای شما به صورت آنی و بدون وقفه پردازش شوند."
        )
        keyboard = finance_manager.build_wallet_menu_keyboard(balance, is_rep=False)

    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

async def deposit_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    thread_id = update.message.message_thread_id if update.message else None
    role, rep = database.get_user_role(user.id, chat.id, thread_id)
    if role == "unauthorized_group":
        return

    args = context.args or []
    amount = 0
    if args and args[0].isdigit():
        amount = int(args[0])

    if amount <= 0:
        context.user_data["fsm_state"] = finance_manager.STATE_AWAITING_RECEIPT_AMOUNT
        context.user_data["deposit_role"] = "rep" if role == "rep" else "end_user"
        await update.message.reply_text(
            "💳 **لطفاً مبلغ واریزی مورد نظر را به تومان وارد کنید:**\n"
            "مثال: `/deposit 500000` یا ارسال عدد `500000`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    card_info = database.get_card_info()
    dep_role = "rep" if role == "rep" else "end_user"
    target_rep_id = rep["id"] if (dep_role == "rep" and rep) else None
    tr_code = database.create_payment_receipt(
        user_id=user.id,
        role=dep_role,
        amount_toman=amount,
        rep_id=target_rep_id
    )
    context.user_data["active_receipt_code"] = tr_code
    context.user_data["deposit_amount"] = amount
    context.user_data["deposit_role"] = dep_role
    context.user_data["fsm_state"] = finance_manager.STATE_AWAITING_RECEIPT_PHOTO

    msg = finance_manager.format_card_instructions(amount, tr_code, card_info)
    keyboard = finance_manager.build_card_payment_keyboard(tr_code)
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

async def card_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_TELEGRAM_ID:
        return
    args = context.args or []
    if len(args) >= 3:
        card_num = args[0]
        card_holder = args[1].replace("_", " ")
        bank_name = " ".join(args[2:])
        database.update_card_info(card_num, card_holder, bank_name)
        await update.message.reply_text(
            f"✅ **اطلاعات کارت بانکی با موفقیت بروزرسانی شد:**\n\n"
            f"• شماره کارت: `{card_num}`\n"
            f"• بنام: **{card_holder}**\n"
            f"• بانک: **{bank_name}**",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        card_info = database.get_card_info()
        await update.message.reply_text(
            f"💳 **اطلاعات کارت بانکی فعلی:**\n\n"
            f"• شماره کارت: `{card_info['card_number']}`\n"
            f"• بنام: **{card_info['card_holder']}**\n"
            f"• بانک: **{card_info['bank_name']}**\n\n"
            "جهت تغییر، دستور زیر را ارسال کنید:\n"
            "`/card <شماره_کارت> <نام_دارنده_با_زیرخط> <نام_بانک>`\n"
            "مثال: `/card 6037991812345678 علی_رضایی بانک_ملی`",
            parse_mode=ParseMode.MARKDOWN
        )

async def sync_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_TELEGRAM_ID:
        return
    result = await asyncio.to_thread(database.sync_all_active_reps_detailed)
    if result.get("status") != "success":
        await update.message.reply_text(
            f"❌ همگام‌سازی انجام نشد: {result.get('message', 'خطای نامشخص')}"
        )
        return
    await update.message.reply_text(
        "✅ همگام‌سازی ۳X-UI انجام شد.\n"
        f"• اکانت‌های به‌روزشده: **{result['count']}**\n"
        f"• اکانت‌های مبهم/بدون واگذاری جدید: **{result['ambiguous']}**\n"
        f"• اکانت‌های بدون تطبیق نماینده: **{result['unmatched']}**",
        parse_mode=ParseMode.MARKDOWN
    )

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    thread_id = update.message.message_thread_id if update.message else None
    role, rep = database.get_user_role(user.id, chat.id, thread_id)
    if role == "unauthorized_group":
        return

    if role == "super_admin" and rep is None:
        reps = database.get_all_active_reps()
        lines = ["📊 **آمار تجمیعی سیستم و پایگاه داده:**\n"]
        with database.get_db() as conn:
            cur = conn.cursor()
            for r in reps:
                t = cur.execute("SELECT count(*) FROM accounts WHERE rep_id = ?", (r["id"],)).fetchone()[0]
                a = cur.execute("SELECT count(*) FROM accounts WHERE rep_id = ? AND enable = 1", (r["id"],)).fetchone()[0]
                d = t - a
                lines.append(f"• **{r['name']}**: کل: **{t}** | فعال: **{a}** | غیرفعال: **{d}**")

            # Personal/unassigned accounts
            t_un = cur.execute("SELECT count(*) FROM accounts WHERE rep_id IS NULL").fetchone()[0]
            a_un = cur.execute("SELECT count(*) FROM accounts WHERE rep_id IS NULL AND enable = 1").fetchone()[0]
            d_un = t_un - a_un
            if t_un > 0:
                lines.append(f"• **اکانت‌های شخصی/آزاد**: کل: **{t_un}** | فعال: **{a_un}** | غیرفعال: **{d_un}**")

            t_all = cur.execute("SELECT count(*) FROM accounts").fetchone()[0]
            a_all = cur.execute("SELECT count(*) FROM accounts WHERE enable = 1").fetchone()[0]
            lines.append(f"\n🌐 **مجموع کل سرور:** **{t_all}** اکانت (فعال: **{a_all}**)")

        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)
        return

    rep_id = rep["id"]
    rep_name = rep["name"]
    with database.get_db() as conn:
        cur = conn.cursor()
        total = cur.execute("SELECT count(*) FROM accounts WHERE rep_id = ?", (rep_id,)).fetchone()[0]
        active = cur.execute("SELECT count(*) FROM accounts WHERE rep_id = ? AND enable = 1", (rep_id,)).fetchone()[0]
        disabled = total - active

    stats_text = (
        f"📊 **آمار دیتابیس نمایندگی:** {rep_name}\n\n"
        f"• کل اکانت‌های ثبت‌شده: **{total}**\n"
        f"• اکانت‌های فعال: **{active}**\n"
        f"• اکانت‌های قطع/غیرفعال: **{disabled}**"
    )
    await update.message.reply_text(stats_text, parse_mode=ParseMode.MARKDOWN)

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("admin_state", None)
    await update.message.reply_text("✅ تمام وضعیت‌های تعاملی و فرم‌های فعال لغو شدند.")

# --- Renewal & Reserve Queue Logic ---

async def process_account_renewal(
    user, role, rep, acc_record, client_info, gb: float, days: float,
    plan_code: str, effective_first: bool, curr_comment: str, comment_updated: bool = False
):
    account_code = acc_record["account_code"]
    email = acc_record["email"]
    is_super_admin = (role == "super_admin")
    scope_flt = "all" if is_super_admin else (rep["scope_filter"] if rep else "all")
    by_code_only = False if is_super_admin else True

    # Read dynamic system settings
    rollover_max_gb = database.get_setting("rollover_max_gb", default=2.0, cast_type=float)
    rollover_max_days = database.get_setting("rollover_max_days", default=2.0, cast_type=float)
    lock_ttl = database.get_setting("renewal_lock_ttl_sec", default=20, cast_type=int)

    # 1. Anti-spam / duplicate click lock
    if not database.acquire_renewal_lock(account_code, ttl_seconds=lock_ttl):
        pending_res = database.get_pending_reservation(account_code)
        keyboard = nlp_matcher.build_account_keyboard(account_code, is_enabled=True, on_first_connect=effective_first, role=role, pending_res=pending_res)
        return False, f"⚠️ **اکانت `{account_code}` چند لحظه پیش پردازش شد.**\nجهت جلوگیری از اعمال تکراری، لطفاً {lock_ttl} ثانیه صبر کنید.", keyboard

    # 2. Extract current state
    rem_gb = float(client_info.get("remaining_gb", 0) if isinstance(client_info.get("remaining_gb"), (int, float)) else 999.0)
    days_left = client_info.get("days_left")
    is_relative = client_info.get("is_relative", False)
    is_expired = client_info.get("is_expired", False)
    is_depleted = client_info.get("is_depleted", False)
    is_enabled = bool(client_info.get("enabled", True))

    rem_days = float(days_left if days_left is not None else (client_info.get("relative_days", 30) if is_relative else 999.0))
    if is_expired:
        rem_days = 0.0
    if is_depleted:
        rem_gb = 0.0

    plan = database.get_plan(plan_code)
    plan_name = plan["name"] if plan else f"{int(gb)} گیگ {int(days)} روزه"
    charge = plan["rep_price"] if plan else int(gb * 15000)

    # 3. Decision Logic:
    target_rep_id = acc_record.get("rep_id")
    is_admin_exempt = is_super_admin or (target_rep_id is None)

    # Representative Credit Limit Enforcement
    if not is_admin_exempt and target_rep_id:
        allowed, curr_debt, credit_limit = database.check_rep_credit_limit(target_rep_id, charge)
        if not allowed:
            pending_res = database.get_pending_reservation(account_code)
            keyboard = nlp_matcher.build_account_keyboard(account_code, is_enabled=True, on_first_connect=effective_first, role=role, pending_res=pending_res)
            return False, (
                f"⛔️ **سقف اعتبار مجاز نماینده تکمیل شده است.**\n\n"
                f"• سقف مجاز: **{credit_limit:,} تومان**\n"
                f"• بدهی فعلی: **{curr_debt:,} تومان**\n"
                f"• هزینه این تمدید: **{charge:,} تومان**\n\n"
                f"لطفاً ابتدا از طریق منوی `/balance` یا `/deposit` اقدام به تسویه حساب فرمایید."
            ), keyboard

    # Condition for immediate direct renewal:
    # If expired OR depleted OR disabled OR (rem_gb <= rollover_max_gb AND rem_days <= rollover_max_days)
    can_renew_immediate = (
        is_expired or is_depleted or not is_enabled or
        (rem_gb <= rollover_max_gb and rem_days <= rollover_max_days)
    )

    if can_renew_immediate:
        countdown_param = True if effective_first else "auto"
        operation_lock_key = f"rep-renew-pending:{account_code.lower()}"
        operation_lock_token = database.acquire_operation_lock(operation_lock_key, ttl_seconds=300)
        if not operation_lock_token:
            pending_res = database.get_pending_reservation(account_code)
            keyboard = nlp_matcher.build_account_keyboard(
                account_code, is_enabled=is_enabled, on_first_connect=effective_first,
                role=role, pending_res=pending_res
            )
            return False, "⏳ درخواست تمدید قبلی هنوز در حال پردازش یا نیازمند تطبیق است؛ از اجرای دوباره خودداری کنید.", keyboard

        # Record the debit before the external side effect. SQLite serializes the
        # credit-limit check and debit, so parallel renewals cannot exceed the limit.
        new_bal = None
        if not is_admin_exempt:
            try:
                new_bal = database.record_ledger_entry(
                    rep_id=target_rep_id,
                    account_code=account_code,
                    email=email,
                    plan_code=plan_code,
                    tx_type="debit",
                    amount=charge,
                    description=f"پیش‌پرداخت تمدید {plan_name} (در انتظار تأیید پنل)",
                    actor_id=user.id,
                    actor_name=user.full_name or user.username
                )
            except ValueError as exc:
                database.release_operation_lock(operation_lock_key, operation_lock_token)
                pending_res = database.get_pending_reservation(account_code)
                keyboard = nlp_matcher.build_account_keyboard(
                    account_code, is_enabled=is_enabled, on_first_connect=effective_first,
                    role=role, pending_res=pending_res
                )
                return False, f"⛔️ ثبت مالی تمدید انجام نشد: {exc}", keyboard

        try:
            ren_res = await asyncio.to_thread(
                xui_manager.renew_client,
                email, gb, days, comment=curr_comment, rollover=True, countdown=countdown_param,
                scope=scope_flt, by_code_only=by_code_only
            )
        except Exception:
            logger.exception("X-UI renewal outcome is unknown for %s", account_code)
            # Keep the debit hold and operation lease: the panel may have applied
            # the renewal even though its response was lost.
            pending_res = database.get_pending_reservation(account_code)
            keyboard = nlp_matcher.build_account_keyboard(
                account_code, is_enabled=is_enabled, on_first_connect=effective_first,
                role=role, pending_res=pending_res
            )
            return False, (
                "⚠️ نتیجه درخواست تمدید از پنل نامشخص است. مبلغ در وضعیت محفوظ باقی مانده؛ "
                "لطفاً تا بررسی وضعیت پنل دوباره اقدام نکنید."
            ), keyboard

        if not _xui_operation_succeeded(ren_res):
            if _xui_operation_failed(ren_res):
                if not is_admin_exempt:
                    try:
                        database.register_rep_payment(
                            target_rep_id, charge,
                            f"استرداد پیش‌پرداخت به دلیل رد قطعی تمدید {account_code}",
                            actor_id=user.id, actor_name=user.full_name or user.username
                        )
                    except Exception:
                        logger.exception("Could not refund representative renewal hold for %s", account_code)
                        pending_res = database.get_pending_reservation(account_code)
                        keyboard = nlp_matcher.build_account_keyboard(
                            account_code, is_enabled=is_enabled, on_first_connect=effective_first,
                            role=role, pending_res=pending_res
                        )
                        return False, "⚠️ پنل تمدید را رد کرد اما استرداد مالی ثبت نشد؛ موضوع نیازمند بررسی مدیریت است.", keyboard
                database.release_operation_lock(operation_lock_key, operation_lock_token)
                pending_res = database.get_pending_reservation(account_code)
                keyboard = nlp_matcher.build_account_keyboard(
                    account_code, is_enabled=is_enabled, on_first_connect=effective_first,
                    role=role, pending_res=pending_res
                )
                return False, (
                    "❌ پنل تمدید را رد کرد؛ هیچ تمدیدی انجام نشد و وجه/اعتبار پیش‌پرداخت‌شده مسترد شد."
                ), keyboard

            # Unknown response: do not refund a possibly completed external operation.
            pending_res = database.get_pending_reservation(account_code)
            keyboard = nlp_matcher.build_account_keyboard(
                account_code, is_enabled=is_enabled, on_first_connect=effective_first,
                role=role, pending_res=pending_res
            )
            return False, (
                "⚠️ پاسخ پنل موفقیت یا شکست قطعی را مشخص نمی‌کند؛ مبلغ محفوظ مانده و بررسی دستی لازم است."
            ), keyboard

        database.release_operation_lock(operation_lock_key, operation_lock_token)
        report_text = ren_res.get("report", "")
        if comment_updated:
            report_text = f"👤 **نام مشتری روی اکانت ثبت شد:** `{curr_comment}`\n\n" + report_text

        if is_admin_exempt:
            database.log_audit(
                rep_id=None, account_code=account_code, email=email,
                action="admin_direct_renew", actor_id=user.id, actor_name=user.full_name or user.username,
                details={"gb": gb, "days": days, "mode": "on_connect" if effective_first else "rollover", "charge": 0, "exempt": True},
                result_status="success"
            )
            report_text += "\n\n👑 **عملیات مستقیم مدیریت کل (معاف از صورت‌حساب نمایندگی)**"
        else:
            database.log_audit(
                rep_id=target_rep_id, account_code=account_code, email=email,
                action="renew_account", actor_id=user.id, actor_name=user.full_name or user.username,
                details={"gb": gb, "days": days, "mode": "on_connect" if effective_first else "rollover", "charge": charge, "balance_after": new_bal},
                result_status="success"
            )
            report_text += f"\n\n💳 **فاکتور تمدید:** `{charge:,} تومان` | **مانده بدهی نماینده:** `{new_bal:,} تومان`"

        keyboard = nlp_matcher.build_account_keyboard(account_code, is_enabled=True, on_first_connect=effective_first, role=role, pending_res=None)

        await asyncio.to_thread(
            send_admin_log,
            f"⚡️ **تمدید مستقیم اکانت:** `{email}` ({curr_comment})\n"
            f"• بسته: {plan_name} ({gb}G / {days}D)\n"
            f"• مجری: {user.full_name} ({'مدیریت (معاف)' if is_admin_exempt else f'{charge:,} تومان'})"
        )
        return True, report_text, keyboard
    else:
        # Case 2: Reserve Queue
        existing_res = database.get_pending_reservation(account_code)
        if existing_res:
            warn_text = (
                f"⚠️ **اکانت `{account_code}` از قبل یک بسته رزرو فعال در صف دارد:**\n\n"
                f"• پکیج رزرو: **{existing_res['plan_name']}** ({existing_res['gb']}G / {existing_res['days']}D)\n"
                f"• زمان ثبت: `{existing_res['created_at'][:16]}`\n\n"
                f"❌ ثبت رزرو دوم برای یک اکانت امکان‌پذیر نیست.\n"
                f"می‌توانید با دکمه‌های زیر بسته فعلی را فعال فوری کرده یا آن را لغو و وجه را عودت دهید."
            )
            keyboard = nlp_matcher.build_account_keyboard(account_code, is_enabled=is_enabled, on_first_connect=effective_first, role=role, pending_res=existing_res)
            return False, warn_text, keyboard

        res_rep_id = None if is_admin_exempt else target_rep_id
        res_charge = 0 if is_admin_exempt else charge

        res_id = database.create_reservation(
            rep_id=res_rep_id,
            account_code=account_code,
            email=email,
            plan_code=plan_code,
            plan_name=plan_name,
            gb=gb,
            days=days,
            on_first_connect=1 if effective_first else 0,
            charge_amount=res_charge,
            actor_id=user.id,
            actor_name=user.full_name or user.username
        )

        if is_admin_exempt:
            fin_info = "• وضعیت مالی: 👑 **عملیات مستقیم مدیریت کل (معاف از صورت‌حساب نمایندگی)**"
        else:
            # Reservation and representative debit were committed atomically by create_reservation.
            new_bal = database.get_rep_balance(target_rep_id)
            fin_info = f"• فاکتور رزرو: `{charge:,} تومان` | **مانده بدهی نماینده:** `{new_bal:,} تومان`"

        pending_dict = database.get_reservation_by_id(res_id)
        keyboard = nlp_matcher.build_account_keyboard(account_code, is_enabled=is_enabled, on_first_connect=effective_first, role=role, pending_res=pending_dict)

        res_msg = (
            f"📦 **اکانت در حال حاضر فعال است و اعتبار دارد.**\n\n"
            f"بسته **{plan_name}** به عنوان **بسته رزرو** ثبت شد و به محض اتمام حجم یا پایان تاریخ بسته فعلی، به صورت خودکار و بدون سوخت شدن اعتبار فعلی فعال خواهد شد.\n\n"
            f"📊 **وضعیت اعتبار فعلی اکانت:**\n"
            f"• حجم باقیمانده فعلی: **{rem_gb:.2f} گیگابایت**\n"
            f"• زمان باقیمانده فعلی: **{rem_days:.1f} روز**\n\n"
            f"📦 **مشخصات بسته در انتظار رزرو:**\n"
            f"• پکیج: **{plan_name}** ({gb} گیگ / {days} روز)\n"
            f"• نحوه فعال‌سازی: خودکار توسط دیده‌بان سیستم (Watchdog)\n"
            f"{fin_info}"
        )
        if comment_updated:
            res_msg = f"👤 **نام مشتری روی اکانت ثبت شد:** `{curr_comment}`\n\n" + res_msg

        admin_bal_text = "👑 معاف از صورت‌حساب (مدیریت کل)" if is_admin_exempt else f"فاکتور: {charge:,} تومان | مانده بدهی: {new_bal:,} تومان"
        await asyncio.to_thread(
            send_admin_log,
            f"📦 **ثبت بسته رزرو جدید برای اکانت:** `{email}` ({curr_comment})\n"
            f"• نماینده: {rep['name'] if rep else 'عملیات مستقیم مدیریت کل'}\n"
            f"• بسته رزرو: {plan_name} ({gb} گیگ / {days} روز)\n"
            f"• وضعیت: {admin_bal_text}"
        )
        return True, res_msg, keyboard

# --- Photo & Receipt Handler ---

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.photo:
        return

    user = update.effective_user
    chat = update.effective_chat
    thread_id = update.message.message_thread_id
    role, rep = database.get_user_role(user.id, chat.id, thread_id)
    if role == "unauthorized_group":
        return
    if database.is_user_blocked(user.id):
        await update.message.reply_text("⛔️ دسترسی شما به این بات مسدود شده است.")
        return

    photo_id = update.message.photo[-1].file_id
    caption = (update.message.caption or "").strip()
    tr_code = context.user_data.get("active_receipt_code")
    amount = int(context.user_data.get("deposit_amount") or 0)
    dep_role = context.user_data.get("deposit_role") or ("rep" if role == "rep" else "end_user")
    target_rep_id = rep["id"] if (dep_role == "rep" and rep) else None

    if amount <= 0 and caption:
        import re
        digits_match = re.findall(r"\b\d[\d,]{3,}\b", caption)
        if digits_match:
            try:
                amount = int(digits_match[0].replace(",", ""))
            except ValueError:
                amount = 0

    if amount <= 0:
        await update.message.reply_text(
            "⚠️ مبلغ فیش مشخص نیست. ابتدا از گزینه ثبت فیش یا دستور /deposit مبلغ را ثبت کنید."
        )
        return

    try:
        if tr_code:
            receipt = database.get_payment_receipt(tr_code)
            if (
                not receipt
                or int(receipt["user_id"]) != int(user.id)
                or receipt["status"] != "pending"
                or receipt["role"] != dep_role
            ):
                await update.message.reply_text("⛔️ فیش در انتظار ارسال برای این حساب یافت نشد یا دیگر قابل ویرایش نیست.")
                return
            with database.get_db() as conn:
                cur = conn.cursor()
                cur.execute("""
                    UPDATE payment_receipts
                    SET receipt_photo_id = ?, receipt_note = ?,
                        amount_toman = CASE WHEN amount_toman = 0 THEN ? ELSE amount_toman END
                    WHERE id = ? AND user_id = ? AND status = 'pending'
                """, (photo_id, caption, amount, receipt["id"], user.id))
                if cur.rowcount != 1:
                    await update.message.reply_text("⚠️ فیش هم‌زمان تغییر کرده است؛ لطفاً وضعیت را بررسی کنید.")
                    return
        else:
            # Support a direct photo only when its amount can be established.
            tr_code = database.create_payment_receipt(
                user_id=user.id, role=dep_role, amount_toman=amount,
                rep_id=target_rep_id, photo_id=photo_id, note=caption
            )
        receipt = database.get_payment_receipt(tr_code)
        if not receipt:
            raise RuntimeError("Persisted receipt could not be reloaded")
    except Exception:
        logger.exception("Could not persist incoming payment receipt")
        await update.message.reply_text("❌ ثبت فیش انجام نشد. لطفاً دوباره تلاش کنید یا با پشتیبانی تماس بگیرید.")
        return

    for key in ("fsm_state", "active_receipt_code", "deposit_amount", "deposit_role"):
        context.user_data.pop(key, None)

    amount_disp = f"**{receipt['amount_toman']:,} تومان**"
    await update.message.reply_text(
        f"🚀 **رسید پرداخت شما با موفقیت دریافت و ثبت گردید.**\n\n"
        f"• کد پیگیری: `{receipt['tracking_code']}`\n"
        f"• مبلغ واریزی: {amount_disp}\n\n"
        "به محض بررسی و تایید توسط مدیریت، حساب شما شارژ یا تسویه خواهد شد.",
        parse_mode=ParseMode.MARKDOWN
    )

    admin_tg_id = database.get_setting("admin_telegram_id", default=ADMIN_TELEGRAM_ID, cast_type=int)
    admin_dm_top = database.get_setting("admin_dm_topic_id", default=ADMIN_DM_LOGS_TOPIC_ID, cast_type=int)
    card_text = finance_manager.format_admin_receipt_card(receipt, user.full_name, user.username)
    review_keyboard = finance_manager.build_admin_receipt_review_keyboard(receipt["id"])

    if admin_tg_id:
        try:
            await context.bot.send_photo(
                chat_id=admin_tg_id,
                message_thread_id=admin_dm_top if admin_dm_top else None,
                photo=photo_id,
                caption=card_text,
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=review_keyboard
            )
        except Exception:
            logger.exception("Failed to forward receipt image; sending text fallback")
            try:
                await context.bot.send_message(
                    chat_id=admin_tg_id,
                    message_thread_id=admin_dm_top if admin_dm_top else None,
                    text=card_text + "\n\n⚠️ ارسال تصویر ناموفق بود؛ فیش در پایگاه داده ثبت است.",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=review_keyboard
                )
            except Exception:
                logger.exception("Failed to send receipt review fallback to admin")

# --- Message & NLP Router ---

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
        
    user = update.effective_user
    chat = update.effective_chat
    thread_id = update.message.message_thread_id
    msg_text = update.message.text or ""
    
    role, rep = database.get_user_role(user.id, chat.id, thread_id)
    if role == "unauthorized_group":
        return

    if database.is_user_blocked(user.id):
        await update.message.reply_text("⛔️ دسترسی شما به این بات توسط مدیریت مسدود شده است.")
        return

    # Text triggers for glass dashboard
    clean_text = msg_text.strip().lower()
    if clean_text in ("منو", "پنل", "menu", "panel", "پیشخوان", "داشبورد", "منوی اصلی"):
        if role == "super_admin":
            await admin_panel.show_admin_dashboard(update, context)
            return
        elif role == "rep" and rep:
            await rep_panel.show_rep_dashboard(update, context, rep)
            return
        else:
            await user_portal.show_user_portal(update, context)
            return

    # Check Admin Interactive Input State (FSM) - STRICTLY in private DM only
    if role == "super_admin" and chat.type == "private":
        if await admin_panel.process_admin_text_input(update, context):
            return

    # Check Rep Interactive Input State (FSM)
    if role == "rep" and rep:
        if await rep_panel.process_rep_text_input(update, context, rep):
            return

    # Check End-User Interactive Input State (FSM)
    if role == "end_user":
        if await user_portal.process_user_text_input(update, context):
            return

    # Check Financial FSM State (Card amount or rejection reason)
    fsm_state = context.user_data.get("fsm_state")
    if fsm_state == finance_manager.STATE_AWAITING_RECEIPT_AMOUNT:
        text_clean = msg_text.replace(",", "").strip()
        if text_clean.isdigit() and int(text_clean) > 0:
            amount = int(text_clean)
            context.user_data["deposit_amount"] = amount
            card_info = database.get_card_info()
            dep_role = context.user_data.get("deposit_role", "end_user" if role == "end_user" else "rep")
            target_rep_id = rep["id"] if (dep_role == "rep" and rep) else None
            tr_code = database.create_payment_receipt(
                user_id=user.id,
                role=dep_role,
                amount_toman=amount,
                rep_id=target_rep_id
            )
            context.user_data["active_receipt_code"] = tr_code
            context.user_data["fsm_state"] = finance_manager.STATE_AWAITING_RECEIPT_PHOTO

            msg = finance_manager.format_card_instructions(amount, tr_code, card_info)
            keyboard = finance_manager.build_card_payment_keyboard(tr_code)
            await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            return
        else:
            await update.message.reply_text("⚠️ لطفاً مبلغ را به صورت عدد معتبر لاتین وارد کنید (مثال: `500000`).", parse_mode=ParseMode.MARKDOWN)
            return

    reply_to = update.message.reply_to_message
    reply_text = (reply_to.text or reply_to.caption or "") if reply_to else ""

    # Detect Intent and Parameters
    intent, params = nlp_matcher.detect_intent(msg_text)
    
    # 1. Action: New Account Allocation (Admin / Rep only)
    if intent == "new_account" and role in ("super_admin", "rep"):
        gb = float(params.get("gb", 20.0))
        days = int(params.get("days", 30))
        comment = params.get("comment", "")
        if gb <= 0 or days <= 0:
            await update.message.reply_text("❌ حجم و مدت اکانت باید مثبت باشند.")
            return

        target_rep_id = rep["id"] if rep else None
        is_exempt = role == "super_admin" or target_rep_id is None
        plan_code = f"{int(gb)}gb_{int(days)}d"
        plan = database.get_plan(plan_code) or database.get_plan("20gb_30d")
        charge = int(plan["rep_price"]) if plan else 300_000
        if not is_exempt and charge <= 0:
            await update.message.reply_text("❌ قیمت صدور اکانت معتبر نیست؛ با مدیریت تماس بگیرید.")
            return

        lock_key = f"new-account:{role}:{user.id}:{plan_code}"
        lock_token = database.acquire_operation_lock(lock_key, ttl_seconds=300)
        if not lock_token:
            await update.message.reply_text("⏳ درخواست صدور دیگری در حال پردازش است یا نتیجه قبلی نیازمند بررسی است؛ لطفاً دوباره اجرا نکنید.")
            return

        new_bal = None
        if not is_exempt:
            try:
                new_bal = database.record_ledger_entry(
                    rep_id=target_rep_id, account_code="", email="", plan_code=plan_code,
                    tx_type="debit", amount=charge,
                    description=f"پیش‌پرداخت صدور اکانت {int(gb)}G/{days}D (در انتظار تأیید پنل)",
                    actor_id=user.id, actor_name=user.full_name or user.username
                )
            except ValueError as exc:
                database.release_operation_lock(lock_key, lock_token)
                await update.message.reply_text(f"⛔️ صدور اکانت انجام نشد: {exc}")
                return

        scope_for_alloc = rep["scope_filter"] if rep else "all"
        try:
            alloc_res = await asyncio.to_thread(
                xui_manager.allocate_pool_client, gb, days,
                comment=comment, scope=scope_for_alloc
            )
        except Exception:
            logger.exception("New account allocation outcome is unknown for %s", user.id)
            await update.message.reply_text(
                "⚠️ پاسخ پنل نامشخص است؛ مبلغ/اعتبار در وضعیت محفوظ باقی مانده و بررسی دستی لازم است. لطفاً درخواست صدور دیگری نفرستید."
            )
            return

        if isinstance(alloc_res, dict) and alloc_res.get("status") == "success":
            email = alloc_res.get("email")
            if not email:
                logger.error("X-UI reports account allocation success without email: %r", alloc_res)
                await update.message.reply_text("⚠️ پنل موفقیت را گزارش کرد اما شناسه اکانت ناقص است؛ مبلغ برای تطبیق محفوظ ماند.")
                return
            sub_id = alloc_res.get("sub_id")
            try:
                code = xui_manager.extract_account_identifier(email) or email
                database.upsert_account_record(
                    rep_id=target_rep_id, email=email, account_code=code,
                    customer_name=comment, sub_id=sub_id, enable=1,
                    total_gb=gb, expiry_time=alloc_res.get("data", {}).get("expiry_time", 0)
                )
            except Exception:
                logger.exception("X-UI allocated %s but local account persistence failed", email)
                await update.message.reply_text(
                    "⚠️ اکانت در پنل ایجاد شد اما ثبت محلی کامل نشد؛ مبلغ محفوظ است و تطبیق پنل/دیتابیس لازم است."
                )
                return

            deliv_text = alloc_res.get("deliverable", "")
            if is_exempt:
                deliv_text += "\n\n👑 **عملیات مستقیم مدیریت کل (معاف از صورت‌حساب نمایندگی)**"
                try:
                    database.log_audit(
                        rep_id=None, account_code=code, email=email,
                        action="admin_allocate_new_account", actor_id=user.id,
                        actor_name=user.full_name or user.username,
                        details={"gb": gb, "days": days, "comment": comment, "exempt": True},
                        result_status="success"
                    )
                except Exception:
                    logger.exception("Audit log failed for admin allocation %s", email)
            else:
                try:
                    database.log_audit(
                        rep_id=target_rep_id, account_code=code, email=email,
                        action="allocate_new_account", actor_id=user.id,
                        actor_name=user.full_name or user.username,
                        details={"gb": gb, "days": days, "comment": comment, "charge": charge, "balance_after": new_bal},
                        result_status="success"
                    )
                except Exception:
                    logger.exception("Audit log failed for representative allocation %s", email)
                deliv_text += f"\n\n💳 **فاکتور صدور:** `{charge:,} تومان` | **مانده بدهی:** `{new_bal:,} تومان`"

            database.release_operation_lock(lock_key, lock_token)
            keyboard = nlp_matcher.build_account_keyboard(code, is_enabled=True, role=role)
            sub_url = alloc_res.get("sub_url")
            qr_file = None
            if sub_url:
                try:
                    qr_file = f"/tmp/qr_new_{code}.png"
                    await asyncio.to_thread(xui_manager.generate_qr, sub_url, qr_file)
                except Exception:
                    logger.exception("Failed to generate QR for new account %s", code)
                    qr_file = None

            try:
                if qr_file and os.path.exists(qr_file):
                    with open(qr_file, "rb") as photo:
                        await update.message.reply_photo(
                            photo=photo, caption=deliv_text,
                            parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
                        )
                    os.remove(qr_file)
                else:
                    await update.message.reply_text(
                        deliv_text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
                    )
            except Exception:
                logger.exception("Failed to deliver new account information to Telegram for %s", email)

            await asyncio.to_thread(
                send_admin_log,
                f"🆕 **صدور اکانت جدید توسط:** {user.full_name}\n"
                f"• اکانت: `{email}` ({comment or 'ثبت‌نشده'})\n"
                f"• پکیج: {gb} گیگ / {days} روز\n"
                f"• وضعیت: {'مدیریت (معاف)' if is_exempt else f'{charge:,} تومان'}"
            )
            return

        if isinstance(alloc_res, dict) and (
            alloc_res.get("status") in ("error", "failed", "failure")
            or alloc_res.get("success") is False
        ):
            if not is_exempt:
                try:
                    database.register_rep_payment(
                        target_rep_id, charge,
                        f"استرداد پیش‌پرداخت به دلیل شکست قطعی صدور {plan_code}",
                        actor_id=user.id, actor_name=user.full_name or user.username
                    )
                except Exception:
                    logger.exception("Failed to reverse representative account precharge")
                    await update.message.reply_text("⚠️ پنل صدور را رد کرد اما استرداد مالی ثبت نشد؛ مدیریت باید بررسی کند.")
                    return
            database.release_operation_lock(lock_key, lock_token)
            await update.message.reply_text(
                f"❌ پنل صدور اکانت را رد کرد؛ {'پیش‌پرداخت مسترد شد.' if not is_exempt else 'هزینه‌ای ثبت نشد.'} "
                f"{alloc_res.get('message', '')}"
            )
            return

        logger.error("Ambiguous X-UI allocation response for %s: %r", user.id, alloc_res)
        await update.message.reply_text(
            "⚠️ پاسخ پنل برای تأیید یا رد صدور کافی نیست؛ مبلغ/اعتبار محفوظ است و بررسی دستی لازم است."
        )
        return
    # 2. Extract account code from reply or message
    account_code = nlp_matcher.extract_account_code(reply_text, msg_text)

    if not account_code:
        if chat.id < 0:
            return
        await update.message.reply_text(
            "⚠️ شناسه یا کد اکانت یافت نشد.\nلطفاً روی پیام اکانت ریپلای کنید یا کد/نام کاربری آن را بفرستید.",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    # Check End-User access restriction
    if role == "end_user":
        user_accs = database.get_user_accounts(user.id)
        matched_user_acc = any(
            a["account_code"].lower() == account_code.lower() or 
            a["email"].lower() == account_code.lower() or
            account_code.lower() in a["email"].lower()
            for a in user_accs
        )
        if not matched_user_acc:
            await update.message.reply_text("⛔️ شما به این اکانت دسترسی ندارید. ابتدا با دستور `/link <کد_اکانت>` آن را ثبت کنید.", parse_mode=ParseMode.MARKDOWN)
            return

    # Super Admin vs Rep Scope Configuration
    is_super_admin = (role == "super_admin")
    scope_flt = "all" if is_super_admin else (rep["scope_filter"] if rep else "all")
    by_code_only = False if is_super_admin else True

    # 1. Lookup in local database accounts
    acc_record = None
    if is_super_admin:
        with database.get_db() as conn:
            row = conn.cursor().execute(
                "SELECT * FROM accounts WHERE LOWER(account_code) = ? OR LOWER(email) = ? OR LOWER(email) LIKE ? LIMIT 1",
                (account_code.lower(), account_code.lower(), f"%{account_code.lower()}%")
            ).fetchone()
            if row:
                acc_record = dict(row)
    elif rep:
        acc_record = database.is_account_in_rep_scope(rep["id"], account_code)

    # 2. Query panel (Direct inspection / Fallback)
    query_target = acc_record["email"] if acc_record else account_code
    search_res = await asyncio.to_thread(
        xui_manager.inspect_client,
        query_target,
        scope=scope_flt,
        by_code_only=by_code_only
    )

    if search_res.get("status") != "ok":
        if search_res.get("status") == "multiple_matches":
            cands = "\n".join([f"• `{c}`" for c in search_res.get("candidates", [])[:8]])
            await update.message.reply_text(
                f"⚠️ **چند اکانت مطابق با `{account_code}` یافت شد:**\n\n{cands}\n\nلطفاً شناسه دقیق‌تری را وارد کنید.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        await update.message.reply_text(
            f"⚠️ **اکانت `{account_code}` در سرور ۳X-UI یافت نشد.**",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    client_info = search_res["data"]
    raw_client = client_info["raw"]
    email = client_info["email"]
    account_code = email  # Real verified email/code for inline buttons
    curr_comment = (raw_client.get("comment") or "").strip()
    is_enabled = bool(raw_client.get("enable", True))

    # Strict Representative Scope Isolation
    if rep:
        is_in_rep_scope = False
        rep_scope = (rep.get("scope_filter") or "").strip().lower()
        tokens = [t.strip() for t in rep_scope.split(",") if t.strip()]
        if rep_scope == "all":
            is_in_rep_scope = True
        elif any(tok in email.lower() for tok in tokens):
            is_in_rep_scope = True
        elif acc_record and acc_record.get("rep_id") == rep["id"]:
            is_in_rep_scope = True

        if not is_in_rep_scope:
            await update.message.reply_text(
                f"⚠️ **اکانت `{account_code}` در محدوده مجاز شما یافت نشد.**",
                parse_mode=ParseMode.MARKDOWN
            )
            return

    if not acc_record:
        if is_super_admin:
            target_rep_id = database.resolve_account_rep_id(email)
            acc_record = {
                "id": 0,
                "rep_id": target_rep_id,
                "email": email,
                "account_code": email,
                "customer_name": curr_comment,
                "sub_id": client_info.get("sub_id", ""),
                "enable": 1 if is_enabled else 0,
                "total_gb": client_info.get("total_gb", 0),
                "expiry_time": client_info.get("expiry_time", 0)
            }
        elif rep:
            database.upsert_account_record(
                rep_id=rep["id"],
                email=email,
                account_code=account_code,
                customer_name=curr_comment,
                sub_id=client_info.get("sub_id", ""),
                enable=1 if is_enabled else 0,
                total_gb=client_info.get("total_gb", 0),
                expiry_time=client_info.get("expiry_time", 0)
            )
            acc_record = database.is_account_in_rep_scope(rep["id"], email)

    # Customer Name Management (Rule 3)
    new_customer_name = nlp_matcher.clean_customer_name(msg_text) or nlp_matcher.clean_customer_name(reply_text)
    comment_updated = False
    if new_customer_name and (not curr_comment or curr_comment.lower() == "sm"):
        await asyncio.to_thread(xui_manager.set_client_comment, email, new_customer_name)
        curr_comment = new_customer_name
        client_info["raw"]["comment"] = new_customer_name
        comment_updated = True
        database.upsert_account_record(
            rep_id=acc_record.get("rep_id"),
            email=email,
            account_code=account_code,
            customer_name=new_customer_name
        )

    # 3. Execute matched intent
    if intent == "suspend" and role in ("super_admin", "rep"):
        res = await asyncio.to_thread(xui_manager.set_client_enable, email, enable=False, scope=scope_flt)
        if res.get("status") == "ok":
            database.upsert_account_record(rep_id=acc_record.get("rep_id"), email=email, account_code=account_code, enable=0)
            rem_gb = client_info.get("remaining_gb", 0)
            exp_text = client_info.get("expiry_desc", "نامشخص")

            text_out = (
                f"🛑 **اتصال اکانت قطع (غیرفعال) شد.**\n\n"
                f"📊 **وضعیت محفوظ اکانت:** `{email}`\n"
                f"👤 **توضیحات:** `{curr_comment or 'ثبت‌نشده'}`\n"
                f"• **حجم باقیمانده:** **{rem_gb:.2f} گیگابایت** (محفوظ و دست‌نخورده)\n"
                f"• **اعتبار زمانی باقیمانده:** **{exp_text}**"
            )
            on_first = database.get_user_first_connect_mode(user.id)
            keyboard = nlp_matcher.build_account_keyboard(account_code, is_enabled=False, on_first_connect=on_first, role=role)
            await update.message.reply_text(text_out, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            database.log_audit(
                rep_id=acc_record.get("rep_id"), account_code=account_code, email=email,
                action="suspend_account", actor_id=user.id, actor_name=user.full_name or user.username,
                details={"rem_gb": rem_gb}, result_status="success"
            )
            await asyncio.to_thread(send_admin_log, f"🛑 **قطع اتصال اکانت:** `{email}` توسط {user.full_name}\nحجم محفوظ: {rem_gb:.2f} GB")
        else:
            await update.message.reply_text(f"❌ خطا در غیرفعال‌سازی اکانت: {res.get('message')}")

    elif intent == "activate" and role in ("super_admin", "rep"):
        res = await asyncio.to_thread(xui_manager.set_client_enable, email, enable=True, scope=scope_flt)
        if res.get("status") == "ok":
            database.upsert_account_record(rep_id=acc_record.get("rep_id"), email=email, account_code=account_code, enable=1)
            rem_gb = client_info.get("remaining_gb", 0)
            exp_text = client_info.get("expiry_desc", "نامشخص")
            
            text_out = (
                f"✅ **اتصال اکانت مجدداً فعال شد.**\n\n"
                f"📊 **وضعیت اکانت:** `{email}`\n"
                f"👤 **توضیحات:** `{curr_comment or 'ثبت‌نشده'}`\n"
                f"• **حجم باقیمانده:** **{rem_gb:.2f} گیگابایت**\n"
                f"• **اعتبار زمانی باقیمانده:** **{exp_text}**"
            )
            on_first = database.get_user_first_connect_mode(user.id)
            keyboard = nlp_matcher.build_account_keyboard(account_code, is_enabled=True, on_first_connect=on_first, role=role)
            await update.message.reply_text(text_out, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            database.log_audit(
                rep_id=acc_record["rep_id"], account_code=account_code, email=email,
                action="activate_account", actor_id=user.id, actor_name=user.full_name or user.username,
                details={"rem_gb": rem_gb}, result_status="success"
            )
            await asyncio.to_thread(send_admin_log, f"✅ **برقراری مجدد اتصال اکانت:** `{email}` توسط {user.full_name}")
        else:
            await update.message.reply_text(f"❌ خطا در برقراری اتصال اکانت: {res.get('message')}")

    elif intent == "renew" and role in ("super_admin", "rep"):
        gb = params.get("gb", 20.0)
        days = params.get("days", 30.0)
        first_connect_req = params.get("first_connect", False)
        mode_first = database.get_user_first_connect_mode(user.id)
        effective_first = first_connect_req or mode_first
        plan_code = f"{int(gb)}gb_{int(days)}d"

        ok, msg_out, keyboard = await process_account_renewal(
            user=user, role=role, rep=rep, acc_record=acc_record, client_info=client_info,
            gb=gb, days=days, plan_code=plan_code, effective_first=effective_first,
            curr_comment=curr_comment, comment_updated=comment_updated
        )
        await update.message.reply_text(msg_out, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

    elif intent == "deliverable":
        links = await asyncio.to_thread(xui_manager.get_client_links, email)
        sub_id = client_info.get("sub_id", "")
        sub_url = xui_manager.get_sub_url(sub_id)
        qr_needed = params.get("qr", False)
        conf_qr = params.get("conf_qr", False)
        
        on_first = database.get_user_first_connect_mode(user.id)
        pending_res = database.get_pending_reservation(account_code)
        keyboard = nlp_matcher.build_account_keyboard(account_code, is_enabled=is_enabled, on_first_connect=on_first, role=role, pending_res=pending_res)
        
        qr_file = None
        if qr_needed:
            try:
                target_url = links[0] if (conf_qr and links) else sub_url
                qr_file = f"/tmp/qr_msg_{account_code}.png"
                await asyncio.to_thread(xui_manager.generate_qr, target_url, qr_file)
            except Exception as e:
                logger.error(f"Failed to generate QR: {e}")
                qr_file = None
                
        deliv_text = xui_manager.format_client_deliverable(client_info, links, is_admin=False, generate_qr_code=False)
        if qr_file and os.path.exists(qr_file):
            with open(qr_file, "rb") as photo:
                await update.message.reply_photo(
                    photo=photo,
                    caption=deliv_text,
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=keyboard
                )
            os.remove(qr_file)
        else:
            await update.message.reply_text(deliv_text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

    else:
        # Default: Status Report + Quick Actions Keyboard
        status_text = xui_manager.format_status_report(client_info, is_admin=is_super_admin)
        pending_res = database.get_pending_reservation(account_code)
        if pending_res:
            status_text += (
                f"\n\n📦 **بسته رزرو در صف انتظار:** **{pending_res['plan_name']}** ({pending_res['gb']}G / {pending_res['days']}D)\n"
                f"*(فعال‌سازی خودکار پس از اتمام بسته فعلی)*"
            )
        if comment_updated:
            status_text = f"👤 **نام مشتری روی اکانت ثبت شد:** `{curr_comment}`\n\n" + status_text
            
        on_first = database.get_user_first_connect_mode(user.id)
        keyboard = nlp_matcher.build_account_keyboard(account_code, is_enabled=is_enabled, on_first_connect=on_first, role=role, pending_res=pending_res)
        await update.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

# --- Interactive Callback Handler ---

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    thread_id = update.effective_message.message_thread_id if update.effective_message else None
    
    role, rep = database.get_user_role(user.id, chat.id, thread_id)
    if role == "unauthorized_group":
        await query.answer("⛔️ دسترسی غیرمجاز در این تاپیک.", show_alert=True)
        return

    if database.is_user_blocked(user.id):
        await query.answer("⛔️ دسترسی شما به این بات مسدود شده است.", show_alert=True)
        return

    data = query.data or ""

    # Admin Receipt Verification Callbacks
    if data.startswith("admin_approve_rcp_"):
        if role != "super_admin":
            await query.answer("⛔️ این عملیات فقط برای ادمین کل سیستم مجاز است.", show_alert=True)
            return
        raw_id = data.replace("admin_approve_rcp_", "")
        target_rcp = int(raw_id) if raw_id.isdigit() else raw_id
        try:
            rcp = database.approve_payment_receipt(target_rcp, user.id, user.full_name or "Admin")
            await query.edit_message_reply_markup(reply_markup=finance_manager.build_admin_reviewed_keyboard("approved"))
            await query.answer(f"✅ فیش {rcp['tracking_code']} با موفقیت تایید شد.")
            
            try:
                role_fa = "نماینده" if rcp["role"] == "rep" else "کاربر"
                notify_txt = (
                    f"🎉 **تایید فیش پرداختی ({role_fa}):**\n\n"
                    f"• کد رهگیری: `{rcp['tracking_code']}`\n"
                    f"• مبلغ: **{rcp['amount_toman']:,} تومان**\n"
                    f"• وضعیت: **تایید شد و حساب شما شارژ/تسویه گردید.**"
                )
                await context.bot.send_message(chat_id=rcp["user_id"], text=notify_txt, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.error(f"Failed to notify user of receipt approval: {e}")
        except Exception as e:
            await query.answer(f"❌ خطا در تایید: {e}", show_alert=True)
        return

    if data.startswith("admin_reject_rcp_"):
        if role != "super_admin":
            await query.answer("⛔️ این عملیات فقط برای ادمین کل سیستم مجاز است.", show_alert=True)
            return
        raw_id = data.replace("admin_reject_rcp_", "")
        target_rcp = int(raw_id) if raw_id.isdigit() else raw_id
        try:
            rcp = database.reject_payment_receipt(target_rcp, user.id, "عدم تطابق با گردش حساب")
            await query.edit_message_reply_markup(reply_markup=finance_manager.build_admin_reviewed_keyboard("rejected"))
            await query.answer(f"❌ فیش {rcp['tracking_code']} رد شد.")
            
            try:
                notify_txt = (
                    f"⚠️ **رد فیش پرداختی:**\n\n"
                    f"• کد رهگیری: `{rcp['tracking_code']}`\n"
                    f"• مبلغ: **{rcp['amount_toman']:,} تومان**\n"
                    f"• وضعیت: **رد شد** (علت: عدم تطابق با تراکنش‌های بانکی)"
                )
                await context.bot.send_message(chat_id=rcp["user_id"], text=notify_txt, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.error(f"Failed to notify user of receipt rejection: {e}")
        except Exception as e:
            await query.answer(f"❌ خطا در رد فیش: {e}", show_alert=True)
        return

    if data.startswith("admin_detail_rcp_"):
        if role != "super_admin":
            await query.answer("⛔️ این عملیات فقط برای ادمین کل سیستم مجاز است.", show_alert=True)
            return
        raw_id = data.replace("admin_detail_rcp_", "")
        target_rcp = int(raw_id) if raw_id.isdigit() else raw_id
        rcp = database.get_payment_receipt(target_rcp)
        if not rcp:
            await query.answer("❌ فیش یافت نشد.", show_alert=True)
            return
        
        status_fa = "⏳ در انتظار بررسی" if rcp["status"] == "pending" else ("✅ تایید شده" if rcp["status"] == "approved" else "❌ رد شده")
        role_fa = "نماینده" if rcp["role"] == "rep" else "کاربر نهایی"
        note_txt = rcp.get('receipt_note') or 'ندارد'
        text = (
            f"🧾 **شناسنامه و جزئیات فیش پرداختی:**\n\n"
            f"• کد پیگیری: `{rcp['tracking_code']}`\n"
            f"• نقش: **{role_fa}**\n"
            f"• شناسه کاربر: `{rcp['user_id']}`\n"
            f"• مبلغ واریزی: **{rcp['amount_toman']:,} تومان**\n"
            f"• وضعیت فعلی: **{status_fa}**\n"
            f"• تاریخ ارسال: `{rcp['created_at'][:19]}`\n"
            f"• توضیحات فرستنده: _{note_txt}_\n"
        )
        if rcp["status"] == "pending":
            btns = [
                [
                    InlineKeyboardButton("✅ تایید فیش", callback_data=f"admin_approve_rcp_{rcp['id']}"),
                    InlineKeyboardButton("❌ رد فیش", callback_data=f"admin_reject_rcp_{rcp['id']}")
                ]
            ]
        else:
            btns = [[InlineKeyboardButton(status_fa, callback_data="noop")]]
        await query.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=InlineKeyboardMarkup(btns))
        await query.answer()
        return

    if data.startswith("send_receipt_"):
        tr_code = data.replace("send_receipt_", "")
        receipt = database.get_payment_receipt(tr_code)
        if (
            not receipt or int(receipt["user_id"]) != int(user.id)
            or receipt["status"] != "pending"
        ):
            await query.answer("⛔️ این شناسه فیش متعلق به شما نیست یا دیگر در انتظار ارسال نیست.", show_alert=True)
            return
        context.user_data["active_receipt_code"] = receipt["tracking_code"]
        context.user_data["deposit_amount"] = receipt["amount_toman"]
        context.user_data["deposit_role"] = receipt["role"]
        context.user_data["fsm_state"] = finance_manager.STATE_AWAITING_RECEIPT_PHOTO
        await query.answer("📸 منتظر ارسال تصویر فیش...")
        await query.message.reply_text(
            f"📸 **لطفاً تصویر فیش واریزی خود را ارسال فرمایید:**\n\n"
            f"• کد پیگیری: `{tr_code}`\n"
            f"*(توضیحات یا شماره پیگیری را می‌توانید در کپشن بنویسید)*",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if data in ("wallet_charge_start", "rep_deposit_start", "user_deposit_start"):
        context.user_data["fsm_state"] = finance_manager.STATE_AWAITING_RECEIPT_AMOUNT
        context.user_data["deposit_role"] = "rep" if data == "rep_deposit_start" else "end_user"
        await query.answer("مبلغ را وارد کنید.")
        await query.message.reply_text(
            "💳 **لطفاً مبلغ مورد نظر جهت پرداخت را به تومان وارد کنید:**\n"
            "مثال: `500000`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    if data == "cancel_payment":
        context.user_data.pop("fsm_state", None)
        context.user_data.pop("active_receipt_code", None)
        await query.answer("پرداخت لغو شد.")
        await safe_edit_message(query, "❌ عملیات پرداخت لغو شد.")
        return

    parts = data.split(":")
    action = parts[0]
    # Reserve callbacks carry <reservation_id>:<account_email>; other account
    # callbacks carry the account identifier in parts[1].
    account_code = (
        parts[2] if action in ("force_res", "cancel_res") and len(parts) > 2
        else (parts[1] if len(parts) > 1 else "")
    )

    # 0. Admin Navigation
    if action == "admin":
        if role != "super_admin":
            await query.answer("⛔️ دسترسی فقط برای ادمین کل سیستم مجاز است.", show_alert=True)
            return
        await admin_panel.handle_admin_callbacks(update, context)
        return

    # 0.1 Rep Dashboard Navigation
    if action == "rep":
        if role == "rep" and rep:
            await rep_panel.handle_rep_callbacks(update, context, rep)
            return
        elif role == "super_admin":
            reps = database.get_all_reps()
            if reps:
                await rep_panel.handle_rep_callbacks(update, context, reps[0])
                return
        await query.answer("⛔️ دسترسی فقط برای نمایندگان مجاز است.", show_alert=True)
        return

    # 0.2 End-User Portal Navigation
    if action == "user":
        await user_portal.handle_user_callbacks(update, context)
        return

    # Strict End-User Isolation in Callbacks
    if role == "end_user":
        if action in ("suspend", "activate", "qrenew", "force_res", "cancel_res", "toggle_mode", "more_plans"):
            await query.answer("⛔️ این عملیات فقط برای نماینده و مدیریت مجاز است.", show_alert=True)
            return

        user_accs = database.get_user_accounts(user.id)
        matched_user_acc = any(
            a["account_code"].lower() == account_code.lower() or 
            a["email"].lower() == account_code.lower() or
            account_code.lower() in a["email"].lower()
            for a in user_accs
        )
        if not matched_user_acc:
            await query.answer("⛔️ شما به این اکانت دسترسی ندارید. ابتدا با دستور /link آن را ثبت کنید.", show_alert=True)
            return
    
    # 1. More Plans Menu
    if action == "more_plans":
        keyboard = nlp_matcher.build_more_plans_keyboard(account_code)
        try:
            await query.edit_message_reply_markup(reply_markup=keyboard)
        except BadRequest:
            pass
        await query.answer("📋 لیست سایر تعرفه‌ها")
        return

    # 2. Main Menu return
    if action == "main_menu":
        on_first = database.get_user_first_connect_mode(user.id)
        acc_rec = database.is_account_in_rep_scope(rep["id"], account_code) if rep else None
        is_en = bool(acc_rec.get("enable", 1)) if acc_rec else True
        keyboard = nlp_matcher.build_account_keyboard(account_code, is_enabled=is_en, on_first_connect=on_first, role=role)
        try:
            await query.edit_message_reply_markup(reply_markup=keyboard)
        except BadRequest:
            pass
        await query.answer("↩️ بازگشت به منوی اصلی")
        return

    # 3. Toggle Mode (Standard Rollover vs On First Connect)
    if action == "toggle_mode":
        new_mode = database.toggle_user_first_connect_mode(user.id)
        acc_rec = database.is_account_in_rep_scope(rep["id"], account_code) if rep else None
        is_en = bool(acc_rec.get("enable", 1)) if acc_rec else True
        keyboard = nlp_matcher.build_account_keyboard(account_code, is_enabled=is_en, on_first_connect=new_mode, role=role)
        try:
            await query.edit_message_reply_markup(reply_markup=keyboard)
        except BadRequest:
            pass
        mode_label = "شروع با اولین اتصال" if new_mode else "استاندارد (رول‌اوور)"
        await query.answer(f"⚙️ حالت تمدید تغییر یافت به: {mode_label}", show_alert=True)
        return

    # Account Verification
    is_super_admin = (role == "super_admin")
    scope_flt = "all" if is_super_admin else (rep["scope_filter"] if rep else "all")
    by_code_only = False if is_super_admin else True

    acc_record = None
    if is_super_admin:
        with database.get_db() as conn:
            row = conn.cursor().execute(
                "SELECT * FROM accounts WHERE LOWER(account_code) = ? OR LOWER(email) = ? OR LOWER(email) LIKE ? LIMIT 1",
                (account_code.lower(), account_code.lower(), f"%{account_code.lower()}%")
            ).fetchone()
            if row:
                acc_record = dict(row)
    elif rep:
        acc_record = database.is_account_in_rep_scope(rep["id"], account_code)

    if not acc_record and not is_super_admin and role != "end_user":
        await query.answer("⚠️ اکانت در محدوده شما یافت نشد.", show_alert=True)
        return
        
    query_target = acc_record["email"] if acc_record else account_code
    insp = await asyncio.to_thread(xui_manager.inspect_client, query_target, scope=scope_flt, by_code_only=by_code_only)
    if insp.get("status") != "ok":
        await query.answer("❌ خطا در استعلام از سرور پنل", show_alert=True)
        return
        
    client_info = insp["data"]
    raw_client = client_info["raw"]
    email = client_info["email"]
    account_code = email
    curr_comment = (raw_client.get("comment") or "").strip()
    is_enabled = bool(raw_client.get("enable", True))
    on_first = database.get_user_first_connect_mode(user.id)

    if not acc_record:
        target_rep_id = rep["id"] if rep else (database.resolve_account_rep_id(email) if is_super_admin else None)
        acc_record = {
            "id": 0,
            "rep_id": target_rep_id,
            "email": email,
            "account_code": email,
            "customer_name": curr_comment,
            "sub_id": client_info.get("sub_id", ""),
            "enable": 1 if is_enabled else 0,
            "total_gb": client_info.get("total_gb", 0),
            "expiry_time": client_info.get("expiry_time", 0)
        }

    # 4. Status Refresh (BUG-FREE GUARANTEED)
    if action == "status":
        now_time = datetime.now().strftime("%H:%M:%S")
        status_text = xui_manager.format_status_report(client_info, is_admin=is_super_admin)
        pending_res = database.get_pending_reservation(account_code)
        if pending_res:
            status_text += (
                f"\n\n📦 **بسته رزرو در صف انتظار:** **{pending_res['plan_name']}** ({pending_res['gb']}G / {pending_res['days']}D)\n"
                f"*(فعال‌سازی خودکار پس از اتمام بسته فعلی)*"
            )
        status_text += f"\n\n🔄 *آخرین به‌روزرسانی لحظه‌ای:* `{now_time}`"
        keyboard = nlp_matcher.build_account_keyboard(account_code, is_enabled=is_enabled, on_first_connect=on_first, role=role, pending_res=pending_res)
        await safe_edit_message(query, status_text, reply_markup=keyboard)
        await query.answer("📊 وضعیت لحظه‌ای بروزرسانی شد.")

    # 5. Suspend Account
    elif action == "suspend":
        if role not in ("super_admin", "rep"):
            await query.answer("⛔️ عدم دسترسی", show_alert=True)
            return
        res = await asyncio.to_thread(xui_manager.set_client_enable, email, enable=False, scope=scope_flt, by_code_only=by_code_only)
        if res.get("status") == "ok":
            target_rep_id = acc_record.get("rep_id") or (rep["id"] if rep else None)
            database.upsert_account_record(rep_id=target_rep_id, email=email, account_code=account_code, enable=0)
            rem_gb = client_info.get("remaining_gb", 0)
            exp_text = client_info.get("expiry_desc", "نامشخص")
            text_out = (
                f"🛑 **اتصال اکانت قطع (غیرفعال) شد.**\n\n"
                f"📊 **وضعیت محفوظ اکانت:** `{email}`\n"
                f"👤 **توضیحات:** `{curr_comment or 'ثبت‌نشده'}`\n"
                f"• **حجم باقیمانده:** **{rem_gb:.2f} گیگابایت** (محفوظ و دست‌نخورده)\n"
                f"• **اعتبار زمانی باقیمانده:** **{exp_text}**"
            )
            pending_res = database.get_pending_reservation(account_code)
            keyboard = nlp_matcher.build_account_keyboard(account_code, is_enabled=False, on_first_connect=on_first, role=role, pending_res=pending_res)
            await safe_edit_message(query, text_out, reply_markup=keyboard)
            database.log_audit(
                rep_id=target_rep_id, account_code=account_code, email=email,
                action="suspend_account", actor_id=user.id, actor_name=user.full_name or user.username,
                details={"rem_gb": rem_gb}, result_status="success"
            )
            await query.answer("🛑 اتصال اکانت با موفقیت قطع شد.")
            await asyncio.to_thread(send_admin_log, f"🛑 قطع اتصال اکانت `{email}` از دکمه شیشه‌ای")

    # 6. Activate Account
    elif action == "activate":
        if role not in ("super_admin", "rep"):
            await query.answer("⛔️ عدم دسترسی", show_alert=True)
            return
        res = await asyncio.to_thread(xui_manager.set_client_enable, email, enable=True, scope=scope_flt, by_code_only=by_code_only)
        if res.get("status") == "ok":
            target_rep_id = acc_record.get("rep_id") or (rep["id"] if rep else None)
            database.upsert_account_record(rep_id=target_rep_id, email=email, account_code=account_code, enable=1)
            rem_gb = client_info.get("remaining_gb", 0)
            exp_text = client_info.get("expiry_desc", "نامشخص")
            text_out = (
                f"✅ **اتصال اکانت مجدداً فعال شد.**\n\n"
                f"📊 **وضعیت اکانت:** `{email}`\n"
                f"👤 **توضیحات:** `{curr_comment or 'ثبت‌نشده'}`\n"
                f"• **حجم باقیمانده:** **{rem_gb:.2f} گیگابایت**\n"
                f"• **اعتبار زمانی باقیمانده:** **{exp_text}**"
            )
            pending_res = database.get_pending_reservation(account_code)
            keyboard = nlp_matcher.build_account_keyboard(account_code, is_enabled=True, on_first_connect=on_first, role=role, pending_res=pending_res)
            await safe_edit_message(query, text_out, reply_markup=keyboard)
            database.log_audit(
                rep_id=target_rep_id, account_code=account_code, email=email,
                action="activate_account", actor_id=user.id, actor_name=user.full_name or user.username,
                details={"rem_gb": rem_gb}, result_status="success"
            )
            await query.answer("✅ اتصال اکانت مجدداً برقرار گردید.")
            await asyncio.to_thread(send_admin_log, f"✅ برقراری اتصال اکانت `{email}` از دکمه شیشه‌ای")

    # 7. Quick Renewal via Button (qrenew:<account_code>:<plan_code>)
    elif action == "qrenew":
        if role not in ("super_admin", "rep"):
            await query.answer("⛔️ عدم دسترسی", show_alert=True)
            return

        plan_code = parts[2] if len(parts) > 2 else "20gb_30d"
        plan = database.get_plan(plan_code)
        if not plan:
            await query.answer("❌ پلن مورد نظر تعریف نشده است.", show_alert=True)
            return

        gb = plan["gb"]
        days = plan["days"]
        ok, msg_out, keyboard = await process_account_renewal(
            user=user, role=role, rep=rep, acc_record=acc_record, client_info=client_info,
            gb=gb, days=days, plan_code=plan_code, effective_first=on_first,
            curr_comment=curr_comment, comment_updated=False
        )
        await safe_edit_message(query, msg_out, reply_markup=keyboard)
        await query.answer("عملیات تمدید / صف رزرو پردازش شد.")

    # 8. Force Activate Reserved Package
    elif action == "force_res":
        res_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        res_item = database.get_reservation_by_id(res_id)
        if not res_item or res_item["status"] != "pending":
            await query.answer("⚠️ این بسته قبلاً فعال یا لغو شده است.", show_alert=True)
            return

        res_email = res_item["email"]
        if (
            str(res_email).lower() != str(email).lower()
            or (rep and res_item.get("rep_id") != rep.get("id"))
        ):
            await query.answer("⛔️ این رزرو متعلق به اکانت یا نمایندگی شما نیست.", show_alert=True)
            return
        gb = res_item["gb"]
        days = res_item["days"]
        on_first_res = bool(res_item.get("on_first_connect", 0))

        try:
            ren_res = await asyncio.to_thread(
                xui_manager.renew_client,
                res_email, gb, days, comment=None, rollover=False,
                countdown=True if on_first_res else "auto",
                scope=scope_flt, by_code_only=True
            )
        except Exception:
            logger.exception("Force reservation outcome is unknown: %s", res_id)
            await query.answer("⚠️ پاسخ پنل نامشخص است؛ رزرو در صف باقی ماند و نیاز به بررسی دارد.", show_alert=True)
            return
        if not _xui_operation_succeeded(ren_res):
            await query.answer("⚠️ پنل موفقیت فعال‌سازی را تایید نکرد؛ رزرو در صف باقی ماند.", show_alert=True)
            return
        activated = database.activate_reservation(res_id, actor_id=user.id, actor_name=user.full_name or user.username)
        if not activated:
            await query.answer("⚠️ وضعیت رزرو تغییر کرده است؛ از اجرای دوباره خودداری شد.", show_alert=True)
            return
        report_text = ren_res.get("report", "")
        report_text = f"🚀 **بسته رزرو به صورت فوری فعال شد (اعتبار قبلی صفر و دوره جدید آغاز گردید):**\n\n" + report_text
        keyboard = nlp_matcher.build_account_keyboard(account_code, is_enabled=True, on_first_connect=on_first_res, role=role, pending_res=None)
        await safe_edit_message(query, report_text, reply_markup=keyboard)
        await query.answer("🚀 بسته رزرو با موفقیت فعال شد.")
        await asyncio.to_thread(
            send_admin_log,
            f"🚀 **فعال‌سازی فوری بسته رزرو اکانت:** `{res_email}` توسط {user.full_name}\n"
            f"• بسته: {res_item['plan_name']}"
        )

    # 9. Cancel Reserved Package and Refund
    elif action == "cancel_res":
        res_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        res_item = database.get_reservation_by_id(res_id)
        if (
            not res_item or res_item.get("status") != "pending"
            or str(res_item.get("email", "")).lower() != str(email).lower()
            or (rep and res_item.get("rep_id") != rep.get("id"))
        ):
            await query.answer("⛔️ رزرو در انتظار متعلق به اکانت یا نمایندگی شما نیست.", show_alert=True)
            return
        cancel_res = database.cancel_reservation(res_id, actor_id=user.id, actor_name=user.full_name or user.username)
        if not cancel_res:
            await query.answer("⚠️ این بسته قبلاً لغو یا فعال شده است.", show_alert=True)
            return
        res_data, new_bal = cancel_res
        if res_data.get("rep_id") is None:
            fin_info = "• وضعیت مالی: 👑 **عملیات مستقیم مدیریت کل (معاف از صورت‌حساب)**"
        else:
            fin_info = f"• **مانده بدهی جدید نماینده:** **{new_bal:,} تومان**"

        msg_out = (
            f"🗑 **بسته رزرو اکانت `{account_code}` با موفقیت لغو شد.**\n\n"
            f"• بسته لغو شده: **{res_data['plan_name']}**\n"
            f"• مبلغ عودت‌داده‌شده: **{res_data['charge_amount']:,} تومان**\n"
            f"{fin_info}"
        )
        keyboard = nlp_matcher.build_account_keyboard(account_code, is_enabled=is_enabled, on_first_connect=on_first, role=role, pending_res=None)
        await safe_edit_message(query, msg_out, reply_markup=keyboard)
        await query.answer("🗑 بسته رزرو لغو و وجه آن عودت داده شد.", show_alert=True)
        await asyncio.to_thread(
            send_admin_log,
            f"🗑 **لغو بسته رزرو و عودت وجه برای اکانت:** `{res_data['email']}` توسط {user.full_name}\n"
            f"• مبلغ عودت: {res_data['charge_amount']:,} تومان | مانده بدهی: {new_bal:,} تومان"
        )

    # 8. Subscription Link Only
    elif action == "deliv_sub":
        sub_id = client_info.get("sub_id", "")
        sub_url = xui_manager.get_sub_url(sub_id)
        msg_out = f"🔗 **لینک سابسکریپشن اکانت `{account_code}`:**\n\n`{sub_url}`"
        keyboard = nlp_matcher.build_account_keyboard(account_code, is_enabled=is_enabled, on_first_connect=on_first, role=role)
        await safe_edit_message(query, msg_out, reply_markup=keyboard)
        await query.answer("🔗 لینک سابسکریپشن واکشی شد.")

    # 9. Subscription QR Code
    elif action == "deliv_qr":
        sub_id = client_info.get("sub_id", "")
        sub_url = xui_manager.get_sub_url(sub_id)
        qr_file = None
        if sub_url:
            try:
                qr_file = f"/tmp/qr_sub_{account_code}.png"
                await asyncio.to_thread(xui_manager.generate_qr, sub_url, qr_file)
            except Exception as e:
                logger.error(f"QR error: {e}")
        keyboard = nlp_matcher.build_account_keyboard(account_code, is_enabled=is_enabled, on_first_connect=on_first, role=role)
        if qr_file and os.path.exists(qr_file):
            with open(qr_file, "rb") as photo:
                await chat.send_photo(
                    photo=photo,
                    caption=f"📱 **QR Code لینک سابسکریپشن اکانت `{account_code}`**\n\n`{sub_url}`",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=keyboard,
                    message_thread_id=thread_id
                )
            os.remove(qr_file)
            await query.answer("📱 QR ساب ارسال شد.")
        else:
            await query.answer("❌ خطا در ایجاد کد QR", show_alert=True)

    # 10. Configs QR Code
    elif action == "deliv_conf_qr":
        links = await asyncio.to_thread(xui_manager.get_client_links, email)
        if not links:
            await query.answer("❌ کانفیگی برای این اکانت یافت نشد.", show_alert=True)
            return
        best_link = links[0]
        qr_file = None
        try:
            qr_file = f"/tmp/qr_conf_{account_code}.png"
            await asyncio.to_thread(xui_manager.generate_qr, best_link, qr_file)
        except Exception as e:
            logger.error(f"QR error: {e}")
        keyboard = nlp_matcher.build_account_keyboard(account_code, is_enabled=is_enabled, on_first_connect=on_first, role=role)
        if qr_file and os.path.exists(qr_file):
            with open(qr_file, "rb") as photo:
                await chat.send_photo(
                    photo=photo,
                    caption=f"🔀 **QR Code اتصال مستقیم به کانفیگ VLESS:**\n\n`{best_link}`",
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=keyboard,
                    message_thread_id=thread_id
                )
            os.remove(qr_file)
            await query.answer("🔀 QR کانفیگ ارسال شد.")
        else:
            await query.answer("❌ خطا در ساخت QR کانفیگ", show_alert=True)

    # 11. Reset SubId
    elif action == "reset_sub":
        res_ok, res_msg, new_sub = await asyncio.to_thread(sub_manager.reset_client_sub_id, xui_manager, email)
        if res_ok:
            new_url = sub_manager.get_subscription_url(new_sub)
            target_rep_id = acc_record.get("rep_id") or (rep["id"] if rep else None)
            database.upsert_account_record(rep_id=target_rep_id, email=email, account_code=account_code, sub_id=new_sub)
            database.log_audit(
                rep_id=target_rep_id, account_code=account_code, email=email,
                action="reset_sub_id", actor_id=user.id, actor_name=user.full_name or user.username,
                details={"new_sub_id": new_sub}, result_status="success"
            )
            out_text = (
                f"🔄 **لینک سابسکریپشن اکانت `{account_code}` با موفقیت بازنشانی شد.**\n\n"
                f"⚠️ لینک قبلی بی‌درنگ باطل گردید.\n\n"
                f"🔗 **لینک جدید سابسکریپشن:**\n`{new_url}`"
            )
            keyboard = nlp_matcher.build_account_keyboard(account_code, is_enabled=is_enabled, on_first_connect=on_first, role=role)
            await safe_edit_message(query, out_text, reply_markup=keyboard)
            await query.answer("✅ لینک سابسکریپشن بازنشانی شد.")
            await asyncio.to_thread(send_admin_log, f"🔄 بازنشانی لینک ساب اکانت `{email}` توسط `{user.id}`", target_rep_id)
        else:
            await query.answer(f"❌ خطا در بازنشانی لینک ساب: {res_msg}", show_alert=True)

# --- Background Reserve Queue Watchdog Task ---

async def watchdog_worker(application: Application):
    logger.info("Starting background Reserve Queue Watchdog...")
    while True:
        try:
            interval = database.get_setting("watchdog_interval_sec", default=300, cast_type=int)
            trigger_gb = database.get_setting("reserve_trigger_gb", default=0.15, cast_type=float)

            # Step 1: Check pending reservations in local SQLite
            pending_reservations = database.get_all_pending_reservations()
            if pending_reservations:
                logger.info(f"Watchdog cycle: inspecting {len(pending_reservations)} pending reserve packages...")

                def fetch_all():
                    s = xui_manager.get_session()
                    r = s.get(f"{xui_manager.BASE_URL}/panel/api/clients/list", timeout=15)
                    return r.json().get("obj", []) if r.ok else []

                all_clients = await asyncio.to_thread(fetch_all)
                client_map = {c.get("email"): c for c in all_clients if c.get("email")}

                for res in pending_reservations:
                    email = res["email"]
                    c = client_map.get(email)
                    if not c:
                        continue

                    client_detail = xui_manager.parse_client_info(c, full_detail=None)
                    rem_gb = float(client_detail.get("remaining_gb", 0) if isinstance(client_detail.get("remaining_gb"), (int, float)) else 999.0)
                    days_left = client_detail.get("days_left")
                    is_expired = client_detail.get("is_expired", False)
                    is_depleted = client_detail.get("is_depleted", False)
                    is_enabled = bool(client_detail.get("enable", True))

                    trigger = False
                    reason = ""
                    if not is_enabled or is_expired or (days_left is not None and days_left <= 0):
                        trigger = True
                        reason = "پایان اعتبار زمانی"
                    elif is_depleted or rem_gb <= trigger_gb:
                        trigger = True
                        reason = f"اتمام حجم (باقیمانده: {rem_gb:.2f} GB)"

                    if trigger:
                        logger.info(f"Watchdog auto-activating reserve package for {email} ({reason})")
                        gb = res["gb"]
                        days = res["days"]
                        on_first = bool(res.get("on_first_connect", 0))

                        ren_res = await asyncio.to_thread(
                            xui_manager.renew_client,
                            email, gb, days, comment=None, rollover=False,
                            countdown=True if on_first else "auto",
                            scope="all", by_code_only=True
                        )
                        if not _xui_operation_succeeded(ren_res):
                            logger.error(
                                "Watchdog did not receive a confirmed X-UI success for reservation %s; "
                                "leaving it pending for reconciliation (response=%r)",
                                res["id"], ren_res
                            )
                            continue
                        activated = database.activate_reservation(res["id"], actor_id=0, actor_name="watchdog")
                        if not activated:
                            logger.warning("Reservation %s changed state before watchdog commit", res["id"])
                            continue

                        account_code = res["account_code"]
                        plan_name = res["plan_name"]
                        rep_id = res["rep_id"]
                        rep_info = database.get_rep_by_id(rep_id)

                        log_msg = (
                            f"🚀 **فعال‌سازی خودکار بسته رزرو (Watchdog):**\n\n"
                            f"• اکانت: `{email}` (`{account_code}`)\n"
                            f"• نماینده: **{rep_info['name'] if rep_info else 'مدیریت کل'}**\n"
                            f"• علت فعال‌سازی: **{reason}**\n"
                            f"• بسته فعال‌شده: **{plan_name}** ({gb} گیگ / {days} روز)\n"
                            f"• وضعیت: شروع دوره جدید با صفر شدن ترافیک قبلی"
                        )
                        await asyncio.to_thread(send_admin_log, log_msg)

                        if rep_info and rep_info.get("group_id") and rep_info.get("topic_id"):
                            try:
                                await application.bot.send_message(
                                    chat_id=rep_info["group_id"],
                                    message_thread_id=rep_info["topic_id"],
                                    text=(
                                        f"🚀 **بسته رزرو اکانت `{account_code}` به صورت خودکار فعال گردید.**\n\n"
                                        f"• علت فعال‌سازی: {reason}\n"
                                        f"• بسته: **{plan_name}** ({gb} گیگ / {days} روز)\n"
                                        f"• وضعیت: آغاز دوره جدید با صفر شدن ترافیک قبلی"
                                    ),
                                    parse_mode=ParseMode.MARKDOWN
                                )
                            except Exception as e:
                                logger.error(f"Failed to send rep auto-activation notice: {e}")

        except Exception as e:
            logger.error(f"Error in watchdog worker cycle: {e}", exc_info=True)

        interval = database.get_setting("watchdog_interval_sec", default=300, cast_type=int)
        await asyncio.sleep(interval)

def main():
    database.init_db()
    startup_sync = database.sync_all_active_reps_detailed()
    if startup_sync.get("status") != "success":
        logger.error("Initial X-UI database sync failed: %s", startup_sync.get("message"))
    
    async def post_init(application: Application):
        asyncio.create_task(watchdog_worker(application))

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("admin", start_cmd))
    app.add_handler(CommandHandler("panel", start_cmd))
    app.add_handler(CommandHandler("menu", start_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("link", link_cmd))
    app.add_handler(CommandHandler("pricing", pricing_cmd))
    app.add_handler(CommandHandler("setprice", setprice_cmd))
    app.add_handler(CommandHandler("billing", billing_cmd))
    app.add_handler(CommandHandler("balance", billing_cmd))
    app.add_handler(CommandHandler("factor", billing_cmd))
    app.add_handler(CommandHandler("wallet", wallet_cmd))
    app.add_handler(CommandHandler("deposit", deposit_cmd))
    app.add_handler(CommandHandler("card", card_cmd))
    app.add_handler(CommandHandler("pay", pay_cmd))
    app.add_handler(CommandHandler("sync", sync_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))
    
    logger.info("Starting upgraded HstAgentBot polling with Watchdog...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
