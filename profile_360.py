"""The 360-degree customer / representative profile — one command centre per person.

Every management surface that names a person (a ticket, a receipt before or after
its decision, a locked transaction, an abuse report, the user list, the
representative tree) links here, and the profile links back to the exact surface
it was opened from.

Access is strictly **bottom-up**: a representative may open the profile of a
downstream user, and never the profile of a peer, of their own parent, or of the
root. ``allow_management`` is the single switch that drops every root-level
mutating control for a read-only viewer.

On top of that read-only view, a representative who is looking at a person who is
genuinely *theirs* — a customer whose ``bot_users.assigned_rep_id`` points at them,
or a sub-representative whose ``representatives.parent_rep_id`` is them — gets a
small, explicit control set of its own (``viewer_rep_id``). It is deliberately
narrow: promote that person to a sub-representative, set their tariff tier, and
record a management note. Every one of those actions is re-checked against the same
scope rule when it is pressed, so a widened, replayed or forwarded callback reaches
nothing. A root viewer keeps the full control set instead, and a root is never
treated as a scoped representative.

Two management actions are offered at every level — issuing a service for this
person and moving an account's ownership — and they are ``v52:`` routes, because
``admin_panel`` refuses every ``admin:`` callback for a non-root actor.

Drill-downs carry their own return path: an account opened from the service list
and a ticket opened from the ticket list both remember *this* card, so Back returns
to the profile rather than to a generic list.

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

#: The three controls a *scoped* representative gets on a downstream person's card.
#: They are the only mutating actions a non-root viewer may reach, and every one of
#: them is re-checked against the scope rule when it is pressed — not only when it
#: is drawn — so a widened or replayed callback cannot reach a peer's data.
SCOPED_PROMOTE_LABEL = "🧑‍💼 ارتقاء به زیرنمایندهٔ من"
SCOPED_TIER_LABEL = "🎖 تعیین سطح تعرفهٔ زیرنماینده"
SCOPED_NOTE_LABEL = "📝 ثبت یادداشت مدیریتی"

#: The two management actions that must work for a root operator *and* for a scoped
#: representative. They are ``v52:`` routes rather than ``admin:`` ones because
#: ``admin_panel._handle_admin_callbacks_impl`` refuses every ``admin:`` callback
#: for a non-root actor: a scoped representative would have been shown a button
#: that only ever answers «دسترسی فقط برای مدیریت کل سیستم مجاز است».
ISSUE_SERVICE_LABEL = "🆕 صدور سرویس جدید برای این کاربر"
TRANSFER_OWNERSHIP_LABEL = "🔄 انتقال مالکیت اکانت"

#: Heading printed on the card above the scoped control set, so the section is
#: visible in the page itself and not only in the keyboard.
SCOPED_SECTION_TITLE = "🧑‍💼 **کنترل‌های نمایندهٔ بالاسری (Scoped Rep Controls):**"

#: Telegram's hard limit for ``callback_data``, mirrored from
#: ``v52_router.MAX_CALLBACK_BYTES``. A drill-down callback that carries the profile
#: return context is only emitted while it still fits; past that the plain route is
#: used, because Telegram rejects the *whole message* for one oversized button.
_CALLBACK_BYTES_LIMIT = 64


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
    """Build a ``p360:`` callback that carries the originating surface with it.

    The originating surface is dropped when the whole callback would exceed
    Telegram's 64-byte ``callback_data`` budget. A rejected callback takes the
    *entire message* down with it, so losing the return context for one unusually
    long reference is strictly better than losing the card, and the button still
    navigates to the right section.
    """
    surface = str(origin or "").strip()
    reference = str(ref or "").strip()
    if surface not in PROFILE_ORIGINS:
        surface, reference = "", ""
    base = f"p360:{section}:{int(user_id)}"
    if not reference:
        return f"{base}::"
    contextual = f"{base}:{surface}:{reference}"
    return contextual if _fits_callback(contextual) else f"{base}::"


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


def scoped_rep_scope_holds(user_id: int, viewer_rep_id: int | None) -> bool:
    """True only when an ACTIVE representative looks at a genuinely downstream person.

    The two edges that make somebody downstream are ``bot_users.assigned_rep_id``
    (the customer's own representative) and ``representatives.parent_rep_id`` (the
    sub-representative's parent). A peer, the viewer's own parent, the root and the
    viewer's own card are all outside the scope, so the scoped controls cannot be
    reached by widening or replaying a callback.

    The viewer's row must be active and undeleted: a representative who was switched
    off keeps read access to their old tree but loses every control over it.
    """
    try:
        viewer = int(viewer_rep_id) if viewer_rep_id is not None else 0
        target = int(user_id)
    except (TypeError, ValueError):
        return False
    if viewer <= 0 or target <= 0:
        return False
    try:
        with database.get_db() as conn:
            rep = conn.execute(
                "SELECT id, telegram_id FROM representatives "
                "WHERE id=? AND is_active=1 AND deleted_at IS NULL",
                (viewer,),
            ).fetchone()
            if rep is None or int(rep["telegram_id"] or 0) == target:
                return False
            customer = conn.execute(
                "SELECT 1 FROM bot_users WHERE telegram_id=? AND assigned_rep_id=?",
                (target, viewer),
            ).fetchone()
            if customer is not None:
                return True
            child = conn.execute(
                "SELECT 1 FROM representatives WHERE telegram_id=? AND parent_rep_id=? "
                "AND deleted_at IS NULL",
                (target, viewer),
            ).fetchone()
        return child is not None
    except Exception:
        logger.debug("Scoped profile scope check failed", exc_info=True)
        return False


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

def _origin_suffix(origin: str, ref: str) -> str:
    """``:origin:ref`` when the profile knows where it was opened from, else ``""``.

    Every management control on the card lives in the ``admin:`` namespace — those
    are the admin panel's writers — but each one is *started from* a 360-degree
    card. Carrying the originating surface inside the callback, instead of stashing
    it in ``context.user_data``, means the return destination survives a lost form
    state and is visible in the audit trail.
    """
    surface = str(origin or "").strip()
    reference = str(ref or "").strip()
    if surface in PROFILE_ORIGINS and reference:
        return f":{surface}:{reference}"
    return ""


def _fits_callback(callback: str) -> bool:
    """Whether a callback still fits Telegram's 64-byte ``callback_data`` budget."""
    return len(str(callback).encode("utf-8")) <= _CALLBACK_BYTES_LIMIT


def _with_context(base: str, origin: str, ref: str) -> str:
    """``base`` plus the originating surface, while the whole callback still fits."""
    suffix = _origin_suffix(origin, ref)
    return base + suffix if suffix and _fits_callback(base + suffix) else base


def management_callback(section: str, user_id: int, origin: str = "", ref: str = "") -> str:
    """The ``v52:`` route for one of the profile's cross-persona management actions.

    Both the root operator and a scoped representative press these, so they cannot
    live in the ``admin:`` namespace, which is refused for every non-root actor.
    """
    return _with_context(f"v52:{section}:{int(user_id)}", origin, ref)


def account_card_callback(account_id: int, user_id: int, origin: str = "", ref: str = "") -> str:
    """Open the account card and remember the 360-degree profile it came from.

    ``v52:sub:<id>`` renders the card with the persona home as its only way back, so
    an operator who drilled in from a profile lost the card they were working from.
    The contextual form carries the target and the originating surface, and the
    card's own Back button is rebuilt from it. A reference long enough to overflow
    the callback budget degrades to the plain route instead of a rejected message.
    """
    suffix = _origin_suffix(origin, ref)
    contextual = f"v52:p360acc:{int(account_id)}:{int(user_id)}{suffix}"
    if suffix and _fits_callback(contextual):
        return contextual
    return f"v52:sub:{int(account_id)}"


def ticket_callback(ticket_id: Any, user_id: int, origin: str = "", ref: str = "") -> str:
    """Open a ticket and remember the 360-degree profile it came from."""
    identifier = str(ticket_id)
    suffix = _origin_suffix(origin, ref)
    contextual = f"v52:p360tkt:{identifier}:{int(user_id)}{suffix}"
    if suffix and _fits_callback(contextual):
        return contextual
    return f"v52:ticket:{identifier}"


def build_profile_page(
    user_id: int,
    *,
    origin: str = "",
    ref: str = "",
    allow_management: bool = True,
    viewer_rep_id: int | None = None,
) -> tuple[str, list]:
    """Full 360-degree page: identity, lifetime money, drill-downs, management.

    Returns ``(text, rows)``; ``("", [])`` means the person is not known here.

    ``viewer_rep_id`` is the representative the page is being rendered *for*, or
    ``None`` for the root operator. It is the only switch that turns on the scoped
    control set: the three buttons appear when the viewer is an active
    representative and the target is genuinely downstream of them, and never
    otherwise. A root viewer keeps the full existing control set instead.
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
    # The scoped control set belongs to the *viewer*, so it is resolved once here and
    # reused by both the body and the keyboard: the section is only announced when
    # the representative looking at this card really is upstream of this person.
    scoped = viewer_rep_id is not None and scoped_rep_scope_holds(user_id, viewer_rep_id)

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
    # The wording is pinned by the S52 contract test, which asserts both
    # «موجودی قابل برداشت» and «مبلغ محفوظ» on this card. It is the surviving card
    # now that the admin panel's duplicate renderer is gone, so it carries the
    # contract wording rather than its own paraphrase.
    lines.append(f"   • مبلغ محفوظ در تراکنش معلق: **{locked:,} تومان**")
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

    if scoped:
        # The section is named in the body as well as drawn in the keyboard, so an
        # operator reading the card knows which of the controls below are theirs.
        lines.append("")
        lines.append(SCOPED_SECTION_TITLE)
        lines.append("   • این سه عملیات فقط روی همین شخص — که زیرمجموعهٔ شماست — مجاز است.")

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
        # Every control carries the originating surface so the card can be redrawn
        # where it was opened from — but only while the whole callback still fits
        # Telegram's 64-byte budget, because one oversized button takes the entire
        # message down with it. ``_with_context`` drops the suffix in that case; the
        # admin handlers all read the origin and ref optionally.
        rows.append([
            InlineKeyboardButton("💰 افزایش / کسر موجودی",
                                 callback_data=_with_context(
                                     f"admin:prompt_charge_user:{user_id}", origin, ref)),
            InlineKeyboardButton("🎖 تغییر سطح / تعرفه",
                                 callback_data=profile_callback("tier", user_id, origin, ref)),
        ])
        toggle = "🟢 رفع مسدودی" if is_blocked else "🔒 مسدودسازی حساب"
        rows.append([
            InlineKeyboardButton(toggle,
                                 callback_data=_with_context(
                                     f"admin:set_block_user:{user_id}:{0 if is_blocked else 1}",
                                     origin, ref)),
            InlineKeyboardButton("🧑‍💼 ارتقاء به نماینده",
                                 callback_data=profile_callback("promote", user_id, origin, ref)),
        ])
        rows.append([
            InlineKeyboardButton("➕ اتصال اکانت",
                                 callback_data=_with_context(
                                     f"admin:prompt_link_user_id:{user_id}", origin, ref)),
            InlineKeyboardButton("❌ قطع اتصال اکانت",
                                 callback_data=_with_context(
                                     f"admin:prompt_unlink_user_acc:{user_id}", origin, ref)),
        ])
        rows.append([
            InlineKeyboardButton("✏️ تغییر Telegram ID",
                                 callback_data=_with_context(
                                     f"admin:prompt_edit_user_id:{user_id}", origin, ref)),
            InlineKeyboardButton("🗑 حذف کاربر",
                                 callback_data=_with_context(
                                     f"admin:confirm_del_user:{user_id}", origin, ref)),
        ])
        if rep_row:
            rows.append([InlineKeyboardButton(
                "👥 مدیریت زیرنماینده‌های این نماینده",
                callback_data=f"v52:subreps:{int(rep_row['id'])}")])

    if allow_management or scoped:
        # Two actions a command hub owes the operator whatever their level: issuing
        # a service for this person, and moving an account's ownership. They are
        # ``v52:`` routes because every ``admin:`` callback is refused for a
        # non-root actor, and both of their pages rebuild this same card.
        rows.append([
            InlineKeyboardButton(
                ISSUE_SERVICE_LABEL,
                callback_data=management_callback("p360new", user_id, origin, ref)),
            InlineKeyboardButton(
                TRANSFER_OWNERSHIP_LABEL,
                callback_data=management_callback("p360own", user_id, origin, ref)),
        ])
    if scoped:
        # Only an ACTIVE representative looking at their own downstream customer or
        # sub-representative reaches these three. A root viewer keeps the full set
        # above instead, and a peer or an upstream viewer gets nothing at all.
        rows.append([InlineKeyboardButton(
            SCOPED_PROMOTE_LABEL,
            callback_data=profile_callback("sc_promote", user_id, origin, ref))])
        rows.append([InlineKeyboardButton(
            SCOPED_TIER_LABEL,
            callback_data=profile_callback("sc_tier", user_id, origin, ref))])
        rows.append([InlineKeyboardButton(
            SCOPED_NOTE_LABEL,
            callback_data=profile_callback("sc_note", user_id, origin, ref))])
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
        # Open the canonical v5.2 service card by account id. The legacy
        # ``user:view_acc:<code>`` callback renders the *customer's own* portal
        # view and issues a live panel query with ``scope="all"``, so an operator
        # opening it from someone else's profile saw the wrong surface and paid a
        # panel round trip for it.
        account_id = account.get("id")
        if account_id:
            rows.append([InlineKeyboardButton(
                f"📦 {code[:28]}",
                callback_data=account_card_callback(
                    int(account_id), user_id, origin, ref))])
        else:
            rows.append([InlineKeyboardButton(
                f"📦 {code[:28]}", callback_data=f"user:view_acc:{code}")])
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
            callback_data=ticket_callback(row["ticket_id"], user_id, origin, ref))])
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


def _subs_page(user_id: int, origin: str, ref: str, rep_row=None, *,
               allow_management: bool = True):
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
                    "SELECT id, name, rep_code, tier, telegram_id FROM representatives "
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
        # A sub-representative is opened through their own 360-degree profile, which
        # applies the same bottom-up rule as any other person. The previous
        # ``admin:rep_detail:`` button was root-only, so a representative looking at
        # their own downstream tree got a button that only ever answered «دسترسی
        # مجاز نیست» — a dead end on their own data.
        sub_origin = "reps" if allow_management else "repnet"
        if children:
            for row in children:
                lines.append(f"   • {escape_markdown(row['name'] or '—')} "
                             f"(`{escape_markdown_code(row['rep_code'] or '')}`) | {escape_markdown(row['tier'] or '')}")
                label = f"🧑‍💼 {str(row['name'] or row['id'])[:24]}"
                if row["telegram_id"]:
                    rows.append([InlineKeyboardButton(
                        label, callback_data=profile_callback(
                            "view", int(row["telegram_id"]), sub_origin, str(int(row["id"]))))])
                elif allow_management:
                    rows.append([InlineKeyboardButton(
                        label, callback_data=f"admin:rep_detail:{int(row['id'])}")])
        else:
            lines.append("   • زیرنماینده‌ای ثبت نشده است.")
    rows.append([InlineKeyboardButton("🔙 بازگشت به پروفایل ۳۶۰ درجه",
                                      callback_data=profile_callback("view", user_id, origin, ref))])
    return "\n".join(lines), rows


def _notes_page(user_id: int, origin: str, ref: str, *, viewer_rep_id: int | None = None):
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
    # A scoped representative writes through the same ``profile_note`` form as the
    # root, so the button keeps its label and only changes which section owns it.
    scoped = viewer_rep_id is not None and scoped_rep_scope_holds(user_id, viewer_rep_id)
    new_note_section = "sc_note" if scoped else "notenew"
    rows = [
        [InlineKeyboardButton("➕ ثبت یادداشت جدید",
                              callback_data=profile_callback(new_note_section, user_id, origin, ref))],
        [InlineKeyboardButton("🔙 بازگشت به پروفایل ۳۶۰ درجه",
                              callback_data=profile_callback("view", user_id, origin, ref))],
    ]
    return "\n".join(lines), rows


def build_notes_page(user_id: int, origin: str = "", ref: str = "",
                     *, viewer_rep_id: int | None = None):
    """Public wrapper so the note form can re-render the list after saving."""
    return _notes_page(int(user_id), origin, ref, viewer_rep_id=viewer_rep_id)


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
# Scoped representative controls
# --------------------------------------------------------------------------

def _scoped_promote_confirm_page(user_id: int, origin: str, ref: str,
                                 viewer_rep_id: int):
    """Step one of the two-step promotion of a downstream person into my tree."""
    lines = [
        f"⚠️ **تایید نهایی ارتقاء به زیرنمایندگی** (`{user_id}`):", "",
        f"این شخص زیر نمایندگی شما (`{int(viewer_rep_id)}`) قرار می‌گیرد:",
        "• رکورد نمایندگی او ساخته می‌شود،",
        "• یال مالی (`rep_edges`) با کلید والد شما ثبت می‌شود،",
        "• نقش پایهٔ او به «نماینده» تغییر می‌کند.",
        "", "برای اعمال، دکمه تایید را بزنید.",
    ]
    rows = [
        [InlineKeyboardButton(
            "✅ بله، زیرنمایندهٔ من شود",
            callback_data=profile_callback("sc_promote_do", user_id, origin, ref))],
        [InlineKeyboardButton(
            "❌ انصراف",
            callback_data=profile_callback("view", user_id, origin, ref))],
    ]
    return "\n".join(lines), rows


def _scoped_tier_page(user_id: int, origin: str, ref: str, rep_row=None):
    """The tier picker for the scoped sub-representative, reusing the tier catalog."""
    if not rep_row:
        lines = [
            f"🎖 **تعیین سطح تعرفهٔ زیرنماینده** (`{user_id}`):", "",
            "این شخص هنوز رکورد نمایندگی ندارد، پس سطح تعرفه‌ای برای تعیین وجود ندارد.",
            "ابتدا او را با دکمهٔ «ارتقاء به زیرنمایندهٔ من» به زیرنمایندگی خود اضافه کنید.",
        ]
        rows = [[InlineKeyboardButton(
            "🔙 بازگشت به پروفایل ۳۶۰ درجه",
            callback_data=profile_callback("view", user_id, origin, ref))]]
        return "\n".join(lines), rows
    try:
        with database.get_db() as conn:
            policies = conn.execute(
                "SELECT tier_code, title_fa, discount_bps FROM tier_policies "
                "WHERE actor_type='rep' ORDER BY rank",
            ).fetchall()
    except Exception:
        logger.debug("Tier policy list failed", exc_info=True)
        policies = []
    current = str(rep_row.get("tier") or "normal")
    lines = [f"🎖 **تعیین سطح تعرفهٔ زیرنماینده** (`{user_id}`):", "",
             f"سطح فعلی: **{escape_markdown(_tier_title('rep', current))}**", "",
             "برای تغییر، سطح مقصد را انتخاب کنید:"]
    rows = []
    for policy in policies:
        code = str(policy["tier_code"])
        marker = " ✅" if code == current else ""
        rows.append([InlineKeyboardButton(
            f"{policy['title_fa']}{marker}",
            callback_data=profile_callback(f"sc_tier_do_{code}", user_id, origin, ref))])
    if not policies:
        lines.append("   • سیاست سطحی برای نمایندگان تعریف نشده است.")
    rows.append([InlineKeyboardButton(
        "🔙 بازگشت به پروفایل ۳۶۰ درجه",
        callback_data=profile_callback("view", user_id, origin, ref))])
    return "\n".join(lines), rows


async def _apply_scoped_promotion(update, context, user_id: int, viewer_rep_id: int,
                                  origin: str, ref: str) -> bool:
    """Promote a downstream person into the viewer's own sub-tree, in ONE transaction.

    ``rep_tree_service.promote_actor`` writes the representative row, its single
    active ``rep_edges`` row and the base role together — the same canonical writer
    the root promotion uses, so a scoped promotion can never leave a representative
    whose price chain has no edge (``BUG-TIER-001``).

    A person who already hangs under a *different* parent is refused: the scope rule
    admits them only because they are a customer of the viewer, and re-parenting
    somebody else's sub-representative is not a scoped operation.
    """
    query = update.callback_query
    existing = _rep_row_for(user_id)
    if existing and existing.get("parent_rep_id") not in (None, int(viewer_rep_id)):
        await query.answer("⛔️ این شخص زیرمجموعهٔ نمایندهٔ دیگری است.", show_alert=True)
        return True
    u_info = database.get_bot_user_by_tg_id(user_id) or {}
    name = str(u_info.get("full_name") or u_info.get("username") or f"rep-{int(user_id)}")
    try:
        import rep_tree_service
        rep_tree_service.promote_actor(
            {"actor_tg_id": int(getattr(update.effective_user, "id", 0) or 0),
             "rep_id": int(viewer_rep_id)},
            int(user_id),
            rep_code=f"rep-{int(user_id)}",
            name=name,
            parent_rep_id=int(viewer_rep_id),
            scope_filter=str(u_info.get("username") or ""),
        )
    except Exception:
        logger.exception("Scoped promotion failed for %s", user_id)
        await query.answer("❌ ارتقاء ناموفق بود؛ لاگ سرور را بررسی کنید.", show_alert=True)
        return True
    await query.answer("✅ این شخص زیرنمایندهٔ شما شد.", show_alert=True)
    text, rows = build_profile_page(user_id, origin=origin, ref=ref,
                                    allow_management=False, viewer_rep_id=viewer_rep_id)
    await message_edit.render_page(
        query, text=text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows))
    return True


async def _apply_scoped_tier(update, context, user_id: int, tier_code: str,
                             viewer_rep_id: int, origin: str, ref: str) -> bool:
    """Set the tier of the scoped sub-representative against the representative catalog.

    ``rep_tree_service.set_actor_tier`` is root-only by design, so a scoped change
    cannot go through it; the tier code is validated against ``tier_policies`` here
    instead, because the picker is not the only way to reach this section.
    """
    query = update.callback_query
    rep_row = _rep_row_for(user_id)
    if not rep_row:
        await query.answer(
            "⚠️ این شخص هنوز نماینده نیست؛ ابتدا او را به زیرنمایندگی ارتقاء دهید.",
            show_alert=True)
        return True
    try:
        with database.get_db() as conn:
            policy = conn.execute(
                "SELECT 1 FROM tier_policies WHERE actor_type='rep' AND tier_code=?",
                (str(tier_code),),
            ).fetchone()
            if policy is None:
                await query.answer("⚠️ این سطح تعرفه برای نمایندگان تعریف نشده است.",
                                   show_alert=True)
                return True
            conn.execute("UPDATE representatives SET tier=? WHERE id=?",
                         (str(tier_code), int(rep_row["id"])))
            conn.commit()
    except Exception:
        logger.exception("Scoped tier change failed for %s", user_id)
        await query.answer("❌ تغییر سطح ناموفق بود.", show_alert=True)
        return True
    title = _tier_title("rep", str(tier_code))
    await query.answer(f"✅ سطح زیرنماینده به «{title}» تغییر یافت.", show_alert=True)
    text, rows = build_profile_page(user_id, origin=origin, ref=ref,
                                    allow_management=False, viewer_rep_id=viewer_rep_id)
    await message_edit.render_page(
        query, text=text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=InlineKeyboardMarkup(rows))
    return True


# --------------------------------------------------------------------------
# Cross-persona management pages (root and scoped representative)
# --------------------------------------------------------------------------

def _person_line(user_id: int) -> str:
    u_info = database.get_bot_user_by_tg_id(user_id) or {}
    name = str(u_info.get("full_name") or u_info.get("username") or "ثبت نشده")
    return f"{escape_markdown(name)} (`{int(user_id)}`)"


def _service_count(user_id: int) -> int:
    return _count(
        "SELECT COUNT(*) FROM accounts WHERE owner_tg_id=? AND deleted_at IS NULL",
        (int(user_id),))


def build_issue_service_page(user_id: int, *, viewer_rep_id: int | None = None,
                             origin: str = "", ref: str = "") -> tuple[str, str, list]:
    """The issuance entry point for one person, for the root and for a scoped rep.

    Issuing a service is a panel-side operation in this build: the account is created
    on a panel and only then owned by the customer. This page therefore does not
    invent a local ``accounts`` row for a service that exists on no panel — it routes
    the operator to the issuance surfaces that exist for their persona and keeps this
    person's 360-degree card as the way back.

    Returns ``(title, body, rows)`` for the v5.2 renderer, which escapes the body.
    """
    user_id = int(user_id)
    scoped = viewer_rep_id is not None and scoped_rep_scope_holds(user_id, viewer_rep_id)
    is_root = viewer_rep_id is None
    lines = [
        f"کاربر: {_person_line(user_id)}",
        f"سرویس‌های فعلی این کاربر: {_service_count(user_id)}",
        "",
        "صدور سرویس در این نسخه روی پنل انجام می‌شود و اکانت ساخته‌شده به همین "
        "کاربر تخصیص می‌یابد؛ این صفحه هیچ اکانت محلی بدون پنل نمی‌سازد.",
    ]
    rows: list[list] = []
    if is_root:
        lines.append("")
        lines.append("به‌عنوان مدیر کل، از استخر آزاد یا با اتصال یک اکانت موجود ادامه دهید.")
        rows.append([InlineKeyboardButton("🏊 استخر آزاد و اکانت‌های تخصیص‌نیافته",
                                          callback_data="admin:pool")])
        rows.append([InlineKeyboardButton(
            "🔗 اتصال اکانت موجود به این کاربر",
            callback_data=_with_context(
                f"admin:prompt_link_user_id:{user_id}", origin, ref))])
    elif scoped:
        lines.append("")
        lines.append("به‌عنوان نمایندهٔ بالاسری این کاربر، صدور اکانت جدید از استخر آزاد "
                     "پنل نمایندگی انجام می‌شود؛ در فرم صدور، نام همین کاربر را وارد کنید.")
        rows.append([InlineKeyboardButton(
            "🆕 صدور اکانت جدید از استخر آزاد (پنل نمایندگی)",
            callback_data="rep:new_acc_plans")])
    rows.append([InlineKeyboardButton(
        f"📱 سرویس‌های فعلی این کاربر ({_service_count(user_id)})",
        callback_data=profile_callback("services", user_id, origin, ref))])
    rows.append([InlineKeyboardButton(
        "🔙 بازگشت به پروفایل ۳۶۰ درجه",
        callback_data=profile_callback("view", user_id, origin, ref))])
    return "🆕 صدور سرویس جدید برای این کاربر", "\n".join(lines), rows


def build_transfer_ownership_page(user_id: int, *, viewer_rep_id: int | None = None,
                                  origin: str = "", ref: str = "") -> tuple[str, str, list]:
    """Move one of this person's accounts to another owner, from their 360 card.

    The transfer itself is the existing root-only v5.2 ownership flow
    (``v52:transfer:<account_id>`` → ``ownership_service.preview_transfer`` with its
    own confirmation), so this page only selects the account. A scoped
    representative cannot move ownership — the writer requires the root identity —
    so for them the page says so and offers the management note instead of a button
    that would only ever answer «فقط در اختیار root است».

    Returns ``(title, body, rows)`` for the v5.2 renderer, which escapes the body.
    """
    user_id = int(user_id)
    scoped = viewer_rep_id is not None and scoped_rep_scope_holds(user_id, viewer_rep_id)
    is_root = viewer_rep_id is None
    try:
        accounts = database.get_user_accounts(user_id) or []
    except Exception:
        logger.debug("Account list failed for %s", user_id, exc_info=True)
        accounts = []
    lines = [
        f"کاربر: {_person_line(user_id)}",
        f"سرویس‌های قابل انتقال: {len(accounts)}",
        "",
    ]
    rows: list[list] = []
    if not accounts:
        lines.append("این کاربر سرویس ثبت‌شده‌ای ندارد؛ چیزی برای انتقال مالکیت وجود ندارد.")
    elif is_root:
        lines.append("اکانتی را که مالکیتش منتقل می‌شود انتخاب کنید. انتقال در سه گام بعدی "
                     "با پیش‌نمایش و تایید نهایی انجام می‌شود و هیچ تغییری پیش از تایید "
                     "اعمال نمی‌شود.")
        for account in accounts[:10]:
            account_id = account.get("id")
            if not account_id:
                continue
            code = str(account.get("account_code") or account.get("email") or account_id)
            rows.append([InlineKeyboardButton(
                f"🔄 انتقال مالکیت {code[:28]}",
                callback_data=f"v52:transfer:{int(account_id)}")])
    else:
        lines.append("انتقال مالکیت اکانت فقط توسط مدیر کل سیستم انجام می‌شود. "
                     "برای پیگیری، درخواست خود را به‌صورت یادداشت مدیریتی ثبت کنید.")
        for account in accounts[:10]:
            code = str(account.get("account_code") or account.get("email") or "—")
            lines.append(f"• {escape_markdown(code)}")
        if scoped:
            rows.append([InlineKeyboardButton(
                SCOPED_NOTE_LABEL,
                callback_data=profile_callback("sc_note", user_id, origin, ref))])
    rows.append([InlineKeyboardButton(
        "🔙 بازگشت به پروفایل ۳۶۰ درجه",
        callback_data=profile_callback("view", user_id, origin, ref))])
    return "🔄 انتقال مالکیت اکانت", "\n".join(lines), rows


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
    # The scoped control set belongs to the *representative* the card is rendered
    # for, so it is derived from the actor's permanent base role rather than from
    # the persona switcher: a super admin viewing the representative workspace is
    # still the root here and keeps the full control set.
    viewer_rep_id: int | None = None
    if not is_root and base_rep:
        try:
            candidate = int(base_rep.get("id") or 0)
        except (TypeError, ValueError):
            candidate = 0
        viewer_rep_id = candidate if candidate > 0 else None
    scoped = viewer_rep_id is not None and scoped_rep_scope_holds(user_id, viewer_rep_id)
    if section.startswith("sc_") and not scoped:
        # Re-checked on every press, not only when the button was drawn: an old
        # message, a forwarded callback or a target that stopped being downstream
        # must not reach a mutating scoped action.
        await query.answer(
            "⛔️ این عملیات فقط برای نمایندهٔ بالاسری فعال همان کاربر مجاز است.",
            show_alert=True)
        return True

    if section == "view":
        text, rows = build_profile_page(
            user_id, origin=origin, ref=ref, allow_management=allow_management,
            viewer_rep_id=viewer_rep_id)
    elif section == "services":
        text, rows = _services_page(user_id, origin, ref)
    elif section == "tickets":
        text, rows = _tickets_page(user_id, origin, ref)
    elif section == "abuse":
        text, rows = _tickets_page(user_id, origin, ref, abuse=True)
    elif section == "tx":
        text, rows = _transactions_page(user_id, origin, ref, _rep_row_for(user_id))
    elif section == "subs":
        text, rows = _subs_page(user_id, origin, ref, _rep_row_for(user_id),
                                allow_management=allow_management)
    elif section == "notes":
        text, rows = _notes_page(user_id, origin, ref, viewer_rep_id=viewer_rep_id)
    elif section == "notenew":
        if not allow_management:
            await query.answer("⛔️ ثبت یادداشت فقط برای مدیریت مجاز است.", show_alert=True)
            return True
        return await _begin_note_input(update, context, user_id, origin, ref)
    elif section == "sc_note":
        return await _begin_note_input(update, context, user_id, origin, ref,
                                       scoped_rep_id=viewer_rep_id)
    elif section == "sc_promote":
        text, rows = _scoped_promote_confirm_page(user_id, origin, ref, viewer_rep_id)
    elif section == "sc_promote_do":
        return await _apply_scoped_promotion(
            update, context, user_id, viewer_rep_id, origin, ref)
    elif section == "sc_tier":
        text, rows = _scoped_tier_page(user_id, origin, ref, _rep_row_for(user_id))
    elif section.startswith("sc_tier_do_"):
        return await _apply_scoped_tier(
            update, context, user_id, section[len("sc_tier_do_"):], viewer_rep_id,
            origin, ref)
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


async def _begin_note_input(update, context, user_id: int, origin: str, ref: str,
                            *, scoped_rep_id: int | None = None) -> bool:
    """Open the 60-second note form, reusing the admin FSM.

    ``scoped_rep_id`` marks the form as a scoped representative's own note. The
    writer stays where it already lives — ``admin_panel``'s ``profile_note`` action —
    and the marker is what lets the text router hand that one form to it for a
    non-root actor without opening any other admin form action to them.
    """
    import admin_panel
    state = {
        "action": "profile_note",
        "target_user_id": int(user_id),
        "origin": origin,
        "ref": ref,
        "ts": __import__("time").time(),
        "return_callback": profile_callback("notes", user_id, origin, ref),
    }
    if scoped_rep_id:
        state["scoped_rep_id"] = int(scoped_rep_id)
    context.user_data["admin_state"] = state
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
    "ISSUE_SERVICE_LABEL", "PROFILE_ORIGINS", "SCOPED_NOTE_LABEL",
    "SCOPED_PROMOTE_LABEL", "SCOPED_SECTION_TITLE", "SCOPED_TIER_LABEL",
    "TRANSFER_OWNERSHIP_LABEL", "account_card_callback", "back_row",
    "build_issue_service_page", "build_notes_page", "build_profile_page",
    "build_transfer_ownership_page", "escape_markdown", "escape_markdown_code",
    "handle_profile_callback", "management_callback", "parse_profile_callback",
    "profile_callback", "scoped_rep_scope_holds", "ticket_callback",
]
