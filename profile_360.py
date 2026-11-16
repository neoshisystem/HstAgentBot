"""The 360-degree customer / representative profile — one command centre per person.

Every management surface that names a person (a ticket, a receipt before or after
its decision, a locked transaction, an abuse report, the user list, the
representative tree) links here, and the profile links back to the exact surface
it was opened from.

Access is strictly **bottom-up**: a representative may open the profile of a
downstream user, and never the profile of a peer, of their own parent, or of the
root. ``allow_management`` is the single switch that drops every mutating control
for a read-only viewer, so a scoped representative can look without touching.

Rendering goes through :mod:`message_edit`, because a profile is routinely opened
from a receipt — a PHOTO message whose body is a caption. ``edit_message_text``
on it raises ``BadRequest: There is no text in the message to edit``, which is
exactly the crash this module exists to prevent.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

import database
import message_edit

logger = logging.getLogger("HstAgentBot.Profile360")

#: Surfaces a profile can be opened from. ``origin`` names the surface, ``ref``
#: its id, and the pair is carried through every drill-down so the back button
#: always returns to the exact record instead of a generic list.
PROFILE_ORIGINS = ("tx", "rcp", "ticket", "reg", "users", "reps", "abuse", "repnet")

_ROLE_LABELS = {
    "end_user": "کاربر عادی",
    "rep": "نماینده",
    "super_admin": "مدیر کل",
}

_REGISTRATION_LABELS = {
    "approved": "✅ تاییدشده",
    "pending_approval": "⏳ در انتظار تایید",
    "awaiting_referral": "🎁 در انتظار کد معرف",
    "rejected": "❌ ردشده",
}


def escape_markdown(value: Any) -> str:
    """Escape untrusted dynamic text before interpolation into legacy Markdown."""
    text = str(value if value is not None else "")
    for token in ("_", "*", "`", "["):
        text = text.replace(token, "\\" + token)
    return text


def escape_markdown_code(value: Any) -> str:
    """Escape a value that will be rendered inside a code span."""
    return str(value if value is not None else "").replace("`", "'")


# --------------------------------------------------------------------------
# Callback encoding
# --------------------------------------------------------------------------

def profile_callback(section: str, user_id: int, origin: str = "", ref: str = "") -> str:
    """Build a ``p360:`` callback that carries the originating surface with it."""
    surface = str(origin or "").strip()
    reference = str(ref or "").strip()
    if surface not in PROFILE_ORIGINS:
        surface, reference = "", ""
    return f"p360:{section}:{int(user_id)}:{surface}:{reference}"


def parse_profile_callback(data: str):
    """Decode a ``p360:`` callback into ``(section, user_id, origin, ref)``.

    Returns ``None`` for anything malformed, so a stale button answers with a
    notice instead of raising inside the dispatcher.
    """
    parts = str(data or "").split(":")
    if len(parts) < 3 or parts[0] != "p360":
        return None
    section = parts[1]
    try:
        user_id = int(parts[2])
    except (TypeError, ValueError):
        return None
    if user_id <= 0:
        return None
    origin = parts[3] if len(parts) > 3 else ""
    ref = parts[4] if len(parts) > 4 else ""
    if origin not in PROFILE_ORIGINS:
        origin, ref = "", ""
    return section, user_id, origin, ref


def back_row(origin: str, ref: str) -> list:
    """The single back button for the surface a profile was opened from."""
    if origin == "tx" and ref:
        return [InlineKeyboardButton("🔙 بازگشت به تراکنش معلق",
                                     callback_data=f"admin:pending_tx:{ref}")]
    if origin == "rcp" and ref:
        return [InlineKeyboardButton("🔙 بازگشت به فیش واریزی",
                                     callback_data=f"admin_detail_rcp_{ref}")]
    if origin == "ticket" and ref:
        return [InlineKeyboardButton("🔙 بازگشت به تیکت",
                                     callback_data=f"v52:ticket:{ref}")]
    if origin == "abuse" and ref:
        return [InlineKeyboardButton("🔙 بازگشت به گزارش تخلف",
                                     callback_data=f"v52:ticket:{ref}")]
    if origin == "reg" and ref:
        return [InlineKeyboardButton("🔙 بازگشت به صف تایید عضویت",
                                     callback_data="admin:pending_reg")]
    if origin == "reps" and ref:
        return [InlineKeyboardButton("🔙 بازگشت به نماینده",
                                     callback_data=f"admin:rep_detail:{ref}")]
    if origin == "repnet":
        return [InlineKeyboardButton("🔙 بازگشت به شبکهٔ من",
                                     callback_data="rep:my_network")]
    return [InlineKeyboardButton("🔙 بازگشت به فهرست کاربران",
                                 callback_data="admin:users:1")]


# --------------------------------------------------------------------------
# Identity resolution
# --------------------------------------------------------------------------

def _rep_row_for(tg_id: int) -> dict | None:
    """The representative row this person owns, if any."""
    try:
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT * FROM representatives WHERE telegram_id=? AND deleted_at IS NULL "
                "ORDER BY id LIMIT 1", (int(tg_id),),
            ).fetchone()
        return dict(row) if row else None
    except Exception:
        logger.debug("Representative lookup failed for %s", tg_id, exc_info=True)
        return None


def _role_label(rep_row: Mapping[str, Any] | None) -> str:
    if rep_row:
        return "زیرنماینده" if rep_row.get("parent_rep_id") else "نماینده مستقل"
    return "کاربر عادی"


def _tier_title(actor_type: str, tier_code: str) -> str:
    """Persian title for a tier code, falling back to the raw code."""
    try:
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT title_fa FROM tier_policies WHERE actor_type=? AND tier_code=?",
                (str(actor_type), str(tier_code)),
            ).fetchone()
        if row and row["title_fa"]:
            return str(row["title_fa"])
    except Exception:
        logger.debug("Tier lookup failed for %s/%s", actor_type, tier_code, exc_info=True)
    return str(tier_code or "normal")


def _count(query: str, params: tuple) -> int:
    try:
        with database.get_db() as conn:
            row = conn.execute(query, params).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        logger.debug("Profile count query failed", exc_info=True)
        return 0


def _collect_counts(user_id: int, rep_row: Mapping[str, Any] | None) -> dict:
    """Every drill-down counter, resolved in one place so the page cannot drift."""
    services = _count(
        "SELECT COUNT(*) FROM accounts WHERE owner_tg_id=? AND deleted_at IS NULL",
        (user_id,),
    )
    tickets = _count(
        "SELECT COUNT(*) FROM support_tickets WHERE sender_tg_id=? "
        "AND ticket_type<>'abuse_report'", (user_id,))
    abuse = _count(
        "SELECT COUNT(*) FROM support_tickets WHERE sender_tg_id=? "
        "AND ticket_type='abuse_report'", (user_id,))
    invited = _count(
        "SELECT COUNT(*) FROM bot_users WHERE referred_by_user_id=?", (user_id,))
    sub_reps = 0
    if rep_row:
        sub_reps = _count(
            "SELECT COUNT(*) FROM representatives WHERE parent_rep_id=? AND deleted_at IS NULL",
            (int(rep_row["id"]),))
    return {
        "services": services, "tickets": tickets, "abuse": abuse,
        "invited": invited, "sub_reps": sub_reps,
        "notes": database.count_profile_notes(user_id),
    }


# --------------------------------------------------------------------------
# The page
# --------------------------------------------------------------------------

def build_profile_page(
    user_id: int,
    *,
    origin: str = "",
    ref: str = "",
    allow_management: bool = True,
) -> tuple[str, list]:
    """Full 360-degree page: identity, lifetime money, drill-downs, management.

    Returns ``(text, rows)``; ``("", [])`` means the person is not known here.
    """
    user_id = int(user_id)
    u_info = database.get_bot_user_by_tg_id(user_id) or {}
    rep_row = _rep_row_for(user_id)
    if not u_info and not rep_row:
        return "", []

    wallet = int(u_info.get("wallet_balance") or 0)
    if rep_row and rep_row.get("balance_toman") is not None:
        wallet = int(rep_row.get("balance_toman") or 0)
    try:
        locked = int(database.get_user_locked_amount(user_id) or 0)
    except Exception:
        locked = 0
    is_blocked = int(u_info.get("is_blocked") or 0) == 1
    lifetime = database.get_profile_lifetime_totals(
        user_id, rep_row.get("id") if rep_row else None)
    counts = _collect_counts(user_id, rep_row)

    tier_code = str((rep_row or {}).get("tier") or u_info.get("tier") or "normal")
    tier_actor = "rep" if rep_row else "end_user"

    lines = ["👤 **پروفایل ۳۶۰ درجه:**", ""]
    lines.append(f"• **نام:** {escape_markdown(u_info.get('full_name') or 'ثبت نشده')}")
    if u_info.get("username"):
        lines.append(f"• **یوزرنیم:** @{escape_markdown(u_info.get('username'))}")
    else:
        lines.append("• **یوزرنیم:** ندارد")
    lines.append(f"• **شناسه عددی تلگرام:** `{user_id}`")
    lines.append(f"• **تاریخ عضویت:** `{escape_markdown_code(str(u_info.get('created_at') or '—')[:19])}`")
    lines.append(f"• **وضعیت حساب:** {'🔴 مسدود' if is_blocked else '🟢 فعال'}")
    lines.append(f"• **وضعیت عضویت:** "
                 + _REGISTRATION_LABELS.get(str(u_info.get("registration_status") or "approved"), "نامشخص"))
    lines.append(f"• **نقش فعلی:** **{_role_label(rep_row)}**")
    lines.append(f"• **سطح تعرفه (Tier):** **{escape_markdown(_tier_title(tier_actor, tier_code))}**")
    if rep_row and rep_row.get("parent_rep_id"):
        lines.append(f"• **نماینده بالاسری:** `{int(rep_row['parent_rep_id'])}`")
    elif u_info.get("assigned_rep_id"):
        lines.append(f"• **نماینده بالاسری:** `{int(u_info['assigned_rep_id'])}`")
    referrer = u_info.get("referred_by_user_id")
    if referrer:
        lines.append(f"• **معرف:** `{int(referrer)}` (کد `{escape_markdown_code(u_info.get('referred_by_code') or '—')}`)")
    else:
        lines.append("• **معرف:** ندارد")

    lines.append("")
    lines.append("💰 **کیف پول:**")
    lines.append(f"   • موجودی قابل برداشت: **{wallet:,} تومان**")
    lines.append(f"   • محفوظ در تراکنش معلق: **{locked:,} تومان**")
    lines.append("")
    lines.append("📈 **آمار تجمیعی کل دوره (Lifetime):**")
    lines.append(f"   • مجموع کل واریزی‌ها: **{lifetime['deposits_toman']:,} تومان**")
    lines.append(f"   • مجموع کل خریدها: **{lifetime['purchases_toman']:,} تومان**")
    lines.append(f"   • خالص: **{lifetime['net_toman']:,} تومان**")

    notes = database.get_profile_notes(user_id, limit=3)
    if notes:
        lines.append("")
        lines.append(f"📝 **یادداشت‌های داخلی ({counts['notes']}):**")
        for note in notes:
            lines.append(
                f"   • {escape_markdown(str(note.get('body') or '')[:120])} "
                f"— _{escape_markdown(str(note.get('author_name') or '—'))} "
                f"({escape_markdown_code(str(note.get('created_at') or '')[:16])})_")

    rows: list[list[InlineKeyboardButton]] = []
    rows.append([InlineKeyboardButton(
        f"📱 سرویس‌ها و اکانت‌های کاربر ({counts['services']})",
        callback_data=profile_callback("services", user_id, origin, ref))])
    rows.append([InlineKeyboardButton(
        f"🎫 تیکت‌های کاربر ({counts['tickets']})",
        callback_data=profile_callback("tickets", user_id, origin, ref))])
    rows.append([InlineKeyboardButton(
        f"🚨 گزارش‌های تخلف ({counts['abuse']})",
        callback_data=profile_callback("abuse", user_id, origin, ref))])
    rows.append([InlineKeyboardButton(
        "🧾 سوابق واریزی‌ها و تراکنش‌ها",
        callback_data=profile_callback("tx", user_id, origin, ref))])
    rows.append([InlineKeyboardButton(
        f"👥 زیرمجموعه‌ها و زیرنماینده‌ها ({counts['invited'] + counts['sub_reps']})",
        callback_data=profile_callback("subs", user_id, origin, ref))])
    rows.append([InlineKeyboardButton(
        f"📝 یادداشت‌های داخلی مدیریت ({counts['notes']})",
        callback_data=profile_callback("notes", user_id, origin, ref))])

    if allow_management:
        rows.append([
            InlineKeyboardButton("💰 افزایش / کسر موجودی",
                                 callback_data=f"admin:prompt_charge_user:{user_id}"),
            InlineKeyboardButton("🎖 تغییر سطح / تعرفه",
                                 callback_data=profile_callback("tier", user_id, origin, ref)),
        ])
        toggle = "🟢 رفع مسدودی" if is_blocked else "🔒 مسدودسازی حساب"
        rows.append([
            InlineKeyboardButton(toggle,
                                 callback_data=f"admin:set_block_user:{user_id}:{0 if is_blocked else 1}"),
            InlineKeyboardButton("🧑‍💼 ارتقاء به نماینده",
                                 callback_data=profile_callback("promote", user_id, origin, ref)),
        ])
        if rep_row:
            rows.append([InlineKeyboardButton(
                "👥 مدیریت زیرنماینده‌های این نماینده",
                callback_data=f"v52:subreps:{int(rep_row['id'])}")])
    rows.append(back_row(origin, ref))
    return "\n".join(lines), rows


# --------------------------------------------------------------------------
# Drill-down pages
# --------------------------------------------------------------------------

def _services_page(user_id: int, origin: str, ref: str):
    accounts = []
    try:
        accounts = database.get_user_accounts(user_id) or []
    except Exception:
        logger.debug("Account list failed for %s", user_id, exc_info=True)
    lines = [f"📱 **سرویس‌ها و اکانت‌های کاربر** (`{user_id}`):", ""]
    rows = []
    if not accounts:
        lines.append("   • سرویسی ثبت نشده است.")
    for account in accounts[:20]:
        state = "🟢 فعال" if account.get("enable") else "🔴 غیرفعال"
        code = str(account.get("account_code") or "—")
        lines.append(f"   • `{escape_markdown_code(code)}` | {state}")
        rows.append([InlineKeyboardButton(f"📦 {code[:28]}", callback_data=f"user:view_acc:{code}")])
    rows.append([InlineKeyboardButton("🔙 بازگشت به پروفایل ۳۶۰ درجه",
                                      callback_data=profile_callback("view", user_id, origin, ref))])
    return "\n".join(lines), rows


def _tickets_page(user_id: int, origin: str, ref: str, *, abuse: bool = False):
    kind = "abuse_report" if abuse else "other"
    comparator = "=" if abuse else "<>"
    try:
        with database.get_db() as conn:
            rows_raw = conn.execute(
                f"SELECT ticket_id, status, ticket_type, created_at FROM support_tickets "
                f"WHERE sender_tg_id=? AND ticket_type{comparator}'abuse_report' "
                f"ORDER BY created_at DESC LIMIT 20", (int(user_id),),
            ).fetchall()
    except Exception:
        logger.debug("Ticket list failed for %s", user_id, exc_info=True)
        rows_raw = []
    title = "🚨 **گزارش‌های تخلف کاربر**" if abuse else "🎫 **تیکت‌های کاربر**"
    lines = [f"{title} (`{user_id}`):", ""]
    rows = []
    if not rows_raw:
        lines.append("   • موردی ثبت نشده است.")
    for row in rows_raw:
        lines.append(f"   • `{escape_markdown_code(str(row['ticket_id']))}` | "
                     f"{escape_markdown(str(row['status']))}")
        rows.append([InlineKeyboardButton(
            f"🎫 {str(row['ticket_id'])[:24]}",
            callback_data=f"v52:ticket:{row['ticket_id']}")])
    rows.append([InlineKeyboardButton("🔙 بازگشت به پروفایل ۳۶۰ درجه",
                                      callback_data=profile_callback("view", user_id, origin, ref))])
    return "\n".join(lines), rows


def _transactions_page(user_id: int, origin: str, ref: str, rep_row=None):
    rows_raw = []
    try:
        with database.get_db() as conn:
            rows_raw = conn.execute(
                "SELECT transaction_type, amount, description, created_at "
                "FROM financial_ledger WHERE (user_id=? OR (? > 0 AND rep_id=?)) "
                "ORDER BY id DESC LIMIT 10",
                (int(user_id), int(rep_row["id"]) if rep_row else 0,
                 int(rep_row["id"]) if rep_row else 0),
            ).fetchall()
    except Exception:
        logger.debug("Ledger list failed for %s", user_id, exc_info=True)
    receipts = []
    try:
        with database.get_db() as conn:
            receipts = conn.execute(
                "SELECT tracking_code, amount_toman, status, created_at "
                "FROM payment_receipts WHERE user_id=? ORDER BY id DESC LIMIT 5",
                (int(user_id),),
            ).fetchall()
    except Exception:
        logger.debug("Receipt list failed for %s", user_id, exc_info=True)

    lines = [f"🧾 **سوابق واریزی‌ها و تراکنش‌های کاربر** (`{user_id}`):", ""]
    lines.append("🔟 **۱۰ تراکنش آخر:**")
    if rows_raw:
        for row in rows_raw:
            sign = "➕" if row["transaction_type"] == "credit" else "➖"
            lines.append(f"   • {sign} **{int(row['amount'] or 0):,} ت** | "
                         f"{escape_markdown(str(row['description'] or '—')[:40])} | "
                         f"`{escape_markdown_code(str(row['created_at'] or '')[:16])}`")
    else:
        lines.append("   • تراکنشی ثبت نشده است.")
    lines.append("")
    lines.append("🧾 **۵ فیش آخر:**")
    if receipts:
        for row in receipts:
            lines.append(f"   • `{escape_markdown_code(str(row['tracking_code']))}` | "
                         f"**{int(row['amount_toman'] or 0):,} ت** | "
                         f"{escape_markdown(str(row['status']))}")
    else:
        lines.append("   • فیشی ثبت نشده است.")
    rows = [[InlineKeyboardButton("🔙 بازگشت به پروفایل ۳۶۰ درجه",
                                  callback_data=profile_callback("view", user_id, origin, ref))]]
    return "\n".join(lines), rows


def _subs_page(user_id: int, origin: str, ref: str, rep_row=None):
    invited = []
    try:
        with database.get_db() as conn:
            invited = conn.execute(
                "SELECT telegram_id, full_name, username, created_at FROM bot_users "
                "WHERE referred_by_user_id=? ORDER BY id DESC LIMIT 20", (int(user_id),),
            ).fetchall()
    except Exception:
        logger.debug("Invitee list failed for %s", user_id, exc_info=True)
    children = []
    if rep_row:
        try:
            with database.get_db() as conn:
                children = conn.execute(
                    "SELECT id, name, rep_code, tier FROM representatives "
                    "WHERE parent_rep_id=? AND deleted_at IS NULL ORDER BY id LIMIT 20",
                    (int(rep_row["id"]),),
                ).fetchall()
        except Exception:
            logger.debug("Sub-rep list failed for %s", user_id, exc_info=True)

    lines = [f"👥 **زیرمجموعه‌ها و زیرنماینده‌های** (`{user_id}`):", ""]
    rows = []
    lines.append(f"🎁 **کاربران دعوت‌شده ({len(invited)}):**")
    if invited:
        for row in invited:
            lines.append(f"   • {escape_markdown(row['full_name'] or '—')} "
                         f"(`{int(row['telegram_id'])}`)")
            rows.append([InlineKeyboardButton(
                f"👤 {str(row['full_name'] or row['telegram_id'])[:26]}",
                callback_data=profile_callback("view", int(row["telegram_id"]), "users", ""))])
    else:
        lines.append("   • کاربری دعوت نشده است.")
    if rep_row:
        lines.append("")
        lines.append(f"🧑‍💼 **زیرنماینده‌های مستقیم ({len(children)}):**")
        if children:
            for row in children:
                lines.append(f"   • {escape_markdown(row['name'] or '—')} "
                             f"(`{escape_markdown_code(row['rep_code'] or '')}`) | {escape_markdown(row['tier'] or '')}")
                rows.append([InlineKeyboardButton(
                    f"🧑‍💼 {str(row['name'] or row['id'])[:24]}",
                    callback_data=f"admin:rep_detail:{int(row['id'])}")])
        else:
            lines.append("   • زیرنماینده‌ای ثبت نشده است.")
    rows.append([InlineKeyboardButton("🔙 بازگشت به پروفایل ۳۶۰ درجه",
                                      callback_data=profile_callback("view", user_id, origin, ref))])
    return "\n".join(lines), rows


def _notes_page(user_id: int, origin: str, ref: str):
    notes = database.get_profile_notes(user_id, limit=20)
    lines = [f"📝 **یادداشت‌های داخلی مدیریت** (`{user_id}`):", "",
             "این یادداشت‌ها فقط برای مدیریت قابل مشاهده‌اند و به کاربر نمایش داده نمی‌شوند.", ""]
    if notes:
        for note in notes:
            lines.append(f"   • {escape_markdown(str(note.get('body') or ''))}")
            lines.append(f"     — _{escape_markdown(str(note.get('author_name') or '—'))} | "
                         f"`{escape_markdown_code(str(note.get('created_at') or '')[:16])}`_")
    else:
        lines.append("   • یادداشتی ثبت نشده است.")
    rows = [
        [InlineKeyboardButton("➕ ثبت یادداشت جدید",
                              callback_data=profile_callback("notenew", user_id, origin, ref))],
        [InlineKeyboardButton("🔙 بازگشت به پروفایل ۳۶۰ درجه",
                              callback_data=profile_callback("view", user_id, origin, ref))],
    ]
    return "\n".join(lines), rows


def build_notes_page(user_id: int, origin: str = "", ref: str = ""):
    """Public wrapper so the note form can re-render the list after saving."""
    return _notes_page(int(user_id), origin, ref)


def _tier_page(user_id: int, origin: str, ref: str, rep_row=None):
    actor_type = "rep" if rep_row else "end_user"
    try:
        with database.get_db() as conn:
            policies = conn.execute(
                "SELECT tier_code, title_fa, discount_bps FROM tier_policies "
                "WHERE actor_type=? ORDER BY rank", (actor_type,),
            ).fetchall()
    except Exception:
        logger.debug("Tier policy list failed", exc_info=True)
        policies = []
    current = str((rep_row or {}).get("tier") or "normal")
    lines = [f"🎖 **تغییر سطح کاربری / تعرفه** (`{user_id}`):", "",
             f"سطح فعلی: **{escape_markdown(_tier_title(actor_type, current))}**", "",
             "برای تغییر، سطح مقصد را انتخاب کنید:"]
    rows = []
    for policy in policies:
        code = str(policy["tier_code"])
        marker = " ✅" if code == current else ""
        rows.append([InlineKeyboardButton(
            f"{policy['title_fa']}{marker}",
            callback_data=profile_callback(f"tier_do_{code}", user_id, origin, ref))])
    if not policies:
        lines.append("   • سیاست سطحی تعریف نشده است.")
    rows.append([InlineKeyboardButton("🔙 بازگشت به پروفایل ۳۶۰ درجه",
                                      callback_data=profile_callback("view", user_id, origin, ref))])
    return "\n".join(lines), rows


def _promote_page(user_id: int, origin: str, ref: str, rep_row=None):
    if rep_row:
        lines = [f"🧑‍💼 **مدیریت نمایندگی** (`{user_id}`):", "",
                 f"این شخص از قبل **{_role_label(rep_row)}** است."]
    else:
        lines = [f"🧑‍💼 **ارتقاء به نماینده / زیرنماینده** (`{user_id}`):", "",
                 "این عملیات دو مرحله‌ای است و پس از تایید نهایی اعمال می‌شود."]
    rows = []
    if not rep_row:
        rows.append([InlineKeyboardButton(
            "🧑‍💼 ارتقاء به نماینده مستقل",
            callback_data=profile_callback("promote_do_independent", user_id, origin, ref))])
        rows.append([InlineKeyboardButton(
            "👥 افزودن به‌عنوان زیرنماینده",
            callback_data=profile_callback("promote_do_sub", user_id, origin, ref))])
    rows.append([InlineKeyboardButton("🔙 بازگشت به پروفایل ۳۶۰ درجه",
                                      callback_data=profile_callback("view", user_id, origin, ref))])
    return "\n".join(lines), rows


def _promote_parent_picker(user_id: int, origin: str, ref: str):
    """Pick the parent representative a new sub-reseller will hang under."""
    try:
        reps = database.get_all_active_reps() or []
    except Exception:
        logger.debug("Active rep list failed", exc_info=True)
        reps = []
    lines = [f"👥 **انتخاب نمایندهٔ والد برای** `{user_id}`:", "",
             "زیرنماینده زیر این نماینده قرار می‌گیرد و سلسله‌مراتب "
             "«کاربر ⬅️ زیرنماینده ⬅️ نمایندهٔ مادر ⬅️ سوپرادمین» شکل می‌گیرد."]
    rows = []
    for rep in reps:
        rows.append([InlineKeyboardButton(
            f"🧑‍💼 {str(rep.get('name') or rep.get('rep_code') or rep['id'])[:28]}",
            callback_data=profile_callback(
                f"promote_confirm_sub_{int(rep['id'])}", user_id, origin, ref))])
    if not reps:
        lines.append("   • نمایندهٔ فعالی ثبت نشده است.")
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data=profile_callback(
        "promote", user_id, origin, ref))])
    return "\n".join(lines), rows


def _promote_confirm_page(user_id: int, origin: str, ref: str, kind: str,
                          actor_name: str, parent_rep_id: int | None = None):
    if kind == "independent":
        label = "نماینده مستقل"
        detail = "یک رکورد نمایندگی مستقل برای او ساخته می‌شود."
    else:
        label = "زیرنماینده"
        detail = (f"او زیر نمایندهٔ والد `{parent_rep_id}` قرار می‌گیرد."
                  if parent_rep_id else "او به‌عنوان زیرنماینده ثبت می‌شود.")
    lines = [
        f"⚠️ **تایید نهایی ارتقاء** (`{user_id}`):", "",
        f"این شخص به **{label}** ارتقاء می‌یابد. {detail}",
        "برای اعمال، دکمه تایید را بزنید.",
    ]
    apply_section = ("promote_apply_independent" if kind == "independent"
                     else f"promote_apply_sub_{int(parent_rep_id)}")
    rows = [
        [InlineKeyboardButton("✅ بله، ارتقاء بده",
                              callback_data=profile_callback(apply_section, user_id, origin, ref))],
        [InlineKeyboardButton("❌ انصراف",
                              callback_data=profile_callback("promote", user_id, origin, ref))],
    ]
    return "\n".join(lines), rows


# --------------------------------------------------------------------------
# Callback handler
# --------------------------------------------------------------------------

async def handle_profile_callback(update, context, *, role: str, rep) -> bool:
    """Handle a ``p360:`` callback. Returns True when it owned the callback.

    Only a super admin, or a representative opening a *downstream* user, is
    admitted. Everything else answers with a refusal instead of silently
    swallowing the press.
    """
    query = update.callback_query
    user = update.effective_user
    parsed = parse_profile_callback(query.data or "")
    if parsed is None:
        await query.answer("⚠️ این دکمه منقضی شده است؛ صفحه را دوباره باز کنید.", show_alert=True)
        return True
    section, user_id, origin, ref = parsed

    import admin_panel

    base_role, base_rep = admin_panel._base_role_of(user, update.effective_chat, None)
    is_root = base_role == "super_admin"
    if not is_root and not _rep_may_open(user_id, base_rep):
        await query.answer("⛔️ دسترسی به این پروفایل مجاز نیست.", show_alert=True)
        return True
    allow_management = is_root

    if section == "view":
        text, rows = build_profile_page(
            user_id, origin=origin, ref=ref, allow_management=allow_management)
    elif section == "services":
        text, rows = _services_page(user_id, origin, ref)
    elif section == "tickets":
        text, rows = _tickets_page(user_id, origin, ref)
    elif section == "abuse":
        text, rows = _tickets_page(user_id, origin, ref, abuse=True)
    elif section == "tx":
        text, rows = _transactions_page(user_id, origin, ref, _rep_row_for(user_id))
    elif section == "subs":
        text, rows = _subs_page(user_id, origin, ref, _rep_row_for(user_id))
    elif section == "notes":
        text, rows = _notes_page(user_id, origin, ref)
    elif section == "notenew":
        if not allow_management:
            await query.answer("⛔️ ثبت یادداشت فقط برای مدیریت مجاز است.", show_alert=True)
            return True
        return await _begin_note_input(update, context, user_id, origin, ref)
    elif section == "tier":
        text, rows = _tier_page(user_id, origin, ref, _rep_row_for(user_id))
    elif section.startswith("tier_do_"):
        if not allow_management:
            await query.answer("⛔️ تغییر سطح فقط برای مدیریت مجاز است.", show_alert=True)
            return True
        return await _apply_tier(update, context, user_id, section[len("tier_do_"):],
                                 origin, ref)
    elif section == "promote":
        text, rows = _promote_page(user_id, origin, ref, _rep_row_for(user_id))
    elif section in ("promote_do_independent", "promote_do_sub"):
        if not allow_management:
            await query.answer("⛔️ ارتقاء فقط برای مدیریت مجاز است.", show_alert=True)
            return True
        if section == "promote_do_independent":
            text, rows = _promote_confirm_page(
                user_id, origin, ref, "independent", getattr(user, "full_name", "") or "")
        else:
            text, rows = _promote_parent_picker(user_id, origin, ref)
    elif section.startswith("promote_confirm_sub_"):
        if not allow_management:
            await query.answer("⛔️ ارتقاء فقط برای مدیریت مجاز است.", show_alert=True)
            return True
        try:
            parent_id = int(section[len("promote_confirm_sub_"):])
        except ValueError:
            await query.answer("⚠️ نمایندهٔ والد نامعتبر است.", show_alert=True)
            return True
        text, rows = _promote_confirm_page(
            user_id, origin, ref, "sub", getattr(user, "full_name", "") or "", parent_id)
    elif section.startswith("promote_apply_"):
        if not allow_management:
            await query.answer("⛔️ ارتقاء فقط برای مدیریت مجاز است.", show_alert=True)
            return True
        target = section[len("promote_apply_"):]
        if target == "independent":
            return await _apply_promotion(update, context, user_id, "independent", None, origin, ref)
        if target.startswith("sub_"):
            try:
                parent_id = int(target[4:])
            except ValueError:
                await query.answer("⚠️ نمایندهٔ والد نامعتبر است.", show_alert=True)
                return True
            return await _apply_promotion(update, context, user_id, "sub", parent_id, origin, ref)
        await query.answer("⚠️ بخش ناشناخته است.", show_alert=True)
        return True
    else:
        await query.answer("⚠️ بخش ناشناخته است.", show_alert=True)
        return True

    if not text:
        await query.answer("❌ کاربر یافت نشد.", show_alert=True)
        return True
    await message_edit.render_page(
        query, text=text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows))
    await query.answer()
    return True


def _rep_may_open(user_id: int, base_rep) -> bool:
    """A representative may open only a downstream user's profile."""
    if not base_rep or not base_rep.get("id"):
        return False
    try:
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT 1 FROM bot_users WHERE telegram_id=? AND assigned_rep_id=?",
                (int(user_id), int(base_rep["id"])),
            ).fetchone()
            if row:
                return True
            child = conn.execute(
                "SELECT 1 FROM representatives WHERE telegram_id=? AND parent_rep_id=?",
                (int(user_id), int(base_rep["id"])),
            ).fetchone()
        return child is not None
    except Exception:
        logger.debug("Scoped profile access check failed", exc_info=True)
        return False


async def _begin_note_input(update, context, user_id: int, origin: str, ref: str) -> bool:
    """Open the 60-second note form, reusing the admin FSM."""
    import admin_panel
    context.user_data["admin_state"] = {
        "action": "profile_note",
        "target_user_id": int(user_id),
        "origin": origin,
        "ref": ref,
        "ts": __import__("time").time(),
        "return_callback": profile_callback("notes", user_id, origin, ref),
    }
    await message_edit.render_page(
        update.callback_query,
        text=(f"📝 **ثبت یادداشت داخلی برای** `{user_id}`:\n\n"
              "متن یادداشت را در یک پیام ارسال کنید. این یادداشت فقط برای مدیریت "
              "قابل مشاهده است.\n\n"
              "⏱ برای انصراف «لغو» را بفرستید."),
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ انصراف از فرم",
                                 callback_data=profile_callback("notes", user_id, origin, ref))]]),
    )
    admin_panel._remember_form_prompt(context.user_data["admin_state"], update.callback_query)
    await update.callback_query.answer()
    return True


async def _apply_tier(update, context, user_id: int, tier_code: str, origin: str, ref: str) -> bool:
    query = update.callback_query
    rep_row = _rep_row_for(user_id)
    try:
        with database.get_db() as conn:
            if rep_row:
                conn.execute("UPDATE representatives SET tier=? WHERE id=?",
                             (tier_code, int(rep_row["id"])))
            else:
                conn.execute("UPDATE bot_users SET tier=? WHERE telegram_id=?",
                             (tier_code, int(user_id)))
            conn.commit()
    except Exception:
        logger.exception("Tier change failed for %s", user_id)
        await query.answer("❌ تغییر سطح ناموفق بود.", show_alert=True)
        return True
    title = _tier_title("rep" if rep_row else "end_user", tier_code)
    await query.answer(f"✅ سطح به «{title}» تغییر یافت.", show_alert=True)
    text, rows = build_profile_page(user_id, origin=origin, ref=ref, allow_management=True)
    await message_edit.render_page(
        query, text=text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows))
    return True


async def _apply_promotion(update, context, user_id: int, kind: str,
                           parent_rep_id: int | None, origin: str, ref: str) -> bool:
    """Create the representative record, its financial edge and the base role.

    All three writes go through :func:`rep_tree_service.promote_actor` in ONE
    transaction. The previous implementation wrote ``representatives.parent_rep_id``
    and never created the matching ``rep_edges`` row, so a freshly promoted
    representative had no resolvable price chain and every later purchase, renewal
    and quote failed with «representative must have exactly one active parent edge»
    — that is ``BUG-TIER-001``. A promotion stays idempotent: a person who is
    already a representative is refused rather than duplicated.
    """
    query = update.callback_query
    if _rep_row_for(user_id):
        await query.answer("⚠️ این شخص از قبل نماینده است.", show_alert=True)
        return True
    u_info = database.get_bot_user_by_tg_id(user_id) or {}
    name = str(u_info.get("full_name") or u_info.get("username") or f"rep-{user_id}")
    try:
        import rep_tree_service
        rep_tree_service.promote_actor(
            {"actor_tg_id": int(getattr(update.effective_user, "id", 0) or 0), "rep_id": None},
            int(user_id),
            rep_code=f"rep-{int(user_id)}",
            name=name,
            parent_rep_id=parent_rep_id,
            scope_filter=str(u_info.get("username") or ""),
        )
    except Exception:
        logger.exception("Promotion failed for %s", user_id)
        await query.answer("❌ ارتقاء ناموفق بود؛ لاگ سرور را بررسی کنید.", show_alert=True)
        return True
    label = "نماینده مستقل" if kind == "independent" else "زیرنماینده"
    await query.answer(f"✅ ارتقاء به «{label}» انجام شد.", show_alert=True)
    text, rows = build_profile_page(user_id, origin=origin, ref=ref, allow_management=True)
    await message_edit.render_page(
        query, text=text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows))
    return True


__all__ = [
    "PROFILE_ORIGINS", "back_row", "build_profile_page", "escape_markdown",
    "escape_markdown_code", "handle_profile_callback", "parse_profile_callback",
    "profile_callback",
]
