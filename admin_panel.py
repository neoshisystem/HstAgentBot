import os
import sys
import time
import logging
import re
import sqlite3
import asyncio
import uuid
from datetime import datetime
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import ContextTypes

import database
import message_edit
import nlp_matcher
import service_errors_fa
import ui_v52

from server_integrations import load_integration
xui_manager = load_integration("xui_manager")
backup_and_report = load_integration("backup_and_report")

logger = logging.getLogger("HstAgentBot.AdminPanel")


_V52_ADMIN_ADAPTER_FACTORY = None

# The advanced settings centre is grouped so an operator never has to scan a flat
# list of 17+ English keys. Every key stays editable; only its presentation and its
# post-save destination change.
SETTINGS_CATEGORIES = (
    ("group", "📢 تنظیمات سوپرگروه و تاپیک‌های گزارش",
     ("main_group_id", "operational_logs_topic_id", "pending_receipts_topic_id",
      "backups_topic_id", "rep_logs_topic_id", "support_tickets_topic_id", "admin_dm_topic_id")),
    ("registration", "🔐 سیاست‌های ثبت‌نام، عضویت و کد معرف",
     ("registration_mode", "referral_requires_approval", "referral_signup_bonus",
      "referral_first_purchase_bonus", "referral_purchase_reward_percent",
      "sub_profile_limit")),
    ("finance", "💰 آستانه‌های مالی، رزرو و قوانین تمدید (Rollover)",
     ("rollover_max_gb", "rollover_max_days", "reserve_trigger_gb", "renewal_lock_ttl_sec")),
    ("monitoring", "🔍 تنظیمات مانیتورینگ، دیده‌بان (Watchdog) و استخر آزاد",
     ("watchdog_interval_sec", "pool_warning_threshold", "critical_audit_gb", "critical_audit_days")),
    ("features", "🎛 قابلیت‌های رابط کاربری و چندباتی (v5.2 Features)",
     ("v52_ui_enabled", "v52_persona_switcher_enabled", "admin_telegram_id")),
)
SETTINGS_CATEGORY_TITLES = {key: title for key, title, _keys in SETTINGS_CATEGORIES}
SETTINGS_CATEGORY_OF = {k: key for key, _title, keys in SETTINGS_CATEGORIES for k in keys}
SETTINGS_OTHER_TITLE = "🧩 سایر پارامترها"

# Internal counters and bookkeeping rows are not operator settings. They must not
# appear in the settings centre at all — not in a category, not in the catch-all —
# because there is nothing an operator could usefully change about them.
HIDDEN_SETTINGS = frozenset({"ticket_code_seq"})

#: Whole namespaces of internal bookkeeping. ``bootstrap_v52`` writes machine-readable
#: JSON markers here (``v52.bootstrap.financial_cutoff``); they are not operator
#: settings. They used to leak into «سایر پارامترها», where the ``_`` in every JSON
#: key was read as Markdown italic and Telegram rejected the whole page.
HIDDEN_SETTING_PREFIXES = ("v52.bootstrap.",)


def is_hidden_setting(key) -> bool:
    """True for an internal row that must never appear in the settings centre."""
    text = str(key or "")
    return text in HIDDEN_SETTINGS or text.startswith(HIDDEN_SETTING_PREFIXES)

# Settings whose value is a switch rather than a number or an identifier. Only
# these render as فعال/غیرفعال: a topic id of 0 is a real identifier, not "off".
BOOLEAN_SETTINGS = frozenset({
    "v52_ui_enabled", "v52_persona_switcher_enabled", "referral_requires_approval",
})
BOOLEAN_ON, BOOLEAN_OFF = "🟢 فعال (1)", "🔴 غیرفعال (0)"

# Readable Persian for a setting whose value is a fixed keyword.
SETTING_VALUE_LABELS = {
    "registration_mode": {
        "open": "باز — همه کاربران می‌توانند ثبت‌نام کنند",
        "approval_required": "نیازمند تایید مدیریت",
        "referral_only": "فقط با کد معرف معتبر",
    },
}

# A raw English key must never reach the screen, so every fallback below is Persian.
SETTING_LABEL_FALLBACK = "پارامتر سیستمی"
SETTING_DESCRIPTION_FALLBACK = (
    "برای این پارامتر توضیح فارسی ثبت نشده است؛ پیش از تغییر مقدار، اثر آن را بررسی کنید."
)


def _has_persian(text) -> bool:
    """True when the text carries at least one Persian letter."""
    return service_errors_fa.is_persian(text)


def setting_label(setting) -> str:
    """Persian title for a settings row; the raw key is never a fallback."""
    label = str(setting.get("label") or "").strip()
    if label and _has_persian(label):
        return label
    return SETTING_LABEL_FALLBACK


def setting_description(setting) -> str:
    """Practical Persian explanation for a settings row, never empty."""
    description = str(setting.get("description") or "").strip()
    if description and _has_persian(description):
        return description
    return SETTING_DESCRIPTION_FALLBACK


def setting_value_display(key: str, value, *, markdown: bool = True) -> str:
    """Readable current value: a switch reads as فعال/غیرفعال, a keyword in Persian.

    ``markdown`` wraps a raw value in a code span for the legacy Markdown pages, where
    a stray backtick or underscore in the value would otherwise break the message. The
    wizard pages render HTML and escape the text themselves, so they pass
    ``markdown=False`` and receive the bare value.
    """
    text = str(value if value is not None else "").strip()
    keywords = SETTING_VALUE_LABELS.get(str(key))
    if keywords and text in keywords:
        return keywords[text]
    if str(key) in BOOLEAN_SETTINGS and text in {"0", "1"}:
        return BOOLEAN_ON if text == "1" else BOOLEAN_OFF
    if not text:
        return "—"
    return f"`{escape_markdown_code(text)}`" if markdown else text


def settings_category_of(key: str) -> str:
    """Category a setting belongs to; unknown keys land in the catch-all bucket."""
    return SETTINGS_CATEGORY_OF.get(str(key), "other")


def settings_category_title(cat_key: str) -> str:
    if cat_key == "other":
        return SETTINGS_OTHER_TITLE
    return SETTINGS_CATEGORY_TITLES.get(cat_key, "🛠 تنظیمات پیشرفته")


def _settings_category_members(cat_key: str):
    """Live settings rows for one category, in the category's declared order."""
    settings_list = [s for s in database.get_all_settings()
                     if not is_hidden_setting(s["key"])]
    if cat_key == "other":
        return [s for s in settings_list if s["key"] not in SETTINGS_CATEGORY_OF]
    order = {k: i for i, k in enumerate(
        [k for k, c in SETTINGS_CATEGORY_OF.items() if c == cat_key])}
    return sorted([s for s in settings_list if s["key"] in order],
                  key=lambda s: order[s["key"]])


def build_settings_category_page(cat_key: str):
    """Readable Persian page for one settings category plus its button rows.

    Only the Persian title, the readable current value and the practical
    explanation reach the screen: the raw English key stays inside the callback
    data, where the operator never reads it.
    """
    title = settings_category_title(cat_key)
    members = _settings_category_members(cat_key)
    lines = [f"**{title}**\n"]
    rows = []
    if not members:
        lines.append("• پارامتری در این دسته‌بندی ثبت نشده است.")
    for setting in members:
        key = setting["key"]
        label = setting_label(setting)
        # The body is sent as Markdown v1, so every interpolated field is escaped: a
        # single ``_`` in a stored value made Telegram reject the entire page with
        # «Can't parse entities». The button label stays raw — buttons are plain text.
        lines.append(
            f"• **{escape_markdown(label)}**\n"
            f"   مقدار فعلی: {setting_value_display(key, setting['value'])}\n"
            f"   {escape_markdown(setting_description(setting))}")
        rows.append([InlineKeyboardButton(
            f"⚙️ {label}",
            callback_data=f"admin:prompt_setting:{key}:{cat_key}",
        )])
    if cat_key == "group":
        rows.append([InlineKeyboardButton(
            "🔄 ساخت و همگام‌سازی خودکار تاپیک‌ها", callback_data="v52:forums:root")])
    if cat_key == "registration":
        rows.append([InlineKeyboardButton(
            "🔐 سیاست ثبت‌نام، عضویت و کد معرف", callback_data="admin:reg_settings")])
        rows.append([InlineKeyboardButton(
            "⏳ درخواست‌های عضویت در انتظار", callback_data="admin:pending_reg")])
    rows.append([InlineKeyboardButton(
        "↩️ بازگشت به دسته‌بندی تنظیمات", callback_data="admin:settings")])
    return "\n".join(lines), rows


def profile_origin_callback(origin: str = "", ref: str = "") -> str:
    """Encode where a profile page was opened from so its back button returns there.

    ``origin`` names the surface (``tx``, ``rcp``, ``ticket``, ``reg``, ``users``,
    ``reps``, ``abuse``, ``repnet``) and ``ref`` its id. The valid set lives in
    :mod:`profile_360` — the single owner of the 360-degree card — so this module
    cannot drift into accepting an origin the card would then silently drop.
    """
    import profile_360
    surface = str(origin or "").strip()
    if surface not in profile_360.PROFILE_ORIGINS:
        return ""
    reference = str(ref or "").strip()
    if not reference:
        return ""
    return f"{surface}:{reference}"


def build_profile_callback(user_id: int, origin: str = "", ref: str = "") -> str:
    """Callback that opens a user profile and remembers the page it came from.

    The ``admin:user_detail:`` form stays the emitted callback: it is already
    inside messages delivered to operators and is pinned by tests. What changed is
    the destination — the handler now renders the one canonical card from
    :mod:`profile_360` instead of this module's own, poorer copy.
    """
    base = f"admin:user_detail:{int(user_id)}"
    encoded = profile_origin_callback(origin, ref)
    return f"{base}:{encoded}" if encoded else base


def build_user_profile_page(
    user_id: int, *, origin: str = "", ref: str = "",
    allow_management: bool = True,
):
    """Thin delegate to the single 360-degree card in :mod:`profile_360`.

    This module used to carry a second, incomplete profile renderer: no lifetime
    totals, no drill-downs, no internal notes, no tier or promotion controls, and
    a back-button set that only knew four origins. An operator who reached a
    profile from the user list, a receipt, a locked transaction or the
    registration queue therefore saw a *different and poorer* page than one
    reached from the ticket surface — the same person, two cards. The renderer now
    exists only in :mod:`profile_360`; this wrapper keeps the ``admin:`` call sites
    and the existing tests working unchanged.
    """
    import profile_360
    return profile_360.build_profile_page(
        int(user_id), origin=origin, ref=ref, allow_management=allow_management)


def _profile_return(user_id: int, origin: str, ref: str, *, notice: str = ""):
    """Re-render the single 360-degree card after a management action.

    Every management action started from a profile must land the operator back on
    that same card with its originating surface preserved. The wallet, block,
    identity and account handlers used to reply with a hand-rolled summary — or
    with the main menu — which silently dropped the profile's own buttons, so the
    operator had to walk back through the user list to continue.

    Returns ``(text, keyboard)``, or ``None`` when the card cannot be rendered —
    the person no longer exists, or the database is unreachable. A management
    action must never fail *because* the follow-up page could not be built, so the
    caller falls back to the plain notice instead of losing the saved result.
    """
    import profile_360
    try:
        text, rows = profile_360.build_profile_page(
            int(user_id), origin=origin, ref=ref, allow_management=True)
    except Exception:
        logger.debug("Profile re-render failed for %s", user_id, exc_info=True)
        return None
    if not text:
        return None
    body = "\n\n".join(part for part in (str(notice or "").strip(), text) if part)
    return body, InlineKeyboardMarkup(rows)


def _base_role_of(user, chat, thread_id=None) -> tuple[str, dict | None]:
    """The actor's PERMANENT role, ignoring any persona override.

    Management guards must use this: the resolved persona changes with the
    workspace switcher, so a super admin viewing the end-user workspace would
    otherwise be refused by their own admin surfaces.
    """
    try:
        return database.get_user_role(
            getattr(user, "id", None), getattr(chat, "id", None), thread_id)
    except Exception:
        logger.debug("Base-role resolution failed", exc_info=True)
        return "end_user", None


def _pending_intent_card(intent: dict):
    """Readable card for one locked transaction plus its resolution buttons.

    Every dynamic value is escaped: the intent note carries panel text such as
    ``EVIDENCE_REQUIRED``, whose lone underscore used to open an italic entity
    that never closed, so Telegram rejected the whole message with
    ``Can't parse entities`` and the caller surfaced a generic crash alert.
    """
    intent_id = str(intent["intent_id"])
    label = "خرید" if intent.get("action_type") == "buy" else "تمدید"
    payer = escape_markdown(intent.get("full_name") or intent.get("username") or "—")
    state_fa = ("در انتظار تعیین تکلیف مدیریت" if intent.get("status") == "needs_reconciliation"
                else "در حال پردازش")
    lines = [
        f"⏳ **تراکنش معلق و قفل‌شده:**\n",
        f"• شناسه: `{escape_markdown_code(intent_id)}`",
        f"• نوع: **{label}** | بسته: **{escape_markdown(intent.get('plan_name') or '—')}**",
        f"• مبلغ قفل‌شده: **{int(intent.get('amount') or 0):,} تومان**",
        f"• پرداخت‌کننده: **{payer}** (`{int(intent.get('user_id') or 0)}`)",
        f"• وضعیت: **{state_fa}**",
        f"• قفل عملیات: `{escape_markdown_code(intent.get('operation_lock_key') or '—')}`",
        f"• تاریخ: `{escape_markdown_code(str(intent.get('created_at') or '')[:19])}`",
    ]
    note = str(intent.get("result_note") or "").strip()
    if note:
        lines.append(f"• یادداشت سیستم: {escape_markdown(note[:220])}")
    lines.append(
        "\nℹ️ «تایید تحویل» برداشت را نهایی و قفل را آزاد می‌کند؛ "
        "«لغو و بازگشت وجه» مبلغ را اتمیک به کیف پول کاربر برمی‌گرداند."
    )
    rows = [
        [InlineKeyboardButton("👤 مشاهده پروفایل کاربر",
                              callback_data=build_profile_callback(
                                  int(intent.get("user_id") or 0), "tx", intent_id))],
        [InlineKeyboardButton("🔄 استعلام مجدد از پنل",
                              callback_data=f"admin:pending_recheck:{intent_id}")],
        [InlineKeyboardButton("✅ پذیرش و تایید نهایی تراکنش",
                              callback_data=f"admin:pending_confirm:{intent_id}")],
        [InlineKeyboardButton("↩️ رد تراکنش و عودت وجه (Refund)",
                              callback_data=f"admin:pending_refund:{intent_id}")],
        [InlineKeyboardButton("↩️ بازگشت به صف تراکنش‌های معلق",
                              callback_data="admin:pending_tx")],
    ]
    return "\n".join(lines), rows


def _pending_intent_confirm_card(intent: dict, action: str):
    """Two-step confirmation before any money moves."""
    intent_id = str(intent["intent_id"])
    amount = int(intent.get("amount") or 0)
    payer = escape_markdown(
        intent.get("full_name") or intent.get("username") or str(intent.get("user_id")))
    if action == "apply":
        title = "✅ **تایید تحویل و نهایی‌سازی تراکنش**"
        consequence = (
            f"برداشت **{amount:,} تومان** نهایی می‌شود، قفل عملیات آزاد و تراکنش «انجام‌شده» ثبت می‌گردد. "
            "هیچ مبلغی به کیف پول بازنمی‌گردد."
        )
        confirm_cb = f"admin:pending_do_apply:{intent_id}"
        confirm_label = "✅ بله، تحویل را تایید کن"
    else:
        title = "💸 **لغو و بازگشت وجه به کیف پول**"
        consequence = (
            f"مبلغ **{amount:,} تومان** به‌صورت اتمیک به کیف پول **{payer}** بازمی‌گردد، "
            "قفل عملیات آزاد و تراکنش «بازگشت‌شده» ثبت می‌گردد."
        )
        confirm_cb = f"admin:pending_do_refund:{intent_id}"
        confirm_label = "💸 بله، وجه را برگردان"
    text = (
        f"{title}\n\n"
        f"• تراکنش: `{escape_markdown_code(intent_id)}`\n"
        f"• پرداخت‌کننده: **{payer}**\n"
        f"• مبلغ: **{amount:,} تومان**\n\n"
        f"⚠️ {consequence}\n\nآیا مطمئن هستید؟"
    )
    rows = [
        [InlineKeyboardButton(confirm_label, callback_data=confirm_cb)],
        [InlineKeyboardButton("❌ انصراف", callback_data=f"admin:pending_tx:{intent_id}")],
    ]
    return text, rows


def build_pending_transactions_page(user_id: int | None = None):
    """Queue of every transaction still holding a customer's money."""
    rows_data = database.list_unresolved_user_action_intents(limit=30, user_id=user_id)
    lines = ["⏳ **تراکنش‌های معلق و قفل‌شده:**\n"]
    buttons = []
    if not rows_data:
        lines.append("✅ در حال حاضر هیچ تراکنش معلق یا قفل‌شده‌ای وجود ندارد.")
    else:
        total = sum(int(item.get("amount") or 0) for item in rows_data)
        lines.append(f"• تعداد: **{len(rows_data)}** | مجموع مبلغ قفل‌شده: **{total:,} تومان**\n")
        for item in rows_data:
            label = "خرید" if item.get("action_type") == "buy" else "تمدید"
            payer = escape_markdown(item.get("full_name") or item.get("username") or "—")
            lines.append(
                f"• `{escape_markdown_code(str(item['intent_id'])[:10])}` | {label} "
                f"**{escape_markdown(item.get('plan_name') or '—')}** | "
                f"**{int(item.get('amount') or 0):,} ت** | {payer}"
            )
            buttons.append([InlineKeyboardButton(
                f"⏳ {label} {int(item.get('amount') or 0):,} ت — "
                f"{item.get('full_name') or item.get('username') or '—'}"[:60],
                callback_data=f"admin:pending_tx:{item['intent_id']}",
            )])
    return "\n".join(lines), buttons


async def _provision_topics_after_group_change(update, context, group_id_text) -> str:
    """Provision the standard report topics the moment the group id is saved."""
    try:
        from rep_bot import _sync_v52_forum_topics
        group_id = database.normalize_group_chat_id(group_id_text)
        if not group_id:
            return "\n• همگام‌سازی تاپیک‌ها: شناسه گروه صفر است؛ انجام نشد."
        result = await _sync_v52_forum_topics(
            context.bot, int(context.bot.id), int(group_id), owner_key="root",
        )
        if result.get("state") != "completed":
            return f"\n⚠️ همگام‌سازی تاپیک‌ها انجام نشد: `{result.get('reason')}`"
        return (
            f"\n• همگام‌سازی تاپیک‌ها: **{result.get('active', 0)}/{result.get('topics', 0)}** تاپیک فعال"
            f"\n• شناسه‌های ذخیره‌شده در تنظیمات: **{result.get('settings_written', 0)}**"
        )
    except Exception as exc:
        logger.exception("Forum topic provisioning after a group-id change failed")
        return f"\n⚠️ همگام‌سازی تاپیک‌ها ناموفق بود: `{type(exc).__name__}`"


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
            [InlineKeyboardButton("🏷 مدیریت تعرفه‌ها، دسته‌بندی‌ها و پنل‌ها", callback_data="v52:plans"),
             InlineKeyboardButton("🛍 فروشگاه", callback_data="v52:shop")],
            [InlineKeyboardButton("🎫 تیکت‌های پشتیبانی", callback_data="v52:tickets"),
             InlineKeyboardButton("⚙️ تنظیمات تیکت و وضعیت‌ها", callback_data="v52:ticketsettings")],
            [InlineKeyboardButton("🚨 گزارش‌های تخلف", callback_data="v52:abuse:list"),
             InlineKeyboardButton("🎖 سطوح کاربری و قیمت‌گذاری", callback_data="v52:tiers")],
            [InlineKeyboardButton("🧩 همگام‌سازی فروم", callback_data="v52:forums:root")],
            # --- بخش‌های عملیاتی: ادغام‌شده با منوی قدیمی (نه حذف‌شده) ---
            [InlineKeyboardButton("👥 مدیریت نماینده‌ها", callback_data="admin:reps"),
             InlineKeyboardButton("👤 مدیریت کاربران", callback_data="admin:users")],
            [InlineKeyboardButton("📦 تخصیص و اسکوپ", callback_data="admin:scope"),
             InlineKeyboardButton("💰 امور مالی و تسویه‌ها", callback_data="admin:finance")],
            [InlineKeyboardButton("🎁 کدهای تخفیف و کوپن‌ها", callback_data="v52:coupons"),
             InlineKeyboardButton("🔄 همگام‌سازی و آمار", callback_data="admin:sync")],
            [InlineKeyboardButton("⚠️ مانیتورینگ اکانت‌های بحرانی", callback_data="admin:audit"),
             InlineKeyboardButton("🏊 استخر آزاد", callback_data="admin:pool")],
            [InlineKeyboardButton("📦 بسته‌های رزرو در انتظار", callback_data="admin:reserves"),
             InlineKeyboardButton("🛠 تنظیمات پیشرفته سامانه", callback_data="admin:settings")],
            [InlineKeyboardButton("⏳ تراکنش‌های معلق و قفل‌شده", callback_data="admin:pending_tx")],
            [InlineKeyboardButton("💾 پشتیبان‌گیری سریع دیتابیس", callback_data="admin:backup")],
            # --- Persona Switcher ---
            [InlineKeyboardButton("🧑‍💼 ورود به میزکار نمایندگی", callback_data="v52:persona:rep")],
            [InlineKeyboardButton("👤 پورتال کاربری", callback_data="v52:persona:user")],
        ])
    rows = [
        [InlineKeyboardButton("👥 مدیریت نماینده‌ها", callback_data="admin:reps"),
         InlineKeyboardButton("👤 مدیریت کاربران", callback_data="admin:users")],
        [InlineKeyboardButton("📦 تخصیص و اسکوپ", callback_data="admin:scope"),
         InlineKeyboardButton("💰 امور مالی و تسویه‌ها", callback_data="admin:finance")],
        [InlineKeyboardButton("🏷 مدیریت تعرفه‌ها، دسته‌بندی‌ها و پنل‌ها", callback_data="admin:plans"),
         InlineKeyboardButton("🔄 همگام‌سازی و آمار", callback_data="admin:sync")],
        [InlineKeyboardButton("⚠️ مانیتورینگ اکانت‌های بحرانی", callback_data="admin:audit"),
         InlineKeyboardButton("🏊 استخر آزاد", callback_data="admin:pool")],
        [InlineKeyboardButton("📦 بسته‌های رزرو در انتظار", callback_data="admin:reserves"),
         InlineKeyboardButton("🛠 تنظیمات پیشرفته سامانه", callback_data="admin:settings")],
        [InlineKeyboardButton("💾 پشتیبان‌گیری سریع دیتابیس", callback_data="admin:backup")],
    ]
    # The persona switcher is a navigation guarantee, not a v5.2-only feature: the
    # super-admin must always be able to reach the lower workspaces.
    rows.append([InlineKeyboardButton("🧑‍💼 ورود به میزکار نمایندگی",
                                      callback_data="v52:persona:rep")])
    rows.append([InlineKeyboardButton("👤 پورتال کاربری", callback_data="v52:persona:user")])
    return InlineKeyboardMarkup(rows)

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
    from rep_bot import build_actor_navigation_keyboard
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
            await update.message.reply_text(
                notice,
                reply_markup=build_actor_navigation_keyboard(role),
            )
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
        # The dashboard is reached from a receipt, and a receipt is a PHOTO
        # message. Editing its text is illegal, and the old `except BadRequest:
        # pass` swallowed that rejection — the button looked completely dead.
        # message_edit delivers the page as a new message in that case.
        await message_edit.render_page(
            update.callback_query, text=dashboard_text,
            parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard,
        )
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


def _finance_ctx(user, chat) -> dict:
    """The context ``finance_admin_ui`` pages expect from the admin desk.

    ``admin:plans`` already rendered the unified hub with this shape; the plan card
    and the plan mutations need the same one, otherwise the operator would leave the
    unified card for a legacy v4 view that hides the category, kind, audience and
    panel controls.
    """
    return {
        "actor_tg_id": user.id, "telegram_id": user.id, "is_root": True,
        "chat_type": getattr(chat, "type", "private"), "persona": "admin",
    }


def _plan_card_keyboard(plan: dict, plan_code: str, user, chat):
    """The plan card's controls, followed by the unified tariff controls.

    The card's own text and its four edit buttons are pinned by existing tests, so
    they stay exactly as they were; the tariff kind, audience, category and panel
    checkboxes are appended so the card is no longer a half-view of the tariff. The
    footer must remain the last row, which is why the unified rows are inserted
    before ``build_back_to_admin_keyboard`` rather than after it.

    A plan the catalogue cannot resolve (a synthetic record in a test, or a plan
    deleted between the press and the render) leaves the legacy controls in place and
    records the reason instead of failing the whole card.
    """
    rows = [
        [InlineKeyboardButton("✏️ ویرایش قیمت‌ها", callback_data=f"admin:prompt_edit_plan:{plan_code}"),
         InlineKeyboardButton("📐 ویرایش حجم/زمان", callback_data=f"admin:prompt_edit_plan_specs:{plan_code}")],
        [InlineKeyboardButton(
            "⚡️ تغییر حالت سریع/عادی",
            callback_data=f"admin:set_plan_quick:{plan_code}:{0 if plan.get('is_quick') else 1}"
        )],
        [InlineKeyboardButton(
            "⏸ غیرفعال‌سازی موقت تعرفه" if plan.get("is_active", 1) else "🟢 فعال‌سازی تعرفه",
            callback_data=f"admin:set_plan_active:{plan_code}:{0 if plan.get('is_active', 1) else 1}")],
        [InlineKeyboardButton("🗑 حذف این تعرفه", callback_data=f"admin:confirm_del_plan:{plan_code}")],
    ]
    try:
        import finance_admin_ui
        rows.extend(finance_admin_ui.plan_control_rows(_finance_ctx(user, chat), plan_code))
    except Exception:
        logger.warning(
            "Could not render the unified tariff controls for plan %s", plan_code,
            exc_info=True)
    return build_back_to_admin_keyboard(rows, parent_cb="admin:plans")


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
    thread_id = getattr(getattr(update, "effective_message", None), "message_thread_id", None)
    if not user or not chat or chat.type != "private":
        # The receipt-scoped admin callbacks — approve, reject, detail, and the
        # profile / admin-desk buttons that receipt carries — must stay usable
        # from the finance topic of the supergroup, which is where the receipt
        # actually lands. Everything else under ``admin:`` keeps the private-chat
        # requirement, because those pages expose whole-system records.
        from rep_bot import _may_use_admin_callback_in_group
        if not _may_use_admin_callback_in_group(user, chat, thread_id, query.data or ""):
            await query.answer(
                "⛔️ پنل مدیریت فقط در گفت‌وگوی خصوصی ربات قابل استفاده است.",
                show_alert=True
            )
            return
    role, _ = database.get_user_role(user.id, chat.id, thread_id)
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
    if action in {"reg_settings", "reg_mode", "reg_toggle_approval", "pending_reg",
                  "reg_approve", "reg_reject", "reg_approve_all"}:
        from rep_bot import _handle_registration_admin
        await _handle_registration_admin(
            update, context, scope="admin", action=action, sub_action=sub_action)
        return
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
        origin = parts[3] if len(parts) > 3 else ""
        origin_ref = parts[4] if len(parts) > 4 else ""
        base_role, _base_rep = _base_role_of(user, chat, None)
        text, rows = build_user_profile_page(
            user_id, origin=origin, ref=origin_ref,
            allow_management=(base_role == "super_admin"))
        if not text:
            await query.answer("❌ کاربر یافت نشد.", show_alert=True)
            return
        await message_edit.render_page(
            query, text=text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(rows))
        await query.answer("پروفایل ۳۶۰ درجه")

    elif action == "toggle_block_user":
        await query.answer(
            "⚠️ این دکمه قدیمی است و برای جلوگیری از تغییر ناخواسته اجرا نشد. کارت کاربر را دوباره باز کنید.",
            show_alert=True
        )
        return

    elif action == "set_block_user":
        user_id = int(sub_action)
        desired_blocked = parts[3] if len(parts) > 3 else ""
        # The profile carries the surface it was opened from, so the card can be
        # re-rendered in place after the toggle instead of replacing it with a
        # hand-rolled summary that lost every other control.
        origin = parts[4] if len(parts) > 4 else ""
        origin_ref = parts[5] if len(parts) > 5 else ""
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
        rendered = _profile_return(
            user_id, origin, origin_ref,
            notice=f"✅ وضعیت دسترسی کاربر به **{'مسدود' if new_val else 'فعال'}** تغییر یافت.")
        if rendered is None:
            await query.answer("❌ کاربر یافت نشد.", show_alert=True)
            return
        body, keyboard = rendered
        # message_edit, not edit_message_text: the profile is routinely opened from
        # a receipt, which is a PHOTO message whose body is a caption.
        await message_edit.render_page(
            query, text=body, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
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
            [InlineKeyboardButton("🎁 کدهای تخفیف و کوپن‌ها", callback_data="v52:coupons")],
            [InlineKeyboardButton("↩️ لغو و عودت وجه سفارش", callback_data="v52:refunds")],
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

    # 6. Pricing Plans Management — one unified tariff & category hub.
    #
    # ``admin:plans`` and ``v52:plans`` used to render two disconnected menus: this
    # one had CRUD but ignored the category, the tariff kind, the audience and the
    # panel bindings, while the v5.2 menu toggled panel bindings only. Both routes
    # now render the single hub in ``finance_admin_ui``, so an operator can never
    # again edit half of a tariff from one menu and the other half from another.
    elif action == "plans":
        import finance_admin_ui
        page = finance_admin_ui.tariff_hub(
            {"actor_tg_id": user.id, "telegram_id": user.id, "is_root": True,
             "chat_type": getattr(chat, "type", "private"), "persona": "admin"},
            parent_callback="admin:main")
        await query.edit_message_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
        await query.answer("مدیریت تعرفه‌ها، دسته‌بندی‌ها و پنل‌ها")

    elif action == "plan_detail":
        # S59 — the plan card is the unified tariff card, not the legacy v4 view.
        # The old card hid the tariff kind, the audience, the categories and the panel
        # bindings, so a plan reached from the admin desk offered only half of its
        # controls and the operator had to leave for ``v52:plans`` to finish the job.
        import finance_admin_ui
        page = finance_admin_ui.plan_detail(
            _finance_ctx(user, chat), sub_action, parent_callback="admin:plans")
        await query.edit_message_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
        await query.answer("کارت تعرفه")

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
        # S59 — the same unified controls the tariff card carries, so this refresh is
        # not a half-view of the plan either.
        keyboard = _plan_card_keyboard(plan, plan_code, user, chat)
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
        # S59 — the same unified controls the tariff card carries, so this refresh is
        # not a half-view of the plan either.
        keyboard = _plan_card_keyboard(plan, plan_code, user, chat)
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
        # S59 — return to the unified hub, not the legacy v4 list. The old list had no
        # categories, no tariff kind, no audience and no panel bindings, so deleting a
        # plan dropped the operator onto a menu that could not finish the job.
        import finance_admin_ui
        page = finance_admin_ui.tariff_hub(
            _finance_ctx(user, chat), parent_callback="admin:main")
        await query.edit_message_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
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

    # 9. Free Pool Accounts — the real unassigned pool, shown separately from the
    # panel's total client list. The previous version printed only the panel client
    # count and built its text with double-escaped ``\\n``, so the operator saw a
    # literal backslash-n in the message and never saw the pool at all.
    elif action == "pool":
        try:
            with database.get_db() as conn:
                unassigned_total = int(conn.execute(
                    "SELECT COUNT(*) FROM accounts WHERE rep_id IS NULL "
                    "AND owner_tg_id IS NULL AND deleted_at IS NULL").fetchone()[0])
                unassigned_rows = conn.execute(
                    "SELECT account_code, email, enable FROM accounts "
                    "WHERE rep_id IS NULL AND owner_tg_id IS NULL AND deleted_at IS NULL "
                    "ORDER BY id LIMIT 10").fetchall()
                registered_total = int(conn.execute(
                    "SELECT COUNT(*) FROM accounts WHERE deleted_at IS NULL").fetchone()[0])
                manual_available = 0
                manual_rows = []
                try:
                    manual_available = int(conn.execute(
                        "SELECT COUNT(*) FROM manual_pool_clients "
                        "WHERE status='available' AND enabled=1").fetchone()[0])
                    manual_rows = conn.execute(
                        "SELECT p.name AS panel_name, m.external_identifier "
                        "FROM manual_pool_clients m JOIN panels p ON p.id=m.panel_id "
                        "WHERE m.status='available' AND m.enabled=1 "
                        "ORDER BY m.id LIMIT 10").fetchall()
                except Exception:
                    logger.debug("manual pool table is unavailable in the pool view", exc_info=True)
            lines = [
                "🏊 **استخر آزاد و اکانت‌های تخصیص‌نیافته:**",
                "",
                f"• اکانت‌های بدون نماینده و بدون مالک تلگرام: **{unassigned_total}**",
                f"• اکانت‌های آماده در استخر دستی پنل‌ها: **{manual_available}**",
                f"• کل اکانت‌های ثبت‌شده در سامانه: **{registered_total}**",
                "",
                "نمونه اکانت‌های تخصیص‌نیافته (بدون نماینده و بدون مالک):",
            ]
            if not unassigned_rows:
                lines.append("• هیچ اکانت تخصیص‌نیافته‌ای وجود ندارد.")
            for row in unassigned_rows:
                state = "🟢" if row["enable"] else "🛑"
                lines.append(
                    f"{state} `{escape_markdown_code(row['account_code'])}` | "
                    f"`{escape_markdown_code(row['email'])}`"
                )
            lines.append("")
            lines.append("نمونه اکانت‌های آماده استخر دستی:")
            if not manual_rows:
                lines.append("• هیچ اکانت آماده‌ای در استخر دستی ثبت نشده است.")
            for row in manual_rows:
                lines.append(
                    f"• پنل **{escape_markdown(row['panel_name'])}** | "
                    f"`{escape_markdown_code(row['external_identifier'])}`"
                )
            text = "\n".join(lines)
        except Exception:
            logger.exception("Free-pool view could not read the local pool tables")
            text = "⚠️ خواندن جداول استخر آزاد ممکن نشد؛ هیچ fallback مستقیمی اجرا نشد."
        try:
            adapter = _get_admin_panel_adapter(context=context)
            clients = await adapter.list_clients(scope_filter="all")
            text += f"\n\n• تعداد کل کلاینت‌های پنل انتخاب‌شده: **{len(clients)}**"
        except Exception:
            logger.exception("Scoped panel adapter read failed in admin pool view")
            text += "\n\n⚠️ خواندن پنل انتخاب‌شده در اسکوپ root ممکن نشد."
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

    # 11. Dynamic System Settings Panel — grouped, never a flat 17-row wall
    elif action == "settings":
        lines = [
            "🛠 **مرکز تنظیمات پیشرفته سامانه:**\n",
            "پارامترها دسته‌بندی شده‌اند. دسته مورد نظر را انتخاب کنید؛ "
            "تغییرات به صورت آنی (Hot-Reload) و بدون ری‌استارت اعمال می‌شوند:\n"
        ]
        setting_buttons = []
        for cat_key, title, _keys in SETTINGS_CATEGORIES:
            count = len(_settings_category_members(cat_key))
            lines.append(f"• {title}: **{count}** پارامتر")
            setting_buttons.append([InlineKeyboardButton(
                f"{title} ({count})", callback_data=f"admin:settings_cat:{cat_key}")])
        other_count = len(_settings_category_members("other"))
        if other_count:
            lines.append(f"• {SETTINGS_OTHER_TITLE}: **{other_count}** پارامتر")
            setting_buttons.append([InlineKeyboardButton(
                f"{SETTINGS_OTHER_TITLE} ({other_count})", callback_data="admin:settings_cat:other")])
        setting_buttons.append([InlineKeyboardButton(
            "🔄 ساخت و همگام‌سازی خودکار تاپیک‌ها", callback_data="v52:forums:root")])
        keyboard = build_back_to_admin_keyboard(setting_buttons)
        await query.edit_message_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer("تنظیمات سیستم")

    # 11.1 One settings category
    elif action == "settings_cat":
        cat_key = sub_action or "group"
        text, rows = build_settings_category_page(cat_key)
        keyboard = build_back_to_admin_keyboard(rows)
        await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
        await query.answer(settings_category_title(cat_key))

    # 11.2 Pending / locked transaction centre with manual settlement
    elif action in {
        "pending_tx", "pending_recheck", "pending_confirm", "pending_refund",
        "pending_do_apply", "pending_do_refund",
    }:
        intent_id = sub_action
        if action == "pending_tx" and not intent_id:
            text, rows = build_pending_transactions_page()
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                          reply_markup=build_back_to_admin_keyboard(rows))
            await query.answer("تراکنش‌های معلق")
            return

        intent = database.get_unresolved_user_action_intent(intent_id)
        if not intent:
            await query.answer("این تراکنش دیگر معلق نیست؛ فهرست تازه شد.", show_alert=True)
            text, rows = build_pending_transactions_page()
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                          reply_markup=build_back_to_admin_keyboard(rows))
            return

        if action == "pending_tx":
            text, rows = _pending_intent_card(intent)
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                          reply_markup=build_back_to_admin_keyboard(rows))
            await query.answer("جزئیات تراکنش")
            return

        if action == "pending_recheck":
            # Honest re-read: the authoritative state is the intent row plus the
            # operation lock. No panel request is issued here, so no panel effect
            # is ever claimed.
            lock_state = "قفل عملیات آزاد شده است"
            try:
                with database.get_db() as conn:
                    lock = conn.execute(
                        "SELECT token FROM operation_locks WHERE lock_key = ?",
                        (str(intent.get("operation_lock_key") or ""),),
                    ).fetchone()
                lock_state = ("قفل عملیات همچنان برقرار است" if lock
                              else "قفل عملیات آزاد شده است")
            except Exception:
                lock_state = "وضعیت قفل قابل خواندن نبود"
            text = (
                "🔄 **استعلام مجدد (بازخوانی وضعیت مرجع):**\n\n"
                f"• تراکنش: `{intent_id}`\n"
                f"• وضعیت ثبت‌شده: **{intent.get('status')}**\n"
                f"• {lock_state}\n"
                f"• یادداشت سیستم: {str(intent.get('result_note') or '—')[:220]}\n\n"
                "ℹ️ این استعلام فقط وضعیت دیتابیس را بازمی‌خواند و هیچ درخواستی به پنل "
                "ارسال نمی‌کند؛ اثر واقعی در پنل باید مستقیماً در 3X-UI بررسی شود. "
                "پس از بررسی، از دو دکمه پایین تصمیم نهایی را ثبت کنید."
            )
            rows = [
                [InlineKeyboardButton("✅ تایید تحویل و نهایی‌سازی",
                                      callback_data=f"admin:pending_confirm:{intent_id}")],
                [InlineKeyboardButton("💸 لغو و بازگشت وجه به کیف پول",
                                      callback_data=f"admin:pending_refund:{intent_id}")],
                [InlineKeyboardButton("↩️ بازگشت به کارت تراکنش",
                                      callback_data=f"admin:pending_tx:{intent_id}")],
            ]
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                          reply_markup=build_back_to_admin_keyboard(rows))
            await query.answer("وضعیت بازخوانی شد")
            return

        if action in {"pending_confirm", "pending_refund"}:
            kind = "apply" if action == "pending_confirm" else "refund"
            text, rows = _pending_intent_confirm_card(intent, kind)
            await query.edit_message_text(text, parse_mode=ParseMode.MARKDOWN,
                                          reply_markup=build_back_to_admin_keyboard(rows))
            await query.answer("تاییدیه دو مرحله‌ای")
            return

        resolution = "APPLIED" if action == "pending_do_apply" else "REFUND_NOT_APPLIED"
        actor_name = user.full_name or "Super Admin"
        stamp = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())
        verb = "تایید تحویل" if resolution == "APPLIED" else "بازگشت دستی وجه"
        evidence = f"{resolution}: {verb} از پنل تراکنش‌های معلق توسط {actor_name} در {stamp}"
        try:
            result = await asyncio.to_thread(
                database.resolve_user_action_intent,
                intent_id, user.id, actor_name, resolution, evidence,
            )
        except ValueError as exc:
            await query.answer(f"❌ تعیین تکلیف انجام نشد: {exc}", show_alert=True)
            return
        except Exception:
            logger.exception("Manual settlement of intent %s failed", intent_id)
            await query.answer("❌ تعیین تکلیف با خطای غیرمنتظره متوقف شد.", show_alert=True)
            return

        if resolution == "REFUND_NOT_APPLIED":
            try:
                await context.bot.send_message(
                    chat_id=int(result.get("user_id") or intent.get("user_id")),
                    text=(
                        "💸 **بازگشت وجه تراکنش معلق:**\n\n"
                        f"• مبلغ **{int(result.get('amount') or 0):,} تومان** به کیف پول شما بازگشت داده شد.\n"
                        f"• موجودی جدید: **{int(result.get('new_balance') or 0):,} تومان**"
                    ),
                    parse_mode=ParseMode.MARKDOWN,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("💳 کیف پول من", callback_data="user:wallet"),
                    ]]),
                )
            except Exception:
                logger.exception("Could not notify the payer about the manual refund")
            headline = f"✅ وجه تراکنش `{intent_id}` به کیف پول کاربر بازگشت داده شد."
        else:
            headline = f"✅ تراکنش `{intent_id}` به‌عنوان تحویل‌شده نهایی و قفل آن آزاد شد."

        text, rows = build_pending_transactions_page()
        await query.edit_message_text(
            f"{headline}\n\n{text}", parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_back_to_admin_keyboard(rows),
        )
        await query.answer("تعیین تکلیف ثبت شد.")

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
            f"• سطح تعرفه: **{escape_markdown(str(rep.get('tier') or 'normal'))}**",
            f"• سقف اعتبار: `{rep['credit_limit_toman']:,} تومان`",
            f"• بدهی جاری: `{rep['balance_toman']:,} تومان`"
        ]
        if rep.get("parent_rep_id"):
            lines.append(f"• نمایندهٔ والد: `{int(rep['parent_rep_id'])}`")
        keyboard = build_back_to_admin_keyboard([
            [InlineKeyboardButton("👤 پروفایل ۳۶۰ درجه",
                                  callback_data=f"admin:user_detail:{int(rep['telegram_id'])}:reps:{rep_id}"),
             InlineKeyboardButton("👥 زیرنماینده‌های این نماینده",
                                  callback_data=f"v52:subreps:{rep_id}")],
            [InlineKeyboardButton("🟢 تسویه / تعدیل تراز", callback_data=f"admin:prompt_pay:{rep['rep_code']}"),
             InlineKeyboardButton("💳 تغییر سقف اعتبار", callback_data=f"admin:prompt_edit_credit:{rep_id}")],
            [InlineKeyboardButton("✏️ ویرایش مشخصات", callback_data=f"admin:prompt_edit_rep:{rep_id}")],
            [InlineKeyboardButton("⏸ تعلیق / فعال‌سازی", callback_data=f"admin:set_rep_active:{rep_id}:{0 if rep['is_active'] else 1}"),
             InlineKeyboardButton("📦 آرشیو نماینده", callback_data=f"admin:confirm_del_rep:{rep_id}")]
        ], parent_cb="admin:reps")
        await message_edit.render_page(
            query, text="\n".join(lines), parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
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
        # The four user-management prompts are started from a 360-degree card and
        # must return there. The card carries its originating surface inside the
        # callback, so both the prompt's own back button and the page rendered after
        # a successful save land on the profile instead of the user list.
        profile_prompt = action in {
            "prompt_charge_user", "prompt_edit_user_id",
            "prompt_link_user_id", "prompt_unlink_user_acc",
        }
        prompt_origin = parts[3] if profile_prompt and len(parts) > 3 else ""
        prompt_ref = parts[4] if profile_prompt and len(parts) > 4 else ""
        if profile_prompt and prompt_origin and prompt_ref:
            prompt_parent_cb = build_profile_callback(
                int(sub_action), prompt_origin, prompt_ref)
        # S59 — a price or spec edit is started from one plan's card, so finishing or
        # cancelling it must land back on that card instead of dumping the operator on
        # the whole list and losing the plan they were working on.
        if action in ("prompt_edit_plan", "prompt_edit_plan_specs") and sub_action:
            prompt_parent_cb = f"admin:plan_detail:{sub_action}"

        # S59 — the wizard is resolved BEFORE the legacy chain, not after it. Each
        # legacy branch below edits the prompt message and answers the query, so
        # resolving the flow afterwards drew the old prompt first and then rendered
        # the wizard over the same message: a visible double edit on every launch.
        # A flow that can open now owns the message and returns from here.
        if action == "prompt_setting":
            _setting_cat = parts[3] if len(parts) > 3 else settings_category_of(sub_action)
            prompt_parent_cb = f"admin:settings_cat:{_setting_cat}"
            _setting_rows = {s["key"]: s for s in database.get_all_settings()}
            _setting_row = _setting_rows.get(sub_action)
            _wizard_flow_key = (
                _register_setting_wizard(sub_action, setting_label(_setting_row),
                                         _setting_row.get("value"))
                if _setting_row else None)
            _wizard_seed = ({"current_value": _setting_row.get("value")}
                            if _setting_row else {})
        else:
            _wizard_flow_key = ADMIN_WIZARD_FLOW_OF.get(action)
            _wizard_seed = _admin_wizard_seed(action, sub_action, parts)
        # A flow that acts on a specific record cannot open without that record's
        # context; the legacy prompt below then remains the fallback.
        _wizard_needs_seed = action in {
            "prompt_edit_rep", "prompt_edit_credit", "prompt_pay", "prompt_edit_plan",
            "prompt_edit_plan_specs", "prompt_charge_user", "prompt_edit_user_id",
            "prompt_link_user_id", "prompt_unlink_user_acc",
        }
        if _wizard_flow_key and (_wizard_seed or not _wizard_needs_seed):
            _wizard_state = _start_admin_wizard(
                context, query, _wizard_flow_key, prompt_parent_cb,
                seed=_wizard_seed, origin=prompt_origin, ref=prompt_ref)
            await ui_v52.wizard_render(update, context, _wizard_state)
            await query.answer()
            return

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
            context.user_data["admin_state"] = {
                "action": "charge_user", "target_user_id": int(user_id),
                "origin": prompt_origin, "ref": prompt_ref, "ts": now_ts,
            }
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
            cat_key = parts[3] if len(parts) > 3 else settings_category_of(setting_key)
            all_s = {s["key"]: s for s in database.get_all_settings()}
            target_s = all_s.get(setting_key)
            if not target_s:
                await query.answer("❌ تنظیم مورد نظر یافت نشد.", show_alert=True)
                return
            context.user_data["admin_state"] = {
                "action": "edit_setting", "key": setting_key,
                "label": setting_label(target_s), "category": cat_key, "ts": now_ts,
            }
            prompt_parent_cb = f"admin:settings_cat:{cat_key}"
            msg = (
                f"🛠 **ویرایش تنظیمات سامانه:**\n\n"
                f"• عنوان: **{escape_markdown(setting_label(target_s))}**\n"
                f"• توضیح: {escape_markdown(setting_description(target_s))}\n"
                f"• مقدار فعلی: {setting_value_display(setting_key, target_s.get('value'))}\n\n"
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
                "action": "edit_user_id", "target_user_id": target_user_id,
                "origin": prompt_origin, "ref": prompt_ref, "ts": now_ts
            }
            await query.edit_message_text(
                f"✏️ **تغییر شناسه تلگرام کاربر `{target_user_id}`:**\nشناسه عددی جدید را ارسال کنید.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=build_back_to_admin_keyboard(parent_cb=prompt_parent_cb)
            )
            await query.answer()

        elif action == "prompt_link_user_id":
            target_user_id = int(sub_action)
            context.user_data["admin_state"] = {
                "action": "link_user_account", "target_user_id": target_user_id,
                "origin": prompt_origin, "ref": prompt_ref, "ts": now_ts
            }
            await query.edit_message_text(
                f"🔗 **اتصال اکانت به کاربر `{target_user_id}`:**\nکد دقیق اکانت را ارسال کنید.",
                parse_mode=ParseMode.MARKDOWN, reply_markup=build_back_to_admin_keyboard(parent_cb=prompt_parent_cb)
            )
            await query.answer()

        elif action == "prompt_unlink_user_acc":
            target_user_id = int(sub_action)
            context.user_data["admin_state"] = {
                "action": "unlink_user_account", "target_user_id": target_user_id,
                "origin": prompt_origin, "ref": prompt_ref, "ts": now_ts
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

        # S59 — the wizard was resolved and started at the top of this branch, before
        # the legacy prompts, so nothing is rendered twice here. What remains below is
        # the pre-wizard ``admin_state`` fallback, kept for a form that was already in
        # flight when the upgrade landed and for the hand-built states the tests drive.

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
        # Cancelling must return to the card the delete was started from.
        del_origin = parts[3] if len(parts) > 3 else ""
        del_ref = parts[4] if len(parts) > 4 else ""
        u_info = database.get_bot_user_by_tg_id(target_user_id)
        if not u_info:
            await query.answer("کاربر یافت نشد.", show_alert=True)
            return
        keyboard = build_back_to_admin_keyboard([
            [InlineKeyboardButton("🗑 بله، حذف دسترسی کاربر", callback_data=f"admin:do_del_user:{target_user_id}")],
            [InlineKeyboardButton("❌ انصراف",
                                  callback_data=build_profile_callback(target_user_id, del_origin, del_ref))]
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


async def _replace_incoming(update, context, text: str, **kwargs) -> None:
    """Delete the operator's raw input and post the result as one fresh message.

    The admin flows are single-message: the prompt card is edited in place and the raw
    answer is removed, so a search does not leave the query and its result sitting in
    the chat as two separate messages. Telegram refuses a reply whose target was just
    deleted, so the result is always sent as a new message rather than through
    ``update.message.reply_text``. A failed delete (too old, already gone, no
    permission) is logged and never fails the flow.
    """
    chat = getattr(update, "effective_chat", None)
    bot = getattr(context, "bot", None)
    if chat is None or bot is None:
        return
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    message_id = getattr(message, "message_id", None)
    if message_id is not None:
        try:
            await bot.delete_message(chat_id=chat.id, message_id=message_id)
        except Exception as delete_error:
            logger.warning(
                "Could not delete admin input message %s: %s", message_id, delete_error)
    await bot.send_message(chat_id=chat.id, text=text, **kwargs)


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

    if action == "profile_note":
        target_uid = int(admin_state.get("target_user_id") or 0)
        if target_uid <= 0:
            context.user_data.pop("admin_state", None)
            return False
        try:
            database.add_profile_note(
                target_uid, update.effective_user.id,
                update.effective_user.full_name or "Admin", text)
        except ValueError as exc:
            return await _reply_fsm_validation_error(
                update, context, admin_state, f"یادداشت قابل ثبت نیست: {exc}")
        origin = admin_state.get("origin") or ""
        ref = admin_state.get("ref") or ""
        # A scoped representative's note keeps its own control set on the page it
        # lands on; without the viewer the list would offer the root-only
        # «ثبت یادداشت جدید» button back to them.
        scoped_rep_id = admin_state.get("scoped_rep_id")
        context.user_data.pop("admin_state", None)
        import profile_360
        page_text, rows = profile_360.build_notes_page(
            target_uid, origin, ref,
            viewer_rep_id=int(scoped_rep_id) if scoped_rep_id else None)
        await update.message.reply_text(
            "✅ یادداشت داخلی ثبت شد.\n\n" + page_text,
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(rows))
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
            notice = (
                f"✅ **کیف پول کاربر `{target_uid}` بروزرسانی شد.**\n"
                f"• میزان {sign_str}: **{abs(amount):,} تومان**\n"
                f"• مانده جدید: **{new_bal:,} تومان**"
            )
        except ValueError as exc:
            return await _reply_fsm_validation_error(update,context,admin_state,f"مقدار باید عددی و معتبر باشد: {exc}")
        context.user_data.pop("admin_state",None)
        await _reply_profile_after_action(update, admin_state, target_uid, notice)
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
        cat_key = admin_state.get("category") or settings_category_of(setting_key)
        new_val = text.strip()
        try:
            if setting_key in database.GROUP_ID_SETTING_KEYS:
                # Storing the bare ``100…`` form silently breaks every send.
                new_val = str(database.normalize_group_chat_id(new_val))
            database.set_setting(setting_key,new_val,actor_id=update.effective_user.id,actor_name=update.effective_user.full_name or "Admin")
        except ValueError as exc:
            return await _reply_fsm_validation_error(update,context,admin_state,f"مقدار تنظیم معتبر نیست: {exc}")
        context.user_data.pop("admin_state",None)
        extra = ""
        if setting_key == "main_group_id":
            extra = await _provision_topics_after_group_change(update, context, new_val)
        page_text, page_rows = build_settings_category_page(cat_key)
        await update.message.reply_text(
            f"✅ **تنظیمات با موفقیت بروزرسانی شد.**\n\n"
            f"• پارامتر: **{label}** (`{setting_key}`)\n"
            f"• مقدار جدید: `{new_val}`{extra}\n\n"
            f"تغییرات به صورت آنی (Hot-Reload) در سیستم اعمال گردید.\n\n"
            f"{page_text}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_back_to_admin_keyboard(page_rows)
        )
        return True
    
    if action == "pay":
        rep_code = admin_state.get("rep_code")
        rep_info = database.get_rep_by_code(rep_code)
        if not rep_info:
            await update.message.reply_text(
                "❌ نماینده یافت نشد.",
                reply_markup=build_admin_main_keyboard(),
            )
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
            await _replace_incoming(
                update, context, status_text, parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_back_to_admin_keyboard(parent_cb="admin:scope"))
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
            await _replace_incoming(
                update, context, f"🔎 {len(matches)} نتیجه برای جستجوی شما:",
                reply_markup=build_back_to_admin_keyboard(rows, parent_cb="admin:users:1"))
        context.user_data.pop("admin_state", None)
        return True

    elif action == "edit_user_id":
        old_id = int(admin_state["target_user_id"])
        try:
            new_id = int(text)
            if new_id <= 0:
                raise ValueError("Telegram ID must be positive")
            database.change_user_tg_id(old_id, new_id)
            notice = (
                f"✅ شناسه کاربر از `{old_id}` به `{new_id}` تغییر کرد؛ "
                "سوابق و مالکیت‌های مرتبط منتقل شدند."
            )
        except ValueError as exc:
            return await _reply_fsm_validation_error(update,context,admin_state,f"تغییر شناسه انجام نشد: {exc}")
        context.user_data.pop("admin_state",None)
        # The identity changed, so the card is re-rendered for the NEW id: the old
        # one no longer resolves and would render an empty page.
        await _reply_profile_after_action(update, admin_state, new_id, notice)
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
        context.user_data.pop("admin_state",None)
        await _reply_profile_after_action(update, admin_state, target_user_id, response)
        return True

    elif action == "unlink_user_account":
        target_user_id = int(admin_state["target_user_id"])
        unlinked = database.unlink_account_owner(text, target_user_id)
        if not unlinked:
            return await _reply_fsm_validation_error(update,context,admin_state,"اکانت متعلق به این کاربر نیست یا یافت نشد.")
        context.user_data.pop("admin_state",None)
        await _reply_profile_after_action(
            update, admin_state, target_user_id,
            f"✅ اتصال اکانت `{text}` از کاربر `{target_user_id}` قطع شد.")
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



# --------------------------------------------------------------------------- #
# S58 — step-by-step operator wizards
#
# Every ``admin:prompt_*`` form used to be one raw text line: six positional fields
# for a representative, six for a plan, three for a card. One wrong field silently
# reordered the rest and there was no way to see what was about to be written. Each
# form is now a conversation that ends on a preview card, and nothing reaches the
# database until the operator presses «✅ تایید و ثبت نهایی».
#
# The legacy ``admin_state`` handlers below are deliberately kept intact: they still
# serve a form that was already in flight when the upgrade landed, and the states the
# tests build by hand. A wizard only ever starts from the button.
# --------------------------------------------------------------------------- #

_WZ_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")

#: Quick-pick banks for the card form; the operator can still type any other name.
CARD_BANKS: tuple[str, ...] = (
    "ملی", "ملت", "سامان", "پاسارگاد", "صادرات", "تجارت", "کشاورزی", "بلوبانک",
)

#: Quick-pick credit ceilings for a representative, in Toman.
CREDIT_LIMIT_CHOICES: tuple[tuple[str, str], ...] = (
    ("بدون سقف (نامحدود)", "0"),
    ("۱ میلیون تومان", "1000000"),
    ("۵ میلیون تومان", "5000000"),
    ("۱۰ میلیون تومان", "10000000"),
    ("۵۰ میلیون تومان", "50000000"),
)


def _wz_escape(value) -> str:
    """Escape one dynamic value for the wizard's HTML body."""
    from html import escape
    return escape(str(value if value is not None else ""), quote=True)


def _wz_int(raw, label: str, *, minimum=None, maximum=None, allow_negative=False):
    text = str(raw or "").translate(_WZ_DIGITS).replace(",", "").replace("٬", "").strip()
    if not re.fullmatch(r"-?[0-9]+", text or ""):
        raise ValueError(f"{label} باید یک عدد صحیح باشد.")
    value = int(text)
    if not allow_negative and value < 0:
        raise ValueError(f"{label} نمی‌تواند منفی باشد.")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} نباید کمتر از {minimum} باشد.")
    if maximum is not None and value > maximum:
        raise ValueError(f"{label} نباید بیشتر از {maximum} باشد.")
    return value


def _wz_price_input(raw) -> list[int]:
    """Accept one price for a single rate, or ``rep user`` for the «both» choice.

    The step cannot see which rate was chosen — ``parse`` only receives the raw text —
    so the shape of the answer carries it: one number means «the rate I picked», two
    numbers mean «the representative rate, then the user rate». The flow resolves the
    pair against the chosen field in ``commit_edit`` and in the preview.
    """
    # U+066C is the Arabic thousands separator, so «۱۲۰٬۰۰۰» is one number and not two.
    # It is stripped before splitting; «,»/«،»/whitespace still separate two values.
    cleaned = str(raw or "").replace("\u066c", "")
    chunks = [chunk for chunk in re.split(r"[\s,،]+", cleaned) if chunk]
    if not chunks:
        raise ValueError("نرخ نمی‌تواند خالی باشد.")
    if len(chunks) > 2:
        raise ValueError(
            "حداکثر دو عدد وارد کنید: نرخ نماینده و سپس نرخ کاربر.")
    return [_wz_int(chunk, "نرخ", minimum=0) for chunk in chunks]


def _wz_resolve_prices(field: str, new_value, current_rep: int, current_user: int):
    """Resolve the chosen rate(s) into ``(rep_price, user_price)``.

    One implementation for the preview and the write, so the card can never promise a
    change the commit does not make. «both» with a single number sets both rates to
    that number; with a pair it sets each rate to its own.
    """
    numbers = new_value if isinstance(new_value, (list, tuple)) else [new_value]
    numbers = [int(item) for item in numbers if item is not None]
    if not numbers:
        raise ValueError("نرخ جدید مشخص نیست؛ گام قبل را دوباره پر کنید.")
    rep_price, user_price = int(current_rep), int(current_user)
    if field == "rep_price":
        rep_price = numbers[0]
    elif field == "user_price":
        user_price = numbers[0]
    else:
        rep_price = numbers[0]
        user_price = numbers[1] if len(numbers) > 1 else numbers[0]
    return rep_price, user_price


def _wz_gb(raw, label: str = "حجم"):
    text = str(raw or "").translate(_WZ_DIGITS).replace(",", "").strip()
    try:
        value = float(text)
    except (TypeError, ValueError):
        raise ValueError(f"{label} باید یک عدد باشد.")
    if value < 0:
        raise ValueError(f"{label} نمی‌تواند منفی باشد.")
    return value


def _wz_text(raw, label: str, *, maximum: int = 120):
    value = str(raw or "").strip()
    if not value:
        raise ValueError(f"{label} نمی‌تواند خالی باشد.")
    if len(value) > maximum:
        raise ValueError(f"{label} نباید بیشتر از {maximum} نویسه باشد.")
    return value


def _wz_code(raw, label: str = "کد"):
    value = str(raw or "").translate(_WZ_DIGITS).strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,47}", value or ""):
        raise ValueError(
            f"{label} باید با حرف یا رقم لاتین شروع شود و فقط شامل حرف کوچک، رقم، "
            "خط تیره و زیرخط باشد.")
    return value


def _wz_scope(raw):
    value = str(raw or "").strip()
    return "" if value in ("", "-", "بدون", "هیچ", "بدون فیلتر") else value


def _wz_money(value) -> str:
    try:
        return f"{int(value):,} تومان"
    except (TypeError, ValueError):
        return "—"


def _wz_debt_after(values) -> int:
    """The representative's debt once this settlement is applied.

    The preview rendered ``values['debt_after']``, which no step ever set, so the card
    always promised «۰» regardless of the amount. The arithmetic now mirrors
    ``commit_pay``: a payment reduces the debt by the amount received.
    """
    explicit = values.get("debt_after")
    if explicit is not None:
        return int(explicit)
    return int(values.get("current_debt") or 0) - int(values.get("amount") or 0)


def _wz_balance_after(values) -> int:
    """The wallet balance once this manual charge or deduction is applied.

    Same defect as the settlement card: ``balance_after`` was never set, so the operator
    confirmed a change without seeing the resulting balance.
    """
    explicit = values.get("balance_after")
    if explicit is not None:
        return int(explicit)
    return int(values.get("current_balance") or 0) + int(values.get("amount") or 0)


def _wz_rep_tier_choices() -> tuple[tuple[str, str], ...]:
    """The representative tiers the operator can pick, read live from the catalogue."""
    rows: list[tuple[str, str]] = []
    try:
        with database.get_db() as conn:
            if database._table_present(conn, "tier_policies"):
                for row in conn.execute(
                    "SELECT tier_code,title_fa FROM tier_policies WHERE actor_type='rep' "
                    "ORDER BY rank,tier_code"
                ).fetchall():
                    rows.append((str(row["title_fa"]), str(row["tier_code"])))
    except Exception:
        rows = []
    if not rows:
        rows = [("سطح عادی", "normal")]
    return tuple(rows)


def _wz_plan_category_choices() -> tuple[tuple[str, str], ...]:
    try:
        import pricing_service
        with database.get_db() as conn:
            return tuple((f"{row['emoji']} {row['title_fa']}", row["category_key"])
                         for row in pricing_service.list_plan_categories(conn, active_only=True))
    except Exception:
        return ()


def _wz_panel_choices() -> tuple[tuple[str, str], ...]:
    try:
        with database.get_db() as conn:
            return tuple((f"پنل {row['name']}", str(int(row["id"])))
                         for row in conn.execute(
                             "SELECT id,name FROM panels WHERE lifecycle='active' ORDER BY id"
                         ).fetchall())
    except Exception:
        return ()


def _wz_link_new_rep_edge(rep_code: str) -> str:
    """Give a freshly created representative its single active financial edge.

    ``pricing_service._rep_cost`` refuses the whole price chain when that edge is
    missing (``BUG-TIER-001``), so a representative created without it cannot buy,
    renew or quote. The write is best-effort: the representative row exists either
    way, and the operator is told to finish the link from the tree page.
    """
    try:
        import rep_tree_service
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT id FROM representatives WHERE rep_code=?", (rep_code,)).fetchone()
            if row is None:
                return ""
            rep_id = int(row["id"])
            if conn.execute("SELECT 1 FROM rep_edges WHERE child_rep_id=? AND active=1",
                            (rep_id,)).fetchone() is not None:
                return ""
            rep_tree_service.link_rep_edge(conn, rep_id, None)
            conn.commit()
        return ""
    except Exception as exc:
        logger.warning("Could not link the financial edge for %s: %s", rep_code, exc)
        return ("\n\n⚠️ اتصال مالی این نماینده ثبت نشد؛ از «درخت نمایندگان» آن را تکمیل کنید.")


# --- the flows -------------------------------------------------------------- #

def _register_rep_wizards() -> None:
    """Register the add/edit representative flows with the live tier catalogue."""
    tier_choices = _wz_rep_tier_choices()

    def commit_add(values, actor):
        rep_code = str(values["rep_code"])
        name = str(values["name"])
        telegram_id = int(values["telegram_id"])
        database.add_or_update_rep(rep_code, name, telegram_id, 0, 0,
                                   str(values.get("scope_filter") or ""))
        database.log_audit(
            rep_id=None, account_code=rep_code, email=None, action="add_rep",
            actor_id=int(actor.get("id") or 0), actor_name=actor.get("name") or "Admin",
            details={"name": name, "telegram_id": telegram_id,
                     "tier": values.get("tier"), "scope": values.get("scope_filter") or ""},
            result_status="success")
        extra = _wz_link_new_rep_edge(rep_code)
        limit = int(values.get("credit_limit") or 0)
        if limit or str(values.get("tier") or "normal") != "normal":
            with database.get_db() as conn:
                row = conn.execute("SELECT id FROM representatives WHERE rep_code=?",
                                   (rep_code,)).fetchone()
                if row is not None:
                    conn.execute("UPDATE representatives SET tier=? WHERE id=?",
                                 (str(values.get("tier") or "normal"), int(row["id"])))
                    conn.commit()
            if limit:
                with database.get_db() as conn:
                    row = conn.execute("SELECT id FROM representatives WHERE rep_code=?",
                                       (rep_code,)).fetchone()
                if row is not None:
                    database.update_rep_credit_limit(int(row["id"]), limit)
        return (f"نماینده {name} با کد {rep_code} ثبت شد.\n"
                f"شناسه تلگرام: {telegram_id}\n"
                f"سطح: {values.get('tier') or 'normal'}\n"
                f"سقف اعتبار: {'نامحدود' if not limit else _wz_money(limit)}" + extra)

    ui_v52.register_flow(ui_v52.WizardFlow(
        "admin_add_rep", "افزودن نماینده جدید",
        steps=(
            ui_v52.WizardStep("name", "نام نمایشی نماینده",
                              hint="نام می‌تواند فاصله داشته باشد.", parse=lambda raw: _wz_text(raw, "نام نمایشی")),
            ui_v52.WizardStep("rep_code", "کد نماینده",
                              hint="کد لاتین یکتا؛ می‌توانید خودکار بسازید.",
                              parse=lambda raw: _wz_code(raw, "کد نماینده"),
                              generate=lambda values: "rep_" + uuid.uuid4().hex[:8]),
            ui_v52.WizardStep("telegram_id", "شناسه تلگرام نماینده",
                              parse=lambda raw: _wz_int(raw, "شناسه تلگرام", minimum=1)),
            ui_v52.WizardStep("tier", "سطح تعرفه نماینده", choices=tier_choices),
            ui_v52.WizardStep("credit_limit", "سقف اعتبار", choices=CREDIT_LIMIT_CHOICES,
                              parse=lambda raw: _wz_int(raw, "سقف اعتبار", minimum=0)),
            ui_v52.WizardStep("scope_filter", "پیشوند فیلتر اسکوپ",
                              hint="اگر نماینده محدود به یک اسکوپ نیست، این گام را رد کنید.",
                              skippable=True, skip_label="⏭ بدون فیلتر اسکوپ",
                              parse=_wz_scope),
        ),
        preview=lambda v: "\n".join([
            f"• نام: <b>{_wz_escape(v.get('name'))}</b>",
            f"• کد: <code>{_wz_escape(v.get('rep_code'))}</code>",
            f"• شناسه تلگرام: <code>{_wz_escape(v.get('telegram_id'))}</code>",
            f"• سطح تعرفه: <b>{_wz_escape(v.get('tier'))}</b>",
            f"• سقف اعتبار: <b>{'نامحدود' if not int(v.get('credit_limit') or 0) else _wz_money(v.get('credit_limit'))}</b>",
            f"• فیلتر اسکوپ: {_wz_escape(v.get('scope_filter') or 'بدون فیلتر')}",
        ]),
        commit=commit_add,
        legacy=_legacy_add_rep,
    ))

    def commit_edit(values, actor):
        rep_id = int(values["rep_id"])
        current = database.get_rep_by_id(rep_id)
        if not current:
            raise ValueError("نماینده موردنظر یافت نشد.")
        field = str(values.get("field"))
        if field not in EDIT_REP_FIELD_FA:
            raise ValueError("مشخصه انتخابی معتبر نیست؛ فرم را دوباره باز کنید.")
        # S59 — the value is coerced for the chosen field before it is written, and the
        # notice reports the record's real previous value. It used to copy the seed's
        # empty ``previous_value`` into the message and to write the raw text straight
        # into an integer column.
        new_value = _parse_edit_rep_value(values.get("new_value"), field)
        merged = {
            "rep_code": current.get("rep_code") or "",
            "name": current.get("name") or "",
            "telegram_id": int(current.get("telegram_id") or 0),
            "group_id": int(current.get("group_id") or 0),
            "topic_id": int(current.get("topic_id") or 0),
            "scope_filter": current.get("scope_filter") or "",
        }
        previous = merged.get(field)
        merged[field] = new_value
        database.update_rep_full(
            rep_id, merged["name"], merged["rep_code"], int(merged["telegram_id"]),
            int(merged["group_id"]), int(merged["topic_id"]), merged["scope_filter"])
        return (f"مشخصات نماینده {merged['name']} به‌روز شد.\n"
                f"«{EDIT_REP_FIELD_FA.get(field, field)}» از "
                f"«{_wz_escape(previous)}» به "
                f"«{_wz_escape(new_value)}» تغییر کرد.")

    ui_v52.register_flow(ui_v52.WizardFlow(
        "admin_edit_rep", "ویرایش مشخصات نماینده",
        steps=(
            ui_v52.WizardStep("field", "کدام مشخصه را ویرایش می‌کنید؟",
                              choices=EDIT_REP_FIELD_CHOICES),
            ui_v52.WizardStep("new_value", "مقدار جدید", parse=_parse_edit_rep_value),
        ),
        preview=lambda v: "\n".join([
            f"• نماینده: <b>{_wz_escape(v.get('rep_name'))}</b> "
            f"(<code>{_wz_escape(v.get('rep_code'))}</code>)",
            f"• مشخصه: <b>{_wz_escape(EDIT_REP_FIELD_FA.get(str(v.get('field')), v.get('field')))}</b>",
            f"• مقدار قبلی: {_wz_escape(_wz_rep_previous_value(v))}",
            f"• مقدار جدید: <b>{_wz_escape(_wz_rep_preview_value(v))}</b>",
        ]),
        commit=commit_edit,
        legacy=lambda raw: None,
    ))


#: The representative fields the edit wizard can change, with their Persian titles.
#: The forum binding is two separate columns, so it is two separate choices: a single
#: «گروه/تاپیک» entry could never write both ``group_id`` and ``topic_id``.
EDIT_REP_FIELD_CHOICES: tuple[tuple[str, str], ...] = (
    ("✏️ ویرایش نام", "name"),
    ("✏️ ویرایش کد", "rep_code"),
    ("✏️ ویرایش آیدی تلگرام", "telegram_id"),
    ("✏️ ویرایش شناسه گروه", "group_id"),
    ("✏️ ویرایش شناسه تاپیک", "topic_id"),
    ("✏️ ویرایش فیلتر اسکوپ", "scope_filter"),
)
EDIT_REP_FIELD_FA = {
    "name": "نام نمایشی",
    "rep_code": "کد نماینده",
    "telegram_id": "شناسه تلگرام",
    "group_id": "شناسه گروه",
    "topic_id": "شناسه تاپیک",
    "scope_filter": "فیلتر اسکوپ",
}

#: The fields that must reach the database as integers; everything else is free text.
_REP_INTEGER_FIELDS = frozenset({"telegram_id", "group_id", "topic_id"})


def _parse_edit_rep_value(raw, field: str | None = None):
    """Parse the new value for a representative field.

    At step 1 the field is unknown — ``parse`` only receives the raw text — so only the
    shape valid for *every* field is enforced there (a non-empty string). Once the field
    is known, on the preview card and in ``commit_edit``, the value is coerced for that
    field, so a Telegram, group or topic id can never reach the database as text.
    """
    text = str(raw if raw is not None else "").strip()
    if not text:
        raise ValueError("مقدار جدید نمی‌تواند خالی باشد.")
    if field is None:
        return text
    if field in _REP_INTEGER_FIELDS:
        return _wz_int(text, EDIT_REP_FIELD_FA.get(field, "شناسه"), minimum=0)
    return _wz_text(text, EDIT_REP_FIELD_FA.get(field, "مقدار جدید"))


def _wz_rep_previous_value(values) -> str:
    """The chosen field's real current value, so the preview never shows a blank."""
    current = values.get("current") or {}
    value = current.get(str(values.get("field") or ""), "")
    text = str(value)
    return text if text.strip() else "—"


def _wz_rep_preview_value(values) -> str:
    """The new value as it will be stored, or the reason it cannot be stored yet.

    Returning the reason inside the card keeps the confirm/edit/cancel buttons live, so
    the operator can fix the value instead of losing the whole form to an error page.
    """
    try:
        return str(_parse_edit_rep_value(
            values.get("new_value"), str(values.get("field") or "") or None))
    except (TypeError, ValueError) as exc:
        return f"⚠️ {exc}"


def _legacy_add_rep(raw):
    """Accept the pre-wizard ``code name tg group topic scope`` line at step 1."""
    parts = str(raw or "").split(maxsplit=5)
    if len(parts) < 6:
        return None
    try:
        return {"rep_code": _wz_code(parts[0], "کد نماینده"), "name": parts[1],
                "telegram_id": int(parts[2].translate(_WZ_DIGITS)),
                "tier": "normal", "credit_limit": 0, "scope_filter": parts[5]}
    except (ValueError, TypeError):
        return None


def _register_plan_wizards() -> None:
    """Register the add/edit plan flows with the live category and panel catalogues."""
    category_choices = _wz_plan_category_choices()
    panel_choices = _wz_panel_choices()

    def commit_add(values, actor):
        plan_code = str(values["plan_code"])
        database.add_or_update_plan(
            plan_code, str(values["name"]), float(values["gb"]), int(values["days"]),
            int(values["rep_price"]), int(values["user_price"]))
        linked = _apply_plan_selection(plan_code, values)
        return (f"تعرفه {values['name']} با کد {plan_code} ثبت شد.\n"
                f"مشخصات: {float(values['gb']):g} گیگ / {int(values['days'])} روز\n"
                f"قیمت نماینده: {_wz_money(values['rep_price'])}\n"
                f"قیمت کاربر: {_wz_money(values['user_price'])}\n"
                f"نوع: {PLAN_KIND_FA_FALLBACK.get(str(values.get('plan_kind')), 'حجمی/زمانی ثابت')}"
                + linked)

    ui_v52.register_flow(ui_v52.WizardFlow(
        "admin_add_plan", "تعریف تعرفه جدید",
        steps=(
            ui_v52.WizardStep("plan_kind", "نوع تعرفه", choices=(
                ("📦 حجمی/زمانی ثابت", "fixed"),
                ("♾ نامحدود", "unlimited"),
                ("🎚 دلخواه", "custom"),
            )),
            ui_v52.WizardStep("name", "نام فارسی تعرفه",
                              parse=lambda raw: _wz_text(raw, "نام تعرفه")),
            ui_v52.WizardStep("plan_code", "کد تعرفه",
                              hint="کد لاتین یکتا؛ می‌توانید خودکار بسازید.",
                              parse=lambda raw: _wz_code(raw, "کد تعرفه"),
                              generate=_generate_plan_code),
            ui_v52.WizardStep("gb", "حجم (گیگابایت)", parse=lambda raw: _wz_gb(raw)),
            ui_v52.WizardStep("days", "مدت (روز)",
                              parse=lambda raw: _wz_int(raw, "مدت", minimum=1, maximum=3650)),
            ui_v52.WizardStep("rep_price", "قیمت پایه نماینده (تومان)",
                              parse=lambda raw: _wz_int(raw, "قیمت نماینده", minimum=0)),
            ui_v52.WizardStep("user_price", "قیمت پایه کاربر (تومان)",
                              parse=lambda raw: _wz_int(raw, "قیمت کاربر", minimum=0)),
            ui_v52.WizardStep("categories", "دسته‌بندی‌های تعرفه",
                              hint="هر تعداد دسته را انتخاب کنید و سپس «پایان انتخاب» را بزنید.",
                              choices=category_choices, multi=True, skippable=True,
                              skip_label="⏭ بدون دسته‌بندی"),
            ui_v52.WizardStep("panels", "پنل‌های مجاز این تعرفه",
                              hint="هر تعداد پنل را انتخاب کنید و سپس «پایان انتخاب» را بزنید.",
                              choices=panel_choices, multi=True, skippable=True,
                              skip_label="⏭ همهٔ پنل‌ها"),
        ),
        preview=lambda v: "\n".join([
            f"• نام: <b>{_wz_escape(v.get('name'))}</b>",
            f"• کد: <code>{_wz_escape(v.get('plan_code'))}</code>",
            f"• نوع: <b>{_wz_escape(PLAN_KIND_FA_FALLBACK.get(str(v.get('plan_kind')), v.get('plan_kind')))}</b>",
            f"• حجم و مدت: <b>{float(v.get('gb') or 0):g} گیگ / {int(v.get('days') or 0)} روز</b>",
            f"• قیمت نماینده: <b>{_wz_money(v.get('rep_price'))}</b>",
            f"• قیمت کاربر: <b>{_wz_money(v.get('user_price'))}</b>",
            f"• دسته‌بندی: {_wz_escape(_wz_category_titles(v.get('categories')))}",
            f"• پنل‌های مجاز: {_wz_escape(_wz_panel_titles(v.get('panels')))}",
        ]),
        commit=commit_add,
        legacy=_legacy_add_plan,
    ))

    def commit_edit(values, actor):
        plan_code = str(values["plan_code"])
        plan = database.get_plan(plan_code)
        if not plan:
            raise ValueError("تعرفه موردنظر یافت نشد.")
        # S59 — «هر دو نرخ» used to read the seed's old prices straight back out of
        # ``values["rep_price"]``/``values["user_price"]``, so choosing it interactively
        # changed nothing at all. The preview and the write now both resolve the answer
        # through one helper, so the card cannot promise a change the commit omits.
        rep_price, user_price = _wz_resolve_prices(
            str(values.get("field")), values.get("new_value"),
            int(plan["rep_price"]), int(plan["user_price"]))
        database.update_plan_price(plan_code, rep_price, user_price)
        return (f"نرخ‌های تعرفه {plan.get('name')} به‌روز شد.\n"
                f"قیمت نماینده: {_wz_money(rep_price)}\n"
                f"قیمت کاربر: {_wz_money(user_price)}")

    ui_v52.register_flow(ui_v52.WizardFlow(
        "admin_edit_plan", "ویرایش نرخ تعرفه",
        steps=(
            ui_v52.WizardStep("field", "کدام نرخ را ویرایش می‌کنید؟", choices=(
                ("✏️ قیمت نماینده", "rep_price"),
                ("✏️ قیمت کاربر", "user_price"),
                ("✏️ هر دو نرخ", "both"),
            )),
            ui_v52.WizardStep("new_value", "نرخ جدید (تومان)",
                              hint="برای «هر دو نرخ» می‌توانید دو عدد بدهید: نرخ نماینده و سپس نرخ کاربر.",
                              parse=_wz_price_input),
        ),
        preview=lambda v: "\n".join([
            f"• تعرفه: <b>{_wz_escape(v.get('plan_name'))}</b> "
            f"(<code>{_wz_escape(v.get('plan_code'))}</code>)",
            f"• نرخ فعلی نماینده: {_wz_money(v.get('current_rep_price'))}",
            f"• نرخ فعلی کاربر: {_wz_money(v.get('current_user_price'))}",
            f"• تغییر: <b>{_wz_escape(_wz_price_change_label(v))}</b>",
        ]),
        commit=commit_edit,
        legacy=_legacy_edit_plan,
    ))

    def commit_specs(values, actor):
        plan_code = str(values["plan_code"])
        database.update_plan_specs(plan_code, str(values["name"]),
                                   float(values["gb"]), int(values["days"]))
        return (f"حجم، زمان و نام تعرفه {values['name']} به‌روز شد.\n"
                f"مشخصات جدید: {float(values['gb']):g} گیگ / {int(values['days'])} روز")

    ui_v52.register_flow(ui_v52.WizardFlow(
        "admin_edit_plan_specs", "ویرایش حجم و زمان تعرفه",
        steps=(
            ui_v52.WizardStep("gb", "حجم جدید (گیگابایت)", parse=lambda raw: _wz_gb(raw)),
            ui_v52.WizardStep("days", "مدت جدید (روز)",
                              parse=lambda raw: _wz_int(raw, "مدت", minimum=1, maximum=3650)),
            ui_v52.WizardStep("name", "نام فارسی تعرفه",
                              parse=lambda raw: _wz_text(raw, "نام تعرفه")),
        ),
        preview=lambda v: "\n".join([
            f"• تعرفه: <b>{_wz_escape(v.get('plan_name'))}</b> "
            f"(<code>{_wz_escape(v.get('plan_code'))}</code>)",
            f"• حجم قبلی ← جدید: {_wz_escape(v.get('previous_gb'))} ← <b>{float(v.get('gb') or 0):g}</b> گیگ",
            f"• مدت قبلی ← جدید: {_wz_escape(v.get('previous_days'))} ← <b>{int(v.get('days') or 0)}</b> روز",
            f"• نام: {_wz_escape(v.get('previous_name'))} ← <b>{_wz_escape(v.get('name'))}</b>",
        ]),
        commit=commit_specs,
        legacy=_legacy_edit_plan_specs,
    ))


PLAN_KIND_FA_FALLBACK = {"fixed": "حجمی/زمانی ثابت", "unlimited": "نامحدود", "custom": "دلخواه"}


def _generate_plan_code(values) -> str:
    """Derive a plan code from the quota, exactly as the old prompt suggested."""
    gb = values.get("gb")
    days = values.get("days")
    if gb is None or days is None:
        return "plan_" + uuid.uuid4().hex[:8]
    quota = f"{float(gb):g}".replace(".", "p")
    return f"{quota}gb_{int(days)}d"


def _wz_category_titles(keys) -> str:
    if not keys:
        return "بدون دسته‌بندی"
    mapping = dict(_wz_plan_category_choices())
    return "، ".join(mapping.get(str(key), str(key)) for key in keys)


def _wz_panel_titles(ids) -> str:
    if not ids:
        return "همهٔ پنل‌ها"
    mapping = dict(_wz_panel_choices())
    return "، ".join(mapping.get(str(item), f"پنل {item}") for item in ids)


def _wz_price_change_label(values) -> str:
    """The preview's change line, resolved exactly as ``commit_edit`` will write it."""
    try:
        rep_price, user_price = _wz_resolve_prices(
            str(values.get("field")), values.get("new_value"),
            int(values.get("current_rep_price") or 0),
            int(values.get("current_user_price") or 0))
    except (TypeError, ValueError):
        return "در انتظار نرخ جدید"
    field = str(values.get("field"))
    if field == "rep_price":
        return f"قیمت نماینده ← {_wz_money(rep_price)}"
    if field == "user_price":
        return f"قیمت کاربر ← {_wz_money(user_price)}"
    return (f"قیمت نماینده ← {_wz_money(rep_price)} و "
            f"قیمت کاربر ← {_wz_money(user_price)}")


def _apply_plan_selection(plan_code: str, values) -> str:
    """Attach the chosen categories and panel bindings to a freshly created plan.

    The tariff hub owns the same relations, so this reuses its service rather than
    writing the link tables directly — one implementation, one set of invariants.
    """
    notes: list[str] = []
    categories = list(values.get("categories") or [])
    panels = list(values.get("panels") or [])
    try:
        import pricing_service
        with database.get_db() as conn:
            if categories:
                pricing_service.set_plan_categories(conn, plan_code, categories)
            for panel_id in panels:
                pricing_service.set_plan_panel_allowed(conn, plan_code, int(panel_id), True)
            conn.commit()
    except Exception as exc:
        logger.warning("Could not attach the plan selection for %s: %s", plan_code, exc)
        notes.append("\n\n⚠️ دسته‌بندی یا پنل‌های این تعرفه ثبت نشد؛ از کارت تعرفه تکمیل کنید.")
    return "".join(notes)


def _legacy_add_plan(raw):
    """Accept the pre-wizard ``code name gb days rep_price user_price`` line."""
    parts = str(raw or "").split()
    if len(parts) < 6:
        return None
    try:
        numeric = parts[-4:]
        return {"plan_kind": "fixed", "plan_code": _wz_code(parts[0], "کد تعرفه"),
                "name": " ".join(parts[1:-4]).strip(),
                "gb": float(numeric[0].translate(_WZ_DIGITS)),
                "days": int(numeric[1].translate(_WZ_DIGITS)),
                "rep_price": int(numeric[2].replace(",", "").translate(_WZ_DIGITS)),
                "user_price": int(numeric[3].replace(",", "").translate(_WZ_DIGITS)),
                "categories": [], "panels": []}
    except (ValueError, TypeError):
        return None


def _legacy_edit_plan(raw):
    """Accept the pre-wizard ``rep_price user_price`` line at step 1."""
    parts = str(raw or "").split()
    if len(parts) < 2:
        return None
    try:
        return {"field": "both", "new_value": _wz_price_input(" ".join(parts[:2]))}
    except (ValueError, TypeError):
        return None


def _legacy_edit_plan_specs(raw):
    """Accept the pre-wizard ``gb days name`` line."""
    parts = str(raw or "").split(maxsplit=2)
    if len(parts) != 3:
        return None
    try:
        return {"gb": float(parts[0].translate(_WZ_DIGITS)),
                "days": int(parts[1].translate(_WZ_DIGITS)), "name": parts[2]}
    except (ValueError, TypeError):
        return None


def _register_money_wizards() -> None:
    """Register the card, settlement, wallet, credit and identity flows."""

    def commit_card(values, actor):
        number = str(values["card_number"])
        holder = str(values["card_holder"])
        bank = str(values["bank_name"])
        database.update_card_info(number, holder, bank)
        return (f"اطلاعات کارت بانکی ویرایش شد.\n"
                f"شماره کارت: {number}\nصاحب حساب: {holder}\nبانک: {bank}\n\n"
                "این کارت هم در تنظیمات و هم در فهرست کارت‌های پرداخت ثبت شد.")

    ui_v52.register_flow(ui_v52.WizardFlow(
        "admin_card_settings", "تنظیمات شماره کارت بانکی",
        steps=(
            ui_v52.WizardStep("card_number", "شماره ۱۶ رقمی کارت",
                              hint="می‌توانید با خط تیره یا فاصله بنویسید.",
                              parse=lambda raw: parse_card_settings_input(raw)[0]),
            ui_v52.WizardStep("card_holder", "نام صاحب حساب",
                              parse=lambda raw: _wz_text(raw, "نام صاحب حساب", maximum=80)),
            ui_v52.WizardStep("bank_name", "نام بانک",
                              choices=tuple((name, name) for name in CARD_BANKS),
                              parse=lambda raw: _wz_text(raw, "نام بانک", maximum=60)),
        ),
        preview=lambda v: "\n".join([
            f"• شماره کارت: <code>{_wz_escape(v.get('card_number'))}</code>",
            f"• صاحب حساب: <b>{_wz_escape(v.get('card_holder'))}</b>",
            f"• بانک: <b>{_wz_escape(v.get('bank_name'))}</b>",
        ]),
        commit=commit_card,
        legacy=_legacy_card_settings,
    ))

    def commit_pay(values, actor):
        rep_code = str(values["rep_code"])
        rep = database.get_rep_by_code(rep_code)
        if not rep:
            raise ValueError("نماینده موردنظر یافت نشد.")
        amount = int(values["amount"])
        description = str(values.get("description") or "تسویه حساب نماینده")
        new_balance = database.register_rep_payment(
            rep_id=rep["id"], amount=amount, description=description,
            actor_id=int(actor.get("id") or 0), actor_name=actor.get("name") or "Admin")
        return (f"واریزی ثبت شد.\n"
                f"نماینده: {rep['name']}\n"
                f"مبلغ تسویه‌شده: {_wz_money(amount)}\n"
                f"شرح: {description}\n"
                f"مانده بدهی جدید: {_wz_money(new_balance)}")

    ui_v52.register_flow(ui_v52.WizardFlow(
        "admin_pay", "ثبت تسویه حساب نماینده",
        steps=(
            ui_v52.WizardStep("amount", "مبلغ واریزی (تومان)",
                              parse=lambda raw: _wz_int(raw, "مبلغ", minimum=1)),
            ui_v52.WizardStep("description", "شرح واریزی",
                              hint="اگر شرح خاصی ندارید این گام را رد کنید.",
                              skippable=True, skip_label="⏭ ثبت با شرح پیش‌فرض",
                              skip_value="تسویه حساب نماینده",
                              parse=lambda raw: _wz_text(raw, "شرح", maximum=200)),
        ),
        preview=lambda v: "\n".join([
            f"• نماینده: <b>{_wz_escape(v.get('rep_name'))}</b> "
            f"(<code>{_wz_escape(v.get('rep_code'))}</code>)",
            f"• بدهی فعلی: <b>{_wz_money(v.get('current_debt'))}</b>",
            f"• مبلغ واریزی: <b>{_wz_money(v.get('amount'))}</b>",
            f"• بدهی پس از تسویه: <b>{_wz_money(_wz_debt_after(v))}</b>",
            f"• شرح: {_wz_escape(v.get('description') or 'تسویه حساب نماینده')}",
        ]),
        commit=commit_pay,
        legacy=_legacy_pay,
    ))

    def commit_charge(values, actor):
        target = int(values["user_id"])
        amount = int(values["amount"])
        new_balance = database.update_user_wallet_manual(
            target, amount, "شارژ / کسر دستی توسط ادمین کل",
            actor_id=int(actor.get("id") or 0), actor_name=actor.get("name") or "Admin")
        return (f"کیف پول کاربر {target} بروزرسانی شد.\n"
                f"میزان {'افزایش' if amount >= 0 else 'کاهش'}: {_wz_money(abs(amount))}\n"
                f"مانده جدید: {_wz_money(new_balance)}")

    ui_v52.register_flow(ui_v52.WizardFlow(
        "admin_charge_user", "شارژ یا کسر دستی کیف پول",
        steps=(
            ui_v52.WizardStep("amount", "مبلغ (تومان)",
                              hint="عدد مثبت شارژ می‌کند و عدد منفی کسر می‌کند.",
                              parse=lambda raw: _wz_int(raw, "مبلغ", allow_negative=True)),
        ),
        preview=lambda v: "\n".join([
            f"• کاربر: <code>{_wz_escape(v.get('user_id'))}</code>",
            f"• مانده فعلی: <b>{_wz_money(v.get('current_balance'))}</b>",
            f"• تغییر: <b>{_wz_money(v.get('amount'))}</b>",
            f"• مانده پس از اعمال: <b>{_wz_money(_wz_balance_after(v))}</b>",
        ]),
        commit=commit_charge,
        legacy=_legacy_charge_user,
    ))

    def commit_credit(values, actor):
        rep_id = int(values["rep_id"])
        limit = int(values["limit"])
        database.update_rep_credit_limit(rep_id, limit)
        return (f"سقف اعتبار نماینده بروزرسانی شد.\n"
                f"سقف جدید: {'نامحدود' if limit <= 0 else _wz_money(limit)}")

    ui_v52.register_flow(ui_v52.WizardFlow(
        "admin_edit_credit", "تغییر سقف اعتبار نماینده",
        steps=(
            ui_v52.WizardStep("limit", "سقف اعتبار جدید", choices=CREDIT_LIMIT_CHOICES,
                              parse=lambda raw: _wz_int(raw, "سقف اعتبار", minimum=0)),
        ),
        preview=lambda v: "\n".join([
            f"• نماینده: <b>{_wz_escape(v.get('rep_name'))}</b>",
            f"• سقف فعلی: {_wz_money(v.get('current_limit'))}",
            f"• سقف جدید: <b>{'نامحدود' if not int(v.get('limit') or 0) else _wz_money(v.get('limit'))}</b>",
        ]),
        commit=commit_credit,
        legacy=_legacy_edit_credit,
    ))

    def commit_user_id(values, actor):
        old_id = int(values["user_id"])
        new_id = int(values["new_id"])
        database.change_user_tg_id(old_id, new_id)
        return (f"شناسه کاربر از {old_id} به {new_id} تغییر کرد؛ "
                "سوابق و مالکیت‌های مرتبط منتقل شدند.")

    ui_v52.register_flow(ui_v52.WizardFlow(
        "admin_edit_user_id", "تغییر شناسه تلگرام کاربر",
        steps=(
            ui_v52.WizardStep("new_id", "شناسه عددی جدید",
                              parse=lambda raw: _wz_int(raw, "شناسه جدید", minimum=1)),
        ),
        preview=lambda v: "\n".join([
            f"• شناسه فعلی: <code>{_wz_escape(v.get('user_id'))}</code>",
            f"• شناسه جدید: <code>{_wz_escape(v.get('new_id'))}</code>",
        ]),
        commit=commit_user_id,
        legacy=_legacy_edit_user_id,
    ))

    def commit_link_account(values, actor):
        target = int(values["user_id"])
        code = str(values["account_code"])
        linked = database.link_user_to_account(code, target, allow_reassign=True)
        if linked.get("status") != "ok":
            raise ValueError(linked.get("message") or "کد اکانت معتبر نیست.")
        database.log_audit(
            rep_id=None, account_code=code, email=None, action="admin_link_user_account",
            actor_id=int(actor.get("id") or 0), actor_name=actor.get("name") or "Admin",
            details={"target_user_id": target}, result_status="success")
        return f"اکانت {code} به کاربر {target} متصل شد."

    ui_v52.register_flow(ui_v52.WizardFlow(
        "admin_link_user_account", "اتصال اکانت به کاربر",
        steps=(
            ui_v52.WizardStep("account_code", "کد دقیق اکانت",
                              parse=lambda raw: _wz_text(raw, "کد اکانت", maximum=64)),
        ),
        preview=lambda v: "\n".join([
            f"• کاربر: <code>{_wz_escape(v.get('user_id'))}</code>",
            f"• اکانت: <code>{_wz_escape(v.get('account_code'))}</code>",
        ]),
        commit=commit_link_account,
        legacy=lambda raw: None,
    ))

    def commit_unlink_account(values, actor):
        target = int(values["user_id"])
        code = str(values["account_code"])
        if not database.unlink_account_owner(code, target):
            raise ValueError("اکانت متعلق به این کاربر نیست یا یافت نشد.")
        return f"اتصال اکانت {code} از کاربر {target} قطع شد."

    ui_v52.register_flow(ui_v52.WizardFlow(
        "admin_unlink_user_account", "قطع اتصال اکانت از کاربر",
        steps=(
            ui_v52.WizardStep("account_code", "کد دقیق اکانت",
                              parse=lambda raw: _wz_text(raw, "کد اکانت", maximum=64)),
        ),
        preview=lambda v: "\n".join([
            f"• کاربر: <code>{_wz_escape(v.get('user_id'))}</code>",
            f"• اکانت برای قطع اتصال: <code>{_wz_escape(v.get('account_code'))}</code>",
        ]),
        commit=commit_unlink_account,
        legacy=lambda raw: None,
    ))

    def commit_note(values, actor):
        target = int(values["user_id"])
        database.add_profile_note(target, int(actor.get("id") or 0),
                                  actor.get("name") or "Admin", str(values["note"]))
        return f"یادداشت داخلی برای کاربر {target} ثبت شد."

    ui_v52.register_flow(ui_v52.WizardFlow(
        "admin_profile_note", "ثبت یادداشت مدیریتی",
        steps=(
            ui_v52.WizardStep("note", "متن یادداشت",
                              parse=lambda raw: _wz_text(raw, "یادداشت", maximum=1000)),
        ),
        preview=lambda v: "\n".join([
            f"• کاربر: <code>{_wz_escape(v.get('user_id'))}</code>",
            f"• یادداشت: <b>{_wz_escape(v.get('note'))}</b>",
        ]),
        commit=commit_note,
        legacy=lambda raw: None,
    ))


def _legacy_card_settings(raw):
    """Accept the pre-wizard ``number | holder | bank`` line."""
    try:
        number, holder, bank = parse_card_settings_input(raw)
    except (ValueError, TypeError):
        return None
    return {"card_number": number, "card_holder": holder, "bank_name": bank}


def _legacy_pay(raw):
    """Accept the pre-wizard ``amount description`` line."""
    parts = str(raw or "").split(maxsplit=1)
    if not parts:
        return None
    try:
        return {"amount": int(parts[0].replace(",", "").translate(_WZ_DIGITS)),
                "description": parts[1] if len(parts) > 1 else "تسویه حساب نماینده"}
    except (ValueError, TypeError):
        return None


def _legacy_charge_user(raw):
    try:
        return {"amount": int(str(raw).replace(",", "").translate(_WZ_DIGITS))}
    except (ValueError, TypeError):
        return None


def _legacy_edit_credit(raw):
    try:
        return {"limit": int(str(raw).replace(",", "").translate(_WZ_DIGITS))}
    except (ValueError, TypeError):
        return None


def _legacy_edit_user_id(raw):
    try:
        return {"new_id": int(str(raw).translate(_WZ_DIGITS))}
    except (ValueError, TypeError):
        return None


def _register_setting_wizard(key: str, label: str, current_value=None) -> str:
    """Register (or refresh) the wizard for one settings key and return its flow key.

    The choices and the current value depend on the key, so the flow is registered on
    demand rather than once at import: a switch gets inline on/off buttons, a keyword
    setting gets one button per allowed value, and anything else gets a validated text
    step. All three end on the same preview card.

    The current value is rendered into the card's own body — an operator changing a
    switch must see what it is set to now, in Persian, before pressing anything.
    """
    flow_key = f"admin_setting_{key}"
    if key in BOOLEAN_SETTINGS:
        choices = ((BOOLEAN_ON, "1"), (BOOLEAN_OFF, "0"))
        step = ui_v52.WizardStep("value", label, choices=choices)
    elif key in SETTING_VALUE_LABELS:
        choices = tuple((text, value) for value, text in SETTING_VALUE_LABELS[key].items())
        step = ui_v52.WizardStep("value", label, choices=choices)
    else:
        step = ui_v52.WizardStep(
            "value", label,
            hint="مقدار جدید را وارد کنید. برای شناسهٔ گروه، عدد را همان‌طور که هست بفرستید.",
            parse=lambda raw: _wz_text(raw, label, maximum=200))

    # The wizard renders HTML and escapes its own text, so the value is taken bare.
    intro = (f"مقدار فعلی این پارامتر: "
             f"{setting_value_display(key, current_value, markdown=False)}")

    def commit(values, actor):
        raw_value = str(values.get("value") or "").strip()
        new_value = raw_value
        if key in database.GROUP_ID_SETTING_KEYS:
            new_value = str(database.normalize_group_chat_id(raw_value))
        database.set_setting(key, new_value, actor_id=int(actor.get("id") or 0),
                             actor_name=actor.get("name") or "Admin")
        return (f"تنظیمات با موفقیت بروزرسانی شد.\n"
                f"پارامتر: {label}\n"
                f"مقدار جدید: {setting_value_display(key, new_value, markdown=False)}\n\n"
                "تغییرات به‌صورت آنی در سامانه اعمال شد.")

    async def after_commit(update, context, values, notice):
        """A new group id must provision its report topics, exactly as the legacy
        text handler did — otherwise the operator saved an id with no topics."""
        if key not in database.GROUP_ID_SETTING_KEYS:
            return ""
        return await _provision_topics_after_group_change(
            update, context, values.get("value"))

    ui_v52.register_flow(ui_v52.WizardFlow(
        flow_key, f"ویرایش تنظیمات — {label}",
        steps=(step,),
        preview=lambda v: "\n".join([
            f"• پارامتر: <b>{_wz_escape(label)}</b>",
            f"• مقدار فعلی: <b>{_wz_escape(setting_value_display(key, v.get('current_value'), markdown=False))}</b>",
            f"• مقدار جدید: <b>{_wz_escape(setting_value_display(key, v.get('value'), markdown=False))}</b>",
        ]),
        commit=commit,
        after_commit=after_commit,
        legacy=lambda raw: {"value": str(raw).strip()} if str(raw).strip() else None,
        intro=intro,
    ))
    return flow_key


def _register_link_user_wizard() -> None:
    """The two-step «attach an account to a Telegram id» flow."""

    def commit(values, actor):
        chat_id = int(values["chat_id"])
        code = str(values["account_code"])
        result = database.link_user_to_account(code, chat_id, allow_reassign=True)
        if result.get("status") != "ok":
            raise ValueError(result.get("message") or "ورودی معتبر نیست.")
        return f"اکانت {code} به Chat ID {chat_id} متصل شد."

    ui_v52.register_flow(ui_v52.WizardFlow(
        "admin_link_user", "اتصال اکانت به Chat ID تلگرام",
        steps=(
            ui_v52.WizardStep("account_code", "کد یا ایمیل اکانت",
                              hint="پس از وارد کردن، مشخصات اکانت نمایش داده می‌شود.",
                              parse=_parse_link_account),
            ui_v52.WizardStep("chat_id", "Chat ID کاربر مقصد",
                              parse=lambda raw: _wz_int(raw, "شناسه کاربر", minimum=1)),
        ),
        preview=lambda v: "\n".join([
            f"• اکانت: <code>{_wz_escape(v.get('account_code'))}</code>",
            f"• مشتری اکانت: {_wz_escape(v.get('account_owner') or 'بدون مالک')}",
            f"• مقصد: <code>{_wz_escape(v.get('chat_id'))}</code>",
        ]),
        commit=commit,
        legacy=_legacy_link_user,
    ))


def _parse_link_account(raw):
    """Resolve the account now, so the operator sees what they are about to link."""
    code = _wz_text(raw, "کد اکانت", maximum=64)
    account = database.get_account_by_code_or_email(code)
    if not account:
        raise ValueError("اکانتی با این کد یا ایمیل پیدا نشد؛ ورودی را بررسی کنید.")
    return {"account_code": account["account_code"],
            "account_owner": account.get("owner_tg_id") or ""}


def _legacy_link_user(raw):
    """Accept the pre-wizard ``chat_id account_code`` line."""
    parts = str(raw or "").split()
    if len(parts) < 2 or not parts[0].isdigit():
        return None
    return {"chat_id": int(parts[0]), "account_code": parts[1]}


def _register_admin_wizards() -> None:
    """Register every operator flow. Called once at import.

    Registration reads the live catalogues (tiers, categories, panels) so the
    buttons match what the operator actually configured. It must therefore never
    take the module down: a database that is not ready yet leaves the flow with its
    fallback choices, and the wizard still opens.
    """
    for register in (_register_rep_wizards, _register_plan_wizards,
                     _register_money_wizards, _register_link_user_wizard):
        try:
            register()
        except Exception:
            logger.exception("Could not register the %s wizard flow", register.__name__)


_register_admin_wizards()


#: Which wizard each ``admin:prompt_*`` button now opens.
ADMIN_WIZARD_FLOW_OF = {
    "prompt_card_settings": "admin_card_settings",
    "prompt_charge_user": "admin_charge_user",
    "prompt_edit_credit": "admin_edit_credit",
    "prompt_pay": "admin_pay",
    "prompt_add_rep": "admin_add_rep",
    "prompt_edit_plan": "admin_edit_plan",
    "prompt_add_plan": "admin_add_plan",
    "prompt_edit_user_id": "admin_edit_user_id",
    "prompt_link_user": "admin_link_user",
    "prompt_link_user_id": "admin_link_user_account",
    "prompt_unlink_user_acc": "admin_unlink_user_account",
    "prompt_edit_rep": "admin_edit_rep",
    "prompt_edit_plan_specs": "admin_edit_plan_specs",
}


def _admin_wizard_seed(action: str, sub_action: str, parts: list[str]) -> dict:
    """The context a flow needs to build its preview, gathered from the callback."""
    seed: dict = {}
    if action in ("prompt_charge_user", "prompt_edit_user_id",
                  "prompt_link_user_id", "prompt_unlink_user_acc"):
        try:
            seed["user_id"] = int(sub_action)
        except (TypeError, ValueError):
            return {}
        seed["current_balance"] = database.get_user_wallet_balance(seed["user_id"])
    elif action in ("prompt_edit_credit", "prompt_edit_rep"):
        try:
            rep_id = int(sub_action)
        except (TypeError, ValueError):
            return {}
        rep = database.get_rep_by_id(rep_id)
        if not rep:
            return {}
        seed["rep_id"] = rep_id
        seed["rep_name"] = rep.get("name") or ""
        seed["rep_code"] = rep.get("rep_code") or ""
        seed["current_limit"] = int(rep.get("credit_limit_toman") or 0)
        if action == "prompt_edit_rep":
            # S59 — the preview needs the record's real current values. The seed used to
            # carry ``previous_value = ""``, so «مقدار قبلی» always rendered as «—» and
            # the confirmation card could not be checked against anything.
            seed["current"] = {
                "name": rep.get("name") or "",
                "rep_code": rep.get("rep_code") or "",
                "telegram_id": int(rep.get("telegram_id") or 0),
                "group_id": int(rep.get("group_id") or 0),
                "topic_id": int(rep.get("topic_id") or 0),
                "scope_filter": rep.get("scope_filter") or "",
            }
    elif action == "prompt_pay":
        rep = database.get_rep_by_code(sub_action)
        if not rep:
            return {}
        seed["rep_code"] = rep.get("rep_code") or sub_action
        seed["rep_name"] = rep.get("name") or ""
        seed["current_debt"] = int(database.get_rep_balance(rep["id"]) or 0)
    elif action in ("prompt_edit_plan", "prompt_edit_plan_specs"):
        plan = database.get_plan(sub_action)
        if not plan:
            return {}
        seed["plan_code"] = plan.get("plan_code") or sub_action
        seed["plan_name"] = plan.get("name") or ""
        seed["current_rep_price"] = int(plan.get("rep_price") or 0)
        seed["current_user_price"] = int(plan.get("user_price") or 0)
        seed["previous_gb"] = plan.get("gb")
        seed["previous_days"] = plan.get("days")
        seed["previous_name"] = plan.get("name") or ""
    return seed


def _start_admin_wizard(context, query, flow_key: str, parent_cb: str, *,
                        seed: dict | None = None, origin: str = "", ref: str = ""):
    """Open one operator wizard on the live conversation store.

    ``admin_state`` is dropped on purpose: the wizard supersedes it, and leaving both
    live would let the legacy handler swallow the answer the wizard is waiting for.
    """
    message = getattr(query, "message", None)
    chat = getattr(message, "chat", None)
    state = ui_v52.start_wizard(
        context.user_data, flow_key, return_callback=parent_cb, origin=origin, ref=ref,
        seed=seed, message_id=getattr(message, "message_id", None),
        chat_id=getattr(chat, "id", None))
    context.user_data.pop("admin_state", None)
    return state


async def _reply_profile_after_action(update, admin_state, user_id, notice: str) -> bool:
    """Return the operator to the profile card after a saved management action.

    A management action started from a 360-degree card must end on that card. The
    handlers used to answer with the main menu, so the operator lost the profile's
    own buttons — including the drill-downs they were working through.
    """
    rendered = _profile_return(
        int(user_id), admin_state.get("origin") or "", admin_state.get("ref") or "",
        notice=notice)
    if rendered is None:
        await update.message.reply_text(
            notice + "\n\n⚠️ پروفایل این کاربر دیگر در دسترس نیست.",
            parse_mode=ParseMode.MARKDOWN, reply_markup=build_admin_main_keyboard())
        return True
    body, keyboard = rendered
    await update.message.reply_text(body, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    return True


async def process_admin_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Process admin FSM input, cleaning the previous prompt only on success or closure."""
    # Keep the callback-to-FSM action contract explicit for audits and routing tests.
    managed_actions = {
        "edit_card_settings", "charge_user", "edit_credit_limit", "edit_setting",
        "pay", "add_rep", "edit_plan", "transfer", "transfer_user_target",
        "inspect", "add_plan", "link_user", "search_user", "edit_user_id",
        "link_user_account", "unlink_user_account", "edit_rep", "edit_plan_specs",
        "profile_note",
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
