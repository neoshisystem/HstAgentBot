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
from xui_result import XUIResult, classify_xui_result

from server_integrations import load_integration
xui_manager = load_integration("xui_manager")

logger = logging.getLogger("RepPanel")


def _v52_ui_enabled() -> bool:
    try:
        value = database.get_setting("v52_ui_enabled", "0")
    except Exception:
        value = "0"
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


async def _dispatch_v52_from_rep(update, context, rep):
    from rep_bot import _dispatch_v52_callback
    return await _dispatch_v52_callback(update, context, role="rep", rep=rep)



def _build_account_keyboard(account_code: str, user_id: int, **kwargs):
    """Build representative account cards from persisted account renewal policies."""
    modes = database.get_account_renewal_modes(account_code, user_id)
    return nlp_matcher.build_account_keyboard(
        account_code,
        on_first_connect=modes["first_connect_mode"],
        rollover_enabled=modes["rollover_enabled"],
        **kwargs,
    )


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
    if _v52_ui_enabled():
        keyboard.extend([[InlineKeyboardButton("👥 زیرنمایندگان", callback_data="v52:subreps:1"),
                          InlineKeyboardButton("👤 پروفایل‌ها", callback_data="v52:profiles")],
                         [InlineKeyboardButton("🛍 فروشگاه", callback_data="v52:shop")]])
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
    if data.startswith("v52:"):
        if data == "v52:subreps:1":
            data = "v52:subreps:" + str(int(rep.get("id") or 0))
            query.data = data
        await _dispatch_v52_from_rep(update, context, rep)
        return
    parts = data.split(":")
    action = parts[1] if len(parts)>1 else "main"
    if action=="cancel_form":
        state=context.user_data.pop("rep_state",None) or {}
        await _cleanup_form_prompt(
            update, context, state,
            preserve_message_id=getattr(getattr(query, "message", None), "message_id", None),
        )
        parent=state.get("return_callback") or "rep:main"
        if not isinstance(parent,str) or not parent.startswith("rep:") or parent in ("rep:cancel_form","rep:retry_form"): parent="rep:main"
        query.data=parent
        await handle_rep_callbacks(update,context,rep)
        return
    if action=="retry_form":
        await query.answer("فرم فعال است؛ مقدار را دوباره ارسال کنید.")
        return
    context.user_data.pop("rep_state",None)

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
        for p in plans:
            extra_btns.append([InlineKeyboardButton(
                f"{p['name']} ({p['rep_price']:,} ت)",
                callback_data=f"rep:new_plan_chosen:{p['plan_code']}"
            )])

        keyboard = build_back_to_rep_keyboard(extra_btns)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("انتخاب پلن")

    # 2.1 Plan chosen: Ask customer name
    elif action == "new_plan_chosen":
        plan_code = parts[2] if len(parts) > 2 else "20gb_30d"
        plan = database.get_plan(plan_code)
        if not plan or not plan.get("is_active", 1):
            await query.answer("❌ پلن یافت نشد یا غیرفعال است.", show_alert=True)
            return

        try:
            intent_id = database.create_rep_action_intent(
                rep_id=rep["id"], actor_tg_id=user.id,
                plan_code=plan["plan_code"], plan_name=plan["name"],
                gb=plan["gb"], days=plan["days"], amount=plan["rep_price"],
            )
        except ValueError as exc:
            await query.answer(f"⛔️ ایجاد درخواست صدور ممکن نشد: {exc}", show_alert=True)
            return

        context.user_data["rep_state"] = {
            "action": "awaiting_new_acc_name",
            "intent_id": intent_id,
            "rep_id": rep["id"],
            "ts": time.time(),
            "return_callback": "rep:new_acc_plans",
        }
        extra_btns = [[InlineKeyboardButton(
            "⏩ بدون نام / رد کردن",
            callback_data=f"rep:issue_acc_direct:{intent_id}:noname"
        )]]
        keyboard = build_back_to_rep_keyboard(extra_btns)
        msg = (
            f"👤 **ثبت نام مشتری برای پلن {plan['name']}:**\n\n"
            f"• هزینه پلن: **{plan['rep_price']:,} تومان**\n\n"
            "لطفاً **نام مشتری** را ارسال فرمایید یا دکمه «بدون نام / رد کردن» را بزنید:\n"
            "این درخواست تا ۱۵ دقیقه معتبر است و فقط یک بار قابل اجراست."
        )
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        _remember_form_prompt(context.user_data["rep_state"], query)
        await query.answer()

    # 2.2 Issue account using one durable representative action intent.
    elif action == "issue_acc_direct":
        intent_id = parts[2] if len(parts) > 2 else ""
        intent = database.get_rep_action_intent(intent_id, rep["id"], user.id)
        if (
            not intent or intent["action_type"] != "issue"
            or intent["status"] != "pending"
            or float(intent["expires_at"]) <= time.time()
        ):
            await query.answer(
                "⛔️ درخواست صدور منقضی شده، قبلاً استفاده شده یا متعلق به شما نیست؛ از منوی صدور دوباره شروع کنید.",
                show_alert=True
            )
            return

        try:
            started = database.begin_rep_action_intent(intent_id, rep["id"], user.id)
        except ValueError as exc:
            await query.answer(f"⛔️ ثبت مالی صدور انجام نشد: {exc}", show_alert=True)
            return
        except Exception:
            logger.exception("Could not claim representative issuance intent %s", intent_id)
            await query.answer("❌ شروع صدور انجام نشد؛ درخواست جدیدی از منوی صدور ایجاد کنید.", show_alert=True)
            return

        if started["status"] != "started":
            messages = {
                "not_found": "⛔️ درخواست صدور معتبر نیست یا متعلق به شما نیست.",
                "already_processed": "⛔️ این درخواست قبلاً اجرا یا برای تطبیق ثبت شده است؛ بدهی مجدد ایجاد نمی‌شود.",
                "expired": "⌛️ درخواست صدور منقضی شده است؛ منوی صدور را دوباره باز کنید.",
                "plan_changed": "⚠️ تعرفه از زمان نمایش منو تغییر کرده است؛ منوی صدور را تازه کنید.",
                "busy": "⏳ این درخواست در حال پردازش است؛ از اجرای مجدد خودداری کنید.",
            }
            await query.answer(messages.get(started["status"], "❌ درخواست صدور قابل اجرا نیست."), show_alert=True)
            return

        intent = started["intent"]
        amount = int(intent["amount"])
        gb = float(intent["gb"])
        days = int(intent["days"])
        cust_name = str(intent.get("customer_name") or "")
        lock_key = started["lock_key"]
        lock_token = started["lock_token"]
        new_bal = started["new_balance"]
        scope_for_alloc = rep.get("scope_filter") or "all"

        try:
            alloc_res = await asyncio.to_thread(
                xui_manager.allocate_pool_client, gb, days,
                comment=cust_name, scope=scope_for_alloc
            )
        except Exception:
            logger.exception("Representative allocation outcome is unknown for rep %s", rep["id"])
            try:
                database.mark_rep_action_intent_for_reconciliation(
                    intent_id, rep["id"], user.id, lock_key, lock_token,
                    "X-UI allocation raised an exception; external result must be reconciled",
                )
            except Exception:
                logger.exception("Could not mark representative issuance intent for reconciliation (%s)", intent_id)
            await query.answer(
                "⚠️ پاسخ پنل نامشخص است؛ پیش‌پرداخت محفوظ و درخواست برای تطبیق مدیریتی قفل شده است.",
                show_alert=True
            )
            return

        if classify_xui_result(alloc_res) == XUIResult.SUCCESS:
            email = alloc_res.get("email")
            if not email:
                try:
                    database.mark_rep_action_intent_for_reconciliation(
                        intent_id, rep["id"], user.id, lock_key, lock_token,
                        "X-UI reported success without an account email",
                    )
                except Exception:
                    logger.exception("Could not mark incomplete representative allocation result (%s)", intent_id)
                await query.answer("⚠️ پنل موفقیت را گزارش کرد اما شناسه اکانت ناقص است؛ پیش‌پرداخت محفوظ و قفل شد.", show_alert=True)
                return

            sub_id = alloc_res.get("sub_id")
            try:
                code = xui_manager.extract_account_identifier(email) or email
                database.upsert_account_record(
                    rep_id=rep["id"], email=email, account_code=code,
                    customer_name=cust_name, sub_id=sub_id, enable=1,
                    total_gb=gb, expiry_time=alloc_res.get("data", {}).get("expiry_time", 0)
                )
            except Exception:
                logger.exception("X-UI allocated %s but local persistence failed", email)
                try:
                    database.mark_rep_action_intent_for_reconciliation(
                        intent_id, rep["id"], user.id, lock_key, lock_token,
                        f"X-UI created account {email}, but local persistence failed",
                    )
                except Exception:
                    logger.exception("Could not mark local account persistence failure (%s)", intent_id)
                await query.answer(
                    "⚠️ اکانت در پنل ایجاد شد اما ثبت محلی کامل نشد؛ پیش‌پرداخت محفوظ و قفل تطبیق حفظ شد.",
                    show_alert=True
                )
                return

            try:
                database.complete_rep_action_intent(
                    intent_id, rep["id"], user.id, lock_key, lock_token, code, email
                )
            except Exception:
                logger.exception("Could not finalize successful representative intent %s", intent_id)
                try:
                    database.mark_rep_action_intent_for_reconciliation(
                        intent_id, rep["id"], user.id, lock_key, lock_token,
                        f"Account {email} exists in panel and local database, but intent finalization failed",
                    )
                except Exception:
                    logger.exception("Could not mark representative intent finalization failure (%s)", intent_id)
                await query.answer(
                    "⚠️ اکانت ثبت شد اما نهایی‌سازی مالی کامل نشد؛ قفل برای تطبیق مدیریتی حفظ شده است.",
                    show_alert=True
                )
                return

            deliv_text = alloc_res.get("deliverable", "")
            deliv_text += (
                f"\n\n🧾 **فاکتور:** \`{amount:,} تومان\`"
                f" | **مانده بدهی جدید:** \`{new_bal:,} تومان\`"
            )
            keyboard = _build_account_keyboard(
                code, user.id, is_enabled=True, role="rep"
            )
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except BadRequest:
                pass
            await query.message.reply_text(
                deliv_text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard
            )
            await query.answer("✅ اکانت جدید با موفقیت صادر شد.")
        elif classify_xui_result(alloc_res) == XUIResult.FAILURE:
            try:
                new_balance = database.refund_rep_action_intent(
                    intent_id, rep["id"], user.id, lock_key, lock_token,
                    actor_name=user.full_name or "Representative",
                )
            except Exception:
                logger.exception("Representative issuance refund failed; lock retained (%s)", intent_id)
                await query.answer(
                    "⚠️ صدور ناموفق بود اما استرداد ثبت نشد؛ قفل برای تطبیق مدیریتی محفوظ مانده است.",
                    show_alert=True
                )
                return
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except BadRequest:
                pass
            await query.answer(
                f"❌ پنل صدور را رد کرد؛ پیش‌پرداخت {amount:,} تومان مسترد شد. مانده بدهی: {new_balance:,} تومان.",
                show_alert=True
            )
        else:
            try:
                database.mark_rep_action_intent_for_reconciliation(
                    intent_id, rep["id"], user.id, lock_key, lock_token,
                    f"Ambiguous X-UI allocation response: {alloc_res!r}"[:500],
                )
            except Exception:
                logger.exception("Could not mark ambiguous representative allocation (%s)", intent_id)
            await query.answer(
                "⚠️ پاسخ پنل مبهم است؛ پیش‌پرداخت محفوظ و قفل برای تطبیق مدیریتی حفظ شد.",
                show_alert=True
            )

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
        requested_code = parts[2] if len(parts) > 2 else ""
        owned_account = database.is_account_in_rep_scope(rep["id"], requested_code)
        if not owned_account:
            await query.answer("⛔️ این اکانت در محدوده ثبت‌شده نمایندگی شما نیست.", show_alert=True)
            return
        acc_code = owned_account["account_code"]
        insp = await asyncio.to_thread(
            xui_manager.inspect_client, owned_account["email"],
            scope=rep.get("scope_filter", "all"), by_code_only=True
        )
        if insp.get("status") == "ok":
            client_info = insp["data"]
            report_text = xui_manager.format_status_report(client_info, is_admin=False)
            pending_res = database.get_pending_reservation(acc_code)
            if pending_res:
                report_text += f"\n\n📦 **بسته رزرو:** **{pending_res['plan_name']}** ({pending_res['gb']}G / {pending_res['days']}D)"
            keyboard = _build_account_keyboard(acc_code, user.id, is_enabled=bool(client_info["raw"].get("enable", True)), role="rep", pending_res=pending_res)
            await query.message.reply_text(report_text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer()
        else:
            await query.answer("⚠️ اطلاعات اکانت در پنل یافت نشد.", show_alert=True)

    # 4. Search Account Prompt
    elif action == "prompt_search":
        context.user_data["rep_state"] = {
            "action": "awaiting_search_query",
            "rep_id": rep["id"],
            "ts": time.time(),
            "return_callback": "rep:main",
        }
        keyboard = build_back_to_rep_keyboard()
        msg = (
            "🔍 **جستجو و استعلام اکانت:**\n\n"
            "لطفاً کد ۴ رقمی اکانت، نام مشتری یا ایمیل آن را ارسال فرمایید:"
        )
        await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        _remember_form_prompt(context.user_data["rep_state"], query)
        await query.answer()

    # 5. Wallet & Financial View
    elif action in ("wallet", "finance"):
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
                    InlineKeyboardButton(f"🚀 فعال‌سازی {r['account_code']}", callback_data=f"rep:confirm_force_res:{r['id']}:{r['account_code']}"),
                    InlineKeyboardButton(f"🗑 لغو و عودت {r['account_code']}", callback_data=f"rep:confirm_cancel_res:{r['id']}:{r['account_code']}")
                ])

        keyboard = build_back_to_rep_keyboard(extra_btns)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("صف بسته‌های رزرو")

    # 6.1 Legacy single-click reservation callbacks are read-only.
    elif action in ("force_res", "cancel_res"):
        await query.answer(
            "⚠️ این دکمه قدیمی است و اجرا نشد. صف رزرو یا کارت اکانت را تازه کنید و مرحله تأیید را انجام دهید.",
            show_alert=True
        )
        return

    # 6.2 First click: display second-stage confirmation without side effects.
    elif action in ("confirm_force_res", "confirm_cancel_res"):
        try:
            res_id = int(parts[2])
        except (IndexError, ValueError):
            await query.answer("شناسه رزرو نامعتبر است.", show_alert=True)
            return
        acc_code = parts[3] if len(parts) > 3 else ""
        res_item = database.get_reservation_by_id(res_id)
        if (
            not res_item or res_item.get("status") != "pending"
            or res_item.get("rep_id") != rep.get("id")
            or str(res_item.get("account_code", "")).lower() != acc_code.lower()
        ):
            await query.answer("⛔️ رزرو در انتظار و متعلق به نمایندگی شما یافت نشد.", show_alert=True)
            return

        if action == "confirm_force_res":
            confirm_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🚀 تأیید فعال‌سازی فوری (بسته فعلی مصرف می‌شود)",
                    callback_data=f"rep:do_force_res:{res_id}:{acc_code}"
                )],
                [InlineKeyboardButton("❌ انصراف / بازگشت به صف", callback_data="rep:reserves")]
            ])
            confirm_text = (
                f"⚠️ **تأیید فعال‌سازی فوری بسته رزرو**\n\n"
                f"اکانت: {acc_code}\n"
                f"پلن رزرو: **{res_item['plan_name']}** ({res_item['gb']}G / {res_item['days']}D)\n\n"
                "با تأیید، بسته فعلی اکانت کنار گذاشته می‌شود و درخواست تمدید فوراً به پنل ارسال خواهد شد."
            )
        else:
            confirm_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"🗑 تأیید لغو و عودت {int(res_item.get('charge_amount') or 0):,} تومان",
                    callback_data=f"rep:do_cancel_res:{res_id}:{acc_code}"
                )],
                [InlineKeyboardButton("❌ انصراف / بازگشت به صف", callback_data="rep:reserves")]
            ])
            confirm_text = (
                f"⚠️ **تأیید لغو بسته رزرو و عودت وجه**\n\n"
                f"اکانت: {acc_code}\n"
                f"پلن رزرو: **{res_item['plan_name']}**\n"
                f"مبلغ قابل عودت به تراز نمایندگی: **{int(res_item.get('charge_amount') or 0):,} تومان**\n\n"
                "با تأیید، رزرو لغو و اعتبار نمایندگی به‌روزرسانی می‌شود."
            )
        await query.edit_message_text(
            confirm_text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=confirm_keyboard
        )
        await query.answer("برای اجرای عملیات، تأیید دوم را بزنید.")
        return

    # 6.3 Second click: revalidate identity and pending status before mutation.
    elif action in ("do_force_res", "do_cancel_res"):
        try:
            res_id = int(parts[2])
        except (IndexError, ValueError):
            await query.answer("شناسه رزرو نامعتبر است.", show_alert=True)
            return
        acc_code = parts[3] if len(parts) > 3 else ""
        res_item = database.get_reservation_by_id(res_id)
        if (
            not res_item or res_item.get("status") != "pending"
            or res_item.get("rep_id") != rep.get("id")
            or str(res_item.get("account_code", "")).lower() != acc_code.lower()
        ):
            await query.answer("⛔️ رزرو تغییر کرده یا دیگر در محدوده نمایندگی شما نیست.", show_alert=True)
            return

        if action == "do_force_res":
            lock_key = f"reservation-activation:{res_id}"
            lock_token = database.acquire_operation_lock(lock_key, ttl_seconds=None)
            if not lock_token:
                await query.answer("⏳ این رزرو در حال فعال‌سازی است یا نتیجه عملیات قبلی نیازمند بررسی است.", show_alert=True)
                return
            # Re-read after acquiring the lease to close the cancel/activate race.
            res_item = database.get_reservation_by_id(res_id)
            if (
                not res_item or res_item.get("status") != "pending"
                or res_item.get("rep_id") != rep.get("id")
                or str(res_item.get("account_code", "")).lower() != acc_code.lower()
            ):
                database.release_operation_lock(lock_key, lock_token)
                await query.answer("⛔️ وضعیت رزرو تغییر کرده یا در محدوده نمایندگی شما نیست.", show_alert=True)
                return
            gb = res_item["gb"]
            days = res_item["days"]
            on_first = bool(res_item.get("on_first_connect", 0))
            try:
                renew_result = await asyncio.to_thread(
                    xui_manager.renew_client,
                    res_item["email"], gb, days, comment=None, rollover=False,
                    countdown=True if on_first else "auto",
                    scope=rep.get("scope_filter") or "all", by_code_only=True
                )
            except Exception:
                logger.exception("Representative force-reserve outcome is unknown: %s", res_id)
                # Keep the lease until it expires: the panel may have renewed the account.
                await query.answer("⚠️ پاسخ پنل نامشخص است؛ رزرو و قفل تطبیق حفظ شدند.", show_alert=True)
                return
            if classify_xui_result(renew_result) == XUIResult.FAILURE:
                # Explicit and consistent negative response only.
                database.release_operation_lock(lock_key, lock_token)
                await query.answer("❌ پنل فعال‌سازی رزرو را رد کرد؛ رزرو در صف باقی ماند.", show_alert=True)
                return
            if classify_xui_result(renew_result) != XUIResult.SUCCESS:
                await query.answer("⚠️ پاسخ پنل مبهم است؛ رزرو و قفل تطبیق حفظ شدند.", show_alert=True)
                return
            try:
                activated = database.activate_reservation(res_id, actor_id=user.id, actor_name=user.full_name or "Rep")
            except Exception:
                logger.exception("Reservation state write failed after confirmed panel renewal: %s", res_id)
                await query.answer("⚠️ پنل بسته را فعال کرد اما ثبت محلی انجام نشد؛ قفل برای بررسی محفوظ است.", show_alert=True)
                return
            if not activated:
                await query.answer("⚠️ پنل عملیات را انجام داد اما ثبت فعال‌سازی ناموفق بود؛ قفل تطبیق حفظ شد.", show_alert=True)
                return
            database.release_operation_lock(lock_key, lock_token)
            result_msg = f"🚀 بسته رزرو اکانت {acc_code} فعال شد."
        else:
            cancel_result = database.cancel_reservation(
                res_id, actor_id=user.id, actor_name=user.full_name or "Rep"
            )
            if not cancel_result:
                await query.answer("⚠️ رزرو قبلاً تغییر وضعیت داده است یا در حال فعال‌سازی است.", show_alert=True)
                return
            res_data, new_bal = cancel_result
            result_msg = f"🗑 رزرو {acc_code} لغو و {res_data['charge_amount']:,} تومان به تراز نماینده عودت داده شد."

        await query.answer(result_msg, show_alert=True)
        await query.edit_message_text(
            result_msg + "\n\nبرای مشاهده صف به‌روزشده از دکمه زیر استفاده کنید.",
            reply_markup=build_back_to_rep_keyboard([
                [InlineKeyboardButton("⏳ بازخوانی صف رزرو", callback_data="rep:reserves")]
            ])
        )
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



async def _cleanup_form_prompt(
    update: Update, context: ContextTypes.DEFAULT_TYPE, state: dict,
    preserve_message_id: int | None = None,
) -> None:
    """Delete only the transient prompt message owned by the current FSM."""
    prompt_msg_id = state.pop("prompt_msg_id", None)
    if prompt_msg_id is None or str(prompt_msg_id) == str(preserve_message_id):
        return
    chat = getattr(update, "effective_chat", None)
    bot = getattr(context, "bot", None)
    if not chat or not bot:
        logger.warning("Cannot clean representative form prompt %s: chat or bot unavailable", prompt_msg_id)
        return
    try:
        await bot.delete_message(chat_id=chat.id, message_id=prompt_msg_id)
    except Exception as cleanup_error:
        logger.warning(
            "Could not remove transient representative form prompt %s: %s",
            prompt_msg_id, cleanup_error,
        )


def _remember_form_prompt(state: dict, query) -> None:
    """Store the ID of the exact message that the form prompt was rendered into."""
    message = getattr(query, "message", None)
    message_id = getattr(message, "message_id", None)
    if message_id is not None:
        state["prompt_msg_id"] = message_id


def build_fsm_validation_keyboard(role: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⬅️ تلاش مجدد",callback_data=f"{role}:retry_form"),
        InlineKeyboardButton("❌ انصراف از فرم",callback_data=f"{role}:cancel_form"),
    ]])


async def _reply_fsm_validation_error(update,context,state,message: str) -> bool:
    attempts=int(state.get("attempts",0))+1
    state["attempts"]=attempts
    state["ts"]=time.time()
    if attempts>=3:
        parent=state.get("return_callback") or "rep:main"
        context.user_data.pop("rep_state",None)
        await update.message.reply_text(f"❌ فرم پس از ۳ تلاش ناموفق بسته شد.\n{message}",
            reply_markup=build_back_to_rep_keyboard(back_callback=parent))
    else:
        await update.message.reply_text(f"⚠️ {message}\nتلاش {attempts} از ۳؛ فرم همچنان فعال است.",
            reply_markup=build_fsm_validation_keyboard("rep"))
    return True


async def _process_rep_text_input_impl(update: Update, context: ContextTypes.DEFAULT_TYPE, rep: dict) -> bool:
    """Processes FSM inputs initiated from representative dashboard."""
    rep_state = context.user_data.get("rep_state")
    if not rep_state:
        return False

    if int(rep_state.get("rep_id") or -1) != int(rep.get("id") or -2):
        context.user_data.pop("rep_state", None)
        await update.message.reply_text(
            "⛔️ زمینه این درخواست متعلق به نمایندگی دیگری است یا دیگر معتبر نیست؛ از پیشخوان نمایندگی دوباره شروع کنید.",
            reply_markup=build_rep_main_keyboard(),
        )
        return True

    if time.time()-rep_state.get("ts",0)>60:
        parent=rep_state.get("return_callback") or "rep:main"
        context.user_data.pop("rep_state",None)
        await update.message.reply_text("⏱ مهلت زمانی تکمیل این فرم به پایان رسیده است. برای شروع دوباره، فرم را از منو باز کنید.",
            reply_markup=build_back_to_rep_keyboard(back_callback=parent))
        return True

    msg_text = update.message.text.strip()
    action = rep_state.get("action")

    if msg_text.lower() in ("cancel","انصراف","لغو"):
        parent=rep_state.get("return_callback") or "rep:main"
        context.user_data.pop("rep_state",None)
        await update.message.reply_text("❌ عملیات لغو شد.",reply_markup=build_back_to_rep_keyboard(back_callback=parent))
        return True

    if action == "awaiting_new_acc_name":
        if not msg_text:
            return await _reply_fsm_validation_error(update,context,rep_state,"نام مشتری نمی‌تواند خالی باشد.")
        intent_id = rep_state.get("intent_id", "")
        intent = database.get_rep_action_intent(intent_id, rep["id"], update.effective_user.id)
        if not intent or intent.get("status") != "pending":
            context.user_data.pop("rep_state", None)
            await update.message.reply_text("❌ درخواست صدور منقضی شده یا دیگر معتبر نیست؛ از منوی صدور دوباره شروع کنید.")
            return True
        try:
            saved=database.set_rep_action_intent_customer_name(intent_id,rep["id"],update.effective_user.id,msg_text)
        except ValueError as exc:
            return await _reply_fsm_validation_error(update,context,rep_state,f"ثبت نام مشتری انجام نشد: {exc}")
        if not saved:
            return await _reply_fsm_validation_error(update,context,rep_state,"ثبت نام مشتری برای این درخواست ممکن نشد؛ ورودی را اصلاح کنید.")
        context.user_data.pop("rep_state", None)
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                f"✅ تایید صدور با هزینه {intent['amount']:,} تومان",
                callback_data=f"rep:issue_acc_direct:{intent_id}"
            )],
            [InlineKeyboardButton("❌ انصراف", callback_data="rep:new_acc_plans")]
        ])
        await update.message.reply_text(
            f"👤 **نام مشتری:** {msg_text}\n"
            f"📦 **پلن:** {intent['plan_name']} ({intent['gb']} گیگ / {intent['days']} روز)\n"
            f"💳 **هزینه:** {intent['amount']:,} تومان\n\n"
            "برای ادامه، صدور را تایید کنید. این درخواست یک شناسه یک‌بارمصرف دارد.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=keyboard
        )
        return True
    elif action == "awaiting_search_query":
        insp = await asyncio.to_thread(xui_manager.inspect_client,msg_text,scope=rep.get("scope_filter","all"),by_code_only=False)
        if insp.get("status") == "ok":
            client_info = insp["data"]
            owned_account = database.is_account_in_rep_scope(rep["id"], client_info.get("email", ""))
            if not owned_account:
                return await _reply_fsm_validation_error(update,context,rep_state,f"اکانت {msg_text} در محدوده ثبت‌شده نمایندگی شما یافت نشد.")
            context.user_data.pop("rep_state",None)
            acc_code = owned_account["account_code"]
            report_text = xui_manager.format_status_report(client_info, is_admin=False)
            pending_res = database.get_pending_reservation(acc_code)
            keyboard = _build_account_keyboard(acc_code, update.effective_user.id, is_enabled=bool(client_info["raw"].get("enable", True)), role="rep", pending_res=pending_res)
            await update.message.reply_text(report_text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        else:
            return await _reply_fsm_validation_error(update,context,rep_state,f"اکانت {msg_text} در محدوده نمایندگی شما یافت نشد.")
        return True

    return False



async def process_rep_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE, rep: dict) -> bool:
    """Process representative FSM input, cleaning its prompt only on success or closure."""
    managed_actions = {"awaiting_new_acc_name", "awaiting_search_query"}
    state = context.user_data.get("rep_state")
    attempts_before = state.get("attempts", 0) if isinstance(state, dict) else 0
    result = await _process_rep_text_input_impl(update, context, rep)
    if isinstance(state, dict) and state.get("action") in managed_actions:
        state_is_active = context.user_data.get("rep_state") is state
        accepted_input = result is True and state.get("attempts", 0) == attempts_before
        if not state_is_active or accepted_input:
            await _cleanup_form_prompt(update, context, state)
    return result
