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

from server_integrations import load_integration
xui_manager = load_integration("xui_manager")
backup_and_report = load_integration("backup_and_report")

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

def get_build_info() -> dict:
    """
    Dynamically extracts live build metadata (version, branch, short commit hash, commit date).
    """
    import subprocess
    import config
    
    version = getattr(config, "VERSION", "v4.0.0-beta.3")
    branch = "beta"
    short_hash = "unknown"
    commit_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    try:
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        r_branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_dir, capture_output=True, text=True, timeout=2
        )
        if r_branch.returncode == 0 and r_branch.stdout.strip():
            branch = r_branch.stdout.strip()
            
        r_hash = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_dir, capture_output=True, text=True, timeout=2
        )
        if r_hash.returncode == 0 and r_hash.stdout.strip():
            short_hash = r_hash.stdout.strip()
            
        r_date = subprocess.run(
            ["git", "log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M"],
            cwd=repo_dir, capture_output=True, text=True, timeout=2
        )
        if r_date.returncode == 0 and r_date.stdout.strip():
            commit_date = r_date.stdout.strip()
    except Exception as e:
        logger.warning(f"Error fetching dynamic git build info: {e}")
        
    return {
        "version": version,
        "branch": branch,
        "short_hash": short_hash,
        "commit_date": commit_date
    }

async def show_admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message=False):
    with database.get_db() as conn:
        cur = conn.cursor()
        total_accs = cur.execute("SELECT count(*) FROM accounts").fetchone()[0]
        active_accs = cur.execute("SELECT count(*) FROM accounts WHERE enable = 1").fetchone()[0]
        reps = database.get_all_reps()
        total_rep_debt = sum([r["balance_toman"] or 0 for r in reps])

    build = get_build_info()
        
    dashboard_text = (
        "👑 **پیشخوان مدیریت جامع سیستم (Super Admin Dashboard)**\n\n"
        "⚙️ وضعیت پنل: **مدیریت و نظارت سامانه**\n"
        f"• کل اکانت‌های ثبت‌شده در دیتابیس: **{total_accs}**\n"
        f"• اکانت‌های فعال: **{active_accs}** | غیرفعال: **{total_accs - active_accs}**\n"
        f"• تعداد کل نماینده‌ها: **{len(reps)}**\n"
        f"• مجموع بدهی تجمیعی نماینده‌ها: **{total_rep_debt:,} تومان**\n\n"
        f"🔖 نسخه سامانه: **{build['version']}** [شاخه `{build['branch']}`]\n"
        f"⚡️ شناسه بیلد: `commit {build['short_hash']}`\n"
        f"🕒 زمان استقرار: `{build['commit_date']}`\n\n"
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
            status_dot = "🟢" if r.get("is_active", 1) else "🛑"
            lines.append(
                f"{status_dot} **{r['name']}** (`{r['rep_code']}`):\n"
                f"• شناسه تلگرام: `{r['telegram_id']}` | بدهی: **{r['balance_toman']:,} تومان**\n"
            )
            extra_btns.append([
                InlineKeyboardButton(f"⚙️ مدیریت {r['name']}", callback_data=f"admin:rep_detail:{r['id']}"),
                InlineKeyboardButton(f"🟢 تسویه", callback_data=f"admin:prompt_pay:{r['rep_code']}")
            ])
            
        extra_btns.append([InlineKeyboardButton("➕ افزودن نماینده جدید", callback_data="admin:prompt_add_rep")])
        keyboard = build_back_to_admin_keyboard(extra_btns)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("مدیریت نماینده‌ها")

    # 3. Users Management (CRUD Paginated & Detail)
    elif action == "users":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        users = database.get_all_bot_users()
        total_users = len(users)
        page_size = 10
        total_pages = max(1, (total_users + page_size - 1) // page_size)
        page = min(max(1, page), total_pages)
        offset = (page - 1) * page_size
        current_users = users[offset:offset + page_size]

        lines = [
            f"👤 **مدیریت کاربران نهایی (صفحه {page} از {total_pages}):**\n",
            "جهت مشاهده شناسنامه، شارژ کیف پول، مسدودسازی یا حذف، کاربر را انتخاب کنید:\n"
        ]
        extra_btns = []
        for u in current_users:
            status_dot = "🔴" if u.get("is_blocked") else "🟢"
            name_str = u['full_name'] or u['username'] or f"ID: {u['telegram_id']}"
            lines.append(f"{status_dot} `{u['telegram_id']}` - **{name_str}** ({u['linked_accounts_count']} اکانت)")
            extra_btns.append([
                InlineKeyboardButton(f"👤 مدیریت {name_str[:15]}", callback_data=f"admin:user_detail:{u['telegram_id']}")
            ])

        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"admin:users:{page - 1}"))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"admin:users:{page + 1}"))
        if nav_row:
            extra_btns.append(nav_row)

        extra_btns.append([
            InlineKeyboardButton("🔍 جستجوی کاربر", callback_data="admin:prompt_search_user"),
            InlineKeyboardButton("➕ اتصال اکانت به Chat ID", callback_data="admin:prompt_link_user")
        ])
        keyboard = build_back_to_admin_keyboard(extra_btns)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("مدیریت کاربران")

    elif action == "user_detail":
        user_id = int(sub_action)
        u_info = database.get_bot_user_by_tg_id(user_id)
        if not u_info:
            await query.answer("❌ کاربر یافت نشد.", show_alert=True)
            return

        is_blocked = u_info.get("is_blocked", 0) == 1
        block_text = "🔴 مسدود (Blocked)" if is_blocked else "🟢 فعال (Active)"
        toggle_btn_text = "🟢 رفع مسدودی" if is_blocked else "🚫 مسدود کردن"

        lines = [
            f"👤 **شناسنامه کاربر نهایی:**\n",
            f"• **نام:** {u_info['full_name']}",
            f"• **یوزرنیم:** @{u_info['username']}" if u_info.get("username") else "• **یوزرنیم:** ندارد",
            f"• **شناسه تلگرام:** `{u_info['telegram_id']}`",
            f"• **وضعیت دسترسی:** **{block_text}**",
            f"• **موجود کیف پول:** **{u_info['wallet_balance']:,} تومان**",
            f"• **تعداد اکانت‌های متصل:** **{u_info['linked_accounts_count']}**"
        ]
        keyboard = build_back_to_admin_keyboard([
            [InlineKeyboardButton("💰 شارژ / کسر کیف پول", callback_data=f"admin:prompt_charge_user:{user_id}")],
            [InlineKeyboardButton(toggle_btn_text, callback_data=f"admin:toggle_block_user:{user_id}"),
             InlineKeyboardButton("✏️ تغییر Telegram ID", callback_data=f"admin:prompt_edit_user_id:{user_id}")],
            [InlineKeyboardButton("➕ اتصال اکانت", callback_data=f"admin:prompt_link_user_id:{user_id}"),
             InlineKeyboardButton("❌ قطع اتصال اکانت", callback_data=f"admin:prompt_unlink_user_acc:{user_id}")],
            [InlineKeyboardButton("🗑 حذف کاربر", callback_data=f"admin:confirm_del_user:{user_id}")]
        ])
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer()

    elif action == "toggle_block_user":
        user_id = int(sub_action)
        new_val = database.toggle_user_block(user_id)
        await query.answer(f"✅ وضعیت کاربر به {'مسدود' if new_val else 'فعال'} تغییر یافت.", show_alert=True)
        u_info = database.get_bot_user_by_tg_id(user_id)
        if not u_info:
            await query.edit_message_text("❌ کاربر یافت نشد.", reply_markup=build_back_to_admin_keyboard())
            return
        is_blocked = bool(u_info.get("is_blocked", 0))
        toggle_text = "🟢 رفع مسدودی" if is_blocked else "🚫 مسدود کردن"
        lines = [
            "👤 **شناسنامه کاربر نهایی:**\n",
            f"• **نام:** {u_info.get('full_name') or 'ثبت نشده'}",
            f"• **یوزرنیم:** @{u_info['username']}" if u_info.get("username") else "• **یوزرنیم:** ندارد",
            f"• **شناسه تلگرام:** `{u_info['telegram_id']}`",
            f"• **وضعیت دسترسی:** **{'🔴 مسدود' if is_blocked else '🟢 فعال'}**",
            f"• **موجودی کیف پول:** **{u_info['wallet_balance']:,} تومان**",
            f"• **تعداد اکانت‌های متصل:** **{u_info['linked_accounts_count']}**"
        ]
        keyboard = build_back_to_admin_keyboard([
            [InlineKeyboardButton("💰 شارژ / کسر کیف پول", callback_data=f"admin:prompt_charge_user:{user_id}")],
            [InlineKeyboardButton(toggle_text, callback_data=f"admin:toggle_block_user:{user_id}")],
            [InlineKeyboardButton("🏠 بازگشت به کاربران", callback_data="admin:users:1")]
        ])
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    # 5. Financial Management Center
    elif action == "finance":
        reps = database.get_all_reps()
        total_debt = sum([r["balance_toman"] or 0 for r in reps])
        pending_rcps = database.get_all_receipts(status="pending", limit=10)
        card_info = database.get_card_info()
        
        lines = [
            "💰 **مرکز جامع امور مالی و فیش‌های واریزی:**\n",
            f"• **شماره کارت فعال سیستم:** `{card_info['card_number']}` ({card_info['card_holder']})",
            f"• **فیش‌های در انتظار تایید:** **{len(pending_rcps)} فیش** ⏳",
            f"• مجموع بدهی وصول‌نشده نماینده‌ها: **{total_debt:,} تومان**\n"
        ]
        extra_btns = [
            [InlineKeyboardButton(f"⏳ فیش‌های منتظر تایید ({len(pending_rcps)})", callback_data="admin:pending_receipts")],
            [InlineKeyboardButton("💳 تنظیمات شماره کارت و حساب", callback_data="admin:prompt_card_settings")],
            [InlineKeyboardButton("📑 تاریخچه کل فیش‌ها و تراکنش‌ها", callback_data="admin:recent_tx:all")]
        ]
        keyboard = build_back_to_admin_keyboard(extra_btns)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("امور مالی")

    elif action == "pending_receipts":
        pending_rcps = database.get_all_receipts(status="pending", limit=10)
        lines = ["⏳ **فیش‌های واریزی در انتظار بررسی و تایید:**\n"]
        extra_btns = []
        if not pending_rcps:
            lines.append("✅ هیچ فیش در انتظار تاییدی در سیستم موجود نیست.")
        else:
            for r in pending_rcps:
                user_type = "نماینده" if r.get("rep_id") else "کاربر"
                lines.append(
                    f"• کد: `{r['tracking_code']}` ({user_type})\n"
                    f"  مبلغ: **{r['amount_toman']:,} تومان** | ثبت: `{r['created_at'][:16]}`"
                )
                extra_btns.append([
                    InlineKeyboardButton(f"✅ تایید {r['tracking_code']}", callback_data=f"admin_approve_rcp_{r['tracking_code']}"),
                    InlineKeyboardButton(f"❌ رد {r['tracking_code']}", callback_data=f"admin_reject_rcp_{r['tracking_code']}")
                ])
        keyboard = build_back_to_admin_keyboard(extra_btns)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer()

    # 6. Pricing Plans Management (Complete CRUD)
    elif action == "plans":
        plans = database.get_pricing_plans()
        lines = ["⚙️ **مدیریت کامل تعرفه‌ها و پلن‌های سیستم:**\n"]
        extra_btns = []
        for p in plans:
            quick_tag = "⚡️ [سریع]" if p["is_quick"] else "📦"
            lines.append(
                f"{quick_tag} **{p['name']}** (`{p['plan_code']}`):\n"
                f"   • خرید نماینده: **{p['rep_price']:,} ت** | فروش کاربر: **{p['user_price']:,} ت**"
            )
            extra_btns.append([
                InlineKeyboardButton(f"⚙️ مدیریت {p['name']}", callback_data=f"admin:plan_detail:{p['plan_code']}")
            ])
            
        extra_btns.append([InlineKeyboardButton("➕ تعریف پلن جدید", callback_data="admin:prompt_add_plan")])
        keyboard = build_back_to_admin_keyboard(extra_btns)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("تعرفه‌ها و پلن‌ها")

    elif action == "plan_detail":
        plan_code = sub_action
        plan = database.get_plan(plan_code)
        if not plan:
            await query.answer("❌ پلن یافت نشد.", show_alert=True)
            return

        quick_text = "⚡️ نمایش سریع (Quick Enabled)" if plan.get("is_quick") else "📦 نمایش عادی"
        lines = [
            f"⚙️ **مشخصات و مدیریت پلن: {plan['name']}**\n",
            f"• کد پلن: `{plan['plan_code']}`",
            f"• مشخصات: **{plan['gb']} گیگابایت / {plan['days']} روز**",
            f"• قیمت نماینده: **{plan['rep_price']:,} تومان**",
            f"• قیمت کاربر نهایی: **{plan['user_price']:,} تومان**",
            f"• حالت نمایش: **{quick_text}**"
        ]
        keyboard = build_back_to_admin_keyboard([
            [InlineKeyboardButton("✏️ ویرایش قیمت‌ها", callback_data=f"admin:prompt_edit_plan:{plan_code}"),
             InlineKeyboardButton("📐 ویرایش حجم/زمان", callback_data=f"admin:prompt_edit_plan_specs:{plan_code}")],
            [InlineKeyboardButton("⚡️ تغییر حالت سریع/عادی", callback_data=f"admin:toggle_plan_quick:{plan_code}")],
            [InlineKeyboardButton("🗑 حذف این تعرفه", callback_data=f"admin:confirm_del_plan:{plan_code}")]
        ])
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer()

    elif action == "toggle_plan_quick":
        plan_code = sub_action
        new_val = database.toggle_plan_quick(plan_code)
        await query.answer("✅ وضعیت نمایش سریع پلن تغییر کرد.", show_alert=True)
        plan = database.get_plan(plan_code)
        if plan:
            quick_text = "⚡️ نمایش سریع" if plan.get("is_quick") else "📦 نمایش عادی"
            lines = [
                f"⚙️ **مشخصات و مدیریت پلن: {plan['name']}**\n",
                f"• کد پلن: `{plan['plan_code']}`",
                f"• مشخصات: **{plan['gb']} گیگابایت / {plan['days']} روز**",
                f"• قیمت نماینده: **{plan['rep_price']:,} تومان**",
                f"• قیمت کاربر نهایی: **{plan['user_price']:,} تومان**",
                f"• حالت نمایش: **{quick_text}**"
            ]
            keyboard = build_back_to_admin_keyboard([
                [InlineKeyboardButton("✏️ ویرایش قیمت‌ها", callback_data=f"admin:prompt_edit_plan:{plan_code}")],
                [InlineKeyboardButton("⚡️ تغییر حالت سریع/عادی", callback_data=f"admin:toggle_plan_quick:{plan_code}")],
                [InlineKeyboardButton("🗑 حذف این تعرفه", callback_data=f"admin:confirm_del_plan:{plan_code}")]
            ])
            await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    elif action == "confirm_del_plan":
        plan_code = sub_action
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"admin:do_del_plan:{plan_code}"),
             InlineKeyboardButton("❌ انصراف", callback_data=f"admin:plan_detail:{plan_code}")]
        ])
        await query.edit_message_text(f"⚠️ **آیا از حذف تعرفه `{plan_code}` مطمئن هستید؟**", parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer()

    elif action == "do_del_plan":
        plan_code = sub_action
        try:
            deleted = database.delete_plan(plan_code)
        except ValueError as exc:
            await query.answer(str(exc), show_alert=True)
            return
        await query.answer("🗑 تعرفه حذف شد." if deleted else "تعرفه یافت نشد.", show_alert=True)
        # Refresh and return to plans list cleanly
        plans = database.get_pricing_plans()
        lines = ["⚙️ **تعرفه‌ها و پلن‌های سیستم:**\n"]
        extra_btns = []
        for p in plans:
            quick_tag = "⚡️ [سریع]" if p.get("is_quick") else "📦"
            active_tag = "🟢" if p.get("is_active", 1) else "🔴 [غیرفعال]"
            lines.append(
                f"{active_tag} {quick_tag} **{p['name']}** (`{p['plan_code']}`):\n"
                f"   • {p['gb']}GB / {p['days']} روز | خرید: **{p['rep_price']:,} ت** | فروش: **{p['user_price']:,} ت**"
            )
            extra_btns.append([
                InlineKeyboardButton(f"⚙️ مدیریت {p['name']}", callback_data=f"admin:plan_detail:{p['plan_code']}")
            ])
        extra_btns.append([InlineKeyboardButton("➕ تعریف پلن جدید", callback_data="admin:prompt_add_plan")])
        keyboard = build_back_to_admin_keyboard(extra_btns)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

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
        return

    # 7. Sync & Server Status
    elif action == "sync":
        result = await asyncio.to_thread(database.sync_all_active_reps_detailed)
        if result.get("status") == "success":
            sync_status = (
                f"✅ همگام‌سازی کامل شد.\n"
                f"• اکانت‌های به‌روزشده: **{result['count']}**\n"
                f"• موارد دارای بیش از یک نماینده محتمل: **{result['ambiguous']}**\n"
                f"• موارد بدون نماینده منطبق: **{result['unmatched']}**"
            )
        else:
            sync_status = f"❌ همگام‌سازی ناموفق بود: {result.get('message', 'خطای نامشخص')}"
        text = (
            "🔄 **گزارش همگام‌سازی سرور ۳X-UI:**\n\n"
            f"{sync_status}\n"
            f"• زمان بررسی: `{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}`"
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
        try:
            res_id = int(parts[2])
        except (IndexError, ValueError):
            await query.answer("شناسه رزرو نامعتبر است.", show_alert=True)
            return
        acc_code = parts[3] if len(parts) > 3 else ""
        res_item = database.get_reservation_by_id(res_id)
        if (
            not res_item or res_item.get("status") != "pending"
            or str(res_item.get("account_code", "")).lower() != acc_code.lower()
        ):
            await query.answer("⚠️ رزرو در انتظار یافت نشد یا اطلاعات حساب با آن تطبیق ندارد.", show_alert=True)
            return

        lock_key = f"reservation-activation:{res_id}"
        lock_token = database.acquire_operation_lock(lock_key, ttl_seconds=900)
        if not lock_token:
            await query.answer("⏳ این رزرو در حال فعال‌سازی است یا نتیجه قبلی نیازمند بررسی است.", show_alert=True)
            return
        res_item = database.get_reservation_by_id(res_id)
        if (
            not res_item or res_item.get("status") != "pending"
            or str(res_item.get("account_code", "")).lower() != acc_code.lower()
        ):
            database.release_operation_lock(lock_key, lock_token)
            await query.answer("⚠️ وضعیت رزرو تغییر کرده است؛ عملیات متوقف شد.", show_alert=True)
            return

        email = res_item["email"]
        gb = res_item["gb"]
        days = res_item["days"]
        on_first = bool(res_item.get("on_first_connect", 0))
        try:
            ren_res = await asyncio.to_thread(
                xui_manager.renew_client,
                email, gb, days, comment=None, rollover=False,
                countdown=True if on_first else "auto", scope="all", by_code_only=True
            )
        except Exception:
            logger.exception("Admin force-reserve outcome is unknown: %s", res_id)
            await query.answer("⚠️ پاسخ پنل نامشخص است؛ رزرو و قفل تطبیق حفظ شدند.", show_alert=True)
            return
        if isinstance(ren_res, dict) and (
            ren_res.get("status") in ("error", "failed", "failure")
            or ren_res.get("success") is False
        ):
            database.release_operation_lock(lock_key, lock_token)
            await query.answer("❌ پنل فعال‌سازی رزرو را رد کرد؛ رزرو در صف باقی ماند.", show_alert=True)
            return
        if not isinstance(ren_res, dict) or not (
            ren_res.get("status") in ("success", "ok") or ren_res.get("success") is True
        ):
            await query.answer("⚠️ پاسخ پنل مبهم است؛ رزرو و قفل تطبیق حفظ شدند.", show_alert=True)
            return

        activated = database.activate_reservation(res_id, actor_id=user.id, actor_name=user.full_name or "Admin")
        if not activated:
            await query.answer("⚠️ پنل عملیات را انجام داد اما ثبت فعال‌سازی ناموفق بود؛ قفل تطبیق حفظ شد.", show_alert=True)
            return
        database.release_operation_lock(lock_key, lock_token)
        await query.answer(f"🚀 بسته رزرو اکانت {acc_code} فعال شد.", show_alert=True)
        await query.edit_message_text(
            f"✅ فعال‌سازی رزرو `{acc_code}` ثبت شد. برای مشاهده صف به‌روزشده، از دکمه زیر استفاده کنید.",
            reply_markup=build_back_to_admin_keyboard([[InlineKeyboardButton("📦 بازخوانی صف رزرو", callback_data="admin:reserves")]])
        )
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
        await query.edit_message_text(
            "✅ عملیات لغو رزرو پردازش شد. برای مشاهده صف به‌روزشده از دکمه زیر استفاده کنید.",
            reply_markup=build_back_to_admin_keyboard([[InlineKeyboardButton("📦 بازخوانی صف رزرو", callback_data="admin:reserves")]])
        )
        return

    # 15. New CRUD Admin Sections (v4.0.0-beta.2)
    elif action == "rep_detail":
        rep_id = int(sub_action)
        rep = database.get_rep_by_id(rep_id)
        if not rep:
            await query.answer("❌ نماینده یافت نشد.", show_alert=True)
            return
        lines = [
            f"👤 **شناسنامه نماینده: {rep['name']}**\n",
            f"• کد: `{rep['rep_code']}` | آیدی: `{rep['telegram_id']}`",
            f"• وضعیت: {'🟢 فعال' if rep['is_active'] else '🔴 معلق'}",
            f"• سقف اعتبار: `{rep['credit_limit_toman']:,} تومان`",
            f"• بدهی جاری: `{rep['balance_toman']:,} تومان`"
        ]
        keyboard = build_back_to_admin_keyboard([
            [InlineKeyboardButton("🟢 تسویه / تعدیل تراز", callback_data=f"admin:prompt_pay:{rep['rep_code']}"),
             InlineKeyboardButton("💳 تغییر سقف اعتبار", callback_data=f"admin:prompt_edit_credit:{rep_id}")],
            [InlineKeyboardButton("✏️ ویرایش مشخصات", callback_data=f"admin:prompt_edit_rep:{rep_id}")],
            [InlineKeyboardButton("⏸ تعلیق / فعال‌سازی", callback_data=f"admin:toggle_rep:{rep_id}"),
             InlineKeyboardButton("📦 آرشیو نماینده", callback_data=f"admin:confirm_del_rep:{rep_id}")]
        ])
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer()

    elif action == "toggle_rep":
        rep_id = int(sub_action)
        new_val = database.toggle_rep_active(rep_id)
        await query.answer(f"✅ وضعیت نماینده به {'فعال' if new_val else 'معلق'} تغییر یافت.", show_alert=True)
        rep = database.get_rep_by_id(rep_id)
        if not rep:
            await query.edit_message_text("❌ نماینده یافت نشد.", reply_markup=build_back_to_admin_keyboard())
            return
        lines = [
            f"👤 **شناسنامه نماینده: {rep['name']}**\n",
            f"• کد: `{rep['rep_code']}` | آیدی: `{rep['telegram_id']}`",
            f"• وضعیت: {'🟢 فعال' if rep['is_active'] else '🔴 معلق'}",
            f"• سقف اعتبار: `{int(rep.get('credit_limit_toman') or 0):,} تومان`",
            f"• بدهی جاری: `{int(rep.get('balance_toman') or 0):,} تومان`"
        ]
        keyboard = build_back_to_admin_keyboard([
            [InlineKeyboardButton("🟢 تسویه / تعدیل تراز", callback_data=f"admin:prompt_pay:{rep['rep_code']}"),
             InlineKeyboardButton("💳 تغییر سقف اعتبار", callback_data=f"admin:prompt_edit_credit:{rep_id}")],
            [InlineKeyboardButton("✏️ ویرایش مشخصات", callback_data=f"admin:prompt_edit_rep:{rep_id}")],
            [InlineKeyboardButton("⏸ تعلیق / فعال‌سازی", callback_data=f"admin:toggle_rep:{rep_id}"),
             InlineKeyboardButton("🗑 حذف نماینده", callback_data=f"admin:confirm_del_rep:{rep_id}")]
        ])
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    elif action in {
        "prompt_card_settings", "prompt_charge_user", "prompt_edit_credit",
        "prompt_setting", "prompt_pay", "prompt_add_rep", "prompt_edit_plan",
        "prompt_transfer", "prompt_inspect", "prompt_add_plan", "prompt_link_user",
        "prompt_search_user", "prompt_edit_user_id", "prompt_link_user_id",
        "prompt_unlink_user_acc", "prompt_edit_rep", "prompt_edit_plan_specs"
    }:
        if chat.type != "private":
            await query.answer("⚠️ این عملیات منحصراً در چت خصوصی ربات در دسترس است.", show_alert=True)
            return
        now_ts = time.time()
        if action == "prompt_card_settings":
            context.user_data["admin_state"] = {"action": "edit_card_settings", "ts": now_ts}
            card_info = database.get_card_info()
            msg = (
                "💳 **تنظیمات شماره کارت بانکی:**\n\n"
                f"شماره کارت فعلی: `{card_info['card_number']}`\n"
                f"صاحب حساب: `{card_info['card_holder']}`\n"
                f"نام بانک: `{card_info['bank_name']}`\n\n"
                "لطفاً اطلاعات جدید را به این فرمت بفرستید:\n"
                "`<شماره_کارت> <نام_صاحب_حساب> <نام_بانک>`\n"
                "مثال:\n"
                "`6037991122334455 علی_حسینی سامان`\n\n"
                "⏱ مهلت ارسال: ۶۰ ثانیه (یا بنویسید `cancel`)"
            )
            keyboard = build_back_to_admin_keyboard()
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer()

        elif action == "prompt_charge_user":
            user_id = sub_action
            context.user_data["admin_state"] = {"action": "charge_user", "target_user_id": int(user_id), "ts": now_ts}
            msg = (
                f"💰 **شارژ / کسر دستی کیف پول کاربر (`{user_id}`):**\n\n"
                "مبلغ را به تومان وارد کنید (عدد مثبت برای شارژ، عدد منفی برای کسر):\n"
                "مثال:\n"
                "`100000` یا `-50000`\n\n"
                "⏱ مهلت ارسال: ۶۰ ثانیه (یا بنویسید `cancel`)"
            )
            keyboard = build_back_to_admin_keyboard()
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer()

        elif action == "prompt_edit_credit":
            rep_id = sub_action
            context.user_data["admin_state"] = {"action": "edit_credit_limit", "target_rep_id": int(rep_id), "ts": now_ts}
            msg = (
                "💳 **تغییر سقف اعتبار نماینده:**\n\n"
                "مبلغ سقف اعتبار جدید را به تومان وارد کنید (0 برای نامحدود):\n"
                "مثال:\n"
                "`10000000`\n\n"
                "⏱ مهلت ارسال: ۶۰ ثانیه (یا بنویسید `cancel`)"
            )
            keyboard = build_back_to_admin_keyboard()
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer()

        elif action == "prompt_setting":
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
        elif action == "prompt_search_user":
            context.user_data["admin_state"] = {"action": "search_user", "ts": now_ts}
            await query.edit_message_text(
                "🔍 **جستجوی کاربر:** شناسه تلگرام، نام یا یوزرنیم را ارسال کنید.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=build_back_to_admin_keyboard()
            )
            await query.answer()

        elif action == "prompt_edit_user_id":
            target_user_id = int(sub_action)
            context.user_data["admin_state"] = {
                "action": "edit_user_id", "target_user_id": target_user_id, "ts": now_ts
            }
            await query.edit_message_text(
                f"✏️ **تغییر شناسه تلگرام کاربر `{target_user_id}`:**\nشناسه عددی جدید را ارسال کنید.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=build_back_to_admin_keyboard()
            )
            await query.answer()

        elif action == "prompt_link_user_id":
            target_user_id = int(sub_action)
            context.user_data["admin_state"] = {
                "action": "link_user_account", "target_user_id": target_user_id, "ts": now_ts
            }
            await query.edit_message_text(
                f"🔗 **اتصال اکانت به کاربر `{target_user_id}`:**\nکد دقیق اکانت را ارسال کنید.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=build_back_to_admin_keyboard()
            )
            await query.answer()

        elif action == "prompt_unlink_user_acc":
            target_user_id = int(sub_action)
            context.user_data["admin_state"] = {
                "action": "unlink_user_account", "target_user_id": target_user_id, "ts": now_ts
            }
            await query.edit_message_text(
                f"🔌 **قطع اتصال اکانت از کاربر `{target_user_id}`:**\nکد دقیق اکانت را ارسال کنید.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=build_back_to_admin_keyboard()
            )
            await query.answer()

        elif action == "prompt_edit_rep":
            target_rep_id = int(sub_action)
            target_rep = database.get_rep_by_id(target_rep_id)
            if not target_rep:
                await query.answer("نماینده یافت نشد.", show_alert=True)
                return
            context.user_data["admin_state"] = {
                "action": "edit_rep", "target_rep_id": target_rep_id, "ts": now_ts
            }
            await query.edit_message_text(
                f"✏️ **ویرایش نماینده {target_rep['name']}**\n"
                "اطلاعات را به ترتیب زیر بفرستید:\n"
                "`<rep_code> <name> <telegram_id> <group_id> <topic_id> <scope_filter>`\n"
                "نام باید بدون فاصله یا با زیرخط باشد.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=build_back_to_admin_keyboard()
            )
            await query.answer()

        elif action == "prompt_edit_plan_specs":
            plan_code = sub_action
            plan = database.get_plan(plan_code)
            if not plan:
                await query.answer("پلن یافت نشد.", show_alert=True)
                return
            context.user_data["admin_state"] = {
                "action": "edit_plan_specs", "plan_code": plan_code, "ts": now_ts
            }
            await query.edit_message_text(
                f"📐 **ویرایش حجم و زمان پلن {plan['name']}**\n"
                "فرمت: `<GB> <days> <نام جدید پلن>`\n"
                "مثال: `25 30 بسته ویژه ماهانه`",
                parse_mode=ParseMode.MARKDOWN, reply_markup=build_back_to_admin_keyboard()
            )
            await query.answer()

    elif action == "confirm_del_user":
        target_user_id = int(sub_action)
        u_info = database.get_bot_user_by_tg_id(target_user_id)
        if not u_info:
            await query.answer("کاربر یافت نشد.", show_alert=True)
            return
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("🗑 بله، حذف دسترسی کاربر", callback_data=f"admin:do_del_user:{target_user_id}")],
            [InlineKeyboardButton("❌ انصراف", callback_data=f"admin:user_detail:{target_user_id}")]
        ])
        await query.edit_message_text(
            f"⚠️ **حذف پروفایل کاربر `{target_user_id}`؟**\n"
            "اکانت‌های متصل از مالکیت او جدا می‌شوند؛ کیف پول، فیش‌ها و سوابق مالی برای حسابرسی حفظ خواهند شد.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )
        await query.answer()

    elif action == "do_del_user":
        target_user_id = int(sub_action)
        removed = database.delete_bot_user(target_user_id)
        await query.answer("پروفایل کاربر حذف شد." if removed else "کاربر یافت نشد.", show_alert=True)
        await query.edit_message_text(
            "✅ پروفایل حذف شد؛ سوابق مالی و پرداختی حفظ شدند." if removed else "⚠️ تغییری انجام نشد.",
            reply_markup=build_back_to_admin_keyboard([[InlineKeyboardButton("👥 بازگشت به کاربران", callback_data="admin:users:1")]])
        )
        return

    elif action == "confirm_del_rep":
        target_rep_id = int(sub_action)
        target_rep = database.get_rep_by_id(target_rep_id)
        if not target_rep:
            await query.answer("نماینده یافت نشد.", show_alert=True)
            return
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("📦 بله، آرشیو نماینده", callback_data=f"admin:do_del_rep:{target_rep_id}")],
            [InlineKeyboardButton("❌ انصراف", callback_data=f"admin:rep_detail:{target_rep_id}")]
        ])
        await query.edit_message_text(
            f"⚠️ **آرشیو نماینده {target_rep['name']}؟**\n"
            "برای حفظ دفترکل مالی و لاگ‌های حسابرسی، سابقه حذف فیزیکی نمی‌شود؛ دسترسی نماینده غیرفعال خواهد شد.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
        )
        await query.answer()

    elif action == "do_del_rep":
        target_rep_id = int(sub_action)
        archived = database.delete_rep(target_rep_id)
        await query.answer("نماینده آرشیو شد." if archived else "نماینده یافت نشد.", show_alert=True)
        await query.edit_message_text(
            "✅ نماینده غیرفعال شد و سوابق او حفظ شدند." if archived else "⚠️ تغییری انجام نشد.",
            reply_markup=build_back_to_admin_keyboard([[InlineKeyboardButton("👥 بازگشت به نمایندگان", callback_data="admin:reps")]])
        )
        return

    elif action == "recent_tx":
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        entries, total, total_pages = database.get_financial_ledger_paginated(
            rep_id=None, page=page, page_size=10
        )
        lines = [f"📑 **تاریخچه تراکنش‌های سیستم ({total} مورد) — صفحه {page}/{total_pages}:**\n"]
        for tx in entries:
            sign = "🔺 بدهکار" if tx["transaction_type"] == "debit" else "🟢 بستانکار"
            if tx.get("rep_id") is not None:
                identity = f"نماینده: {tx.get('rep_name') or tx['rep_id']}"
            elif tx.get("user_id") is not None:
                identity_name = tx.get("user_full_name") or (
                    f"@{tx['user_username']}" if tx.get("user_username") else str(tx["user_id"])
                )
                identity = f"کاربر: {identity_name}"
            else:
                identity = f"عامل: {tx.get('actor_name') or tx.get('actor_id')}"
            lines.append(
                f"• {sign} | {identity}\n"
                f"  `{tx['created_at'][:16]}` | **{tx['amount']:,} تومان**\n"
                f"  {tx['description']} | مانده: `{tx['balance_after']:,}`"
            )
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"admin:recent_tx:{page-1}"))
        if page < total_pages:
            nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"admin:recent_tx:{page+1}"))
        extra = [nav] if nav else []
        keyboard = build_back_to_admin_keyboard(extra)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("تاریخچه تراکنش‌ها")

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

    # Active form input takes precedence over account/NLP routing. To exit,
    # the administrator explicitly cancels the form or lets it expire.
    action = admin_state.get("action")

    if action == "edit_card_settings":
        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            await update.message.reply_text("⚠️ فرمت نامعتبر است. فرمت صحیح: `<شماره کارت> <نام دارنده> <نام بانک>`", reply_markup=build_admin_main_keyboard())
            context.user_data.pop("admin_state", None)
            return True
        c_num, c_holder, c_bank = parts
        database.update_card_info(c_num, c_holder, c_bank)
        await update.message.reply_text(
            f"✅ **اطلاعات کارت بانکی با موفقیت ویرایش شد.**\n\n"
            f"• شماره کارت: `{c_num}`\n• صاحب حساب: **{c_holder}**\n• بانک: **{c_bank}**",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_admin_main_keyboard()
        )
        context.user_data.pop("admin_state", None)
        return True

    elif action == "charge_user":
        target_uid = admin_state.get("target_user_id")
        try:
            amount = int(text.replace(",", "").strip())
            new_bal = database.update_user_wallet_manual(
                target_uid, amount, "شارژ / کسر دستی توسط ادمین کل",
                actor_id=update.effective_user.id, actor_name=update.effective_user.full_name or "Admin"
            )
            sign_str = "افزایش" if amount >= 0 else "کاهش"
            await update.message.reply_text(
                f"✅ **کیف پول کاربر `{target_uid}` با موفقیت بروزرسانی شد.**\n\n"
                f"• میزان {sign_str}: **{abs(amount):,} تومان**\n"
                f"• مانده جدید کیف پول: **{new_bal:,} تومان**",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_admin_main_keyboard()
            )
        except ValueError:
            await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید.", reply_markup=build_admin_main_keyboard())
        context.user_data.pop("admin_state", None)
        return True

    elif action == "edit_credit_limit":
        target_rid = admin_state.get("target_rep_id")
        try:
            limit_val = int(text.replace(",", "").strip())
            database.update_rep_credit_limit(target_rid, limit_val)
            limit_text = f"{limit_val:,} تومان" if limit_val > 0 else "نامحدود"
            await update.message.reply_text(
                f"✅ **سقف اعتبار نماینده بروزرسانی شد.**\n\n• سقف اعتبار جدید: **{limit_text}**",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_admin_main_keyboard()
            )
        except ValueError:
            await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید.", reply_markup=build_admin_main_keyboard())
        context.user_data.pop("admin_state", None)
        return True

    elif action == "edit_setting":
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
        try:
            numeric = parts[-4:]
            p_gb = float(numeric[0].translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")))
            p_days = int(numeric[1].translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")))
            p_rep_p = int(numeric[2].replace(",", "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")))
            p_user_p = int(numeric[3].replace(",", "").translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")))
            p_name = " ".join(parts[1:-4]).strip()
            if not p_name:
                raise ValueError("Plan name is required")
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
        res = database.link_user_to_account(code, chat_id, allow_reassign=True)
        if res.get("status") == "ok":
            await update.message.reply_text(f"✅ اکانت `{code}` به Chat ID `{chat_id}` متصل گردید.", reply_markup=build_admin_main_keyboard())
        else:
            await update.message.reply_text(f"❌ خطا: {res.get('message')}")
        context.user_data.pop("admin_state", None)
        return True

    elif action == "search_user":
        matches = database.search_bot_users(text)
        if not matches:
            await update.message.reply_text("کاربری با این مشخصات پیدا نشد.", reply_markup=build_admin_main_keyboard())
        else:
            rows = []
            for item in matches:
                label = item.get("full_name") or item.get("username") or str(item["telegram_id"])
                rows.append([InlineKeyboardButton(
                    f"{label[:24]} ({item['telegram_id']})",
                    callback_data=f"admin:user_detail:{item['telegram_id']}"
                )])
            await update.message.reply_text(
                f"🔎 {len(matches)} نتیجه برای جستجوی شما:",
                reply_markup=build_back_to_admin_keyboard(rows)
            )
        context.user_data.pop("admin_state", None)
        return True

    elif action == "edit_user_id":
        old_id = int(admin_state["target_user_id"])
        try:
            new_id = int(text)
            if new_id <= 0:
                raise ValueError("Telegram ID must be positive")
            database.change_user_tg_id(old_id, new_id)
            await update.message.reply_text(
                f"✅ شناسه کاربر از `{old_id}` به `{new_id}` تغییر کرد؛ سوابق و مالکیت‌های مرتبط منتقل شدند.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=build_admin_main_keyboard()
            )
        except ValueError as exc:
            await update.message.reply_text(f"❌ تغییر شناسه انجام نشد: {exc}", reply_markup=build_admin_main_keyboard())
        context.user_data.pop("admin_state", None)
        return True

    elif action == "link_user_account":
        target_user_id = int(admin_state["target_user_id"])
        linked = database.link_user_to_account(
            text, target_user_id, allow_reassign=True
        )
        if linked.get("status") == "ok":
            database.log_audit(
                rep_id=None, account_code=text, email=None, action="admin_link_user_account",
                actor_id=update.effective_user.id, actor_name=update.effective_user.full_name or "Admin",
                details={"target_user_id": target_user_id}, result_status="success"
            )
            response = f"✅ اکانت `{text}` به کاربر `{target_user_id}` متصل شد."
        else:
            response = f"❌ اتصال انجام نشد: {linked.get('message')}"
        await update.message.reply_text(response, parse_mode=ParseMode.MARKDOWN, reply_markup=build_admin_main_keyboard())
        context.user_data.pop("admin_state", None)
        return True

    elif action == "unlink_user_account":
        target_user_id = int(admin_state["target_user_id"])
        unlinked = database.unlink_account_owner(text, target_user_id)
        await update.message.reply_text(
            "✅ اتصال اکانت قطع شد." if unlinked else "❌ اکانت متعلق به این کاربر نیست یا یافت نشد.",
            reply_markup=build_admin_main_keyboard()
        )
        context.user_data.pop("admin_state", None)
        return True

    elif action == "edit_rep":
        target_rep_id = int(admin_state["target_rep_id"])
        parts = text.split(maxsplit=5)
        if len(parts) != 6:
            await update.message.reply_text(
                "فرمت باید شش بخش داشته باشد: rep_code name telegram_id group_id topic_id scope_filter",
                reply_markup=build_admin_main_keyboard()
            )
            context.user_data.pop("admin_state", None)
            return True
        r_code, r_name, r_tg, r_group, r_topic, r_scope = parts
        try:
            database.update_rep_full(
                target_rep_id, r_name, r_code, int(r_tg), int(r_group), int(r_topic), r_scope
            )
            await update.message.reply_text("✅ اطلاعات نماینده به‌روزرسانی شد.", reply_markup=build_admin_main_keyboard())
        except ValueError:
            await update.message.reply_text("❌ شناسه‌های تلگرام/گروه/تاپیک باید عددی باشند.", reply_markup=build_admin_main_keyboard())
        context.user_data.pop("admin_state", None)
        return True

    elif action == "edit_plan_specs":
        parts = text.split(maxsplit=2)
        if len(parts) != 3:
            await update.message.reply_text(
                "فرمت نامعتبر است. نمونه: `25 30 بسته ویژه ماهانه`",
                parse_mode=ParseMode.MARKDOWN, reply_markup=build_admin_main_keyboard()
            )
            context.user_data.pop("admin_state", None)
            return True
        try:
            gb = float(parts[0].translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")))
            days = int(parts[1].translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")))
            database.update_plan_specs(admin_state["plan_code"], parts[2], gb, days)
            await update.message.reply_text("✅ حجم، زمان و نام پلن به‌روزرسانی شد.", reply_markup=build_admin_main_keyboard())
        except ValueError as exc:
            await update.message.reply_text(f"❌ ویرایش پلن انجام نشد: {exc}", reply_markup=build_admin_main_keyboard())
        context.user_data.pop("admin_state", None)
        return True

    return False
