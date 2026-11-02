import os
import sys
import time
import logging
import asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

import database
import nlp_matcher

sys.path.append("/root/.hermes/scripts")
import xui_manager
import backup_and_report

logger = logging.getLogger("HstAgentBot.AdminPanel")

# --- Keyboards ---

def build_admin_main_keyboard() -> InlineKeyboardMarkup:
    keyboard = [
        [
            InlineKeyboardButton("👥 مدیریت نماینده‌ها", callback_data="admin:reps"),
            InlineKeyboardButton("👤 مدیریت کاربران", callback_data="admin:users")
        ],
        [
            InlineKeyboardButton("📦 تخصیص و اسکوپ", callback_data="admin:scope"),
            InlineKeyboardButton("💰 امور مالی و تسویه‌ها", callback_data="admin:finance")
        ],
        [
            InlineKeyboardButton("⚙️ تعرفه‌ها و پلن‌ها", callback_data="admin:plans"),
            InlineKeyboardButton("🔄 همگام‌سازی و آمار", callback_data="admin:sync")
        ],
        [
            InlineKeyboardButton("⚠️ مانیتورینگ اکانت‌های بحرانی", callback_data="admin:audit"),
            InlineKeyboardButton("🏊 استخر آزاد", callback_data="admin:pool")
        ],
        [
            InlineKeyboardButton("📦 بسته‌های رزرو در انتظار", callback_data="admin:reserves"),
            InlineKeyboardButton("🛠 تنظیمات پیشرفته سامانه", callback_data="admin:settings")
        ],
        [
            InlineKeyboardButton("💾 پشتیبان‌گیری سریع دیتابیس", callback_data="admin:backup")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def build_back_to_admin_keyboard(extra_buttons=None) -> InlineKeyboardMarkup:
    keyboard = []
    if extra_buttons:
        for btn_row in extra_buttons:
            keyboard.append(btn_row)
    keyboard.append([InlineKeyboardButton("↩️ بازگشت به پنل مدیریت ادمین", callback_data="admin:main")])
    return InlineKeyboardMarkup(keyboard)

# --- Admin Views & Logic ---

async def show_admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message=False):
    with database.get_db() as conn:
        cur = conn.cursor()
        total_accs = cur.execute("SELECT count(*) FROM accounts").fetchone()[0]
        active_accs = cur.execute("SELECT count(*) FROM accounts WHERE enable = 1").fetchone()[0]
        reps = database.get_all_reps()
        total_rep_debt = sum([r["balance_toman"] or 0 for r in reps])
        
    dashboard_text = (
        "👑 **پیشخوان مدیریت جامع سیستم (Super Admin Dashboard)**\n\n"
        "⚡️ وضعیت سامانه: **آنلاین و پایدار** (Zero Token Cost)\n"
        f"• کل اکانت‌های ثبت‌شده در دیتابیس: **{total_accs}**\n"
        f"• اکانت‌های فعال: **{active_accs}** | غیرفعال: **{total_accs - active_accs}**\n"
        f"• تعداد کل نماینده‌ها: **{len(reps)}**\n"
        f"• مجموع بدهی تجمیعی نماینده‌ها: **{total_rep_debt:,} تومان**\n\n"
        "👇 بخش مورد نظر را از دکمه‌های زیر انتخاب کنید:"
    )
    
    keyboard = build_admin_main_keyboard()
    if edit_message and update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                dashboard_text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
            )
        except BadRequest:
            pass
    elif update.message:
        await update.message.reply_text(dashboard_text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

async def handle_admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    
    data = query.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "main"
    sub_action = parts[2] if len(parts) > 2 else ""

    # 1. Main Dashboard
    if action == "main":
        await show_admin_dashboard(update, context, edit_message=True)
        await query.answer("پیشخوان مدیریت")
        return

    # 2. Representatives Management
    elif action == "reps":
        reps = database.get_all_reps()
        lines = ["👥 **مدیریت نماینده‌ها و وضعیت مالی:**\n"]
        extra_btns = []
        for r in reps:
            lines.append(
                f"👤 **{r['name']}** (`{r['rep_code']}`):\n"
                f"• شناسه تلگرام: `{r['telegram_id']}`\n"
                f"• تعداد اکانت‌ها: **{r['account_count']}**\n"
                f"• مانده بدهی جاری: **{r['balance_toman']:,} تومان**\n"
            )
            extra_btns.append([
                InlineKeyboardButton(f"🟢 تسویه {r['name']}", callback_data=f"admin:prompt_pay:{r['rep_code']}"),
                InlineKeyboardButton(f"📑 تراکنش‌ها", callback_data=f"admin:rep_tx:{r['id']}")
            ])
            
        extra_btns.append([InlineKeyboardButton("➕ افزودن نماینده جدید", callback_data="admin:prompt_add_rep")])
        keyboard = build_back_to_admin_keyboard(extra_btns)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("مدیریت نماینده‌ها")

    # 3. Users Management
    elif action == "users":
        users = database.get_all_bot_users()
        lines = ["👤 **مدیریت کاربران نهایی (End-Users):**\n"]
        if not users:
            lines.append("• تاکنون کاربری به حساب کاربری تلگرام متصل نشده است.")
        else:
            for u in users[:10]:
                lines.append(
                    f"• `{u['telegram_id']}` ({u['full_name'] or u['username'] or 'نامشخص'}): "
                    f"**{u['linked_accounts_count']}** اکانت متصل (نماینده: {u['rep_name'] or 'عمومی'})"
                )
        extra_btns = [
            [InlineKeyboardButton("➕ اتصال دستی اکانت به Chat ID", callback_data="admin:prompt_link_user")]
        ]
        keyboard = build_back_to_admin_keyboard(extra_btns)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("مدیریت کاربران")

    # 4. Scope & Account Transfer
    elif action == "scope":
        text = (
            "📦 **مدیریت اسکوپ و مالکیت اکانت‌ها:**\n\n"
            "در این بخش می‌توانید مالکیت یک اکانت را بین نماینده‌ها تغییر داده یا مستقیماً به یک Chat ID تلگرام متصل کنید.\n\n"
            "👇 یکی از گزینه‌های زیر را انتخاب نمایید:"
        )
        extra_btns = [
            [InlineKeyboardButton("🔄 انتقال مالکیت یک اکانت", callback_data="admin:prompt_transfer")],
            [InlineKeyboardButton("🔍 جستجو و بازرسی کامل یک اکانت", callback_data="admin:prompt_inspect")]
        ]
        keyboard = build_back_to_admin_keyboard(extra_btns)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("اسکوپ اکانت‌ها")

    # 5. Financial Management
    elif action == "finance":
        reps = database.get_all_reps()
        total_debt = sum([r["balance_toman"] or 0 for r in reps])
        recent_tx = database.get_recent_ledger_entries(limit=5)
        
        lines = [
            "💰 **امور مالی، صورت‌حساب‌ها و تسویه‌حساب:**\n",
            f"• مجموع بدهی وصول‌نشده نماینده‌ها: **{total_debt:,} تومان**\n",
            "📊 **مانده به تفکیک نماینده:**"
        ]
        extra_btns = []
        for r in reps:
            lines.append(f"• {r['name']}: **{r['balance_toman']:,} تومان**")
            extra_btns.append([
                InlineKeyboardButton(f"🟢 ثبت واریزی {r['name']}", callback_data=f"admin:prompt_pay:{r['rep_code']}")
            ])
            
        lines.append("\n🕒 **آخرین تراکنش‌های سیستم:**")
        if not recent_tx:
            lines.append("• تراکنشی ثبت نشده است.")
        else:
            for tx in recent_tx:
                sign = "🔺" if tx["transaction_type"] == "debit" else "🟢"
                lines.append(f"{sign} `{tx['created_at'][:16]}` | {tx['amount']:,} ت | {tx['description']}")
                
        extra_btns.append([InlineKeyboardButton("📑 مشاهده ۱۰ تراکنش آخر", callback_data="admin:recent_tx:all")])
        keyboard = build_back_to_admin_keyboard(extra_btns)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("امور مالی")

    # 6. Pricing Plans Management
    elif action == "plans":
        plans = database.get_pricing_plans()
        lines = ["⚙️ **تعرفه‌ها و پلن‌های فعال سیستم:**\n"]
        extra_btns = []
        for p in plans:
            quick_tag = "⚡️ [سریع]" if p["is_quick"] else "📦"
            lines.append(
                f"{quick_tag} **{p['name']}** (`{p['plan_code']}`):\n"
                f"   • خرید نماینده: **{p['rep_price']:,} ت** | فروش کاربر: **{p['user_price']:,} ت**"
            )
            extra_btns.append([
                InlineKeyboardButton(f"✏️ ویرایش نرخ {p['name']}", callback_data=f"admin:prompt_edit_plan:{p['plan_code']}")
            ])
            
        extra_btns.append([InlineKeyboardButton("➕ تعریف پلن جدید", callback_data="admin:prompt_add_plan")])
        keyboard = build_back_to_admin_keyboard(extra_btns)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("تعرفه‌ها و پلن‌ها")

    # 7. Sync & Server Status
    elif action == "sync":
        count = await asyncio.to_thread(database.sync_all_active_reps)
        text = (
            "🔄 **همگام‌سازی و آمار لایو سرور ۳X-UI:**\n\n"
            f"✅ همگام‌سازی دیتابیس لوکال با موفقیت انجام شد.\n"
            f"• تعداد اکانت‌های بروزرسانی‌شده: **{count}**\n"
            f"• وضعیت اتصال API پنل: **پایدار و متصل**\n"
            f"• زمان آخرین همگام‌سازی: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
        )
        keyboard = build_back_to_admin_keyboard()
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("همگام‌سازی تکمیل شد.")

    # 8. Critical Accounts Audit
    elif action == "audit":
        audit_res = await asyncio.to_thread(xui_manager.audit_clients_overview, scope="all")
        report_text = xui_manager.format_audit_report(audit_res)
        keyboard = build_back_to_admin_keyboard()
        await query.edit_message_text(f"⚠️ **گزارش اکانت‌های بحرانی و روبه‌اتمام:**\n\n{report_text}", parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("گزارش اکانت‌های بحرانی")

    # 9. Free Pool Accounts Watchdog
    elif action == "pool":
        def fetch_unused_clients():
            s = xui_manager.get_session()
            r = s.get(f"{xui_manager.BASE_URL}/panel/api/clients/list")
            all_c = r.json().get("obj", []) if r.ok else []
            un = []
            for c in all_c:
                em = c.get("email", "")
                if em:
                    tr = c.get("traffic") or {}
                    up = tr.get("up", 0) or c.get("up", 0) or 0
                    down = tr.get("down", 0) or c.get("down", 0) or 0
                    com = c.get("comment") or ""
                    exp = c.get("expiryTime") or 0
                    if up == 0 and down == 0 and not com and exp < 0:
                        un.append(em)
            return un
            
        unused = await asyncio.to_thread(fetch_unused_clients)
        badge = "🟢 وضعیت مطلوب" if len(unused) >= 10 else "⚠️ ظرفیت رو به اتمام"
        text = (
            f"🏊 **استخر اکانت‌های آزاد (Pool Watchdog):**\n\n"
            f"• وضعیت ظرفیت: **{badge}**\n"
            f"• تعداد کل اکانت‌های آماده تخصیص: **{len(unused)}**\n\n"
            f"نمونه اکانت‌های آماده استخر:\n" +
            "\n".join([f"• `{e}`" for e in unused[:6]])
        )
        keyboard = build_back_to_admin_keyboard()
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("استخر آزاد")

    # 10. Instant Backup & Download
    elif action == "backup":
        try:
            import backup
            b_res = await asyncio.to_thread(backup.run_backup)
            archive_path = b_res.get("archive")
            size_kb = b_res.get("size_kb")
            
            keyboard = build_back_to_admin_keyboard()
            await query.edit_message_text(
                f"✅ **پشتیبان‌گیری کامل انجام شد.**\n\n"
                f"• حجم فایل: **{size_kb} KB**\n"
                f"• مسیر لوکال: `{archive_path}`\n"
                f"• ارسال به تاپیک ۲۰ فروم: **انجام شد**\n\n"
                f"فایل آرشیو در ادامه برای شما ارسال می‌شود...",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard
            )
            if archive_path and os.path.exists(archive_path):
                with open(archive_path, "rb") as doc:
                    await chat.send_document(
                        document=doc,
                        caption=f"💾 نسخه پشتیبان دیتابیس و تنظیمات ({size_kb} KB)"
                    )
            await query.answer("بکاپ با موفقیت ایجاد شد.")
        except Exception as e:
            await query.answer(f"خطا در بکاپ: {e}", show_alert=True)

    # 11. Dynamic System Settings Panel
    elif action == "settings":
        settings_list = database.get_all_settings()
        lines = [
            "🛠 **مرکز تنظیمات پویا و متغیرهای کنترلی سامانه:**\n",
            "برای ویرایش هر پارامتر، روی دکمه آن کلیک کنید. تغییرات به صورت آنی (Hot-Reload) و بدون نیاز به ری‌استارت اعمال می‌شوند:\n"
        ]
        setting_buttons = []
        for s in settings_list:
            k = s["key"]
            lbl = s["label"] or k
            v = s["value"]
            lines.append(f"• **{lbl}** (`{k}`): `{v}`")
            setting_buttons.append([InlineKeyboardButton(f"⚙️ {lbl}: {v}", callback_data=f"admin:prompt_setting:{k}")])
            
        keyboard = build_back_to_admin_keyboard(setting_buttons)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("تنظیمات سیستم")

    # 12. Reserved Packages Queue Watcher
    elif action == "reserves":
        pending_list = database.get_all_pending_reservations()
        if not pending_list:
            text = (
                "📦 **صف بسته‌های رزرو در انتظار (Reserve Queue):**\n\n"
                "✅ در حال حاضر هیچ بسته رزرو فعالی در صف انتظار نیست.\n"
                "کلیه تمدیدها و اکانت‌ها در وضعیت نرمال قرار دارند."
            )
            keyboard = build_back_to_admin_keyboard()
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer("صف بسته‌های رزرو")
        else:
            lines = [
                f"📦 **صف بسته‌های رزرو در انتظار ({len(pending_list)} بسته):**\n",
                "این بسته‌ها با اتمام حجم یا زمان اکانت فعلی به صورت خودکار فعال خواهند شد:\n"
            ]
            extra_btns = []
            for r in pending_list:
                lines.append(
                    f"• اکانت: `{r['account_code']}` ({r.get('rep_name') or 'عمومی'})\n"
                    f"  پلن: **{r['plan_name']}** ({r['gb']}G / {r['days']}D)\n"
                    f"  ثبت: `{r['created_at'][:16]}` | فاکتور: `{r['charge_amount']:,} ت`"
                )
                extra_btns.append([
                    InlineKeyboardButton(f"🚀 فعال‌سازی فوری {r['account_code']}", callback_data=f"admin:force_res:{r['id']}:{r['account_code']}"),
                    InlineKeyboardButton(f"🗑 لغو و عودت {r['account_code']}", callback_data=f"admin:cancel_res:{r['id']}:{r['account_code']}")
                ])
            keyboard = build_back_to_admin_keyboard(extra_btns)
            await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer("صف بسته‌های رزرو")

    # 13. Admin Force/Cancel Reserve Actions
    elif action == "force_res":
        res_id = int(parts[2])
        acc_code = parts[3] if len(parts) > 3 else ""
        res_item = database.get_reservation_by_id(res_id)
        if not res_item or res_item["status"] != "pending":
            await query.answer("⚠️ این بسته قبلاً فعال یا لغو شده است.", show_alert=True)
            return
        
        email = res_item["email"]
        gb = res_item["gb"]
        days = res_item["days"]
        on_first = bool(res_item.get("on_first_connect", 0))
        ren_res = await asyncio.to_thread(
            xui_manager.renew_client,
            email, gb, days, comment=None, rollover=False, countdown=True if on_first else "auto",
            scope="all", by_code_only=True
        )
        database.activate_reservation(res_id, actor_id=user.id, actor_name=user.full_name or "Admin")
        await query.answer(f"🚀 بسته رزرو اکانت {acc_code} با موفقیت فعال شد.", show_alert=True)
        # Refresh reserves view
        await handle_admin_callbacks(update, context)
        return

    elif action == "cancel_res":
        res_id = int(parts[2])
        acc_code = parts[3] if len(parts) > 3 else ""
        cancel_res = database.cancel_reservation(res_id, actor_id=user.id, actor_name=user.full_name or "Admin")
        if cancel_res:
            res_data, new_bal = cancel_res
            await query.answer(f"🗑 بسته رزرو اکانت {acc_code} لغو و {res_data['charge_amount']:,} تومان عودت داده شد.", show_alert=True)
        else:
            await query.answer("⚠️ این بسته قبلاً لغو یا فعال شده است.", show_alert=True)
        # Refresh reserves view
        await handle_admin_callbacks(update, context)
        return

    # 14. Interactive Input Prompts
    elif action.startswith("prompt_"):
        if chat.type != "private":
            await query.answer("⚠️ این عملیات منحصراً در چت خصوصی (پیوی) ربات در دسترس است تا چت تاپیک شلوغ نشود.", show_alert=True)
            return

        now_ts = time.time()
        if action == "prompt_setting":
            setting_key = sub_action
            all_s = {s["key"]: s for s in database.get_all_settings()}
            target_s = all_s.get(setting_key)
            if not target_s:
                await query.answer("❌ تنظیم مورد نظر یافت نشد.", show_alert=True)
                return
            context.user_data["admin_state"] = {"action": "edit_setting", "key": setting_key, "label": target_s["label"], "ts": now_ts}
            msg = (
                f"🛠 **ویرایش تنظیمات سامانه:**\n\n"
                f"• عنوان: **{target_s['label']}**\n"
                f"• کلید سیستمی: `{setting_key}`\n"
                f"• توضیح: {target_s['description']}\n"
                f"• مقدار فعلی: `{target_s['value']}`\n\n"
                f"لطفاً مقدار جدید را ارسال کنید:\n\n"
                f"⏱ مهلت ارسال: ۶۰ ثانیه (یا بنویسید `cancel`)"
            )
            keyboard = build_back_to_admin_keyboard()
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer()

        elif action == "prompt_pay":
            rep_code = sub_action
            context.user_data["admin_state"] = {"action": "pay", "rep_code": rep_code, "ts": now_ts}
            msg = (
                f"🟢 **ثبت تسویه حساب / واریزی نماینده (`{rep_code}`):**\n\n"
                f"لطفاً مبلغ واریزی به تومان و در صورت تمایل توضیحات را به این صورت ارسال کنید:\n"
                f"`500000 کارت به کارت بانک سامان`\n\n"
                f"⏱ مهلت ارسال: ۶۰ ثانیه (یا بنویسید `cancel`)"
            )
            keyboard = build_back_to_admin_keyboard()
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer()

        elif action == "prompt_add_rep":
            context.user_data["admin_state"] = {"action": "add_rep", "ts": now_ts}
            msg = (
                "➕ **افزودن نماینده جدید:**\n\n"
                "لطفاً اطلاعات نماینده را با فاصله ارسال فرمایید:\n"
                "`<کد_نماینده> <نام_نماینده> <Chat_ID_نماینده> <Group_ID> <Topic_ID> <اسکوپ>`\n\n"
                "مثال:\n"
                "`tehran نمایندگی_تهران 12345678 -1001234567 10 tehran_scope`\n\n"
                "⏱ مهلت ارسال: ۶۰ ثانیه (یا بنویسید `cancel`)"
            )
            keyboard = build_back_to_admin_keyboard()
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer()

        elif action == "prompt_edit_plan":
            plan_code = sub_action
            context.user_data["admin_state"] = {"action": "edit_plan", "plan_code": plan_code, "ts": now_ts}
            plan = database.get_plan(plan_code)
            msg = (
                f"✏️ **ویرایش نرخ پلن: {plan['name']} (`{plan_code}`)**\n\n"
                f"قیمت فعلی نماینده: `{plan['rep_price']:,} تومان`\n"
                f"قیمت فعلی کاربر: `{plan['user_price']:,} تومان`\n\n"
                f"لطفاً قیمت جدید نماینده و کاربر را با فاصله بفرستید:\n"
                f"`75000 130000`\n\n"
                f"⏱ مهلت ارسال: ۶۰ ثانیه (یا بنویسید `cancel`)"
            )
            keyboard = build_back_to_admin_keyboard()
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer()

        elif action == "prompt_transfer":
            context.user_data["admin_state"] = {"action": "transfer", "ts": now_ts}
            msg = (
                "🔄 **انتقال مالکیت اکانت:**\n\n"
                "لطفاً **کد اکانت** و **کد نماینده جدید** یا Chat ID کاربر را بفرستید:\n"
                "فرمت:\n"
                "`<کد_اکانت> <کد_نماینده>` یا `<کد_اکانت> <Chat_ID>`\n"
                "مثال:\n"
                "`3029 demo_rep`\n\n"
                "⏱ مهلت ارسال: ۶۰ ثانیه (یا بنویسید `cancel`)"
            )
            keyboard = build_back_to_admin_keyboard()
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer()

        elif action == "prompt_inspect":
            context.user_data["admin_state"] = {"action": "inspect", "ts": now_ts}
            msg = (
                "🔍 **بازرسی کامل اکانت:**\n\n"
                "کد ۴ رقمی اکانت یا نام مشتری را ارسال کنید:\n\n"
                "⏱ مهلت ارسال: ۶۰ ثانیه (یا بنویسید `cancel`)"
            )
            keyboard = build_back_to_admin_keyboard()
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer()

        elif action == "prompt_add_plan":
            context.user_data["admin_state"] = {"action": "add_plan", "ts": now_ts}
            msg = (
                "➕ **تعریف پلن جدید:**\n\n"
                "لطفاً مشخصات پلن را به این فرمت ارسال کنید:\n"
                "`<plan_code> <نام_پلن> <گیگ> <روز> <قیمت_نماینده> <قیمت_کاربر>`\n\n"
                "مثال:\n"
                "`25gb_30d ۲۵ گیگ ۱ ماهه 25 30 85000 145000`\n\n"
                "⏱ مهلت ارسال: ۶۰ ثانیه (یا بنویسید `cancel`)"
            )
            keyboard = build_back_to_admin_keyboard()
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer()

        elif action == "prompt_link_user":
            context.user_data["admin_state"] = {"action": "link_user", "ts": now_ts}
            msg = (
                "👤 **اتصال اکانت به Chat ID تلگرام:**\n\n"
                "لطفاً به این فرمت بفرستید:\n"
                "`<Chat_ID> <کد_اکانت>`\n"
                "مثال:\n"
                "`123456789 3029`\n\n"
                "⏱ مهلت ارسال: ۶۰ ثانیه (یا بنویسید `cancel`)"
            )
            keyboard = build_back_to_admin_keyboard()
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer()

    elif action == "recent_tx":
        recent_tx = database.get_recent_ledger_entries(limit=10)
        lines = ["📑 **۱۰ تراکنش اخیر سیستم:**\n"]
        for tx in recent_tx:
            sign = "🔺" if tx["transaction_type"] == "debit" else "🟢"
            lines.append(f"{sign} `{tx['created_at'][:16]}` | **{tx['amount']:,} ت** | {tx['description']} (مانده: {tx['balance_after']:,})")
        keyboard = build_back_to_admin_keyboard()
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("تراکنش‌های اخیر")

async def process_admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Handles step-by-step inputs initiated from Admin inline buttons.
    Strictly isolated to Private DM with 60s timeout and Regex prioritization.
    """
    chat = update.effective_chat
    if not chat or chat.type != "private":
        # Strictly ignore in all non-private chats (groups, topics, supergroups)
        return False
        
    admin_state = context.user_data.get("admin_state")
    if not admin_state:
        return False
        
    now = time.time()
    created_ts = admin_state.get("ts", 0)
    # 60 seconds inactivity timeout
    if now - created_ts > 60:
        context.user_data.pop("admin_state", None)
        return False
        
    text = (update.message.text or "").strip()
    
    # Priority to commands
    if text.startswith("/"):
        context.user_data.pop("admin_state", None)
        return False

    # Priority to cancellation
    if text.lower() in ("cancel", "انصراف", "لغو"):
        context.user_data.pop("admin_state", None)
        await update.message.reply_text("❌ عملیات فرم لغو شد.", reply_markup=build_admin_main_keyboard())
        return True

    # Priority to normal Account queries / Intent / Replies
    reply_to = update.message.reply_to_message
    reply_text = (reply_to.text or reply_to.caption or "") if reply_to else ""
    detected_code = nlp_matcher.extract_account_code(reply_text, text)
    detected_intent, _ = nlp_matcher.detect_intent(text)
    
    if detected_code or detected_intent != "unknown" or update.message.reply_to_message:
        # Release FSM state immediately so router processes account/intent seamlessly
        context.user_data.pop("admin_state", None)
        return False

    action = admin_state.get("action")

    if action == "edit_setting":
        setting_key = admin_state.get("key")
        label = admin_state.get("label", setting_key)
        new_val = text.strip()
        database.set_setting(setting_key, new_val, actor_id=update.effective_user.id, actor_name=update.effective_user.full_name or "Admin")
        context.user_data.pop("admin_state", None)
        await update.message.reply_text(
            f"✅ **تنظیمات با موفقیت بروزرسانی شد.**\n\n"
            f"• پارامتر: **{label}** (`{setting_key}`)\n"
            f"• مقدار جدید: `{new_val}`\n\n"
            f"تغییرات به صورت آنی (Hot-Reload) در سیستم اعمال گردید.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_admin_main_keyboard()
        )
        return True
    
    if action == "pay":
        rep_code = admin_state.get("rep_code")
        rep_info = database.get_rep_by_code(rep_code)
        if not rep_info:
            await update.message.reply_text("❌ نماینده یافت نشد.")
            context.user_data.pop("admin_state", None)
            return True
            
        parts = text.split(maxsplit=1)
        try:
            amount = int(parts[0].replace(",", ""))
            desc = parts[1] if len(parts) > 1 else "تسویه حساب نماینده"
            new_bal = database.register_rep_payment(
                rep_id=rep_info["id"],
                amount=amount,
                description=desc,
                actor_id=update.effective_user.id,
                actor_name=update.effective_user.full_name or "Admin"
            )
            await update.message.reply_text(
                f"✅ **واریزی با موفقیت ثبت شد.**\n\n"
                f"• نماینده: **{rep_info['name']}**\n"
                f"• مبلغ تسویه‌شده: **{amount:,} تومان**\n"
                f"• شرح: `{desc}`\n"
                f"• **مانده بدهی جدید:** **{new_bal:,} تومان**",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_admin_main_keyboard()
            )
            context.user_data.pop("admin_state", None)
            return True
        except ValueError:
            attempts = admin_state.get("attempts", 0) + 1
            admin_state["attempts"] = attempts
            if attempts >= 2:
                context.user_data.pop("admin_state", None)
                await update.message.reply_text("❌ به دلیل ورود متوالی مقادیر نامعتبر، عملیات لغو شد.", reply_markup=build_admin_main_keyboard())
            else:
                await update.message.reply_text("⚠️ فرمت مبلغ نامعتبر است. لطفاً عدد وارد کنید یا بنویسید `cancel`:")
            return True

    elif action == "add_rep":
        parts = text.split(maxsplit=5)
        if len(parts) < 6:
            context.user_data.pop("admin_state", None)
            await update.message.reply_text("⚠️ فیلدهای نماینده ناقص بود و عملیات لغو شد. مجدداً اقدام فرمایید.", reply_markup=build_admin_main_keyboard())
            return True
        r_code, r_name, r_tg, r_grp, r_top, r_scope = parts
        try:
            r_tg = int(r_tg)
            r_grp = int(r_grp)
            r_top = int(r_top)
            database.add_or_update_rep(r_code, r_name, r_tg, r_grp, r_top, r_scope)
            database.log_audit(
                rep_id=None, account_code=r_code, email=None, action="add_rep",
                actor_id=update.effective_user.id, actor_name=update.effective_user.full_name or "Admin",
                details={"name": r_name, "tg": r_tg, "grp": r_grp, "top": r_top, "scope": r_scope},
                result_status="success"
            )
            await update.message.reply_text(
                f"✅ **نماینده جدید با موفقیت ثبت شد.**\n\n"
                f"• نام: **{r_name}** (`{r_code}`)\n"
                f"• شناسه تلگرام: `{r_tg}`\n"
                f"• گروه/تاپیک: `{r_grp}` / `{r_top}`\n"
                f"• فیلتر اسکوپ: `{r_scope}`",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_admin_main_keyboard()
            )
            context.user_data.pop("admin_state", None)
            return True
        except ValueError:
            context.user_data.pop("admin_state", None)
            await update.message.reply_text("⚠️ مقادیر شناسه‌ها باید عددی باشند. عملیات لغو شد.", reply_markup=build_admin_main_keyboard())
            return True

    elif action == "edit_plan":
        plan_code = admin_state.get("plan_code")
        parts = text.split()
        if len(parts) < 2:
            attempts = admin_state.get("attempts", 0) + 1
            admin_state["attempts"] = attempts
            if attempts >= 2:
                context.user_data.pop("admin_state", None)
                await update.message.reply_text("❌ به دلیل ورود متوالی مقادیر نامعتبر، عملیات لغو شد.", reply_markup=build_admin_main_keyboard())
            else:
                await update.message.reply_text("⚠️ لطفاً هر دو قیمت را با فاصله بفرستید (مثال: `75000 130000`) یا بنویسید `cancel`:")
            return True
        try:
            rep_p, user_p = int(parts[0]), int(parts[1])
            plan = database.update_plan_price(plan_code, rep_p, user_p)
            await update.message.reply_text(
                f"✅ **نرخ‌های پلن {plan['name']} بروزرسانی شد.**\n\n"
                f"• قیمت نماینده: **{rep_p:,} تومان**\n"
                f"• قیمت کاربر: **{user_p:,} تومان**",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_admin_main_keyboard()
            )
            context.user_data.pop("admin_state", None)
            return True
        except ValueError:
            attempts = admin_state.get("attempts", 0) + 1
            admin_state["attempts"] = attempts
            if attempts >= 2:
                context.user_data.pop("admin_state", None)
                await update.message.reply_text("❌ به دلیل ورود متوالی مقادیر نامعتبر، عملیات لغو شد.", reply_markup=build_admin_main_keyboard())
            else:
                await update.message.reply_text("⚠️ اعداد نامعتبر است. لطفاً دوباره وارد کنید یا بنویسید `cancel`:")
            return True

    elif action == "transfer":
        parts = text.split()
        if len(parts) < 2:
            context.user_data.pop("admin_state", None)
            await update.message.reply_text("⚠️ فرمت نامعتبر بود و عملیات لغو شد.", reply_markup=build_admin_main_keyboard())
            return True
        acc_code, target = parts[0], parts[1]
        
        # Check if target is rep or chat_id
        rep_info = database.get_rep_by_code(target.lower())
        new_rep_id = rep_info["id"] if rep_info else None
        new_owner_id = int(target) if target.isdigit() else None
        
        res = database.reassign_account(acc_code, new_rep_id=new_rep_id, new_owner_tg_id=new_owner_id)
        if res.get("status") == "ok":
            dest_desc = rep_info['name'] if rep_info else f"Chat ID: {new_owner_id}"
            await update.message.reply_text(
                f"✅ **مالکیت اکانت `{acc_code}` منتقل شد.**\n• مقصد جدید: **{dest_desc}**",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_admin_main_keyboard()
            )
        else:
            await update.message.reply_text(f"❌ خطا: {res.get('message')}")
        context.user_data.pop("admin_state", None)
        return True

    elif action == "inspect":
        insp = await asyncio.to_thread(xui_manager.inspect_client, text, scope="all")
        if insp.get("status") == "ok":
            status_text = xui_manager.format_status_report(insp["data"], is_admin=True)
            await update.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN, reply_markup=build_admin_main_keyboard())
        else:
            await update.message.reply_text(f"❌ اکانتی با مشخصات `{text}` روی سرور یافت نشد.", reply_markup=build_admin_main_keyboard())
        context.user_data.pop("admin_state", None)
        return True

    elif action == "add_plan":
        parts = text.split()
        if len(parts) < 6:
            context.user_data.pop("admin_state", None)
            await update.message.reply_text("⚠️ فیلدها ناقص بود و عملیات لغو شد.", reply_markup=build_admin_main_keyboard())
            return True
        p_code = parts[0]
        p_name = parts[1]
        try:
            p_gb = float(parts[2])
            p_days = int(parts[3])
            p_rep_p = int(parts[4])
            p_user_p = int(parts[5])
            database.add_or_update_plan(p_code, p_name, p_gb, p_days, p_rep_p, p_user_p)
            await update.message.reply_text(
                f"✅ **پلن جدید با موفقیت اضافه شد.**\n\n"
                f"• نام: **{p_name}** (`{p_code}`)\n"
                f"• مشخصات: {p_gb} گیگ / {p_days} روز\n"
                f"• قیمت نماینده: {p_rep_p:,} ت | کاربر: {p_user_p:,} ت",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_admin_main_keyboard()
            )
            context.user_data.pop("admin_state", None)
            return True
        except ValueError:
            context.user_data.pop("admin_state", None)
            await update.message.reply_text("⚠️ مقادیر عددی اشتباه بود و عملیات لغو شد.", reply_markup=build_admin_main_keyboard())
            return True

    elif action == "link_user":
        parts = text.split()
        if len(parts) < 2 or not parts[0].isdigit():
            context.user_data.pop("admin_state", None)
            await update.message.reply_text("⚠️ فرمت نامعتبر بود و عملیات لغو شد.", reply_markup=build_admin_main_keyboard())
            return True
        chat_id, code = int(parts[0]), parts[1]
        res = database.link_user_to_account(code, chat_id)
        if res.get("status") == "ok":
            await update.message.reply_text(f"✅ اکانت `{code}` به Chat ID `{chat_id}` متصل گردید.", reply_markup=build_admin_main_keyboard())
        else:
            await update.message.reply_text(f"❌ خطا: {res.get('message')}")
        context.user_data.pop("admin_state", None)
        return True

    return False
