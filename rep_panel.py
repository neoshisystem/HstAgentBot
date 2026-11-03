"""
rep_panel.py - Representative Interactive Dashboard for HstAgentBot
Provides a 100% inline keyboard-driven interface for Representatives:
1. [🆕 صدور اکانت جدید]
2. [📋 لیست اکانت‌های من (صفحه‌بندی)]
3. [🔍 جستجو و استعلام اکانت]
4. [💰 کیف پول و صورت‌حساب مالی]
5. [⏳ صف بسته‌های رزرو من]
6. [🏷 تعرفه‌های همکاری] & [📊 آمار کلی]
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

sys.path.append("/root/.hermes/scripts")
import xui_manager

logger = logging.getLogger("RepPanel")


def build_rep_main_keyboard() -> InlineKeyboardMarkup:
    """Builds the main interactive dashboard for a representative."""
    keyboard = [
        [
            InlineKeyboardButton("🆕 صدور اکانت جدید", callback_data="rep:new_acc_plans"),
            InlineKeyboardButton("📋 لیست اکانت‌های من", callback_data="rep:acc_list:all:1"),
        ],
        [
            InlineKeyboardButton("🔍 جستجو و استعلام اکانت", callback_data="rep:prompt_search"),
            InlineKeyboardButton("💰 کیف پول و صورت‌حساب", callback_data="rep:wallet"),
        ],
        [
            InlineKeyboardButton("⏳ صف بسته‌های رزرو من", callback_data="rep:reserves"),
            InlineKeyboardButton("📊 آمار کلی اکانت‌ها", callback_data="rep:stats"),
        ],
        [
            InlineKeyboardButton("🏷 تعرفه‌های همکاری", callback_data="rep:pricing"),
        ]
    ]
    return InlineKeyboardMarkup(keyboard)


def build_back_to_rep_keyboard(extra_buttons=None, back_callback="rep:main") -> InlineKeyboardMarkup:
    """Builds two-tier navigation back button returning to previous step or representative dashboard."""
    buttons = []
    if extra_buttons:
        for row in extra_buttons:
            buttons.append(row)
    if back_callback and back_callback != "rep:main":
        buttons.append([
            InlineKeyboardButton("↩️ بازگشت به مرحله قبل", callback_data=back_callback),
            InlineKeyboardButton("🏠 منوی اصلی نماینده", callback_data="rep:main")
        ])
    else:
        buttons.append([InlineKeyboardButton("🏠 بازگشت به پیشخوان نماینده", callback_data="rep:main")])
    return InlineKeyboardMarkup(buttons)


async def show_rep_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE, rep: dict, edit_message=False):
    """Renders or edits the representative interactive dashboard."""
    user = update.effective_user
    summary = database.get_rep_financial_summary(rep["id"])
    credit_limit = database.get_rep_credit_limit(rep["id"])
    limit_text = f"{credit_limit:,} تومان" if credit_limit > 0 else "نامحدود"
    status_text = "🟢 فعال" if rep.get("is_active", 1) else "🔴 معلق"

    text = (
        f"🤖 **پیشخوان مدیریت نمایندگی: {rep['name']}**\n\n"
        f"• وضعیت پنل: **{status_text}**\n"
        f"• مانده بدهی جاری: **{summary['balance_toman']:,} تومان**\n"
        f"• سقف اعتبار مجاز: **{limit_text}**\n"
        f"• تعداد اکانت‌های تحت پوشش: **{summary.get('tx_count', 0)} تراکنش**\n\n"
        "👇 لطفاً یکی از گزینه‌های زیر را انتخاب فرمایید:"
    )
    keyboard = build_rep_main_keyboard()

    if edit_message and update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        except BadRequest:
            pass
    elif update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)


async def handle_rep_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE, rep: dict):
    """Routes and handles all representative inline callbacks (data starting with rep:)."""
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    thread_id = update.effective_message.message_thread_id if update.effective_message else None

    data = query.data or ""
    parts = data.split(":")
    action = parts[1] if len(parts) > 1 else "main"

    # 1. Main Dashboard
    if action == "main":
        await show_rep_dashboard(update, context, rep, edit_message=True)
        await query.answer("پیشخوان نماینده")
        return

    # 2. New Account: Plan Selection
    elif action == "new_acc_plans":
        plans = database.get_pricing_plans(active_only=True)
        lines = [
            "🆕 **صدور اکانت جدید از استخر آزاد:**\n",
            "پلن مورد نظر خود را جهت تخصیص انتخاب فرمایید:\n"
        ]
        extra_btns = []
        row = []
        for p in plans:
            row.append(InlineKeyboardButton(
                f"{p['name']} ({p['rep_price']:,} ت)",
                callback_data=f"rep:new_plan_chosen:{p['plan_code']}"
            ))
            if len(row) == 2:
                extra_btns.append(row)
                row = []
        if row:
            extra_btns.append(row)

        keyboard = build_back_to_rep_keyboard(extra_btns)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("انتخاب پلن")

    # 2.1 Plan chosen: Ask customer name
    elif action == "new_plan_chosen":
        plan_code = parts[2] if len(parts) > 2 else "20gb_30d"
        plan = database.get_plan(plan_code)
        if not plan:
            await query.answer("❌ پلن یافت نشد.", show_alert=True)
            return

        # Check rep credit limit first
        allowed, curr_debt, credit_limit = database.check_rep_credit_limit(rep["id"], plan["rep_price"])
        if not allowed:
            await query.answer(f"⛔️ سقف اعتبار تکمیل است (بدهی: {curr_debt:,} ت | سقف: {credit_limit:,} ت)", show_alert=True)
            return

        context.user_data["rep_state"] = {
            "action": "awaiting_new_acc_name",
            "plan_code": plan_code,
            "rep_id": rep["id"],
            "ts": time.time()
        }

        extra_btns = [
            [InlineKeyboardButton("⏩ بدون نام / رد کردن", callback_data=f"rep:issue_acc_direct:{plan_code}:noname")]
        ]
        keyboard = build_back_to_rep_keyboard(extra_btns)
        msg = (
            f"👤 **ثبت نام مشتری برای پلن {plan['name']}:**\n\n"
            f"• هزینه پلن: **{plan['rep_price']:,} تومان**\n\n"
            f"لطفاً **نام مشتری** را ارسال فرمایید یا دکمه «بدون نام / رد کردن» را بزنید:"
        )
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer()

    # 2.2 Issue account directly
    elif action == "issue_acc_direct":
        plan_code = parts[2] if len(parts) > 2 else "20gb_30d"
        cust_name = "" if (len(parts) > 3 and parts[3] == "noname") else context.user_data.get("rep_temp_name", "")
        plan = database.get_plan(plan_code)
        if not plan:
            await query.answer("❌ پلن یافت نشد.", show_alert=True)
            return

        allowed, curr_debt, credit_limit = database.check_rep_credit_limit(rep["id"], plan["rep_price"])
        if not allowed:
            await query.answer(f"⛔️ سقف اعتبار تکمیل است.", show_alert=True)
            return

        gb = plan["gb"]
        days = plan["days"]
        scope_for_alloc = rep.get("scope_filter") or "all"

        alloc_res = await asyncio.to_thread(xui_manager.allocate_pool_client, gb, days, comment=cust_name, scope=scope_for_alloc)
        if alloc_res.get("status") == "success":
            email = alloc_res.get("email")
            sub_id = alloc_res.get("sub_id")
            code = xui_manager.extract_account_identifier(email) or email

            database.upsert_account_record(
                rep_id=rep["id"],
                email=email,
                account_code=code,
                customer_name=cust_name,
                sub_id=sub_id,
                enable=1,
                total_gb=gb,
                expiry_time=alloc_res.get("data", {}).get("expiry_time", 0)
            )

            new_bal = database.record_ledger_entry(
                rep_id=rep["id"],
                account_code=code,
                email=email,
                plan_code=plan_code,
                tx_type="debit",
                amount=plan["rep_price"],
                description=f"صدور اکانت جدید {int(gb)}G/{int(days)}D ({cust_name or 'بدون نام'})",
                actor_id=user.id,
                actor_name=user.full_name or "Rep"
            )

            deliv_text = alloc_res.get("deliverable", "")
            deliv_text += f"\n\n🧾 **فاکتور:** `{plan['rep_price']:,} تومان` | **مانده بدهی جدید:** `{new_bal:,} تومان`"
            keyboard = nlp_matcher.build_account_keyboard(code, is_enabled=True, on_first_connect=False, role="rep")
            await query.message.reply_text(deliv_text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer("✅ اکانت جدید با موفقیت صادر شد.")
        else:
            await query.answer(f"❌ خطا: {alloc_res.get('message', 'موجودی استخر تمام شده است')}", show_alert=True)

    # 3. Paginated Accounts List
    elif action == "acc_list":
        filter_type = parts[2] if len(parts) > 2 else "all"
        page = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 1

        accs, total, total_pages = database.get_rep_accounts_paginated(rep["id"], filter_type=filter_type, page=page, page_size=10)

        filter_row = [
            InlineKeyboardButton("✅ همه" if filter_type == "all" else "همه", callback_data=f"rep:acc_list:all:1"),
            InlineKeyboardButton("✅ 🟢 فعال" if filter_type == "active" else "🟢 فعال", callback_data=f"rep:acc_list:active:1"),
            InlineKeyboardButton("✅ 🔴 بحرانی" if filter_type == "critical" else "🔴 بحرانی", callback_data=f"rep:acc_list:critical:1"),
            InlineKeyboardButton("✅ 📦 رزرو" if filter_type == "reserved" else "📦 رزرو", callback_data=f"rep:acc_list:reserved:1"),
        ]

        lines = [
            f"📋 **لیست اکانت‌های تحت پوشش ({total} اکانت - صفحه {page} از {total_pages}):**\n",
            "جهت مشاهده وضعیت و مدیریت هر اکانت، روی دکمه آن کلیک کنید:\n"
        ]

        acc_buttons = [filter_row]
        row = []
        for a in accs:
            c_name = f" ({a['customer_name']})" if a.get("customer_name") else ""
            status_dot = "🟢" if a.get("enable") else "🛑"
            row.append(InlineKeyboardButton(f"{status_dot} {a['account_code']}{c_name}", callback_data=f"rep:view_acc:{a['account_code']}"))
            if len(row) == 2:
                acc_buttons.append(row)
                row = []
        if row:
            acc_buttons.append(row)

        nav_row = []
        if page > 1:
            nav_row.append(InlineKeyboardButton("⬅️ صفحه قبل", callback_data=f"rep:acc_list:{filter_type}:{page - 1}"))
        if page < total_pages:
            nav_row.append(InlineKeyboardButton("صفحه بعد ➡️", callback_data=f"rep:acc_list:{filter_type}:{page + 1}"))
        if nav_row:
            acc_buttons.append(nav_row)

        keyboard = build_back_to_rep_keyboard(acc_buttons)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("لیست اکانت‌ها")

    # 3.1 View Specific Account Card
    elif action == "view_acc":
        acc_code = parts[2] if len(parts) > 2 else ""
        insp = await asyncio.to_thread(xui_manager.inspect_client, acc_code, scope=rep.get("scope_filter", "all"), by_code_only=True)
        if insp.get("status") == "ok":
            client_info = insp["data"]
            report_text = xui_manager.format_status_report(client_info, is_admin=False)
            pending_res = database.get_pending_reservation(acc_code)
            if pending_res:
                report_text += f"\n\n📦 **بسته رزرو:** **{pending_res['plan_name']}** ({pending_res['gb']}G / {pending_res['days']}D)"
            keyboard = nlp_matcher.build_account_keyboard(acc_code, is_enabled=bool(client_info["raw"].get("enable", True)), role="rep", pending_res=pending_res)
            await query.message.reply_text(report_text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer()
        else:
            await query.answer("⚠️ اطلاعات اکانت در پنل یافت نشد.", show_alert=True)

    # 4. Search Account Prompt
    elif action == "prompt_search":
        context.user_data["rep_state"] = {
            "action": "awaiting_search_query",
            "rep_id": rep["id"],
            "ts": time.time()
        }
        keyboard = build_back_to_rep_keyboard()
        msg = (
            "🔍 **جستجو و استعلام اکانت:**\n\n"
            "لطفاً کد ۴ رقمی اکانت، نام مشتری یا ایمیل آن را ارسال فرمایید:"
        )
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer()

    # 5. Wallet & Financial View
    elif action == "wallet":
        summary = database.get_rep_financial_summary(rep["id"])
        credit_limit = database.get_rep_credit_limit(rep["id"])
        limit_text = f"{credit_limit:,} تومان" if credit_limit > 0 else "نامحدود"
        recent_tx = database.get_recent_ledger_entries(rep_id=rep["id"], limit=5)

        lines = [
            f"💰 **کیف پول و تراز مالی: {rep['name']}**\n",
            f"• **مانده بدهی جاری:** **{summary['balance_toman']:,} تومان**\n",
            f"• **سقف اعتبار مجاز:** **{limit_text}**\n",
            f"• مجموع فاکتورهای صادره: `{summary['total_debits']:,} تومان`\n",
            f"• مجموع تسویه‌حساب‌های تاییدشده: `{summary['total_credits']:,} تومان`\n",
            "\n🕒 **۵ تراکنش مالی اخیر شما:**"
        ]
        if not recent_tx:
            lines.append("• تراکنشی ثبت نشده است.")
        else:
            for tx in recent_tx:
                sign = "🔺 بدهکار" if tx["transaction_type"] == "debit" else "🟢 بستانکار"
                lines.append(f"• {sign} `{tx['created_at'][:16]}`: **{tx['amount']:,} ت** ({tx['description']})")

        extra_btns = [
            [
                InlineKeyboardButton("📤 ثبت فیش واریزی / تسویه", callback_data="rep_deposit_start"),
                InlineKeyboardButton("📑 تاریخچه فیش‌های من", callback_data="rep:receipts_history")
            ]
        ]
        keyboard = build_back_to_rep_keyboard(extra_btns)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("کیف پول و مالی")

    # 5.1 Receipts History
    elif action == "receipts_history":
        rcps = database.get_rep_receipts(rep["id"], limit=10)
        lines = ["📑 **تاریخچه فیش‌های واریزی ارسالی شما:**\n"]
        if not rcps:
            lines.append("• تاکنون فیش واریزی ثبت نکرده‌اید.")
        else:
            for r in rcps:
                status_icon = "⏳ در انتظار" if r["status"] == "pending" else ("✅ تایید شده" if r["status"] == "approved" else "❌ رد شده")
                lines.append(
                    f"• `{r['tracking_code']}` | **{r['amount_toman']:,} ت** | {status_icon}\n"
                    f"  ثبت: `{r['created_at'][:16]}`"
                )
                if r.get("rejection_reason") and r["status"] == "rejected":
                    lines.append(f"  علت رد: _{r['rejection_reason']}_")

        keyboard = build_back_to_rep_keyboard([[InlineKeyboardButton("📤 ثبت فیش واریزی جدید", callback_data="rep_deposit_start")]], back_callback="rep:wallet")
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("تاریخچه فیش‌ها")

    # 6. Reserve Packages Queue
    elif action == "reserves":
        res_list = database.get_rep_reservations(rep["id"])
        lines = [f"⏳ **صف بسته‌های رزرو من ({len(res_list)} بسته در انتظار):**\n"]
        extra_btns = []
        if not res_list:
            lines.append("• در حال حاضر هیچ بسته رزرو فعالی در صف انتظار شما نیست.")
        else:
            for r in res_list:
                lines.append(
                    f"• اکانت: `{r['account_code']}` | پلن: **{r['plan_name']}** ({r['gb']}G / {r['days']}D)\n"
                    f"  هزینه: `{r['charge_amount']:,} ت` | ثبت: `{r['created_at'][:16]}`"
                )
                extra_btns.append([
                    InlineKeyboardButton(f"🚀 فعال‌سازی {r['account_code']}", callback_data=f"rep:force_res:{r['id']}:{r['account_code']}"),
                    InlineKeyboardButton(f"🗑 لغو و عودت {r['account_code']}", callback_data=f"rep:cancel_res:{r['id']}:{r['account_code']}")
                ])

        keyboard = build_back_to_rep_keyboard(extra_btns)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("صف بسته‌های رزرو")

    # 6.1 Force Activate or Cancel Reserve
    elif action in ("force_res", "cancel_res"):
        res_id = int(parts[2])
        acc_code = parts[3] if len(parts) > 3 else ""
        if action == "force_res":
            res_item = database.get_reservation_by_id(res_id)
            if not res_item or res_item["status"] != "pending":
                await query.answer("⚠️ این بسته قبلاً فعال یا لغو شده است.", show_alert=True)
                return
            gb = res_item["gb"]
            days = res_item["days"]
            on_first = bool(res_item.get("on_first_connect", 0))
            await asyncio.to_thread(
                xui_manager.renew_client,
                res_item["email"], gb, days, comment=None, rollover=False,
                countdown=True if on_first else "auto", scope="all", by_code_only=True
            )
            database.activate_reservation(res_id, actor_id=user.id, actor_name=user.full_name or "Rep")
            await query.answer(f"🚀 بسته رزرو اکانت {acc_code} با موفقیت فعال شد.", show_alert=True)
        else:
            cancel_res = database.cancel_reservation(res_id, actor_id=user.id, actor_name=user.full_name or "Rep")
            if cancel_res:
                res_data, new_bal = cancel_res
                await query.answer(f"🗑 بسته رزرو لغو و {res_data['charge_amount']:,} ت عودت داده شد.", show_alert=True)
            else:
                await query.answer("⚠️ این بسته قبلاً لغو یا فعال شده است.", show_alert=True)

        # Refresh view
        await handle_rep_callbacks(update, context, rep)
        return

    # 7. Wholesale Pricing Plans
    elif action == "pricing":
        plans = database.get_pricing_plans(active_only=True)
        lines = ["🏷 **تعرفه‌ها و نرخ‌های همکاری نمایندگی:**\n"]
        for p in plans:
            quick_tag = "⚡️ [سریع]" if p["is_quick"] else "📦"
            lines.append(
                f"{quick_tag} **{p['name']}**:\n"
                f"   • قیمت خرید شما: **{p['rep_price']:,} تومان** | فروش مصوب کاربر: `{p['user_price']:,} تومان`"
            )
        keyboard = build_back_to_rep_keyboard()
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("تعرفه‌ها")

    # 8. Representative Statistics
    elif action == "stats":
        summary = database.get_rep_financial_summary(rep["id"])
        _, total_all, _ = database.get_rep_accounts_paginated(rep["id"], "all", 1, 1)
        _, total_act, _ = database.get_rep_accounts_paginated(rep["id"], "active", 1, 1)
        _, total_crit, _ = database.get_rep_accounts_paginated(rep["id"], "critical", 1, 1)
        res_list = database.get_rep_reservations(rep["id"])

        text = (
            f"📊 **آمار کلی اکانت‌های نمایندگی: {rep['name']}**\n\n"
            f"• کل اکانت‌های ثبت‌شده: **{total_all}**\n"
            f"• اکانت‌های فعال: **{total_act}** 🟢\n"
            f"• اکانت‌های بحرانی / منقضی: **{total_crit}** 🔴\n"
            f"• بسته‌های رزرو در انتظار: **{len(res_list)}** ⏳\n\n"
            f"• مانده بدهی تراز: **{summary['balance_toman']:,} تومان**"
        )
        keyboard = build_back_to_rep_keyboard()
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("آمار کلی")


async def process_rep_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE, rep: dict) -> bool:
    """Processes FSM inputs initiated from representative dashboard."""
    rep_state = context.user_data.get("rep_state")
    if not rep_state:
        return False

    # Check timeout (90s)
    if time.time() - rep_state.get("ts", 0) > 90:
        context.user_data.pop("rep_state", None)
        return False

    msg_text = update.message.text.strip()
    action = rep_state.get("action")

    if msg_text.lower() in ("cancel", "انصراف", "لغو"):
        context.user_data.pop("rep_state", None)
        await update.message.reply_text("❌ عملیات لغو شد.", reply_markup=build_rep_main_keyboard())
        return True

    if action == "awaiting_new_acc_name":
        plan_code = rep_state["plan_code"]
        context.user_data["rep_temp_name"] = msg_text
        context.user_data.pop("rep_state", None)

        # Trigger issue directly with provided name
        plan = database.get_plan(plan_code)
        gb = plan["gb"]
        days = plan["days"]
        scope_for_alloc = rep.get("scope_filter") or "all"

        alloc_res = await asyncio.to_thread(xui_manager.allocate_pool_client, gb, days, comment=msg_text, scope=scope_for_alloc)
        if alloc_res.get("status") == "success":
            email = alloc_res.get("email")
            sub_id = alloc_res.get("sub_id")
            code = xui_manager.extract_account_identifier(email) or email

            database.upsert_account_record(
                rep_id=rep["id"],
                email=email,
                account_code=code,
                customer_name=msg_text,
                sub_id=sub_id,
                enable=1,
                total_gb=gb,
                expiry_time=alloc_res.get("data", {}).get("expiry_time", 0)
            )

            new_bal = database.record_ledger_entry(
                rep_id=rep["id"],
                account_code=code,
                email=email,
                plan_code=plan_code,
                tx_type="debit",
                amount=plan["rep_price"],
                description=f"صدور اکانت جدید {int(gb)}G/{int(days)}D ({msg_text})",
                actor_id=update.effective_user.id,
                actor_name=update.effective_user.full_name or "Rep"
            )

            deliv_text = alloc_res.get("deliverable", "")
            deliv_text += f"\n\n🧾 **فاکتور:** `{plan['rep_price']:,} تومان` | **مانده بدهی جدید:** `{new_bal:,} تومان`"
            keyboard = nlp_matcher.build_account_keyboard(code, is_enabled=True, on_first_connect=False, role="rep")
            await update.message.reply_text(deliv_text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        else:
            await update.message.reply_text(f"❌ خطا: {alloc_res.get('message', 'موجودی استخر تمام شده است')}")
        return True

    elif action == "awaiting_search_query":
        context.user_data.pop("rep_state", None)
        insp = await asyncio.to_thread(xui_manager.inspect_client, msg_text, scope=rep.get("scope_filter", "all"), by_code_only=False)
        if insp.get("status") == "ok":
            client_info = insp["data"]
            acc_code = client_info["email"]
            report_text = xui_manager.format_status_report(client_info, is_admin=False)
            pending_res = database.get_pending_reservation(acc_code)
            keyboard = nlp_matcher.build_account_keyboard(acc_code, is_enabled=bool(client_info["raw"].get("enable", True)), role="rep", pending_res=pending_res)
            await update.message.reply_text(report_text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        else:
            await update.message.reply_text(f"⚠️ اکانت `{msg_text}` در محدوده نمایندگی شما یافت نشد.", reply_markup=build_rep_main_keyboard())
        return True

    return False
