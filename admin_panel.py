import os
import sys
import time
import logging
import re
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


_V52_ADMIN_ADAPTER_FACTORY = None


def _v52_admin_enabled() -> bool:
    enabled = {"1", "true", "yes", "on", "enabled"}
    try:
        value = database.get_setting("v52_ui_enabled", "0")
    except Exception:
        value = "0"
    environment = os.getenv("V52_UI_ENABLED", os.getenv("V52_MIGRATIONS_ENABLED", "0"))
    return str(value).strip().lower() in enabled or str(environment).strip().lower() in enabled


def _get_admin_panel_adapter_factory(context=None):
    """Resolve injected factory first; otherwise build the scoped W17 factory."""
    global _V52_ADMIN_ADAPTER_FACTORY
    containers = []
    if context is not None:
        containers.extend([getattr(context, "bot_data", None),
                           getattr(getattr(context, "application", None), "bot_data", None)])
    for container in containers:
        if isinstance(container, dict):
            for key in ("panel_adapter_factory", "adapter_factory", "v52_adapter_factory"):
                candidate = container.get(key)
                if callable(getattr(candidate, "resolve_account_adapter", None)):
                    return candidate
    if _V52_ADMIN_ADAPTER_FACTORY is not None:
        return _V52_ADMIN_ADAPTER_FACTORY
    from panel_adapters.factory import PanelAdapterFactory
    import requests

    def panel_loader(panel_id):
        with database.get_db() as conn:
            row = conn.execute("SELECT * FROM panels WHERE id=?", (int(panel_id),)).fetchone()
        if row is None:
            return None
        record = dict(row)
        record["is_active"] = str(record.get("lifecycle") or "").lower() == "active"
        return record

    def account_loader(account_id):
        with database.get_db() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id=? AND deleted_at IS NULL",
                               (int(account_id),)).fetchone()
        return None if row is None else dict(row)

    def account_id_resolver(panel_id, email):
        with database.get_db() as conn:
            row = conn.execute("SELECT id FROM accounts WHERE panel_id=? AND lower(email)=lower(?) "
                               "AND deleted_at IS NULL ORDER BY id LIMIT 1",
                               (int(panel_id), str(email))).fetchone()
        return None if row is None else int(row["id"])

    data = getattr(context, "bot_data", None) if context is not None else None
    manual_repository = data.get("manual_pool_repository") if isinstance(data, dict) else None
    _V52_ADMIN_ADAPTER_FACTORY = PanelAdapterFactory(
        panel_loader=panel_loader, account_loader=account_loader,
        session_factory=lambda _panel: requests.Session(), manual_repository=manual_repository,
        account_id_resolver=account_id_resolver, transaction_probe=lambda: False,
    )
    return _V52_ADMIN_ADAPTER_FACTORY


def _get_admin_panel_adapter(panel_id=None, context=None, adapter_factory=None):
    """Return the selected active panel adapter; never construct a URL directly."""
    candidate = adapter_factory
    if candidate is None and context is not None:
        data = getattr(context, "bot_data", None)
        if isinstance(data, dict):
            candidate = data.get("panel_adapter_factory") or data.get("adapter_factory")
    selected = panel_id
    if selected is None:
        with database.get_db() as conn:
            row = conn.execute("SELECT id FROM panels WHERE lifecycle='active' "
                               "ORDER BY is_default DESC,id LIMIT 1").fetchone()
        selected = int(row["id"]) if row is not None else None
    if selected is None:
        raise RuntimeError("هیچ پنل فعالی برای مدیریت انتخاب نشده است.")
    if callable(getattr(candidate, "get_adapter", None)):
        return candidate.get_adapter(int(selected))
    if callable(getattr(candidate, "list_clients", None)):
        return candidate
    return _get_admin_panel_adapter_factory(context).get_adapter(int(selected))


async def _dispatch_v52_from_admin(update, context):
    from rep_bot import _dispatch_v52_callback
    return await _dispatch_v52_callback(update, context, role="super_admin", rep=None)


# --- Keyboards ---

def build_admin_main_keyboard() -> InlineKeyboardMarkup:
    """Return the focused v5.2 admin workspace after rollout."""
    if _v52_admin_enabled():
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🖥 مدیریت پنل‌ها", callback_data="v52:panels"),
             InlineKeyboardButton("🤖 مدیریت بات‌ها", callback_data="v52:bots")],
            [InlineKeyboardButton("🌐 مدیریت دامنه‌ها", callback_data="v52:domains"),
             InlineKeyboardButton("💳 کارت‌های پرداخت", callback_data="v52:cards")],
            [InlineKeyboardButton("🏷 تعرفه‌ها", callback_data="v52:plans"),
             InlineKeyboardButton("🛍 فروشگاه", callback_data="v52:shop")],
            [InlineKeyboardButton("🎫 تیکت‌های پشتیبانی", callback_data="v52:tickets"),
             InlineKeyboardButton("🧩 همگام‌سازی فروم", callback_data="v52:forums:root")],
            # --- بخش‌های عملیاتی: ادغام‌شده با منوی قدیمی (نه حذف‌شده) ---
            [InlineKeyboardButton("👥 مدیریت نماینده‌ها", callback_data="admin:reps"),
             InlineKeyboardButton("👤 مدیریت کاربران", callback_data="admin:users")],
            [InlineKeyboardButton("📦 تخصیص و اسکوپ", callback_data="admin:scope"),
             InlineKeyboardButton("💰 امور مالی و تسویه‌ها", callback_data="admin:finance")],
            [InlineKeyboardButton("⚙️ تعرفه‌ها و پلن‌ها", callback_data="admin:plans"),
             InlineKeyboardButton("🔄 همگام‌سازی و آمار", callback_data="admin:sync")],
            [InlineKeyboardButton("⚠️ مانیتورینگ اکانت‌های بحرانی", callback_data="admin:audit"),
             InlineKeyboardButton("🏊 استخر آزاد", callback_data="admin:pool")],
            [InlineKeyboardButton("📦 بسته‌های رزرو در انتظار", callback_data="admin:reserves"),
             InlineKeyboardButton("🛠 تنظیمات پیشرفته سامانه", callback_data="admin:settings")],
            [InlineKeyboardButton("💾 پشتیبان‌گیری سریع دیتابیس", callback_data="admin:backup")],
            # --- Persona Switcher ---
            [InlineKeyboardButton("🧑‍💼 میزکار نماینده (root_admin_rep)", callback_data="v52:persona:rep")],
            [InlineKeyboardButton("👤 پورتال کاربری", callback_data="v52:persona:user")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 مدیریت نماینده‌ها", callback_data="admin:reps"),
         InlineKeyboardButton("👤 مدیریت کاربران", callback_data="admin:users")],
        [InlineKeyboardButton("📦 تخصیص و اسکوپ", callback_data="admin:scope"),
         InlineKeyboardButton("💰 امور مالی و تسویه‌ها", callback_data="admin:finance")],
        [InlineKeyboardButton("⚙️ تعرفه‌ها و پلن‌ها", callback_data="admin:plans"),
         InlineKeyboardButton("🔄 همگام‌سازی و آمار", callback_data="admin:sync")],
        [InlineKeyboardButton("⚠️ مانیتورینگ اکانت‌های بحرانی", callback_data="admin:audit"),
         InlineKeyboardButton("🏊 استخر آزاد", callback_data="admin:pool")],
        [InlineKeyboardButton("📦 بسته‌های رزرو در انتظار", callback_data="admin:reserves"),
         InlineKeyboardButton("🛠 تنظیمات پیشرفته سامانه", callback_data="admin:settings")],
        [InlineKeyboardButton("💾 پشتیبان‌گیری سریع دیتابیس", callback_data="admin:backup")],
    ])

def build_back_to_admin_keyboard(
    extra_buttons=None,
    parent_cb: str | None = None,
    parent_label: str = "⬅️ بازگشت به مرحله قبل",
) -> InlineKeyboardMarkup:
    """Build a consistent two-tier navigation footer for admin submenus."""
    keyboard = list(extra_buttons or [])
    if parent_cb:
        keyboard.append([InlineKeyboardButton(parent_label, callback_data=parent_cb)])
    keyboard.append([
        InlineKeyboardButton("↩️ بازگشت به پنل مدیریت ادمین", callback_data="admin:main")
    ])
    return InlineKeyboardMarkup(keyboard)


def escape_markdown(value) -> str:
    """Escape untrusted dynamic text before interpolation into Telegram Markdown v1."""
    text = "" if value is None else str(value)
    for char in (chr(92), "_", "*", "[", chr(96)):
        text = text.replace(char, chr(92) + char)
    return text


def escape_markdown_code(value) -> str:
    """Escape backslashes/backticks for dynamic values rendered inside Markdown code spans."""
    text = "" if value is None else str(value)
    return text.replace(chr(92), chr(92) * 2).replace(chr(96), chr(92) + chr(96))

# --- Admin Views & Logic ---

def get_build_info() -> dict:
    """Resolve release metadata without requiring the service user to read .git."""
    import re
    import subprocess
    from pathlib import Path
    import config

    version = getattr(config, "VERSION", "v5.2.0")
    branch = getattr(config, "BUILD_BRANCH", "v5.2-release")
    short_hash = ""
    commit_date = datetime.now().strftime("%Y-%m-%d %H:%M")
    repo_dir = os.path.dirname(os.path.abspath(__file__))
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=repo_dir, capture_output=True, text=True, timeout=2
        )
        candidate = result.stdout.strip() if result.returncode == 0 else ""
        if re.fullmatch(r"[0-9a-fA-F]{7,40}", candidate):
            short_hash = candidate[:7]
        date_result = subprocess.run(
            ["git", "log", "-1", "--format=%cd", "--date=format:%Y-%m-%d %H:%M"],
            cwd=repo_dir, capture_output=True, text=True, timeout=2
        )
        if date_result.returncode == 0 and date_result.stdout.strip():
            commit_date = date_result.stdout.strip()
    except Exception as exc:
        logger.debug("Git metadata unavailable; using installer metadata (%s)", type(exc).__name__)

    if not short_hash:
        candidates = [getattr(config, "BUILD_COMMIT", ""), os.getenv("BUILD_COMMIT", "")]
        try:
            build_info = Path(repo_dir, ".build_info").read_text(encoding="utf-8")
            candidates.extend(re.findall(
                r"(?im)^(?:commit|build_commit)\s*[=:]\s*([0-9a-f]{7,40})\s*$", build_info
            ))
            candidates.extend(re.findall(r"(?m)^([0-9a-f]{7,40})\s*$", build_info))
        except OSError:
            pass
        for value in candidates:
            candidate = str(value or "").strip()
            if re.fullmatch(r"[0-9a-fA-F]{7,40}", candidate):
                short_hash = candidate[:7]
                break
    if not short_hash:
        short_hash = "not-recorded"
    return {
        "version": version, "branch": branch,
        "short_hash": short_hash, "commit_date": commit_date,
    }

async def show_admin_dashboard(update: Update, context: ContextTypes.DEFAULT_TYPE, edit_message=False):
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat or chat.type != "private":
        notice = "⛔️ پنل مدیریت فقط در گفت‌وگوی خصوصی ربات قابل استفاده است."
        if update.callback_query:
            await update.callback_query.answer(notice, show_alert=True)
        elif update.message:
            await update.message.reply_text(notice)
        return
    role, _ = database.get_user_role(user.id, chat.id, None)
    if role != "super_admin":
        notice = "⛔️ دسترسی فقط برای مدیریت کل سیستم مجاز است."
        if update.callback_query:
            await update.callback_query.answer(notice, show_alert=True)
        elif update.message:
            await update.message.reply_text(notice)
        return

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


def _transfer_account_summary(account: dict) -> str:
    """Format the source account fields displayed by the ownership-transfer wizard."""
    code = escape_markdown(account.get("account_code") or "")
    email = escape_markdown(account.get("email") or "")
    customer = escape_markdown(account.get("customer_name") or "ثبت نشده")
    rep_name = escape_markdown(account.get("rep_name") or "بدون نماینده (استخر آزاد)")
    owner_id = account.get("owner_tg_id")
    owner = str(owner_id) if owner_id is not None else "ندارد"
    enabled = "🟢 فعال" if account.get("enable") else "🔴 غیرفعال"
    try:
        volume = f"{float(account.get('total_gb') or 0):g} GB"
    except (TypeError, ValueError):
        volume = str(account.get("total_gb") or 0)
    return (
        f"• کد اکانت: {code}\n"
        f"• ایمیل: {email}\n"
        f"• نام مشتری: {customer}\n"
        f"• نماینده فعلی: {rep_name}\n"
        f"• مالک تلگرام فعلی: {owner}\n"
        f"• وضعیت سرویس: {enabled}\n"
        f"• حجم: {volume}"
    )


def _transfer_account_keyboard(account_code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 انتقال به نماینده دیگر", callback_data=f"admin:tr_select_rep:{account_code}")],
        [InlineKeyboardButton("👤 انتقال به کاربر نهایی", callback_data=f"admin:tr_prompt_user:{account_code}")],
        [InlineKeyboardButton("🏊 بازگردانی به استخر آزاد", callback_data=f"admin:tr_confirm:{account_code}:pool:0")],
        [InlineKeyboardButton("⬅️ بازگشت به بخش اسکوپ", callback_data="admin:scope")],
    ])


def _transfer_parent_keyboard(account_code: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅️ بازگشت به مشخصات اکانت", callback_data=f"admin:tr_account:{account_code}")],
        [InlineKeyboardButton("⬅️ بازگشت به بخش اسکوپ", callback_data="admin:scope")],
    ])


def _transfer_confirm_keyboard(account_code: str, dest_type: str, dest_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ تایید و اجرای انتقال", callback_data=f"admin:tr_execute:{account_code}:{dest_type}:{dest_id}")],
        [InlineKeyboardButton("⬅️ بازگشت به انتخاب مقصد", callback_data=f"admin:tr_account:{account_code}")],
        [InlineKeyboardButton("⬅️ بازگشت به بخش اسکوپ", callback_data="admin:scope")],
    ])


def _transfer_source_snapshot(account: dict) -> dict:
    return {
        "account_id": account.get("id"),
        "rep_id": account.get("rep_id"),
        "owner_tg_id": account.get("owner_tg_id"),
        "updated_at": account.get("updated_at"),
    }


async def handle_admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Guard callback dispatch so unexpected failures do not leave Telegram's spinner running."""
    query = update.callback_query
    try:
        await _handle_admin_callbacks_impl(update, context)
    except Exception:
        logger.exception("Unexpected error while handling/rendering an admin callback")
        if query:
            try:
                await query.answer(
                    "❌ خطای غیرمنتظره رخ داد. لطفاً دوباره تلاش کنید.",
                    show_alert=True,
                )
            except Exception:
                logger.debug("Could not answer failed admin callback query", exc_info=True)


async def _handle_admin_callbacks_impl(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat or chat.type != "private":
        await query.answer(
            "⛔️ پنل مدیریت فقط در گفت‌وگوی خصوصی ربات قابل استفاده است.",
            show_alert=True
        )
        return
    role, _ = database.get_user_role(user.id, chat.id, None)
    if role != "super_admin":
        await query.answer("⛔️ دسترسی فقط برای مدیریت کل سیستم مجاز است.", show_alert=True)
        return

    data = query.data or ""
    if data.startswith("v52:"):
        await _dispatch_v52_from_admin(update, context)
        return
    parts=data.split(":")
    action=parts[1] if len(parts)>1 else "main"
    sub_action=parts[2] if len(parts)>2 else ""
    if action=="cancel_form":
        state=context.user_data.pop("admin_state",None) or {}
        await _cleanup_form_prompt(
            update, context, state,
            preserve_message_id=getattr(getattr(query, "message", None), "message_id", None),
        )
        parent=state.get("return_callback") or "admin:main"
        if not isinstance(parent,str) or not parent.startswith("admin:") or parent in ("admin:cancel_form","admin:retry_form"): parent="admin:main"
        query.data=parent
        await _handle_admin_callbacks_impl(update,context)
        return
    if action=="retry_form":
        await query.answer("فرم فعال است؛ مقدار را دوباره ارسال کنید.")
        return
    context.user_data.pop("admin_state",None)
    if action != "tr_execute":
        context.user_data.pop("transfer_confirm", None)

    # Scope 4.1 — four-step account ownership transfer wizard.
    if action == "tr_account":
        account = database.get_account_by_code_or_email(sub_action)
        if not account:
            await query.answer("❌ اکانت پیدا نشد؛ ویزارد را دوباره آغاز کنید.", show_alert=True)
            return
        await query.edit_message_text(
            "🔄 **گام ۲ از ۴ — مشخصات اکانت و انتخاب نوع مقصد**\n\n"
            + _transfer_account_summary(account)
            + "\n\nنوع مقصد انتقال را انتخاب کنید:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_transfer_account_keyboard(account["account_code"]),
        )
        await query.answer()
        return

    elif action == "tr_select_rep":
        account = database.get_account_by_code_or_email(sub_action)
        if not account:
            await query.answer("❌ اکانت پیدا نشد.", show_alert=True)
            return
        active_reps = database.get_all_active_reps()
        rows = [
            [InlineKeyboardButton(
                f"👥 {str(rep.get('name') or rep.get('rep_code') or rep['id'])[:28]} ({rep.get('rep_code') or rep['id']})",
                callback_data=f"admin:tr_confirm:{account['account_code']}:rep:{rep['id']}",
            )]
            for rep in active_reps
        ]
        if not rows:
            rows = [[InlineKeyboardButton("نماینده فعالی ثبت نشده است", callback_data=f"admin:tr_account:{account['account_code']}")]]
        rows.extend([
            [InlineKeyboardButton("⬅️ بازگشت به مشخصات اکانت", callback_data=f"admin:tr_account:{account['account_code']}")],
            [InlineKeyboardButton("⬅️ بازگشت به بخش اسکوپ", callback_data="admin:scope")],
        ])
        await query.edit_message_text(
            "🔄 **گام ۳ از ۴ — انتخاب نماینده مقصد**\n\n"
            + _transfer_account_summary(account)
            + "\n\nیکی از نمایندگان فعال را انتخاب کنید:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(rows),
        )
        await query.answer()
        return

    elif action == "tr_prompt_user":
        account = database.get_account_by_code_or_email(sub_action)
        if not account:
            await query.answer("❌ اکانت پیدا نشد.", show_alert=True)
            return
        context.user_data["admin_state"] = {
            "action": "transfer_user_target",
            "account_code": account["account_code"],
            "ts": time.time(),
            "return_callback": f"admin:tr_account:{account['account_code']}",
        }
        await query.edit_message_text(
            "🔄 **گام ۳ از ۴ — تعیین کاربر مقصد**\n\n"
            + _transfer_account_summary(account)
            + "\n\nChat ID عددی کاربر مقصد را ارسال کنید.\n"
            "کاربر باید قبلاً در ربات ثبت شده باشد.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_transfer_parent_keyboard(account["account_code"]),
        )
        _remember_form_prompt(context.user_data["admin_state"], query)
        await query.answer()
        return

    elif action == "tr_confirm":
        account_code = sub_action
        dest_type = parts[3] if len(parts) > 3 else ""
        dest_id = parts[4] if len(parts) > 4 else ""
        account = database.get_account_by_code_or_email(account_code)
        if not account:
            await query.answer("❌ اکانت پیدا نشد؛ ویزارد را دوباره آغاز کنید.", show_alert=True)
            return
        if dest_type == "pool" and dest_id == "0":
            destination_label = "🏊 استخر آزاد (بدون نماینده و بدون مالک تلگرام)"
        elif dest_type == "rep" and dest_id.isdigit() and int(dest_id) > 0:
            rep = database.get_rep_by_id(int(dest_id))
            if not rep or not rep.get("is_active", 1):
                await query.answer("❌ نماینده مقصد پیدا نشد یا فعال نیست.", show_alert=True)
                return
            destination_label = f"👥 نماینده: {escape_markdown(rep.get('name') or rep.get('rep_code') or dest_id)}"
        elif dest_type == "user" and dest_id.isdigit() and int(dest_id) > 0:
            destination_label = f"👤 کاربر نهایی با Chat ID: {dest_id}"
        else:
            await query.answer("❌ نوع یا شناسه مقصد معتبر نیست.", show_alert=True)
            return
        context.user_data["transfer_confirm"] = {
            "account_code": account["account_code"],
            "dest_type": dest_type,
            "dest_id": dest_id,
            **_transfer_source_snapshot(account),
            "ts": time.time(),
        }
        await query.edit_message_text(
            "⚠️ **گام ۴ از ۴ — تایید نهایی انتقال مالکیت**\n\n"
            + _transfer_account_summary(account)
            + f"\n\n**مقصد انتخاب‌شده:**\n{destination_label}\n\n"
            "با تایید، مالکیت محلی اکانت در دیتابیس تغییر می‌کند.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_transfer_confirm_keyboard(account["account_code"], dest_type, dest_id),
        )
        await query.answer()
        return

    elif action == "tr_execute":
        account_code = sub_action
        dest_type = parts[3] if len(parts) > 3 else ""
        dest_id = parts[4] if len(parts) > 4 else ""
        confirmation = context.user_data.get("transfer_confirm") or {}
        expected = {"account_code": account_code, "dest_type": dest_type, "dest_id": dest_id}
        if any(confirmation.get(key) != value for key, value in expected.items()):
            await query.answer("❌ تایید انتقال معتبر یا فعال نیست؛ ویزارد را دوباره آغاز کنید.", show_alert=True)
            return
        if time.time() - float(confirmation.get("ts") or 0) > 60:
            context.user_data.pop("transfer_confirm", None)
            await query.answer("⏱ مهلت تایید انتقال به پایان رسیده است؛ ویزارد را دوباره آغاز کنید.", show_alert=True)
            return
        account = database.get_account_by_code_or_email(account_code)
        if not account:
            context.user_data.pop("transfer_confirm", None)
            await query.answer("❌ اکانت دیگر در دسترس نیست.", show_alert=True)
            return
        source_now = _transfer_source_snapshot(account)
        if any(source_now.get(key) != confirmation.get(key) for key in source_now):
            context.user_data.pop("transfer_confirm", None)
            await query.answer("⚠️ مالکیت اکانت از زمان نمایش خلاصه تغییر کرده است؛ ویزارد را دوباره اجرا کنید.", show_alert=True)
            return
        try:
            if dest_type == "pool":
                result = database.reassign_account(
                    account_code, to_pool=True, actor_id=user.id,
                    actor_name=user.full_name or "Admin",
                )
                destination_text = "🏊 استخر آزاد"
            elif dest_type == "rep":
                result = database.reassign_account(
                    account_code, new_rep_id=int(dest_id), to_pool=False, actor_id=user.id,
                    actor_name=user.full_name or "Admin",
                )
                rep = database.get_rep_by_id(int(dest_id))
                destination_text = f"👥 {escape_markdown((rep or {}).get('name') or dest_id)}"
            else:
                result = database.reassign_account(
                    account_code, new_owner_tg_id=int(dest_id), to_pool=False, actor_id=user.id,
                    actor_name=user.full_name or "Admin",
                )
                destination_text = f"👤 Chat ID: {dest_id}"
        except (TypeError, ValueError) as exc:
            await query.answer(f"❌ اجرای انتقال انجام نشد: {exc}", show_alert=True)
            return
        if result.get("status") != "ok":
            await query.answer(
                f"❌ انتقال انجام نشد: {result.get('message') or 'خطای نامشخص'}",
                show_alert=True,
            )
            return
        context.user_data.pop("transfer_confirm", None)
        updated_account = database.get_account_by_code_or_email(account_code) or account
        await query.edit_message_text(
            "✅ **انتقال مالکیت با موفقیت انجام شد.**\n\n"
            + _transfer_account_summary(updated_account)
            + f"\n\n**مقصد نهایی:** {destination_text}\n"
            "🧾 رویداد انتقال در audit_logs ثبت شد.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_back_to_admin_keyboard(parent_cb="admin:scope"),
        )
        await query.answer("✅ انتقال انجام شد.")
        return

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
            rep_name = escape_markdown(r.get("name") or "بدون نام")
            rep_code = escape_markdown_code(r.get("rep_code") or "")
            lines.append(
                f"{status_dot} **{rep_name}** (`{rep_code}`):\n"
                f"• شناسه تلگرام: `{r['telegram_id']}` | بدهی: **{int(r.get('balance_toman') or 0):,} تومان**\n"
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
            raw_name = u.get("full_name") or u.get("username") or f"ID: {u['telegram_id']}"
            name_str = escape_markdown(raw_name)
            lines.append(f"{status_dot} `{u['telegram_id']}` - **{name_str}** ({u['linked_accounts_count']} اکانت)")
            extra_btns.append([
                InlineKeyboardButton(f"👤 مدیریت {str(raw_name)[:15]}", callback_data=f"admin:user_detail:{u['telegram_id']}")
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
            f"• **نام:** {escape_markdown(u_info.get('full_name') or 'ثبت نشده')}",
            f"• **یوزرنیم:** @{escape_markdown(u_info.get('username'))}" if u_info.get("username") else "• **یوزرنیم:** ندارد",
            f"• **شناسه تلگرام:** `{u_info['telegram_id']}`",
            f"• **وضعیت دسترسی:** **{block_text}**",
            f"• **موجود کیف پول:** **{u_info['wallet_balance']:,} تومان**",
            f"• **تعداد اکانت‌های متصل:** **{u_info['linked_accounts_count']}**"
        ]
        keyboard = build_back_to_admin_keyboard([
            [InlineKeyboardButton("💰 شارژ / کسر کیف پول", callback_data=f"admin:prompt_charge_user:{user_id}")],
            [InlineKeyboardButton(toggle_btn_text, callback_data=f"admin:set_block_user:{user_id}:{0 if is_blocked else 1}"),
             InlineKeyboardButton("✏️ تغییر Telegram ID", callback_data=f"admin:prompt_edit_user_id:{user_id}")],
            [InlineKeyboardButton("➕ اتصال اکانت", callback_data=f"admin:prompt_link_user_id:{user_id}"),
             InlineKeyboardButton("❌ قطع اتصال اکانت", callback_data=f"admin:prompt_unlink_user_acc:{user_id}")],
            [InlineKeyboardButton("🗑 حذف کاربر", callback_data=f"admin:confirm_del_user:{user_id}")]
        ], parent_cb="admin:users:1")
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer()

    elif action == "toggle_block_user":
        await query.answer(
            "⚠️ این دکمه قدیمی است و برای جلوگیری از تغییر ناخواسته اجرا نشد. کارت کاربر را دوباره باز کنید.",
            show_alert=True
        )
        return

    elif action == "set_block_user":
        user_id = int(sub_action)
        desired_blocked = parts[3] if len(parts) > 3 else ""
        if desired_blocked not in ("0", "1"):
            await query.answer("⛔️ وضعیت درخواستی معتبر نیست؛ کارت کاربر را تازه کنید.", show_alert=True)
            return
        new_val = database.set_user_block_status(
            user_id, desired_blocked == "1",
            actor_id=user.id, actor_name=user.full_name or "Admin"
        )
        await query.answer(
            f"✅ وضعیت کاربر به {'مسدود' if new_val else 'فعال'} تنظیم شد.",
            show_alert=True
        )
        u_info = database.get_bot_user_by_tg_id(user_id)
        if not u_info:
            await query.edit_message_text("❌ کاربر یافت نشد.", reply_markup=build_back_to_admin_keyboard())
            return
        is_blocked = bool(u_info.get("is_blocked", 0))
        toggle_text = "🟢 رفع مسدودی" if is_blocked else "🚫 مسدود کردن"
        lines = [
            "👤 **شناسنامه کاربر نهایی:**\n",
            f"• **نام:** {escape_markdown(u_info.get('full_name') or 'ثبت نشده')}",
            f"• **یوزرنیم:** @{escape_markdown(u_info.get('username'))}" if u_info.get("username") else "• **یوزرنیم:** ندارد",
            f"• **شناسه تلگرام:** `{u_info['telegram_id']}`",
            f"• **وضعیت دسترسی:** **{'🔴 مسدود' if is_blocked else '🟢 فعال'}**",
            f"• **موجودی کیف پول:** **{u_info['wallet_balance']:,} تومان**",
            f"• **تعداد اکانت‌های متصل:** **{u_info['linked_accounts_count']}**"
        ]
        keyboard = build_back_to_admin_keyboard([
            [InlineKeyboardButton("💰 شارژ / کسر کیف پول", callback_data=f"admin:prompt_charge_user:{user_id}")],
            [InlineKeyboardButton(
                toggle_text,
                callback_data=f"admin:set_block_user:{user_id}:{0 if is_blocked else 1}"
            )],
            [InlineKeyboardButton("🏠 بازگشت به کاربران", callback_data="admin:users:1")]
        ], parent_cb="admin:users:1")
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
        page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
        pending_rcps, total_receipts, total_pages = database.get_receipts_paginated(
            status="pending", page=page, page_size=10
        )
        page = min(max(1, page), total_pages)
        lines = [
            f"⏳ **فیش‌های در انتظار بررسی ({total_receipts} مورد) — صفحه {page}/{total_pages}:**\n"
        ]
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
                    InlineKeyboardButton(
                        f"🔎 جزئیات / تصویر {r['tracking_code']}",
                        callback_data=f"admin_detail_rcp_{r['id']}",
                    )
                ])
                extra_btns.append([
                    InlineKeyboardButton(f"✅ تایید {r['tracking_code']}", callback_data=f"admin_approve_rcp_{r['tracking_code']}"),
                    InlineKeyboardButton(f"❌ رد {r['tracking_code']}", callback_data=f"admin_reject_rcp_{r['tracking_code']}")
                ])
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"admin:pending_receipts:{page - 1}"))
        if page < total_pages:
            nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"admin:pending_receipts:{page + 1}"))
        if nav:
            extra_btns.append(nav)
        keyboard = build_back_to_admin_keyboard(extra_btns, parent_cb="admin:finance")
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("فیش‌های در انتظار بررسی")

    # 6. Pricing Plans Management (Complete CRUD)
    elif action == "plans":
        plans = database.get_pricing_plans()
        lines = ["⚙️ **مدیریت کامل تعرفه‌ها و پلن‌های سیستم:**\n"]
        extra_btns = []
        for p in plans:
            quick_tag = "⚡️ [سریع]" if p["is_quick"] else "📦"
            active_tag = "🟢" if p.get("is_active", 1) else "🔴 [غیرفعال]"
            plan_name = escape_markdown(p.get("name") or "")
            plan_code = escape_markdown_code(p.get("plan_code") or "")
            lines.append(
                f"{active_tag} {quick_tag} **{plan_name}** (`{plan_code}`):\n"
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
            f"⚙️ **مشخصات و مدیریت پلن: {escape_markdown(plan.get('name') or '')}**\n",
            f"• کد پلن: `{escape_markdown_code(plan['plan_code'])}`",
            f"• مشخصات: **{plan['gb']} گیگابایت / {plan['days']} روز**",
            f"• قیمت نماینده: **{plan['rep_price']:,} تومان**",
            f"• قیمت کاربر نهایی: **{plan['user_price']:,} تومان**",
            f"• حالت نمایش: **{quick_text}**",
            f"• وضعیت تعرفه: **{'🟢 فعال' if plan.get('is_active', 1) else '🔴 غیرفعال'}**"
        ]
        active_button = "⏸ غیرفعال‌سازی موقت تعرفه" if plan.get("is_active", 1) else "🟢 فعال‌سازی تعرفه"
        keyboard = build_back_to_admin_keyboard([
            [InlineKeyboardButton("✏️ ویرایش قیمت‌ها", callback_data=f"admin:prompt_edit_plan:{plan_code}"),
             InlineKeyboardButton("📐 ویرایش حجم/زمان", callback_data=f"admin:prompt_edit_plan_specs:{plan_code}")],
            [InlineKeyboardButton("⚡️ تغییر حالت سریع/عادی", callback_data=f"admin:set_plan_quick:{plan_code}:{0 if plan.get('is_quick') else 1}")],
            [InlineKeyboardButton(active_button, callback_data=f"admin:set_plan_active:{plan_code}:{0 if plan.get('is_active', 1) else 1}")],
            [InlineKeyboardButton("🗑 حذف این تعرفه", callback_data=f"admin:confirm_del_plan:{plan_code}")]
        ], parent_cb="admin:plans")
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer()

    elif action == "toggle_plan_quick":
        await query.answer(
            "⚠️ این دکمه قدیمی است و تغییری اعمال نشد. جزئیات پلن را دوباره باز کنید.",
            show_alert=True
        )
        return

    elif action == "set_plan_quick":
        plan_code = sub_action
        desired_quick = parts[3] if len(parts) > 3 else ""
        if desired_quick not in ("0", "1"):
            await query.answer("⛔️ وضعیت درخواستی معتبر نیست؛ جزئیات پلن را تازه کنید.", show_alert=True)
            return
        try:
            new_val = database.set_plan_quick_status(
                plan_code, desired_quick == "1",
                actor_id=user.id, actor_name=user.full_name or "Admin"
            )
        except ValueError as exc:
            await query.answer(f"❌ تغییر وضعیت انجام نشد: {exc}", show_alert=True)
            return
        plan = database.get_plan(plan_code)
        if not plan:
            await query.answer("❌ پلن یافت نشد.", show_alert=True)
            return
        await query.answer(
            "✅ نمایش سریع پلن فعال شد." if new_val else "✅ نمایش سریع پلن غیرفعال شد.",
            show_alert=True
        )
        quick_text = "⚡️ نمایش سریع" if plan.get("is_quick") else "📦 نمایش عادی"
        lines = [
            f"⚙️ **مشخصات و مدیریت پلن: {escape_markdown(plan.get('name') or '')}**\n",
            f"• کد پلن: `{escape_markdown_code(plan['plan_code'])}`",
            f"• مشخصات: **{plan['gb']} گیگابایت / {plan['days']} روز**",
            f"• قیمت نماینده: **{plan['rep_price']:,} تومان**",
            f"• قیمت کاربر نهایی: **{plan['user_price']:,} تومان**",
            f"• حالت نمایش: **{quick_text}**",
            f"• وضعیت تعرفه: **{'🟢 فعال' if plan.get('is_active', 1) else '🔴 غیرفعال'}**"
        ]
        active_button = "⏸ غیرفعال‌سازی موقت تعرفه" if plan.get("is_active", 1) else "🟢 فعال‌سازی تعرفه"
        keyboard = build_back_to_admin_keyboard([
            [InlineKeyboardButton("✏️ ویرایش قیمت‌ها", callback_data=f"admin:prompt_edit_plan:{plan_code}")],
            [InlineKeyboardButton(
                "⚡️ تغییر حالت سریع/عادی",
                callback_data=f"admin:set_plan_quick:{plan_code}:{0 if plan.get('is_quick') else 1}"
            )],
            [InlineKeyboardButton(active_button, callback_data=f"admin:set_plan_active:{plan_code}:{0 if plan.get('is_active', 1) else 1}")],
            [InlineKeyboardButton("🗑 حذف این تعرفه", callback_data=f"admin:confirm_del_plan:{plan_code}")]
        ], parent_cb="admin:plans")
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    elif action == "set_plan_active":
        plan_code = sub_action
        desired_active = parts[3] if len(parts) > 3 else ""
        if desired_active not in ("0", "1"):
            await query.answer("⛔️ وضعیت درخواستی معتبر نیست؛ جزئیات پلن را تازه کنید.", show_alert=True)
            return
        try:
            new_val = database.set_plan_active_status(
                plan_code, desired_active == "1",
                actor_id=user.id, actor_name=user.full_name or "Admin"
            )
        except ValueError as exc:
            await query.answer(f"❌ تغییر وضعیت تعرفه انجام نشد: {exc}", show_alert=True)
            return
        plan = database.get_plan(plan_code)
        if not plan:
            await query.answer("❌ پلن یافت نشد.", show_alert=True)
            return
        await query.answer(
            "✅ تعرفه فعال شد." if new_val else "✅ تعرفه موقتاً غیرفعال شد.",
            show_alert=True,
        )
        quick_text = "⚡️ نمایش سریع" if plan.get("is_quick") else "📦 نمایش عادی"
        lines = [
            f"⚙️ **مشخصات و مدیریت پلن: {escape_markdown(plan.get('name') or '')}**\n",
            f"• کد پلن: `{escape_markdown_code(plan['plan_code'])}`",
            f"• مشخصات: **{plan['gb']} گیگابایت / {plan['days']} روز**",
            f"• قیمت نماینده: **{plan['rep_price']:,} تومان**",
            f"• قیمت کاربر نهایی: **{plan['user_price']:,} تومان**",
            f"• حالت نمایش: **{quick_text}**",
            f"• وضعیت تعرفه: **{'🟢 فعال' if plan.get('is_active', 1) else '🔴 غیرفعال'}**",
        ]
        active_button = "⏸ غیرفعال‌سازی موقت تعرفه" if plan.get("is_active", 1) else "🟢 فعال‌سازی تعرفه"
        keyboard = build_back_to_admin_keyboard([
            [InlineKeyboardButton("✏️ ویرایش قیمت‌ها", callback_data=f"admin:prompt_edit_plan:{plan_code}"),
             InlineKeyboardButton("📐 ویرایش حجم/زمان", callback_data=f"admin:prompt_edit_plan_specs:{plan_code}")],
            [InlineKeyboardButton(
                "⚡️ تغییر حالت سریع/عادی",
                callback_data=f"admin:set_plan_quick:{plan_code}:{0 if plan.get('is_quick') else 1}"
            )],
            [InlineKeyboardButton(active_button, callback_data=f"admin:set_plan_active:{plan_code}:{0 if plan.get('is_active', 1) else 1}")],
            [InlineKeyboardButton("🗑 حذف این تعرفه", callback_data=f"admin:confirm_del_plan:{plan_code}")]
        ], parent_cb="admin:plans")
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        return

    elif action == "confirm_del_plan":
        plan_code = sub_action
        keyboard = build_back_to_admin_keyboard([
            [InlineKeyboardButton("✅ بله، حذف شود", callback_data=f"admin:do_del_plan:{plan_code}"),
             InlineKeyboardButton("❌ انصراف", callback_data=f"admin:plan_detail:{plan_code}")]
        ], parent_cb="admin:plans")
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

    # 8. Critical Accounts Audit — read-only through the selected root-scoped adapter.
    elif action == "audit":
        try:
            adapter = _get_admin_panel_adapter(context=context)
            clients = await adapter.list_clients(scope_filter="all")
            audit_res = [
                {"action": str(getattr(client, "email", "") or "panel_client"),
                 "result_status": "enabled" if getattr(client, "enable", False) else "disabled"}
                for client in clients
            ]
        except Exception:
            logger.exception("Scoped panel adapter read failed in admin audit view")
            audit_res = []
        report_text = xui_manager.format_audit_report(audit_res)
        keyboard = build_back_to_admin_keyboard()
        await query.edit_message_text(
            f"⚠️ <b>گزارش اکانت‌های بحرانی و روبه‌اتمام:</b>\n\n{report_text}",
            parse_mode=ParseMode.HTML, reply_markup=keyboard)
        await query.answer("گزارش اکانت‌های بحرانی")

    # 9. Free Pool Accounts Watchdog — read-only through the selected root-scoped adapter.
    elif action == "pool":
        try:
            adapter = _get_admin_panel_adapter(context=context)
            clients = await adapter.list_clients(scope_filter="all")
            labels = [str(getattr(client, "email", "")) for client in clients if getattr(client, "email", None)]
            text = ("🏊 **نمای کلی حساب‌های پنل انتخاب‌شده:**\\n\\n"
                    f"• تعداد حساب‌های قابل مشاهده در اسکوپ root: **{len(clients)}**\\n"
                    "• نمونه شناسه‌ها:\\n" + "\\n".join(f"• {item}" for item in labels[:6]))
        except Exception:
            logger.exception("Scoped panel adapter read failed in admin pool view")
            text = "⚠️ خواندن پنل انتخاب‌شده در اسکوپ root ممکن نشد؛ هیچ fallback مستقیمی اجرا نشد."
        keyboard = build_back_to_admin_keyboard()
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("نمای پنل")

    # 10. Instant Backup & Download
    elif action == "backup":
        try:
            import backup
            b_res = await asyncio.to_thread(backup.run_backup)
            archive_path = b_res.get("archive")
            size_kb = b_res.get("size_kb")
            
            keyboard = build_back_to_admin_keyboard()
            upload_status = b_res.get("telegram_upload_status", "unknown")
            if upload_status == "sent":
                upload_line = "✅ ارسال به تاپیک پشتیبان‌گیری تلگرام تأیید شد."
            elif upload_status == "failed":
                upload_line = "⚠️ آرشیو محلی ساخته شد، اما ارسال تلگرام ناموفق بود."
            else:
                upload_line = "⚠️ آرشیو محلی ساخته شد، اما مقصد تلگرام پیکربندی نشده است."
            await query.edit_message_text(
                f"✅ **فایل پشتیبان محلی ایجاد شد.**\n\n"
                f"• حجم فایل: **{size_kb} KB**\n"
                f"• مسیر لوکال: `{archive_path}`\n"
                f"• وضعیت تلگرام: {upload_line}\n"
                f"• خطای ارسال: {b_res.get('telegram_upload_error') or 'ندارد'}\n\n"
                f"فایل آرشیو محلی در ادامه برای شما ارسال می‌شود...",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=keyboard
            )
            if archive_path and os.path.exists(archive_path):
                try:
                    with open(archive_path, "rb") as doc:
                        await chat.send_document(
                            document=doc,
                            caption=f"💾 نسخه پشتیبان دیتابیس و تنظیمات ({size_kb} KB)",
                            reply_markup=build_back_to_admin_keyboard(),
                        )
                except Exception:
                    logger.exception("Local archive was created but could not be delivered to the admin chat")
                    await query.answer(
                        "⚠️ فایل بکاپ روی سرور ساخته شد، اما ارسال فایل در همین چت ناموفق بود. مسیر محلی در پیام بالاست.",
                        show_alert=True
                    )
                    return
            await query.answer(
                "✅ بکاپ محلی ساخته و ارسال تلگرام تأیید شد."
                if upload_status == "sent" else
                "⚠️ بکاپ محلی ساخته شد؛ وضعیت ارسال تلگرام را در پیام بالا بررسی کنید.",
                show_alert=upload_status != "sent"
            )
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
                    InlineKeyboardButton(f"🚀 فعال‌سازی فوری {r['account_code']}", callback_data=f"admin:confirm_force_res:{r['id']}:{r['account_code']}"),
                    InlineKeyboardButton(f"🗑 لغو و عودت {r['account_code']}", callback_data=f"admin:confirm_cancel_res:{r['id']}:{r['account_code']}")
                ])
            keyboard = build_back_to_admin_keyboard(extra_btns)
            await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer("صف بسته‌های رزرو")

    # 13. Reservation actions require a separate confirmation click.
    elif action in ("force_res", "cancel_res"):
        await query.answer(
            "⚠️ این دکمه قدیمی است و اجرا نشد. صف رزرو را بازخوانی کنید و تأیید دو مرحله‌ای را انجام دهید.",
            show_alert=True
        )
        return

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
            or str(res_item.get("account_code", "")).lower() != acc_code.lower()
        ):
            await query.answer(
                "⚠️ رزرو در انتظار یافت نشد یا اطلاعات حساب با آن تطبیق ندارد.",
                show_alert=True
            )
            return

        if action == "confirm_force_res":
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    "🚀 تأیید فعال‌سازی فوری در پنل",
                    callback_data=f"admin:do_force_res:{res_id}:{acc_code}"
                )],
                [InlineKeyboardButton("❌ انصراف / بازگشت به صف", callback_data="admin:reserves")]
            ])
            prompt = (
                f"⚠️ **تأیید فعال‌سازی فوری بسته رزرو**\n\n"
                f"اکانت: `{acc_code}`\n"
                f"پلن: **{res_item['plan_name']}** ({res_item['gb']}G / {res_item['days']}D)\n\n"
                "با تأیید، درخواست تمدید فوراً به پنل X-UI ارسال می‌شود."
            )
        else:
            buttons = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"🗑 تأیید لغو و عودت {int(res_item.get('charge_amount') or 0):,} تومان",
                    callback_data=f"admin:do_cancel_res:{res_id}:{acc_code}"
                )],
                [InlineKeyboardButton("❌ انصراف / بازگشت به صف", callback_data="admin:reserves")]
            ])
            prompt = (
                f"⚠️ **تأیید لغو بسته رزرو و عودت وجه**\n\n"
                f"اکانت: `{acc_code}`\n"
                f"پلن: **{res_item['plan_name']}**\n"
                f"مبلغ قابل عودت: **{int(res_item.get('charge_amount') or 0):,} تومان**\n\n"
                "با تأیید، رزرو لغو و اعتبار نماینده به‌روزرسانی می‌شود."
            )
        await query.edit_message_text(
            prompt, parse_mode=ParseMode.MARKDOWN, reply_markup=buttons
        )
        await query.answer("لطفاً عملیات را در مرحله دوم تأیید کنید.")
        return

    elif action == "do_force_res":
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
            await query.answer("⚠️ رزرو تغییر کرده یا دیگر در انتظار نیست.", show_alert=True)
            return

        lock_key = f"reservation-activation:{res_id}"
        lock_token = database.acquire_operation_lock(lock_key, ttl_seconds=None)
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

    elif action == "do_cancel_res":
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
            await query.answer("⚠️ رزرو تغییر کرده یا دیگر در انتظار نیست.", show_alert=True)
            return

        cancel_result = database.cancel_reservation(
            res_id, actor_id=user.id, actor_name=user.full_name or "Admin"
        )
        if cancel_result:
            res_data, new_bal = cancel_result
            await query.answer(
                f"🗑 بسته رزرو اکانت {acc_code} لغو و {res_data['charge_amount']:,} تومان عودت داده شد.",
                show_alert=True
            )
        else:
            await query.answer("⚠️ این بسته در حال فعال‌سازی است یا قبلاً لغو/فعال شده است.", show_alert=True)
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
            f"👤 **شناسنامه نماینده: {escape_markdown(rep.get('name') or 'بدون نام')}**\n",
            f"• کد: `{escape_markdown_code(rep.get('rep_code') or '')}` | آیدی: `{rep['telegram_id']}`",
            f"• وضعیت: {'🟢 فعال' if rep['is_active'] else '🔴 معلق'}",
            f"• سقف اعتبار: `{rep['credit_limit_toman']:,} تومان`",
            f"• بدهی جاری: `{rep['balance_toman']:,} تومان`"
        ]
        keyboard = build_back_to_admin_keyboard([
            [InlineKeyboardButton("🟢 تسویه / تعدیل تراز", callback_data=f"admin:prompt_pay:{rep['rep_code']}"),
             InlineKeyboardButton("💳 تغییر سقف اعتبار", callback_data=f"admin:prompt_edit_credit:{rep_id}")],
            [InlineKeyboardButton("✏️ ویرایش مشخصات", callback_data=f"admin:prompt_edit_rep:{rep_id}")],
            [InlineKeyboardButton("⏸ تعلیق / فعال‌سازی", callback_data=f"admin:set_rep_active:{rep_id}:{0 if rep['is_active'] else 1}"),
             InlineKeyboardButton("📦 آرشیو نماینده", callback_data=f"admin:confirm_del_rep:{rep_id}")]
        ], parent_cb="admin:reps")
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer()

    elif action == "toggle_rep":
        await query.answer(
            "⚠️ این دکمه قدیمی است و اجرا نشد. برای جلوگیری از تعلیق/فعال‌سازی ناخواسته، شناسنامه نماینده را دوباره باز کنید.",
            show_alert=True
        )
        return

    elif action == "set_rep_active":
        rep_id = int(sub_action)
        desired_active = parts[3] if len(parts) > 3 else ""
        if desired_active not in ("0", "1"):
            await query.answer("⛔️ وضعیت درخواستی معتبر نیست؛ شناسنامه نماینده را تازه کنید.", show_alert=True)
            return
        try:
            new_val = database.set_rep_active_status(
                rep_id, desired_active == "1",
                actor_id=user.id, actor_name=user.full_name or "Admin"
            )
        except ValueError as exc:
            await query.answer(f"❌ تغییر وضعیت انجام نشد: {exc}", show_alert=True)
            return
        rep = database.get_rep_by_id(rep_id)
        if not rep:
            await query.answer("❌ نماینده یافت نشد.", show_alert=True)
            return
        await query.answer(f"✅ وضعیت نماینده به {'فعال' if new_val else 'معلق'} تنظیم شد.", show_alert=True)
        lines = [
            f"👤 **شناسنامه نماینده: {escape_markdown(rep.get('name') or 'بدون نام')}**\n",
            f"• کد: `{escape_markdown_code(rep.get('rep_code') or '')}` | آیدی: `{rep['telegram_id']}`",
            f"• وضعیت: {'🟢 فعال' if rep['is_active'] else '🔴 معلق'}",
            f"• سقف اعتبار: `{int(rep.get('credit_limit_toman') or 0):,} تومان`",
            f"• بدهی جاری: `{int(rep.get('balance_toman') or 0):,} تومان`"
        ]
        keyboard = build_back_to_admin_keyboard([
            [InlineKeyboardButton("🟢 تسویه / تعدیل تراز", callback_data=f"admin:prompt_pay:{rep['rep_code']}"),
             InlineKeyboardButton("💳 تغییر سقف اعتبار", callback_data=f"admin:prompt_edit_credit:{rep_id}")],
            [InlineKeyboardButton("✏️ ویرایش مشخصات", callback_data=f"admin:prompt_edit_rep:{rep_id}")],
            [InlineKeyboardButton(
                "⏸ تعلیق / فعال‌سازی",
                callback_data=f"admin:set_rep_active:{rep_id}:{0 if rep['is_active'] else 1}"
            ),
             InlineKeyboardButton("🗑 حذف نماینده", callback_data=f"admin:confirm_del_rep:{rep_id}")]
        ], parent_cb="admin:reps")
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
        prompt_parent_cb = {
            "prompt_card_settings": "admin:finance",
            "prompt_charge_user": "admin:users:1",
            "prompt_edit_credit": "admin:reps",
            "prompt_setting": "admin:settings",
            "prompt_pay": "admin:reps",
            "prompt_add_rep": "admin:reps",
            "prompt_edit_plan": "admin:plans",
            "prompt_transfer": "admin:scope",
            "prompt_inspect": "admin:scope",
            "prompt_add_plan": "admin:plans",
            "prompt_link_user": "admin:users:1",
            "prompt_search_user": "admin:users:1",
            "prompt_edit_user_id": "admin:users:1",
            "prompt_link_user_id": "admin:users:1",
            "prompt_unlink_user_acc": "admin:users:1",
            "prompt_edit_rep": "admin:reps",
            "prompt_edit_plan_specs": "admin:plans",
        }[action]
        if action == "prompt_card_settings":
            context.user_data["admin_state"] = {"action": "edit_card_settings", "ts": now_ts}
            card_info = database.get_card_info()
            msg = (
                "💳 **تنظیمات شماره کارت بانکی:**\n\n"
                f"شماره کارت فعلی: `{card_info['card_number']}`\n"
                f"صاحب حساب: `{card_info['card_holder']}`\n"
                f"نام بانک: `{card_info['bank_name']}`\n\n"
                "فرمت پیشنهادی با جداکننده پایپ:\n"
                "6037-9911-2233-4455 | علی رضا محمدی | بانک سامان\n\n"
                "یا سه خط جداگانه:\n"
                "6037-9911-2233-4455\n"
                "علی رضا محمدی\n"
                "بانک سامان\n\n"
                "فرمت فاصله‌ای هوشمند نیز پشتیبانی می‌شود:\n"
                "6037991122334455 علی رضا محمدی بانک سامان\n\n"
                "⏱ مهلت ارسال: ۶۰ ثانیه (یا بنویسید `cancel`)"
            )
            keyboard = build_back_to_admin_keyboard(parent_cb=prompt_parent_cb)
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
            keyboard = build_back_to_admin_keyboard(parent_cb=prompt_parent_cb)
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
            keyboard = build_back_to_admin_keyboard(parent_cb=prompt_parent_cb)
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
                f"• عنوان: **{escape_markdown(target_s.get('label') or '')}**\n"
                f"• کلید سیستمی: `{escape_markdown_code(setting_key)}`\n"
                f"• توضیح: {escape_markdown(target_s.get('description') or '')}\n"
                f"• مقدار فعلی: `{escape_markdown_code(target_s.get('value') or '')}`\n\n"
                f"لطفاً مقدار جدید را ارسال کنید:\n\n"
                f"⏱ مهلت ارسال: ۶۰ ثانیه (یا بنویسید `cancel`)"
            )
            keyboard = build_back_to_admin_keyboard(parent_cb=prompt_parent_cb)
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
            keyboard = build_back_to_admin_keyboard(parent_cb=prompt_parent_cb)
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
            keyboard = build_back_to_admin_keyboard(parent_cb=prompt_parent_cb)
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer()

        elif action == "prompt_edit_plan":
            plan_code = sub_action
            context.user_data["admin_state"] = {"action": "edit_plan", "plan_code": plan_code, "ts": now_ts}
            plan = database.get_plan(plan_code)
            msg = (
                f"✏️ **ویرایش نرخ پلن: {escape_markdown(plan.get('name') or '')} (`{plan_code}`)**\n\n"
                f"قیمت فعلی نماینده: `{plan['rep_price']:,} تومان`\n"
                f"قیمت فعلی کاربر: `{plan['user_price']:,} تومان`\n\n"
                f"لطفاً قیمت جدید نماینده و کاربر را با فاصله بفرستید:\n"
                f"`75000 130000`\n\n"
                f"⏱ مهلت ارسال: ۶۰ ثانیه (یا بنویسید `cancel`)"
            )
            keyboard = build_back_to_admin_keyboard(parent_cb=prompt_parent_cb)
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer()

        elif action == "prompt_transfer":
            context.user_data["admin_state"] = {
                "action": "transfer",
                "ts": now_ts,
                "return_callback": prompt_parent_cb,
            }
            msg = (
                "🔄 **ویزارد انتقال مالکیت اکانت — گام ۱ از ۴:**\n\n"
                "کد اکانت یا ایمیل دقیق سرویس را ارسال کنید.\n"
                "برای سازگاری با روش قبلی، می‌توانید یک خط شامل\n"
                "`<کد_اکانت> <کد_نماینده_یا_Chat_ID>` بفرستید.\n\n"
                "⏱ مهلت ارسال: ۶۰ ثانیه (یا بنویسید `cancel`)"
            )
            keyboard = build_back_to_admin_keyboard(parent_cb=prompt_parent_cb)
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer()

        elif action == "prompt_inspect":
            context.user_data["admin_state"] = {"action": "inspect", "ts": now_ts}
            msg = (
                "🔍 **بازرسی کامل اکانت:**\n\n"
                "کد ۴ رقمی اکانت یا نام مشتری را ارسال کنید:\n\n"
                "⏱ مهلت ارسال: ۶۰ ثانیه (یا بنویسید `cancel`)"
            )
            keyboard = build_back_to_admin_keyboard(parent_cb=prompt_parent_cb)
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
            keyboard = build_back_to_admin_keyboard(parent_cb=prompt_parent_cb)
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
            keyboard = build_back_to_admin_keyboard(parent_cb=prompt_parent_cb)
            await query.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            await query.answer()
        elif action == "prompt_search_user":
            context.user_data["admin_state"] = {"action": "search_user", "ts": now_ts}
            await query.edit_message_text(
                "🔍 **جستجوی کاربر:** شناسه تلگرام، نام یا یوزرنیم را ارسال کنید.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=build_back_to_admin_keyboard(parent_cb=prompt_parent_cb)
            )
            await query.answer()

        elif action == "prompt_edit_user_id":
            target_user_id = int(sub_action)
            context.user_data["admin_state"] = {
                "action": "edit_user_id", "target_user_id": target_user_id, "ts": now_ts
            }
            await query.edit_message_text(
                f"✏️ **تغییر شناسه تلگرام کاربر `{target_user_id}`:**\nشناسه عددی جدید را ارسال کنید.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=build_back_to_admin_keyboard(parent_cb=prompt_parent_cb)
            )
            await query.answer()

        elif action == "prompt_link_user_id":
            target_user_id = int(sub_action)
            context.user_data["admin_state"] = {
                "action": "link_user_account", "target_user_id": target_user_id, "ts": now_ts
            }
            await query.edit_message_text(
                f"🔗 **اتصال اکانت به کاربر `{target_user_id}`:**\nکد دقیق اکانت را ارسال کنید.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=build_back_to_admin_keyboard(parent_cb=prompt_parent_cb)
            )
            await query.answer()

        elif action == "prompt_unlink_user_acc":
            target_user_id = int(sub_action)
            context.user_data["admin_state"] = {
                "action": "unlink_user_account", "target_user_id": target_user_id, "ts": now_ts
            }
            await query.edit_message_text(
                f"🔌 **قطع اتصال اکانت از کاربر `{target_user_id}`:**\nکد دقیق اکانت را ارسال کنید.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=build_back_to_admin_keyboard(parent_cb=prompt_parent_cb)
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
                f"✏️ **ویرایش نماینده {escape_markdown(target_rep.get('name') or 'بدون نام')}**\n"
                "اطلاعات را به ترتیب زیر بفرستید:\n"
                "`<rep_code> <name> <telegram_id> <group_id> <topic_id> <scope_filter>`\n"
                "نام باید بدون فاصله یا با زیرخط باشد.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=build_back_to_admin_keyboard(parent_cb=prompt_parent_cb)
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
                f"📐 **ویرایش حجم و زمان پلن {escape_markdown(plan.get('name') or '')}**\n"
                "فرمت: `<GB> <days> <نام جدید پلن>`\n"
                "مثال: `25 30 بسته ویژه ماهانه`",
                parse_mode=ParseMode.MARKDOWN, reply_markup=build_back_to_admin_keyboard(parent_cb=prompt_parent_cb)
            )
            await query.answer()

        _form_state_scope3=context.user_data.get("admin_state")
        if _form_state_scope3:
            _form_state_scope3.setdefault("return_callback",prompt_parent_cb)
            _remember_form_prompt(_form_state_scope3, query)

        _scope3_state=context.user_data.get("admin_state")
        if _scope3_state:
            _scope3_state.setdefault("return_callback",prompt_parent_cb)
            _remember_form_prompt(_scope3_state, query)

    elif action == "confirm_del_user":
        target_user_id = int(sub_action)
        u_info = database.get_bot_user_by_tg_id(target_user_id)
        if not u_info:
            await query.answer("کاربر یافت نشد.", show_alert=True)
            return
        keyboard = build_back_to_admin_keyboard([
            [InlineKeyboardButton("🗑 بله، حذف دسترسی کاربر", callback_data=f"admin:do_del_user:{target_user_id}")],
            [InlineKeyboardButton("❌ انصراف", callback_data=f"admin:user_detail:{target_user_id}")]
        ], parent_cb="admin:users:1")
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
            reply_markup=build_back_to_admin_keyboard(parent_cb="admin:users:1")
        )
        return

    elif action == "confirm_del_rep":
        target_rep_id = int(sub_action)
        target_rep = database.get_rep_by_id(target_rep_id)
        if not target_rep:
            await query.answer("نماینده یافت نشد.", show_alert=True)
            return
        keyboard = build_back_to_admin_keyboard([
            [InlineKeyboardButton("📦 بله، آرشیو نماینده", callback_data=f"admin:do_del_rep:{target_rep_id}")],
            [InlineKeyboardButton("❌ انصراف", callback_data=f"admin:rep_detail:{target_rep_id}")]
        ], parent_cb="admin:reps")
        await query.edit_message_text(
            f"⚠️ **آرشیو نماینده {escape_markdown(target_rep.get('name') or 'بدون نام')}؟**\n"
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
            reply_markup=build_back_to_admin_keyboard(parent_cb="admin:reps")
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
                rep_name = escape_markdown(tx.get("rep_name") or "بدون نام")
                rep_code = escape_markdown(tx.get("rep_code") or "بدون کد")
                identity = f"نماینده: {rep_name} ({rep_code} | ID {tx['rep_id']})"
            elif tx.get("user_id") is not None:
                user_name = escape_markdown(tx.get("user_full_name") or "بدون نام")
                raw_username = tx.get("user_username")
                user_tag = f"@{escape_markdown(raw_username)}" if raw_username else "بدون یوزرنیم"
                identity = f"کاربر: {user_name} ({user_tag} | Telegram ID {tx['user_id']})"
            else:
                actor_name = escape_markdown(tx.get("actor_name") or tx.get("actor_id"))
                identity = f"عامل: {actor_name} (ID {tx.get('actor_id')})"
            description = escape_markdown(tx.get("description") or "")
            lines.append(
                f"• {sign} | {identity}\n"
                f"  `{tx['created_at'][:16]}` | **{tx['amount']:,} تومان**\n"
                f"  {description} | مانده: `{tx['balance_after']:,}`"
            )
        nav = []
        if page > 1:
            nav.append(InlineKeyboardButton("⬅️ قبلی", callback_data=f"admin:recent_tx:{page-1}"))
        if page < total_pages:
            nav.append(InlineKeyboardButton("بعدی ➡️", callback_data=f"admin:recent_tx:{page+1}"))
        extra = [nav] if nav else []
        keyboard = build_back_to_admin_keyboard(extra, parent_cb="admin:finance")
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("تاریخچه تراکنش‌ها")


def parse_card_settings_input(raw_text: str) -> tuple[str, str, str]:
    digits=str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩","01234567890123456789")
    raw=(raw_text or "").translate(digits).strip()
    pattern=re.compile(r"(?<!\d)(?:\d[\s-]*){15}\d(?!\d)")
    match=pattern.search(raw)
    if not match: raise ValueError("شماره کارت باید دقیقاً ۱۶ رقم داشته باشد.")
    card=re.sub(r"[\s-]","",match.group(0))
    if len(card)!=16 or not card.isdigit(): raise ValueError("شماره کارت معتبر نیست.")
    def split_fields(value):
        words=value.strip(" \t\r\n|,;:-").split()
        if len(words)<2: raise ValueError("نام صاحب‌حساب و نام بانک را جداگانه وارد کنید.")
        marker=max((i for i,w in enumerate(words) if w in ("بانک","بانك")),default=-1)
        if 0<marker<len(words)-1: return " ".join(words[:marker])," ".join(words[marker:])
        banks=("قرض الحسنه مهر ایران","قرض‌الحسنه مهر ایران","اقتصاد نوین","توسعه تعاون","صنعت و معدن","ایران زمین","رفاه کارگران","خاورمیانه","کارآفرین","گردشگری","پاسارگاد","پارسیان","صادرات","کشاورزی","پست بانک","مهر ایران","سرمایه","سامان","ملت","ملی","سپه","مسکن","تجارت","شهر","آینده","دی","سینا","قوامین","رسالت")
        normalized=[w.replace("ي","ی").replace("ك","ک") for w in words]
        for bank in sorted(banks,key=lambda x:len(x.split()),reverse=True):
            suffix=[w.replace("ي","ی").replace("ك","ک") for w in bank.split()]
            if len(words)>len(suffix) and normalized[-len(suffix):]==suffix:
                return " ".join(words[:-len(suffix)])," ".join(words[-len(suffix):])
        return " ".join(words[:-1]),words[-1]
    if "|" in raw:
        fields=[x.strip() for x in raw.split("|")]
        if len(fields)!=3: raise ValueError("فرمت پایپ باید سه بخش شماره کارت، صاحب‌حساب و بانک داشته باشد.")
        first=pattern.search(fields[0])
        if not first or re.sub(r"[\s-]","",first.group(0))!=card: raise ValueError("شماره کارت باید در بخش اول باشد.")
        holder,bank=fields[1],fields[2]
    elif "\n" in raw or "\r" in raw:
        lines=[x.strip() for x in raw.splitlines() if x.strip()]
        pos=next((i for i,x in enumerate(lines) if pattern.search(x)),-1)
        if pos<0: raise ValueError("شماره کارت در ورودی پیدا نشد.")
        line=lines[pos]; m=pattern.search(line)
        residue=(line[:m.start()]+" "+line[m.end():]).strip(" \t|,;:-")
        rest=lines[:pos]+lines[pos+1:]
        if residue and rest: holder,bank=residue," ".join(rest)
        elif len(rest)>=2: holder,bank=rest[0]," ".join(rest[1:])
        elif len(rest)==1: holder,bank=split_fields(rest[0])
        else: holder,bank=split_fields(residue)
    else:
        holder,bank=split_fields((raw[:match.start()]+" "+raw[match.end():]).strip(" \t|,;:-"))
    holder=re.sub(r"\s+"," ",holder).strip()
    bank=re.sub(r"\s+"," ",bank).strip()
    if not holder or not bank: raise ValueError("نام صاحب‌حساب و نام بانک نمی‌توانند خالی باشند.")
    return card,holder,bank


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
        logger.warning("Cannot clean admin form prompt %s: chat or bot unavailable", prompt_msg_id)
        return
    try:
        await bot.delete_message(chat_id=chat.id, message_id=prompt_msg_id)
    except Exception as cleanup_error:
        logger.warning(
            "Could not remove transient admin form prompt %s: %s",
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
        parent=state.get("return_callback") or "admin:main"
        context.user_data.pop("admin_state",None)
        await update.message.reply_text(f"❌ فرم پس از ۳ تلاش ناموفق بسته شد.\n{message}",
            reply_markup=build_back_to_admin_keyboard(parent_cb=parent))
    else:
        await update.message.reply_text(f"⚠️ {message}\nتلاش {attempts} از ۳؛ فرم همچنان فعال است.",
            reply_markup=build_fsm_validation_keyboard("admin"))
    return True


async def _process_admin_text_input_impl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
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
    if now-created_ts>60:
        parent=admin_state.get("return_callback") or "admin:main"
        context.user_data.pop("admin_state",None)
        await update.message.reply_text("⏱ مهلت زمانی تکمیل این فرم به پایان رسیده است. برای شروع دوباره، فرم را از منو باز کنید.",
            reply_markup=build_back_to_admin_keyboard(parent_cb=parent))
        return True
        
    text = (update.message.text or "").strip()
    
    # Priority to commands
    if text.startswith("/"):
        context.user_data.pop("admin_state", None)
        return False

    # Priority to cancellation
    if text.lower() in ("cancel","انصراف","لغو"):
        parent=admin_state.get("return_callback") or "admin:main"
        context.user_data.pop("admin_state",None)
        await update.message.reply_text("❌ عملیات فرم لغو شد.",reply_markup=build_back_to_admin_keyboard(parent_cb=parent))
        return True

    # Active form input takes precedence over account/NLP routing. To exit,
    # the administrator explicitly cancels the form or lets it expire.
    action = admin_state.get("action")

    if action == "edit_card_settings":
        try:
            c_num,c_holder,c_bank=parse_card_settings_input(text)
            database.update_card_info(c_num,c_holder,c_bank)
        except ValueError as exc:
            return await _reply_fsm_validation_error(update,context,admin_state,f"اطلاعات کارت قابل ثبت نیست: {exc}")
        context.user_data.pop("admin_state",None)
        await update.message.reply_text(
            f"✅ اطلاعات کارت بانکی با موفقیت ویرایش شد.\nشماره کارت: {c_num}\nصاحب حساب: {c_holder}\nبانک: {c_bank}",
            reply_markup=build_admin_main_keyboard())
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
        except ValueError as exc:
            return await _reply_fsm_validation_error(update,context,admin_state,f"مقدار باید عددی و معتبر باشد: {exc}")
        context.user_data.pop("admin_state",None)
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
        except ValueError as exc:
            return await _reply_fsm_validation_error(update,context,admin_state,f"مقدار باید عددی و معتبر باشد: {exc}")
        context.user_data.pop("admin_state",None)
        return True

    elif action == "edit_setting":
        setting_key = admin_state.get("key")
        label = admin_state.get("label", setting_key)
        new_val = text.strip()
        try:
            database.set_setting(setting_key,new_val,actor_id=update.effective_user.id,actor_name=update.effective_user.full_name or "Admin")
        except ValueError as exc:
            return await _reply_fsm_validation_error(update,context,admin_state,f"مقدار تنظیم معتبر نیست: {exc}")
        context.user_data.pop("admin_state",None)
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
        except ValueError as exc:
            return await _reply_fsm_validation_error(update,context,admin_state,f"فرمت مبلغ نامعتبر است: {exc}")

    elif action == "add_rep":
        parts = text.split(maxsplit=5)
        if len(parts)<6:
            return await _reply_fsm_validation_error(update,context,admin_state,"فیلدهای نماینده ناقص است؛ شش بخش لازم را ارسال کنید.")
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
        except ValueError as exc:
            return await _reply_fsm_validation_error(update,context,admin_state,f"مقادیر شناسه‌ها باید عددی و معتبر باشند: {exc}")

    elif action == "edit_plan":
        plan_code = admin_state.get("plan_code")
        parts = text.split()
        if len(parts)<2:
            return await _reply_fsm_validation_error(update,context,admin_state,"دو قیمت جدید نماینده و کاربر را به ترتیب ارسال کنید.")
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
        except ValueError as exc:
            return await _reply_fsm_validation_error(update,context,admin_state,f"قیمت‌ها باید عددی و معتبر باشند: {exc}")

    elif action == "transfer":
        legacy_parts = text.split()
        if len(legacy_parts) >= 2:
            # Backward-compatible old single-line format: account identifier + rep code or Chat ID.
            acc_code, target = legacy_parts[0], legacy_parts[1]
            rep_info = database.get_rep_by_code(target.lower())
            new_rep_id = rep_info["id"] if rep_info else None
            new_owner_id = int(target) if target.isdigit() else None
            result = database.reassign_account(
                acc_code, new_rep_id=new_rep_id, new_owner_tg_id=new_owner_id,
                actor_id=update.effective_user.id,
                actor_name=update.effective_user.full_name or "Admin",
            )
            if result.get("status") != "ok":
                return await _reply_fsm_validation_error(update, context, admin_state,
                    f"انتقال با فرمت قدیمی انجام نشد: {result.get('message') or 'مقصد معتبر نیست.'}")
            context.user_data.pop("admin_state", None)
            target_label = rep_info["name"] if rep_info else f"Chat ID: {new_owner_id}"
            await update.message.reply_text(
                f"✅ **سازگاری با فرمت قدیمی:** مالکیت اکانت {result['account_code']} به {target_label} منتقل شد.\n"
                "🧾 رویداد انتقال در audit_logs ثبت شد.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_back_to_admin_keyboard(parent_cb="admin:scope"),
            )
            return True
        if not text:
            return await _reply_fsm_validation_error(update, context, admin_state, "کد اکانت یا ایمیل را وارد کنید.")
        account = database.get_account_by_code_or_email(text)
        if not account:
            return await _reply_fsm_validation_error(update, context, admin_state, "اکانتی با این کد یا ایمیل پیدا نشد؛ ورودی را بررسی کنید.")
        admin_state["account_code"] = account["account_code"]
        admin_state["stage"] = "choose_destination"
        admin_state["ts"] = time.time()
        await update.message.reply_text(
            "🔄 **گام ۲ از ۴ — مشخصات اکانت و انتخاب نوع مقصد**\n\n"
            + _transfer_account_summary(account)
            + "\n\nنوع مقصد انتقال را انتخاب کنید:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_transfer_account_keyboard(account["account_code"]),
        )
        return True

    elif action == "transfer_user_target":
        translated = text.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")).strip()
        if not translated.isdigit() or int(translated) <= 0:
            return await _reply_fsm_validation_error(update, context, admin_state, "Chat ID مقصد باید یک عدد صحیح مثبت باشد.")
        account_code = admin_state.get("account_code")
        account = database.get_account_by_code_or_email(account_code)
        if not account:
            return await _reply_fsm_validation_error(update, context, admin_state, "اکانت پیدا نشد؛ ویزارد را دوباره آغاز کنید.")
        chat_id = int(translated)
        context.user_data.pop("admin_state", None)
        await update.message.reply_text(
            "🔄 **گام ۳ از ۴ — کاربر مقصد تعیین شد**\n\n"
            + _transfer_account_summary(account)
            + f"\n\n**کاربر مقصد:** Chat ID {chat_id}\n"
            "برای مشاهده خلاصه نهایی و تایید انتقال، دکمه زیر را انتخاب کنید.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("ادامه به خلاصه تایید نهایی", callback_data=f"admin:tr_confirm:{account['account_code']}:user:{chat_id}")],
                [InlineKeyboardButton("⬅️ بازگشت به مشخصات اکانت", callback_data=f"admin:tr_account:{account['account_code']}")],
                [InlineKeyboardButton("⬅️ بازگشت به بخش اسکوپ", callback_data="admin:scope")],
            ]),
        )
        return True


    elif action == "inspect":
        insp = await asyncio.to_thread(xui_manager.inspect_client, text, scope="all")
        if insp.get("status") == "ok":
            status_text = xui_manager.format_status_report(insp["data"], is_admin=True)
            await update.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN, reply_markup=build_admin_main_keyboard())
        else:
            return await _reply_fsm_validation_error(update,context,admin_state,f"اکانت {text} روی سرور پیدا نشد؛ شناسه را بررسی کنید.")
        context.user_data.pop("admin_state",None)
        return True

    elif action == "add_plan":
        parts = text.split()
        if len(parts)<6:
            return await _reply_fsm_validation_error(update,context,admin_state,"فیلدهای پلن ناقص است؛ کد، نام، حجم، روز و دو قیمت را وارد کنید.")
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
        except ValueError as exc:
            return await _reply_fsm_validation_error(update,context,admin_state,f"مقادیر پلن معتبر نیستند: {exc}")

    elif action == "link_user":
        parts = text.split()
        if len(parts)<2 or not parts[0].isdigit():
            return await _reply_fsm_validation_error(update,context,admin_state,"فرمت اتصال باید Chat ID عددی و کد اکانت باشد.")
        chat_id, code = int(parts[0]), parts[1]
        res = database.link_user_to_account(code, chat_id, allow_reassign=True)
        if res.get("status") == "ok":
            await update.message.reply_text(f"✅ اکانت `{code}` به Chat ID `{chat_id}` متصل گردید.", reply_markup=build_admin_main_keyboard())
        else:
            return await _reply_fsm_validation_error(update,context,admin_state,f"عملیات انجام نشد: {res.get('message') or 'ورودی معتبر نیست.'}")
        context.user_data.pop("admin_state",None)
        return True

    elif action == "search_user":
        matches = database.search_bot_users(text)
        if not matches:
            return await _reply_fsm_validation_error(update,context,admin_state,"کاربری با این شناسه، نام یا یوزرنیم پیدا نشد.")
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
            return await _reply_fsm_validation_error(update,context,admin_state,f"تغییر شناسه انجام نشد: {exc}")
        context.user_data.pop("admin_state",None)
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
            return await _reply_fsm_validation_error(update,context,admin_state,f"اتصال انجام نشد: {linked.get('message') or 'کد اکانت معتبر نیست.'}")
        await update.message.reply_text(response,parse_mode=ParseMode.MARKDOWN,reply_markup=build_admin_main_keyboard())
        context.user_data.pop("admin_state",None)
        return True

    elif action == "unlink_user_account":
        target_user_id = int(admin_state["target_user_id"])
        unlinked = database.unlink_account_owner(text, target_user_id)
        if not unlinked:
            return await _reply_fsm_validation_error(update,context,admin_state,"اکانت متعلق به این کاربر نیست یا یافت نشد.")
        context.user_data.pop("admin_state",None)
        await update.message.reply_text("✅ اتصال اکانت قطع شد.",reply_markup=build_admin_main_keyboard())
        return True

    elif action == "edit_rep":
        target_rep_id = int(admin_state["target_rep_id"])
        parts = text.split(maxsplit=5)
        if len(parts)!=6:
            return await _reply_fsm_validation_error(update,context,admin_state,"شش فیلد کد نماینده، نام، Telegram ID، Group ID، Topic ID و Scope Filter را وارد کنید.")
        r_code, r_name, r_tg, r_group, r_topic, r_scope = parts
        try:
            database.update_rep_full(
                target_rep_id, r_name, r_code, int(r_tg), int(r_group), int(r_topic), r_scope
            )
            await update.message.reply_text("✅ اطلاعات نماینده به‌روزرسانی شد.", reply_markup=build_admin_main_keyboard())
        except ValueError as exc:
            return await _reply_fsm_validation_error(update,context,admin_state,f"شناسه‌ها باید عددی و معتبر باشند: {exc}")
        context.user_data.pop("admin_state",None)
        return True

    elif action == "edit_plan_specs":
        parts = text.split(maxsplit=2)
        if len(parts)!=3:
            return await _reply_fsm_validation_error(update,context,admin_state,"حجم، تعداد روز و نام جدید پلن را وارد کنید.")
        try:
            gb = float(parts[0].translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")))
            days = int(parts[1].translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")))
            database.update_plan_specs(admin_state["plan_code"], parts[2], gb, days)
            await update.message.reply_text("✅ حجم، زمان و نام پلن به‌روزرسانی شد.", reply_markup=build_admin_main_keyboard())
        except ValueError as exc:
            return await _reply_fsm_validation_error(update,context,admin_state,f"ویرایش پلن انجام نشد: {exc}")
        context.user_data.pop("admin_state",None)
        return True

    return False



async def process_admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Process admin FSM input, cleaning the previous prompt only on success or closure."""
    # Keep the callback-to-FSM action contract explicit for audits and routing tests.
    managed_actions = {
        "edit_card_settings", "charge_user", "edit_credit_limit", "edit_setting",
        "pay", "add_rep", "edit_plan", "transfer", "transfer_user_target",
        "inspect", "add_plan", "link_user", "search_user", "edit_user_id",
        "link_user_account", "unlink_user_account", "edit_rep", "edit_plan_specs",
    }
    state = context.user_data.get("admin_state")
    attempts_before = state.get("attempts", 0) if isinstance(state, dict) else 0
    result = await _process_admin_text_input_impl(update, context)
    if isinstance(state, dict) and state.get("action") in managed_actions:
        state_is_active = context.user_data.get("admin_state") is state
        accepted_input = result is True and state.get("attempts", 0) == attempts_before
        if not state_is_active or accepted_input:
            await _cleanup_form_prompt(update, context, state)
    return result
