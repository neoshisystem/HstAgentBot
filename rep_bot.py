import os
import sys
import json
import logging
import asyncio
import time
import re
import html
from types import SimpleNamespace
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
import v52_router
import ui_v52
import finance_manager
import message_edit
import profile_360
from receipt_notifications import process_receipt_notification_outbox
import sub_manager
from xui_result import XUIResult, classify_xui_result

from server_integrations import load_integration
xui_manager = load_integration("xui_manager")

# Set up logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger("HstAgentBot")


def _is_private_super_admin(update: Update) -> bool:
    """True only for the configured Super Admin in the bot's private chat."""
    user = update.effective_user
    chat = update.effective_chat
    return bool(user and chat and user.id == ADMIN_TELEGRAM_ID and chat.type == "private")



def _xui_operation_succeeded(result) -> bool:
    """True only for a consistent explicit success response."""
    return classify_xui_result(result) == XUIResult.SUCCESS

def _xui_operation_failed(result) -> bool:
    """True only for a consistent explicit failure response."""
    return classify_xui_result(result) == XUIResult.FAILURE


def _build_account_keyboard(
    account_code: str, *, user_id: int, is_enabled: bool = True,
    role: str = "rep", pending_res: dict | None = None,
):
    """Build an account card using the persisted per-account renewal policies."""
    modes = database.get_account_renewal_modes(account_code, user_id)
    return nlp_matcher.build_account_keyboard(
        account_code,
        is_enabled=is_enabled,
        on_first_connect=modes["first_connect_mode"],
        rollover_enabled=modes["rollover_enabled"],
        role=role,
        pending_res=pending_res,
    )


def _can_manage_account_policy(role: str, rep: dict | None, account_code: str) -> bool:
    """Require account ownership for representatives before changing renewal policy."""
    if role == "super_admin":
        return True
    if role == "rep" and rep:
        return bool(database.is_account_in_rep_scope(rep["id"], account_code))
    return False


async def safe_edit_message(
    query, text: str, reply_markup=None, parse_mode=ParseMode.MARKDOWN,
    delete_transient_photo: bool = False,
):
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
        sent_message = await query.message.reply_text(
            text, reply_markup=reply_markup, parse_mode=parse_mode
        )
        caption = (getattr(query.message, "caption", None) or "")
        is_transient_qr_card = (
            delete_transient_photo
            and bool(getattr(query.message, "photo", None))
            and "QR Code" in caption
            and ("سابسکریپشن" in caption or "کانفیگ" in caption)
        )
        if is_transient_qr_card:
            try:
                await query.message.delete()
            except Exception as cleanup_error:
                logger.warning(
                    "Could not remove transient QR navigation card after text fallback: %s",
                    cleanup_error,
                )
        return sent_message
    except Exception as e:
        logger.error(f"safe_edit_message unhandled error: {e}")


async def _cleanup_deposit_amount_prompt(context: ContextTypes.DEFAULT_TYPE, chat_id: int) -> None:
    """Best-effort cleanup for the dedicated card-deposit amount prompt only."""
    state = context.user_data.pop("deposit_form_state", None) or {}
    prompt_msg_id = state.pop("prompt_msg_id", None)
    if prompt_msg_id is None:
        return
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=prompt_msg_id)
    except Exception as cleanup_error:
        logger.warning(
            "Could not remove transient card-deposit amount prompt %s: %s",
            prompt_msg_id, cleanup_error,
        )


def send_admin_log(text: str, rep_id: int | None = None):
    """Queue operational reports through trusted Product routing and the outbox.

    Caller-provided chat/topic identifiers and helper-module tokens are never
    used. Abuse reports are routed by report_router exclusively to root DM.
    """
    try:
        from internal_reporting import send_telegram_message

        result = send_telegram_message(
            None,
            str(text),
            parse_mode="HTML",
            rep_id=rep_id,
        )
        if not isinstance(result, dict) or result.get("queued") is not True:
            logger.warning(
                "Admin report was not queued (state=%s)",
                result.get("state", "unknown") if isinstance(result, dict) else "unknown",
            )
        return result
    except Exception as exc:
        # Avoid logging exception payloads that may contain deployment secrets.
        logger.error("Failed to queue admin report: %s", type(exc).__name__)
        return {"ok": False, "queued": False, "state": "unknown"}

# --- Commands ---


def _onboard_v52_start(user, bot_id: int) -> tuple[str, dict | None]:
    """Idempotently establish identity, default profile, wallet and bot customer link."""
    import identity_service
    import subscription_service

    actor_id = int(user.id)
    is_root = actor_id == int(ADMIN_TELEGRAM_ID)
    now = int(time.time())
    with database.get_db() as conn:
        prior_settings = conn.execute(
            "SELECT active_persona FROM user_settings WHERE user_id=?", (actor_id,)
        ).fetchone()
        if is_root:
            identity_service.ensure_personal_identity(conn, actor_id)
            if prior_settings is None:
                conn.execute(
                    "UPDATE user_settings SET active_persona='admin' WHERE user_id=?",
                    (actor_id,),
                )
        else:
            conn.execute(
                "INSERT INTO bot_users(telegram_id,role,username,full_name) "
                "VALUES(?,'end_user',?,?) ON CONFLICT(telegram_id) DO UPDATE SET "
                "username=CASE WHEN excluded.username<>'' THEN excluded.username ELSE bot_users.username END,"
                "full_name=CASE WHEN excluded.full_name<>'' THEN excluded.full_name ELSE bot_users.full_name END",
                (actor_id, user.username or "", user.full_name or ""),
            )
            conn.execute(
                "INSERT INTO user_wallets(user_id,balance_toman) VALUES(?,0) "
                "ON CONFLICT(user_id) DO NOTHING",
                (actor_id,),
            )
            conn.execute(
                "INSERT INTO user_settings(user_id,first_connect_mode,active_persona) "
                "VALUES(?,0,'end_user') ON CONFLICT(user_id) DO NOTHING",
                (actor_id,),
            )
        subscription_service.ensure_default_profile(conn, actor_id, now=now)
        bot = conn.execute(
            "SELECT status FROM bot_registry WHERE bot_id=?", (int(bot_id),)
        ).fetchone()
        if bot is not None and str(bot["status"]).lower() == "active":
            conn.execute(
                "INSERT INTO bot_customers(bot_id,user_tg_id,started_at) VALUES(?,?,?) "
                "ON CONFLICT(bot_id,user_tg_id) DO NOTHING",
                (int(bot_id), actor_id, now),
            )
        actor = identity_service.resolve_actor(conn, actor_id)
        setting = conn.execute(
            "SELECT active_persona FROM user_settings WHERE user_id=?", (actor_id,)
        ).fetchone()
        selected = str(setting["active_persona"] if setting else "end_user").strip().lower()

        if actor.is_root:
            if selected in {"rep", "representative"} and actor.rep_id is not None:
                rep_row = conn.execute(
                    "SELECT * FROM representatives WHERE id=? AND is_active=1 AND deleted_at IS NULL",
                    (actor.rep_id,),
                ).fetchone()
                if rep_row is not None:
                    return "rep", dict(rep_row)
            if selected in {"end_user", "user"}:
                return "end_user", None
            # Only ADMIN_TELEGRAM_ID can resolve to the administrative persona.
            return "super_admin", None

        if selected in {"rep", "representative"} and actor.persona == "rep" and actor.rep_id is not None:
            rep_row = conn.execute(
                "SELECT * FROM representatives WHERE id=? AND is_active=1 AND deleted_at IS NULL",
                (actor.rep_id,),
            ).fetchone()
            if rep_row is not None:
                return "rep", dict(rep_row)
        # A forged 'admin' persona on an ordinary account resolves to the user portal.
        return "end_user", None


def _register_application_handlers(application: Application, bot_id: int | None = None):
    """Attach the exact same command/message/callback surface to each isolated bot."""
    application.add_handler(CommandHandler("start", start_cmd))
    application.add_handler(CommandHandler("admin", start_cmd))
    application.add_handler(CommandHandler("panel", start_cmd))
    application.add_handler(CommandHandler("menu", start_cmd))
    application.add_handler(CommandHandler("cancel", cancel_cmd))
    application.add_handler(CommandHandler("link", link_cmd))
    application.add_handler(CommandHandler("pricing", pricing_cmd))
    application.add_handler(CommandHandler("setprice", setprice_cmd))
    application.add_handler(CommandHandler("billing", billing_cmd))
    application.add_handler(CommandHandler("balance", billing_cmd))
    application.add_handler(CommandHandler("factor", billing_cmd))
    application.add_handler(CommandHandler("wallet", wallet_cmd))
    application.add_handler(CommandHandler("deposit", deposit_cmd))
    application.add_handler(CommandHandler("card", card_cmd))
    application.add_handler(CommandHandler("pay", pay_cmd))
    application.add_handler(CommandHandler("sync", sync_cmd))
    application.add_handler(CommandHandler("operations", operations_cmd))
    application.add_handler(CommandHandler("unlock", unlock_operation_cmd))
    application.add_handler(CommandHandler("resolveintent", resolve_user_intent_cmd))
    application.add_handler(CommandHandler("resolverepintent", resolve_rep_intent_cmd))
    application.add_handler(CommandHandler("resolvereprenewal", resolve_rep_renewal_cmd))
    application.add_handler(CommandHandler("resolvereservation", resolve_reservation_cmd))
    application.add_handler(CommandHandler("resolveadminrenewal", resolve_admin_renewal_cmd))
    application.add_handler(CommandHandler("stats", stats_cmd))
    application.add_handler(MessageHandler(filters.PHOTO | filters.Document.IMAGE, handle_photo))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    application.add_handler(CallbackQueryHandler(handle_callback))


async def _run_multi_bot_runtime():
    """Verify primary getMe, then start all registry-owned bots with one shared scheduler."""
    import hmac
    import multi_bot_runtime
    from crypto_store import decrypt_secret

    with database.get_db() as conn:
        primary = conn.execute(
            "SELECT bot_id,token_cipher,status FROM bot_registry "
            "WHERE is_primary=1 ORDER BY bot_id"
        ).fetchall()
    if len(primary) != 1 or str(primary[0]["status"]).lower() != "active":
        raise RuntimeError("multi-bot rollout requires exactly one active primary registry identity")
    primary_id = int(primary[0]["bot_id"])
    try:
        registered_token = decrypt_secret(str(primary[0]["token_cipher"]), "hstagent/columns/v1")
    except Exception:
        raise RuntimeError("primary bot registry credential could not be verified") from None
    if not hmac.compare_digest(str(BOT_TOKEN), str(registered_token)):
        raise RuntimeError("BOT_TOKEN does not match the encrypted primary registry credential")

    preflight = Application.builder().token(BOT_TOKEN).build()
    try:
        await preflight.initialize()
        actual = await preflight.bot.get_me()
    finally:
        try:
            await preflight.shutdown()
        except Exception:
            pass
    if int(getattr(actual, "id", 0) or 0) != primary_id:
        raise RuntimeError("Telegram getMe primary identity does not match bot_registry")

    # One scheduler object is shared with every isolated Application. The
    # reservation and receipt workers start once, against the verified primary.
    scheduler = {"owner_bot_id": primary_id, "watchdog_interval_seconds": 300}
    with database.get_db() as conn:
        runtime = await multi_bot_runtime.run_registered_bots(
            conn, database=database, scheduler=scheduler,
            handler_factory=lambda app, selected_bot_id: _register_application_handlers(app, selected_bot_id),
        )
    primary_app = runtime.get("applications", {}).get(primary_id)
    if primary_app is None:
        await multi_bot_runtime.stop_registered_bots(runtime)
        raise RuntimeError("verified primary bot failed to start in multi-bot runtime")
    if runtime.get("failed_bot_ids"):
        logger.error("Some registered bots failed to start: %s", runtime["failed_bot_ids"])

    workers = [
        asyncio.create_task(watchdog_worker(primary_app), name="central-reservation-watchdog"),
        asyncio.create_task(receipt_notification_worker(primary_app), name="central-receipt-outbox"),
    ]
    try:
        await asyncio.Event().wait()
    finally:
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await multi_bot_runtime.stop_registered_bots(runtime)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    thread_id = update.message.message_thread_id if update.message else None

    await _cleanup_deposit_amount_prompt(context, chat.id)
    _reset_all_conversation_states(user.id, _bot_id_of(context), context)

    if _runtime_flag("v52_multi_bot_enabled") and chat and chat.type == "private":
        try:
            role, rep = _onboard_v52_start(user, int(context.bot.id))
        except Exception:
            logger.exception("v5.2 start onboarding failed")
            try:
                fallback_role, _fallback_rep = database.get_user_role(user.id, chat.id, thread_id)
            except Exception:
                fallback_role = "end_user"
            await update.message.reply_text(
                "⛔️ راه‌اندازی هویت/پروفایل امن انجام نشد؛ دسترسی صادر نشد.",
                reply_markup=build_actor_navigation_keyboard(fallback_role),
            )
            return
    else:
        role, rep = database.get_user_role(user.id, chat.id, thread_id)

    # The PERMANENT role, captured before the persona switcher can downgrade it.
    # The registration gate must consult this: root browsing the end-user
    # workspace is still root and must never be asked for a referral code.
    base_role, base_rep = role, rep
    if chat and chat.type == "private" and _runtime_flag("v52_persona_switcher_enabled"):
        role, rep = _resolve_active_persona_role(user, base_role=role, base_rep=rep)

    if await _submit_v52_order_receipt(update, context, role):
        return
    if role == "unauthorized_group":
        return
    if database.is_user_blocked(user.id):
        await update.message.reply_text("⛔️ دسترسی حساب شما به این بات توسط مدیریت مسدود شده است.")
        return
    if await _apply_registration_gate(
            update, context, role, rep, base_role=base_role, base_rep=base_rep):
        return
    if role == "super_admin":
        await admin_panel.show_admin_dashboard(update, context)
        return
    if role == "rep":
        await rep_panel.show_rep_dashboard(update, context, rep)
        return
    await user_portal.show_user_portal(update, context)

def _v52_actor_context(update, context, persona=None, rep=None):
    user, chat = getattr(update, "effective_user", None), getattr(update, "effective_chat", None)
    bot = getattr(context, "bot", None)
    query = getattr(update, "callback_query", None)
    source_message = getattr(query, "message", None) or getattr(update, "effective_message", None)
    return {"actor_tg_id": getattr(user, "id", None), "telegram_id": getattr(user, "id", None),
            "bot_id": getattr(bot, "id", None), "chat_id": getattr(chat, "id", None),
            "chat_type": getattr(chat, "type", None), "persona": persona or "user",
            "rep_id": rep.get("id") if isinstance(rep, dict) else None,
            "is_root": bool(getattr(user, "id", None) == ADMIN_TELEGRAM_ID),
            # The live conversation store and the message the button sits on. A
            # wizard started from a bridge handler must own the SAME ``user_data``
            # the text router reads, or its state is orphaned: the first step renders
            # and no answer ever reaches it. The message id lets it edit in place.
            "user_data": getattr(context, "user_data", None),
            "message_id": getattr(source_message, "message_id", None)}


_V52_TECHNICAL_ERRORS = (TypeError, AttributeError, NameError, ModuleNotFoundError,
                         ImportError, IndexError, KeyError, NotImplementedError)

_V52_ERROR_MESSAGES = {
    "a verified canonical subscription host is unavailable":
        "دامنهٔ اشتراک تأییدشده برای این بات ثبت نشده است؛ ابتدا دامنه را در «مدیریت دامنه‌ها» ثبت و تأیید کنید.",
    "the default profile cannot be deleted": "پروفایل پیش‌فرض قابل حذف نیست.",
    "panel URL must be a non-empty trimmed string":
        "اتصال این پنل هنوز تنظیم نشده است؛ ابتدا از «جزئیات پنل → ویرایش اتصال» نشانی و توکن را ثبت کنید.",
    "actor cannot view this account": "این اکانت برای حساب شما قابل مشاهده نیست.",
    "actor cannot view this account configuration": "پیکربندی این اکانت برای حساب شما قابل مشاهده نیست.",
    "support ticket does not exist": "تیکت موردنظر وجود ندارد.",
    "پنل وجود ندارد.": "پنل موردنظر وجود ندارد.",
}


def _v52_error_page(exc, persona: str, data: str):
    """Render a user-facing error page; programming errors keep their technical text."""
    from ui_v52 import render_error
    parent = "v52:home:" + persona
    if isinstance(exc, _V52_TECHNICAL_ERRORS):
        return render_error(f"اجرای صفحهٔ v5.2 ممکن نشد: {exc}", persona=persona,
                            parent_callback=parent)
    if isinstance(exc, PermissionError) or type(exc).__name__.endswith("Denied"):
        return render_error("دسترسی به این مورد برای حساب شما مجاز نیست.", persona=persona,
                            parent_callback=parent)
    raw = str(exc).strip()
    message = _V52_ERROR_MESSAGES.get(raw) or raw or "این عملیات در وضعیت فعلی قابل انجام نیست."
    return render_error(message, persona=persona, parent_callback=parent)


def _resolve_active_persona_role(user, *, base_role, base_rep):
    """Honour ``user_settings.active_persona`` for private-chat routing.

    Mirrors ``_onboard_v52_start``'s resolution without its onboarding writes.
    Only consulted while the ``v52_persona_switcher_enabled`` flag is on, so the
    legacy role routing stays byte-identical when the feature is off.
    """
    if base_role in {"unauthorized_group", "blocked"}:
        return base_role, base_rep
    actor_id = getattr(user, "id", None)
    if actor_id is None:
        return base_role, base_rep
    actor_id = int(actor_id)
    try:
        import identity_service
        with database.get_db() as conn:
            actor = identity_service.resolve_actor(conn, actor_id)
            setting = conn.execute(
                "SELECT active_persona FROM user_settings WHERE user_id=?", (actor_id,)
            ).fetchone()
            selected = str(setting["active_persona"] if setting else "end_user").strip().lower()
            if actor.is_root:
                if selected in {"rep", "representative"} and actor.rep_id is not None:
                    row = conn.execute(
                        "SELECT * FROM representatives WHERE id=? AND is_active=1 "
                        "AND deleted_at IS NULL", (int(actor.rep_id),)).fetchone()
                    if row is not None:
                        return "rep", dict(row)
                if selected in {"end_user", "user"}:
                    return "end_user", None
                return "super_admin", None
            if selected in {"rep", "representative"} and actor.persona == "rep" \
                    and actor.rep_id is not None:
                row = conn.execute(
                    "SELECT * FROM representatives WHERE id=? AND is_active=1 "
                    "AND deleted_at IS NULL", (int(actor.rep_id),)).fetchone()
                if row is not None:
                    return "rep", dict(row)
            return "end_user", None
    except Exception:
        logger.debug("persona resolution fell back to the legacy role", exc_info=True)
        return base_role, base_rep


def _base_role_of(user, chat, thread_id):
    """The actor's permanent role, ignoring whichever persona they are viewing.

    Management notifications (receipt review, backups, alerts) are delivered to
    the private chat and must stay actionable for the root actor even while the
    persona switcher has them inside the representative or end-user workspace.
    """
    try:
        role, rep = database.get_user_role(user.id, chat.id, thread_id)
    except Exception:
        return None, None
    return role, rep


def _is_root_actor(user) -> bool:
    """True only for the configured root account, by permanent identity.

    Deliberately independent of the resolved persona and of the chat type: a
    receipt lands in the finance topic of the supergroup, and the root operator
    must be able to act on it there. The id is compared against the configured
    root, never against a stored ``bot_users.role`` — that row is seeded as
    ``end_user`` for every new user and is therefore not a trust signal.
    """
    try:
        actor_id = int(getattr(user, "id", 0) or 0)
    except (TypeError, ValueError):
        return False
    if actor_id <= 0:
        return False
    try:
        root_id = int(ADMIN_TELEGRAM_ID or 0)
    except (TypeError, ValueError):
        root_id = 0
    return root_id > 0 and actor_id == root_id


#: Admin callbacks that stay usable from the finance topic of the supergroup.
#: They are receipt-scoped: settling or inspecting one receipt, plus the two
#: navigation buttons that receipt carries. Everything else under ``admin:``
#: still requires the bot's private chat, because those pages expose whole-system
#: financial and personal records.
_RECEIPT_TOPIC_ADMIN_PREFIXES = (
    "admin_approve_rcp_", "admin_reject_rcp_", "admin_detail_rcp_",
    "admin:user_detail:", "admin:main",
)


def _is_receipt_topic_context(chat, thread_id) -> bool:
    """True when the callback came from the configured receipt/finance topic.

    Resolved at call time from the live forum bindings, so a group or topic
    created after startup is honoured instead of a ``config`` constant captured
    at import.
    """
    if chat is None:
        return False
    try:
        import forum_manager
        group_id, topic_id = forum_manager.resolve_topic_destination("pendingreceipts")
    except Exception:
        return False
    if not group_id or not topic_id:
        return False
    try:
        return int(chat.id) == int(group_id) and int(thread_id or 0) == int(topic_id)
    except (TypeError, ValueError):
        return False


def _may_use_admin_callback_in_group(user, chat, thread_id, data) -> bool:
    """The single decision point for admin callbacks outside the private chat.

    Only the configured root, only inside the receipt topic, and only for a
    receipt-scoped callback. Everything else keeps the private-chat requirement.
    """
    if not _is_root_actor(user):
        return False
    if not _is_receipt_topic_context(chat, thread_id):
        return False
    return str(data or "").startswith(_RECEIPT_TOPIC_ADMIN_PREFIXES)


async def _settle_receipt_message(query, receipt, status: str, actor) -> None:
    """Redraw a settled receipt in place, caption included.

    The notification is a PHOTO message, so the status line lives in the caption:
    swapping only the keyboard left «⏳ در انتظار بررسی» on screen after the
    decision was already final. ``message_edit.update_body`` writes the caption
    for a photo and the text for a plain message; a source that cannot be edited
    at all still receives the settled keyboard instead of a dead button.
    """
    decided_by = getattr(actor, "full_name", "") or getattr(actor, "username", "") or "Admin"
    settled_keyboard = finance_manager.build_admin_reviewed_keyboard(
        status, receipt.get("user_id"), receipt.get("id"))
    body = finance_manager.format_admin_receipt_card(
        {**receipt, "status": status}, decided_by=decided_by)
    try:
        await message_edit.update_body(
            query, text=body, parse_mode=ParseMode.MARKDOWN,
            reply_markup=settled_keyboard)
    except Exception:
        logger.debug("Could not redraw the settled receipt body; updating the keyboard only",
                     exc_info=True)
        await message_edit.edit_reply_markup(query, settled_keyboard)


def _persona_aware_role(user, chat, thread_id):
    """Resolve the acting role, honouring the persona switcher in private chats.

    Every entry point that reads a role for routing (commands, text, photo and
    callback handlers) must go through this helper. Resolving the base role in
    one handler and the persona role in another is exactly what let a
    super-admin start a wallet top-up in the end-user workspace and then have
    the amount entry rejected by the role guard.
    """
    role, rep = database.get_user_role(user.id, chat.id, thread_id)
    if chat is not None and getattr(chat, "type", None) == "private" \
            and _runtime_flag("v52_persona_switcher_enabled"):
        role, rep = _resolve_active_persona_role(user, base_role=role, base_rep=rep)
    return role, rep


def _resolve_persona_rep(user, rep=None) -> dict | None:
    """Return the representative workspace row for the acting user, if any.

    The configured root always owns the personal ``root_admin_rep`` row created
    by ``identity_service.ensure_personal_identity``, so the super-admin can open
    the representative workspace instead of hitting ``KeyError: 'id'``.
    """
    if isinstance(rep, dict) and rep.get("id"):
        return rep
    actor_id = getattr(user, "id", None)
    if actor_id is None:
        return None
    actor_id = int(actor_id)
    try:
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT * FROM representatives WHERE telegram_id=? AND is_active=1 "
                "AND deleted_at IS NULL ORDER BY id LIMIT 1", (actor_id,)
            ).fetchone()
            if row is None and actor_id == int(ADMIN_TELEGRAM_ID):
                import identity_service
                identity_service.ensure_personal_identity(conn, actor_id)
                row = conn.execute(
                    "SELECT * FROM representatives WHERE rep_code='root_admin_rep' "
                    "AND is_active=1 AND deleted_at IS NULL"
                ).fetchone()
    except Exception:
        logger.exception("v5.2 persona representative resolution failed")
        return None
    return dict(row) if row is not None else None


def _resolve_finance_continuation(user, context, role, rep):
    """Resolve the role/representative that owns an in-flight top-up workflow.

    The persona switcher lets the root actor start a wallet top-up in the
    end-user or representative workspace and then change persona before the
    amount or the receipt photo is sent. The persisted ``deposit_role`` names the
    workflow's role, so the continuation follows it; a genuine role change by a
    non-root actor is still refused. Returns ``(role, rep)`` or ``None``.
    """
    dep_role = context.user_data.get("deposit_role")
    if dep_role not in ("end_user", "rep"):
        return role, rep
    if dep_role == "rep":
        rep = _resolve_persona_rep(user, rep)
        if rep is None:
            return None
        return ("rep", rep) if role in ("rep", "super_admin") else None
    return ("end_user", None) if role in ("end_user", "super_admin") else None


def build_actor_navigation_keyboard(role: str) -> InlineKeyboardMarkup:
    """Return the "way home" keyboard for the acting role.

    Every handler branch that ends an interaction with a plain message attaches
    this, so a refusal, an error or a completed step never strands the actor in a
    chat with no buttons.
    """
    if role == "rep":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 امور مالی و فیش‌ها", callback_data="rep:finance")],
            [InlineKeyboardButton("🏠 بازگشت به پیشخوان نماینده", callback_data="rep:main")],
        ])
    if role == "end_user":
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("📱 سرویس‌های من", callback_data="user:services")],
            [InlineKeyboardButton("🏠 بازگشت به پورتال کاربری", callback_data="user:main")],
        ])
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 بازگشت به منوی مدیریت", callback_data="admin:main")],
    ])


def _begin_ownership_transfer(ctx, account_id: int, persona: str):
    """W17.19 root transfer: show the account card and ask for the new owner id."""
    from ui_v52 import render_page, render_error
    if not ctx.get("is_root"):
        return render_error("انتقال مالکیت فقط در اختیار root است.", persona=persona,
                            parent_callback=f"v52:sub:{account_id}")
    with database.get_db() as conn:
        account = conn.execute(
            "SELECT id,account_code,email,owner_tg_id,revision,deleted_at FROM accounts WHERE id=?",
            (int(account_id),),
        ).fetchone()
    if account is None or account["deleted_at"] is not None:
        return render_error("اکانت موردنظر وجود ندارد.", persona=persona,
                            parent_callback="v52:home:admin")
    v52_router.DEFAULT_FSM.set(
        ctx, "ownership_transfer", payload={"account_id": int(account_id)},
        parent_callback=f"v52:sub:{account_id}", now=int(time.time()),
    )
    owner = account["owner_tg_id"]
    return render_page(
        "انتقال مالکیت اکانت",
        f"اکانت: {account['account_code'] or account['email']}\n"
        f"مالک فعلی: {owner if owner else 'ثبت نشده'}\n"
        f"revision: {int(account['revision'])}\n\n"
        "شناسه عددی تلگرام مالک جدید را در پیام بعدی ارسال کنید. "
        "هیچ تغییری تا تأیید نهایی اعمال نمی‌شود.",
        persona=persona, page_kind="form", parent_callback=f"v52:sub:{account_id}",
    )


def _load_digital_products() -> list[dict]:
    """Read the active digital catalogue for the v5.2 shop renderer."""
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM digital_products WHERE is_active=1 ORDER BY sort_order, id"
        ).fetchall()
    return [dict(row) for row in rows]


def _load_digital_product(product_id: int) -> dict | None:
    """Read one digital product row; None when it does not exist."""
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM digital_products WHERE id=?", (int(product_id),)
        ).fetchone()
    return dict(row) if row is not None else None


def _load_digital_stock_counts() -> dict[int, int]:
    """Available inventory count per product, so the catalogue reports real stock."""
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT p.id AS product_id, COUNT(i.id) AS count FROM digital_products p "
            "LEFT JOIN digital_inventory i ON i.product_id=p.id AND i.status='available' "
            "GROUP BY p.id"
        ).fetchall()
    return {int(row["product_id"]): int(row["count"]) for row in rows}


def _load_v52_order(order_id: str) -> dict | None:
    """Load a payment order plus its product/delivery facts for the v5.2 delivery UI."""
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT order_id,actor_tg_id,bot_id,merchant_key,purpose,payment_rail,amount_toman,"
            "status,received_toman,created_at,expires_at,revision FROM payment_orders WHERE order_id=?",
            (str(order_id),)).fetchone()
        if row is None:
            return None
        order = {"order_id": str(row[0]), "actor_tg_id": int(row[1]), "bot_id": int(row[2]),
                 "merchant_key": str(row[3]), "purpose": str(row[4]), "payment_rail": str(row[5]),
                 "amount_toman": int(row[6]), "status": str(row[7]), "received_toman": int(row[8]),
                 "created_at": int(row[9]), "expires_at": int(row[10]), "revision": int(row[11])}
        product = conn.execute(
            "SELECT p.id,p.title,p.delivery_type FROM digital_products p "
            "JOIN quotes q ON q.product_id=p.id "
            "JOIN payment_orders o ON o.quote_id=q.quote_id WHERE o.order_id=?",
            (str(order_id),)).fetchone()
        if product is not None:
            order["product_id"] = int(product["id"])
            order["product_title"] = str(product["title"])
            order["delivery_type"] = str(product["delivery_type"])
    return order


def _load_plan(plan_code: str) -> dict | None:
    """Read one pricing plan row; None when it does not exist."""
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM pricing_plans WHERE plan_code=?", (str(plan_code),)).fetchone()
    return dict(row) if row is not None else None


def _plan_panel_scope(plan: dict) -> tuple[str, list[int], list[dict]]:
    """Resolve a plan's real panel bindings so edit_plan never raises on scope."""
    with database.get_db() as conn:
        ids = [int(r["panel_id"]) for r in conn.execute(
            "SELECT panel_id FROM plan_panels WHERE plan_code=? AND allowed=1 ORDER BY panel_id",
            (str(plan.get("plan_code")),)).fetchall()]
        panels = [dict(r) for r in conn.execute(
            "SELECT id,name,lifecycle FROM panels ORDER BY is_default DESC,id").fetchall()]
    declared = str(plan.get("panel_scope") or "selected").strip().lower()
    if declared == "selected" and not ids:
        # Nothing is bound yet: show the unscoped view instead of raising.
        declared = "legacy"
    return declared, ids, panels


def _v52_edit_plan(ctx, persona: str, plan_code: str, *, confirm_all: bool):
    """Render the plan editor for one plan (optionally across every panel)."""
    import finance_admin_ui
    from ui_v52 import render_error
    plan = _load_plan(plan_code)
    if plan is None:
        return render_error("تعرفهٔ موردنظر یافت نشد.", persona=persona, parent_callback="v52:plans")
    scope, ids, panels = _plan_panel_scope(plan)
    if confirm_all:
        scope = "all_explicit"
    return finance_admin_ui.edit_plan(
        ctx, plan, panel_scope=scope, selected_panel_ids=ids, current_panels=panels,
        confirm_all_explicit=bool(confirm_all), parent_callback="v52:plans")


def _v52_quote_page(ctx, persona: str, *, plan_code: str, account_id: int | None):
    """Build a real immutable quote and render the checkout preview for buy/renew."""
    import checkout_ui
    import pricing_service
    import quote_service
    from ui_v52 import render_error
    actor_id = int(ctx.get("actor_tg_id") or 0)
    bot_id = int(ctx.get("bot_id") or 0)
    now = int(time.time())
    purpose = "vpn_renew" if account_id else "vpn_buy"
    if account_id:
        with database.get_db() as conn:
            exists = conn.execute(
                "SELECT 1 FROM accounts WHERE id=? AND deleted_at IS NULL", (int(account_id),)
            ).fetchone()
        if exists is None:
            return render_error("اکانت موردنظر برای تمدید یافت نشد.", persona=persona,
                                parent_callback="v52:home:" + persona)
    try:
        with database.get_db() as conn:
            chain = pricing_service.price_chain(
                conn, plan_code=str(plan_code), seller_key="root",
                buyer_tg_id=actor_id, purpose="sale", now=now)
            quote = quote_service.create_quote(
                conn, actor_tg_id=actor_id, bot_id=bot_id,
                merchant_key=str(chain.get("merchant_key") or "root"), purpose=purpose,
                amount_toman=int(chain["amount_toman"]), spec={"plan_code": str(plan_code)},
                price_edges=list(chain.get("price_edges") or []), account_id=account_id,
                plan_code=str(plan_code), now=now)
    except Exception as exc:
        fallback = f"v52:account:{account_id}" if account_id else "v52:shop"
        return render_error(f"ساخت پیشنهاد قیمت ممکن نشد: {exc}", persona=persona,
                            parent_callback=fallback)
    if account_id:
        return checkout_ui.preview_renew(ctx, int(account_id), str(plan_code), quote=quote)
    return checkout_ui.preview_buy(ctx, str(plan_code), quote=quote)


def _v52_show_renewal_menu(ctx, persona: str, account_id: int):
    """Show the renewal plan selection menu with tier-discounted prices."""
    import time
    import pricing_service
    from ui_v52 import render_page, button, render_error
    actor_id = int(ctx.get("actor_tg_id") or 0)
    rep_id = ctx.get("rep_id")
    now = int(time.time())

    with database.get_db() as conn:
        # Confirm the account exists.
        account = conn.execute(
            "SELECT id,account_code FROM accounts WHERE id=? AND deleted_at IS NULL",
            (account_id,),
        ).fetchone()
        if account is None:
            return render_error("اکانت موردنظر یافت نشد.", persona=persona,
                                parent_callback="v52:home:" + persona)

        # Determine the actor's type and tier code.
        role_scope, tier_code = pricing_service._actor_tier_and_scope(
            conn, user_id=(actor_id if rep_id is None else None), rep_id=rep_id)

        # Look up the tier's discount (bps → percent).
        discount_pct = 0
        tier_row = conn.execute(
            "SELECT discount_bps,title_fa FROM tier_policies "
            "WHERE actor_type=? AND tier_code=?",
            (role_scope, tier_code),
        ).fetchone()
        if tier_row is not None:
            discount_pct = int(tier_row["discount_bps"]) // 100  # bps → percent

        # Load all active plans.
        plan_rows = conn.execute(
            "SELECT * FROM pricing_plans WHERE is_active=1 ORDER BY sort_order,plan_code"
        ).fetchall()

    lines = [f"حساب: {int(account['id'])} · {str(account['account_code'])}"]
    if tier_row is not None:
        lines.append(f"تخفیف رتبه ({str(tier_row['title_fa'])}): {discount_pct}٪")
    else:
        lines.append("تخفیف رتبه: بدون تخفیف (رتبه: عادی)")
    body = "\n".join(lines)

    # Separate quick plans from the rest.
    base_price_col = "rep_price" if persona == "rep" else "user_price"
    quick_plans = []
    other_plans = []
    for p in plan_rows:
        plan = dict(p)
        price = int(plan.get(base_price_col) or plan.get("rep_price") or plan.get("user_price") or 0)
        if discount_pct > 0:
            price = price - (price * discount_pct // 100)
        plan["_effective_price"] = price
        if plan.get("is_quick"):
            quick_plans.append(plan)
        else:
            other_plans.append(plan)

    buttons = []

    # Quick-access rows (up to 5).
    for plan in quick_plans[:5]:
        price_str = f"{plan['_effective_price']:,}"
        label = f"⚡ {plan['name']} · {price_str} تومان"
        buttons.append([button(label, f"v52:renew:{account_id}:{plan['plan_code']}")])

    # "All plans" button.
    parent = f"v52:sub:{account_id}" if persona == "rep" else f"v52:home:{persona}"
    buttons.append([button("📁 سایر تعرفه‌ها / کاتالوگ کامل", f"v52:renew:{account_id}:catalog")])

    return render_page("تمدید سرویس", body, persona=persona,
                       rows=buttons, parent_callback=parent)


def _v52_begin_order(ctx, persona: str, quote_id: str):
    """Create (or reuse) the payment order for a locked quote and offer the enabled rail."""
    import order_service
    from ui_v52 import render_page, button, render_error
    actor_id = int(ctx.get("actor_tg_id") or 0)
    bot_id = int(ctx.get("bot_id") or 0)
    now = int(time.time())
    try:
        with database.get_db() as conn:
            existing = conn.execute(
                "SELECT order_id,status FROM payment_orders WHERE quote_id=?", (str(quote_id),)
            ).fetchone()
            if existing is not None:
                order_id, status = str(existing["order_id"]), str(existing["status"])
            else:
                created = order_service.create_order(
                    conn, quote_id=str(quote_id), actor_tg_id=actor_id, bot_id=bot_id,
                    payment_rail="card_to_card", now=now)
                order_id = str(created["order_id"])
                status = str(created.get("status") or "awaiting_payment")
    except Exception as exc:
        return render_error(f"ایجاد سفارش پرداخت ممکن نشد: {exc}", persona=persona,
                            parent_callback="v52:shop")
    return render_page(
        "انتخاب روش پرداخت", f"سفارش {order_id}\nوضعیت: {status}",
        persona=persona, page_kind="preview", parent_callback="v52:shop",
        rows=[[button("کارت به کارت", f"v52:pay:{order_id}:card")]],
    )


def _v52_receipt_review_page(ctx, persona: str, receipt_id: str):
    """Render the receipt/attempt/order preview for a reviewer or the payer."""
    import checkout_ui
    import payment_service
    from ui_v52 import render_error
    with database.get_db() as conn:
        receipt = conn.execute(
            "SELECT id,order_id,payment_attempt_id FROM payment_receipts "
            "WHERE CAST(id AS TEXT)=? OR tracking_code=?", (str(receipt_id), str(receipt_id))
        ).fetchone()
        if receipt is None or not receipt["payment_attempt_id"]:
            return render_error("رسید موردنظر یافت نشد.", persona=persona, parent_callback="v52:shop")
        attempt_row = conn.execute(
            "SELECT attempt_id,order_id,assigned_card_id,state,evidence_cipher,created_at "
            "FROM payment_attempts WHERE attempt_id=?", (str(receipt["payment_attempt_id"]),)
        ).fetchone()
        if attempt_row is None:
            return render_error("درخواست پرداخت این رسید یافت نشد.", persona=persona,
                                parent_callback="v52:shop")
        attempt = payment_service._open_attempt_result(conn, attempt_row)
        order_row = conn.execute(
            "SELECT order_id,actor_tg_id,bot_id,merchant_key,purpose,payment_rail,amount_toman,"
            "status,received_toman,created_at,expires_at,revision FROM payment_orders WHERE order_id=?",
            (str(attempt_row["order_id"]),)).fetchone()
    if order_row is None:
        return render_error("سفارش این رسید یافت نشد.", persona=persona, parent_callback="v52:shop")
    order = {"order_id": str(order_row[0]), "actor_tg_id": int(order_row[1]), "bot_id": int(order_row[2]),
             "merchant_key": str(order_row[3]), "purpose": str(order_row[4]), "payment_rail": str(order_row[5]),
             "amount_toman": int(order_row[6]), "status": str(order_row[7]), "received_toman": int(order_row[8]),
             "created_at": int(order_row[9]), "expires_at": int(order_row[10]), "revision": int(order_row[11])}
    return checkout_ui.preview_receipt(ctx, attempt, order=order)


def _v52_manual_delivery_page(ctx, persona: str, order_id: str):
    """Two-step manual fulfilment: preview first, then confirm with the DM secret."""
    import goods_ui
    from ui_v52 import render_error
    order = _load_v52_order(order_id)
    if order is None:
        return render_error("سفارش موردنظر یافت نشد.", persona=persona, parent_callback="v52:shop")
    now = int(time.time())
    entry = v52_router.DEFAULT_FSM.peek(ctx, now=now)
    secret = None
    if entry is not None and entry.state == "goods_manual_secret":
        secret = str(entry.payload.get("secret") or "") or None
    if secret:
        return goods_ui.prepare_manual_delivery(ctx, order, secret_payload=secret, confirm=True)
    v52_router.DEFAULT_FSM.set(
        ctx, "goods_manual_secret", payload={"order_id": str(order_id)},
        parent_callback="v52:shop", now=now)
    return goods_ui.prepare_manual_delivery(ctx, order, confirm=False)


def _load_panel_operations(ctx, operation_id=None) -> list[dict]:
    """Load the reconciliation queue for the acting bot (optionally one operation)."""
    bot_id = int(ctx.get("bot_id") or 0)
    sql = ("SELECT operation_id,order_id,actor_tg_id,bot_id,account_id,panel_id,kind,state,"
           "created_at,updated_at FROM panel_operations WHERE bot_id=?")
    args: list = [bot_id]
    if operation_id:
        sql += " AND operation_id=?"
        args.append(str(operation_id))
    sql += " ORDER BY updated_at DESC LIMIT 20"
    with database.get_db() as conn:
        rows = conn.execute(sql, tuple(args)).fetchall()
    return [dict(row) for row in rows]


def _load_refund(refund_id: str) -> dict | None:
    """Read one refund request row; None when it does not exist."""
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT refund_id,order_id,merchant_key,user_tg_id,amount_toman,state,created_at,settled_at "
            "FROM refund_requests WHERE refund_id=?", (str(refund_id),)).fetchone()
    return dict(row) if row is not None else None


def _v52_begin_manual_import(ctx, panel_id: int):
    """Ask for the manual pool batch in private DM (root only)."""
    from ui_v52 import render_page, render_error
    if not ctx.get("is_root"):
        return render_error("ورود دستی استخر فقط در اختیار root است.", persona="admin",
                            parent_callback="v52:panels")
    with database.get_db() as conn:
        panel = conn.execute("SELECT id,name FROM panels WHERE id=?", (int(panel_id),)).fetchone()
    if panel is None:
        return render_error("پنل موردنظر یافت نشد.", persona="admin", parent_callback="v52:panels")
    v52_router.DEFAULT_FSM.set(
        ctx, "inventory_manual_batch", payload={"panel_id": int(panel_id)},
        parent_callback="v52:panels", now=int(time.time()))
    return render_page(
        "ورود دستی استخر",
        f"پنل: {int(panel_id)} · {panel['name']}\n\n"
        "در پیام بعدی، شناسه خارجی و کانفیگ‌ها را در قالب زیر ارسال کنید:\n"
        "<code>identifier | uri1 | uri2</code>\n"
        "مقادیر در دیتابیس رمزنگاری می‌شوند و در لاگ نمایش داده نمی‌شوند.",
        persona="admin", page_kind="form", parent_callback="v52:panels",
    )


def _v52_begin_digital_import(ctx, product_id: int):
    """Ask for the digital inventory batch in private DM (root only)."""
    from ui_v52 import render_page, render_error
    if not ctx.get("is_root"):
        return render_error("ورود موجودی دیجیتال فقط در اختیار root است.", persona="admin",
                            parent_callback="v52:shop")
    with database.get_db() as conn:
        product = conn.execute(
            "SELECT id,title FROM digital_products WHERE id=?", (int(product_id),)).fetchone()
    if product is None:
        return render_error("کالای موردنظر یافت نشد.", persona="admin", parent_callback="v52:shop")
    v52_router.DEFAULT_FSM.set(
        ctx, "inventory_digital_batch", payload={"product_id": int(product_id)},
        parent_callback="v52:shop", now=int(time.time()))
    return render_page(
        "ورود موجودی دیجیتال",
        f"محصول: {int(product_id)} · {product['title']}\n\n"
        "هر قلم محرمانه را در یک خط جداگانه در پیام بعدی ارسال کنید. "
        "مقادیر رمزنگاری می‌شوند و در لاگ نمایش داده نمی‌شوند.",
        persona="admin", page_kind="form", parent_callback="v52:shop",
    )


def _v52_orders_page(ctx, persona: str):
    """List the acting user's own payment orders; another actor's orders never appear."""
    from ui_v52 import render_page
    actor_id = int(ctx.get("actor_tg_id") or 0)
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT order_id,status,purpose,amount_toman,created_at FROM payment_orders "
            "WHERE actor_tg_id=? ORDER BY created_at DESC, order_id LIMIT 12",
            (actor_id,),
        ).fetchall()
    if not rows:
        body = "سفارشی برای این حساب ثبت نشده است."
    else:
        lines = [
            f"• {str(row['order_id'])[:12]} · {row['status']} · "
            f"{int(row['amount_toman']):,} تومان · {row['purpose']}"
            for row in rows
        ]
        body = f"تعداد سفارش‌های شما: {len(rows)}\n" + "\n".join(lines)
    return render_page("سفارش‌های من", body, persona=persona,
                       parent_callback="v52:home:" + persona)


def _v52_finance_page(ctx):
    """Root finance overview: the entries that exist, and nothing invented."""
    from ui_v52 import render_page, button
    with database.get_db() as conn:
        cards = conn.execute("SELECT COUNT(*) FROM bank_cards").fetchone()[0]
        plans = conn.execute("SELECT COUNT(*) FROM pricing_plans WHERE is_active=1").fetchone()[0]
    body = (
        f"کارت‌های ثبت‌شده: {int(cards)}\n"
        f"تعرفه‌های فعال: {int(plans)}\n"
        "تسویه فقط روی edge صریح نماینده انجام می‌شود؛ فروش خرده‌فروشی در تسویه دخالت ندارد."
    )
    return render_page(
        "امور مالی و تسویه‌ها", body, persona="admin",
        rows=[[button("💳 کارت‌های پرداخت", "v52:cards")],
              [button("🏷 تعرفه‌ها", "v52:plans")]],
        parent_callback="v52:home:admin",
    )


def _v52_new_panel_code(conn) -> str:
    """Allocate a unique panel code for a wizard-created panel."""
    used = {str(row[0]) for row in conn.execute("SELECT panel_code FROM panels").fetchall()}
    index = 1
    while f"panel-{index}" in used:
        index += 1
    return f"panel-{index}"


_V52_PANEL_TYPE_ALIASES = {
    "3x-ui": "3x-ui", "3xui": "3x-ui", "xui": "3x-ui", "3x_ui": "3x-ui",
    "marzban": "marzban", "hiddify": "hiddify",
    "rebecca": "rebecca", "x-ui": "rebecca", "rebecca-xui": "rebecca",
    "manual": "manual", "manual-pool": "manual", "pool": "manual",
}


def _v52_normalize_panel_type(panel_type) -> str:
    """Map a wizard/registry slug onto the canonical ``panels.panel_type`` value."""
    key = str(panel_type or "").strip().lower().replace("_", "-")
    return _V52_PANEL_TYPE_ALIASES.get(key, "manual")


def _v52_create_panel_draft(panel_type: str) -> int:
    """Insert a maintenance-mode, non-default, non-sellable panel row for the wizard."""
    selected = _v52_normalize_panel_type(panel_type)
    now = int(time.time())
    with database.get_db() as conn:
        code = _v52_new_panel_code(conn)
        cursor = conn.execute(
            "INSERT INTO panels(panel_code,name,panel_type,lifecycle,is_default,created_at) "
            "VALUES(?,?,?,'maintenance',0,?)",
            (code, f"پنل {code}", selected, now),
        )
        conn.commit()
        return int(cursor.lastrowid)


def _v52_begin_panel_name(ctx, panel_id: int):
    """Step 2: ask for the panel display name and arm the name FSM state."""
    import panel_admin_ui
    pid = int(panel_id)
    v52_router.DEFAULT_FSM.set(
        ctx, "panel_connection_name", payload={"panel_id": pid},
        parent_callback=f"v52:panelconfig:{pid}", now=int(time.time()),
    )
    return panel_admin_ui.render_panel_name_step(ctx, pid)


def _v52_begin_panel_draft(ctx, panel_type: str):
    """Step 1 → 2: create the draft row for the chosen core type, then ask the name."""
    if not ctx.get("is_root"):
        raise PermissionError("افزودن پنل فقط در اختیار root است.")
    selected = _v52_normalize_panel_type(panel_type)
    panel_id = _v52_create_panel_draft(selected)
    return _v52_begin_panel_name(ctx, panel_id)


def _v52_set_panel_type(ctx, panel_id: int, panel_type: str):
    """Step 1 (edit): persist the chosen core type on an existing row, then ask the name."""
    if not ctx.get("is_root"):
        raise PermissionError("ویرایش پنل فقط در اختیار root است.")
    pid = int(panel_id)
    selected = _v52_normalize_panel_type(panel_type)
    with database.get_db() as conn:
        row = conn.execute("SELECT id FROM panels WHERE id=?", (pid,)).fetchone()
        if row is None:
            raise ValueError("پنل موردنظر وجود ندارد.")
        conn.execute("UPDATE panels SET panel_type=?, revision=revision+1 WHERE id=?", (selected, pid))
        conn.commit()
    return _v52_begin_panel_name(ctx, pid)


def _v52_begin_panel_url(ctx, panel_id: int):
    """Step 3: ask for the panel URL and arm the URL FSM state."""
    import panel_admin_ui
    pid = int(panel_id)
    with database.get_db() as conn:
        exists = conn.execute("SELECT 1 FROM panels WHERE id=?", (pid,)).fetchone()
    if exists is None:
        raise ValueError("پنل موردنظر وجود ندارد.")
    v52_router.DEFAULT_FSM.set(
        ctx, "panel_connection_url", payload={"panel_id": pid},
        parent_callback=f"v52:panelname:{pid}", now=int(time.time()),
    )
    return panel_admin_ui.render_panel_url_step(ctx, pid)


def _v52_begin_panel_credential(ctx, panel_id: int):
    """Step 4: ask for the credential; falls back to step 3 when no URL is saved yet."""
    import panel_admin_ui
    pid = int(panel_id)
    with database.get_db() as conn:
        row = conn.execute("SELECT raw_url FROM panels WHERE id=?", (pid,)).fetchone()
    if row is None:
        raise ValueError("پنل موردنظر وجود ندارد.")
    normalized_url = str(row["raw_url"] or "")
    if not normalized_url:
        return _v52_begin_panel_url(ctx, pid)
    v52_router.DEFAULT_FSM.set(
        ctx, "panel_connection_token",
        payload={"panel_id": pid, "normalized_url": normalized_url},
        parent_callback=f"v52:panelurl:{pid}", now=int(time.time()),
    )
    return panel_admin_ui.render_panel_credential_step(ctx, pid)


def _v52_panel_probe_reason(exc) -> str:
    """A concrete, secret-free Persian reason for a failed live login probe."""
    lowered = str(exc or "").strip().lower()
    if "login was rejected" in lowered or "no session cookie" in lowered:
        return "ورود به پنل رد شد؛ نام کاربری/رمز یا توکن نامعتبر است."
    if "could not be decrypted" in lowered or "credential is invalid" in lowered \
            or "credential is missing" in lowered or "does not match its declared auth_type" in lowered:
        return "اعتبارنامهٔ ذخیره‌شده قابل رمزگشایی یا استفاده نیست."
    if "transport failed" in lowered or "timed out" in lowered or "connection" in lowered \
            or "name or service" in lowered or "resolve" in lowered:
        return "اتصال شبکه به پنل برقرار نشد."
    if "unsupported panel type" in lowered or "not verified" in lowered:
        return "این نوع پنل در نسخهٔ فعلی پشتیبانی نمی‌شود."
    if "non-success or malformed" in lowered:
        return "پنل پاسخ موفق/معتبری برای ورود برنگرداند (احتمالاً اعتبارنامه نادرست است)."
    return "پاسخ قابل‌اتکا از پنل دریافت نشد."


async def _v52_probe_panel_connection(panel_id: int):
    """Best-effort live login probe for a 3X-UI panel.

    Returns ``(None, None)`` when the panel type has no wired probe, ``(True, None)``
    on a verified reachable read, and ``(False, reason)`` on any failure. Never
    raises and never opens a socket for a non-3X-UI type.
    """
    with database.get_db() as conn:
        row = conn.execute("SELECT * FROM panels WHERE id=?", (int(panel_id),)).fetchone()
    if row is None:
        return False, "پنل موردنظر وجود ندارد."
    panel_record = dict(row)
    kind = str(panel_record.get("panel_type") or "").lower().replace("_", "-")
    if kind not in {"3x-ui", "3xui", "xui"}:
        return None, None
    try:
        import requests
        from panel_adapters.xui_openapi import XUIOpenAPIAdapter
        adapter = XUIOpenAPIAdapter(panel_record, requests.Session(),
                                    transaction_probe=lambda: False)
        status = await adapter.verify_connection()
        if bool(getattr(status, "reachable", False)):
            return True, None
        return False, "پاسخ قابل‌اتکا از پنل دریافت نشد."
    except Exception as exc:  # noqa: BLE001 - the probe must never escape the wizard
        logger.debug("Panel login probe failed for panel %s", panel_id, exc_info=True)
        return False, _v52_panel_probe_reason(exc)


def _v52_confirm_panel_draft(ctx, step: str):
    """Explain the remaining wizard step without inventing a completed save."""
    from ui_v52 import render_page
    if not ctx.get("is_root"):
        raise PermissionError("افزودن پنل فقط در اختیار root است.")
    body = (
        "برای تکمیل افزودن پنل، ابتدا نشانی و توکن را از مسیر «افزودن پنل» ثبت کنید؛ "
        "سپس از «مدیریت پنل‌ها → مشاهده پنل» اتصال را آزمایش و پنل را فعال کنید.\n"
        "این صفحه هیچ ذخیره‌سازی جدیدی انجام نمی‌دهد."
    )
    return render_page("تکمیل افزودن پنل", body, persona="admin",
                       parent_callback="v52:panels")


def _v52_preview_panel_policy(ctx, panel_id: int):
    """Show the stored panel policy for root, or a clear not-found page."""
    from ui_v52 import render_page, render_error
    if not ctx.get("is_root"):
        raise PermissionError("سیاست پنل فقط در اختیار root است.")
    with database.get_db() as conn:
        row = conn.execute("SELECT * FROM panels WHERE id=?", (int(panel_id),)).fetchone()
    if row is None:
        return render_error("پنل موردنظر وجود ندارد.", persona="admin",
                            parent_callback="v52:panels")
    panel = dict(row)
    body = (
        f"پنل: {int(panel_id)}\n"
        f"وضعیت: {panel.get('lifecycle', '')}\n"
        f"پنهان: {'بله' if int(panel.get('is_hidden') or 0) else 'خیر'}\n"
        f"حداکثر کلاینت: {int(panel.get('max_clients') or 0) or 'نامحدود'}\n"
        f"استراتژی نام‌گذاری: {panel.get('username_strategy', '')}\n"
        f"روش تمدید: {panel.get('renewal_method', '')}\n"
        f"Revision: {int(panel.get('revision') or 1)}\n"
        "تغییر سیاست در این لایه فقط پس از تأیید صریح و با CAS روی revision انجام می‌شود."
    )
    return render_page("سیاست پنل", body, persona="admin",
                       parent_callback=f"v52:panel:{int(panel_id)}")


def _load_confirmation(nonce: str):
    """Read one action confirmation row by nonce."""
    with database.get_db() as conn:
        return conn.execute(
            "SELECT nonce,actor_tg_id,bot_id,action,target_type,target_id,expected_revision,"
            "expires_at,consumed_at,operation_id,payload_json "
            "FROM action_confirmations WHERE nonce=?",
            (str(nonce),)).fetchone()


def _v52_confirmation_page(ctx, persona: str, nonce: str):
    """Preview a pending confirmation without consuming it."""
    from ui_v52 import render_page, render_error
    row = _load_confirmation(nonce)
    if row is None:
        return render_error("این درخواست تأیید وجود ندارد یا منقضی شده است.", persona=persona,
                            parent_callback="v52:home:" + persona)
    if row["consumed_at"] is not None:
        return render_error("این درخواست تأیید قبلاً مصرف شده است.", persona=persona,
                            parent_callback="v52:home:" + persona)
    if int(row["actor_tg_id"]) != int(ctx.get("actor_tg_id") or 0) \
            or int(row["bot_id"]) != int(ctx.get("bot_id") or 0):
        return render_error("این درخواست تأیید متعلق به این کاربر/بات نیست.", persona=persona,
                            parent_callback="v52:home:" + persona)
    if int(row["expires_at"]) <= int(time.time()):
        return render_error("مهلت این درخواست تأیید به پایان رسیده است.", persona=persona,
                            parent_callback="v52:home:" + persona)
    return render_page(
        "پیش‌نمایش درخواست تأیید",
        f"عملیات: {row['action']}\nهدف: {row['target_type']} · {row['target_id']}\n"
        f"revision: {int(row['expected_revision'])}\nانقضا: {int(row['expires_at'])}\n\n"
        "هیچ تغییری تا زدن دکمهٔ تأیید اعمال نمی‌شود.",
        persona=persona, page_kind="preview", parent_callback="v52:home:" + persona,
    )


#: Confirmation actions that mutate ``bot_registry``. Each maps to the service
#: function that both consumes the nonce and performs the transition, so the two can
#: never drift apart and a replayed press cannot apply the change twice.
_BOT_LIFECYCLE_ACTIONS = {
    "bot.activate": "activate_bot",
    "bot.disable": "disable_bot",
    "bot.enable": "enable_bot",
    "bot.reassign": "reassign_bot",
}


def _v52_arm_bot_token_state(ctx, *, rep_id, is_primary) -> None:
    """Arm the private-DM state that accepts the bot token.

    ``begin_register_bot`` renders the "send me the token" form; the operator's next
    private message is then routed to ``bot_register_token``. Without this the form was
    a dead end: the state was never set, so the token answer fell through to the
    ordinary text handlers and the registration could never be completed.
    """
    v52_router.DEFAULT_FSM.set(
        ctx, "bot_register_token",
        payload={"rep_id": None if rep_id is None else int(rep_id),
                 "is_primary": bool(is_primary)},
        parent_callback="v52:bots", now=int(time.time()))


def _v52_bot_actor(ctx) -> int:
    """The acting operator, used as the audit actor of a bot transition."""
    for key in ("actor_tg_id", "telegram_id"):
        value = ctx.get(key) if hasattr(ctx, "get") else None
        if value:
            return int(value)
    return int(ADMIN_TELEGRAM_ID)


def _v52_load_bot(bot_id: int):
    """One ``bot_registry`` row joined with its owner, or ``None`` when unknown."""
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT b.bot_id,b.rep_id,b.is_primary,b.username,b.status,b.revision,"
            "       r.name AS rep_name FROM bot_registry b "
            "LEFT JOIN representatives r ON r.id=b.rep_id WHERE b.bot_id=?",
            (int(bot_id),)).fetchone()
    return dict(row) if row is not None else None


def _v52_prepare_bot_action(ctx, name: str, bot_id: int, new_rep_id=None):
    """Run the matching ``prepare_*`` preview: a nonce is created, nothing changes."""
    import bot_registry_service
    actor, now = _v52_bot_actor(ctx), int(time.time())
    with database.get_db() as conn:
        if name == "bot_enable":
            return bot_registry_service.prepare_bot_enable(
                conn, actor_tg_id=actor, bot_id=int(bot_id), now=now)
        if name == "bot_disable":
            return bot_registry_service.prepare_bot_disable(
                conn, actor_tg_id=actor, bot_id=int(bot_id), now=now)
        if name == "bot_delete":
            return bot_registry_service.prepare_bot_delete(
                conn, actor_tg_id=actor, bot_id=int(bot_id), now=now)
        if name == "bot_reassign":
            return bot_registry_service.prepare_bot_reassign(
                conn, actor_tg_id=actor, bot_id=int(bot_id), new_rep_id=new_rep_id,
                now=now)
    raise ValueError("unknown bot action")


def _v52_apply_bot_action(conn, action: str, row, nonce: str, now: int):
    """Perform the approved bot transition; the service consumes the nonce inside it."""
    import bot_registry_service
    actor_id, bot_id = int(row["actor_tg_id"]), int(row["target_id"])
    if str(action) == "bot.reassign":
        payload = json.loads(str(row["payload_json"] or "{}") or "{}")
        target = payload.get("new_rep_id")
        return bot_registry_service.reassign_bot(
            conn, actor_tg_id=actor_id, bot_id=bot_id, nonce=str(nonce),
            new_rep_id=None if target is None else int(target), now=now)
    return getattr(bot_registry_service, _BOT_LIFECYCLE_ACTIONS[str(action)])(
        conn, actor_tg_id=actor_id, bot_id=bot_id, nonce=str(nonce), now=now)


def _v52_consume_confirmation(ctx, persona: str, nonce: str):
    """CAS-consume a confirmation and run the transition it is bound to.

    The bot-lifecycle actions are performed here rather than merely recorded: the
    service consumes the nonce in the same transaction as the mutation, so the
    operator's approval and the change are one atomic step. Every other action keeps
    the previous behaviour and reports the bound operation id.
    """
    import confirmation_service
    from ui_v52 import render_page, render_error
    row = _load_confirmation(nonce)
    if row is None:
        return render_error("این درخواست تأیید وجود ندارد یا منقضی شده است.", persona=persona,
                            parent_callback="v52:home:" + persona)
    action = str(row["action"])
    now = int(time.time())
    applied, result = None, None
    try:
        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if action in _BOT_LIFECYCLE_ACTIONS:
                    applied = _v52_apply_bot_action(conn, action, row, str(nonce), now)
                else:
                    result = confirmation_service.consume_confirmation(
                        conn, nonce=str(nonce), actor_tg_id=int(row["actor_tg_id"]),
                        bot_id=int(row["bot_id"]), action=action,
                        target_type=str(row["target_type"]), target_id=str(row["target_id"]),
                        expected_revision=int(row["expected_revision"]), now=now)
                conn.commit()
            except Exception:
                if conn.in_transaction:
                    conn.rollback()
                raise
    except Exception as exc:
        return render_error(f"تأیید انجام نشد: {exc}", persona=persona,
                            parent_callback="v52:home:" + persona)
    if applied is not None:
        import bot_admin_ui
        return bot_admin_ui.build_bot_action_result(ctx, applied, action=action)
    notice = "این تأیید قبلاً مصرف شده بود." if result.get("replayed") else "تأیید ثبت شد."
    return render_page(
        "نتیجهٔ تأیید",
        f"{notice}\nشناسه عملیات: {result.get('operation_id')}\n"
        "پیشرفت واقعی عملیات از صف تطبیق قابل پیگیری است.",
        persona=persona, page_kind="result", parent_callback="v52:home:" + persona,
    )


def _v52_cancel_confirmation(ctx, persona: str, nonce: str):
    """Report a discarded confirmation; the row is left to expire untouched."""
    from ui_v52 import render_page, render_error
    row = _load_confirmation(nonce)
    if row is None:
        return render_error("این درخواست تأیید وجود ندارد یا منقضی شده است.", persona=persona,
                            parent_callback="v52:home:" + persona)
    if row["consumed_at"] is not None:
        return render_error("این درخواست تأیید قبلاً مصرف شده است.", persona=persona,
                            parent_callback="v52:home:" + persona)
    return render_page(
        "لغو شد",
        f"عملیات «{row['action']}» لغو شد؛ هیچ تغییری اعمال نشد.\n"
        "این درخواست تأیید تا انقضای طبیعی خود مصرف نمی‌شود.",
        persona=persona, page_kind="result", parent_callback="v52:home:" + persona,
    )


async def _switch_v52_persona(update, context, target: str, role, rep):
    """Persist the requested persona and render the matching workspace."""
    import identity_service
    from ui_v52 import render_error
    query, user = update.callback_query, update.effective_user
    selected = {"admin": "admin", "rep": "rep", "user": "end_user"}.get(str(target))
    if selected is None:
        await _send_v52_page(query, render_error("نقش درخواستی نامعتبر است.",
                                                 parent_callback="v52:home:user"))
        return True
    actor_id = int(user.id)
    is_root = actor_id == int(ADMIN_TELEGRAM_ID)
    try:
        with database.get_db() as conn:
            actor = identity_service.resolve_actor(conn, actor_id)
    except Exception:
        await _send_v52_page(query, render_error(
            "هویت شما برای تغییر نقش ثبت نشده است؛ ابتدا /start را بزنید.",
            parent_callback="v52:home:user"))
        return True
    allowed = {"end_user"}
    if actor.is_root:
        allowed.update({"admin", "rep"})
    if actor.persona == "rep" and actor.rep_id is not None:
        allowed.add("rep")
    if selected not in allowed:
        await _send_v52_page(query, render_error(
            "این نقش برای حساب شما مجاز نیست.", parent_callback="v52:home:user"))
        return True
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO user_settings(user_id,first_connect_mode,active_persona) VALUES(?,0,?) "
            "ON CONFLICT(user_id) DO UPDATE SET active_persona=excluded.active_persona",
            (actor_id, selected))
    if selected == "admin":
        await admin_panel.show_admin_dashboard(update, context, edit_message=True)
    elif selected == "rep":
        active_rep = _resolve_persona_rep(user, rep)
        if active_rep is None:
            await _send_v52_page(query, render_error("میزکار نمایندگی برای شما موجود نیست.",
                                                     parent_callback="v52:home:user"))
            return True
        await rep_panel.show_rep_dashboard(update, context, active_rep, edit_message=True)
    else:
        await user_portal.show_user_portal(update, context, edit_message=True)
    await query.answer("نقش فعال تغییر کرد")
    return True


async def _send_v52_page(query, page, notice=""):
    if page is None:
        from ui_v52 import render_error
        page = render_error("مسیر v5.2 در دسترس نیست.", parent_callback="v52:home:user")
    text_value, markup = getattr(page, "text", None), getattr(page, "reply_markup", None)
    if text_value is None and isinstance(page, dict):
        text_value, markup = page.get("text"), page.get("reply_markup")
    if text_value is None:
        from ui_v52 import render_error
        fallback = render_error("پاسخ UI قابل نمایش نیست.", parent_callback="v52:home:user")
        text_value, markup = fallback.text, fallback.reply_markup
    try:
        await query.edit_message_text(text_value, reply_markup=markup, parse_mode="HTML")
    except BadRequest as exc:
        # Telegram rejects an edit whose text+markup are byte-identical to the
        # current message ("Message is not modified"). That is an idempotent
        # re-render, not a failure: the user already sees the wanted page.
        if "not modified" not in str(exc).lower():
            raise
    await query.answer(notice or "صفحهٔ v5.2")


async def _load_v52_receipt_page(update, context, attempt_id, ctx):
    import checkout_ui
    import payment_service
    query, actor_id = update.callback_query, int(ctx.get("actor_tg_id") or 0)
    bot_id = int(ctx.get("bot_id") or 0)
    with database.get_db() as conn:
        row = conn.execute("SELECT attempt_id,order_id,assigned_card_id,state,evidence_cipher,created_at "
                           "FROM payment_attempts WHERE attempt_id=?", (str(attempt_id),)).fetchone()
        if row is None:
            raise ValueError("درخواست پرداخت پیدا نشد.")
        attempt = payment_service._open_attempt_result(conn, row)
        o = conn.execute("SELECT order_id,actor_tg_id,bot_id,merchant_key,purpose,payment_rail,amount_toman,"
                         "status,received_toman,created_at,expires_at,revision FROM payment_orders WHERE order_id=?",
                         (str(row["order_id"]),)).fetchone()
        if o is None:
            raise ValueError("سفارش متصل به رسید پیدا نشد.")
        order = {"order_id": str(o[0]), "actor_tg_id": int(o[1]), "bot_id": int(o[2]),
                 "merchant_key": str(o[3]), "purpose": str(o[4]), "payment_rail": str(o[5]),
                 "amount_toman": int(o[6]), "status": str(o[7]), "received_toman": int(o[8]),
                 "created_at": int(o[9]), "expires_at": int(o[10]), "revision": int(o[11])}
    if order["actor_tg_id"] != actor_id or order["bot_id"] != bot_id:
        raise PermissionError("این دستور پرداخت متعلق به این کاربر/بات نیست.")
    context.user_data.update({"v52_payment_attempt_id": str(attempt_id),
                              "v52_payment_order_id": order["order_id"],
                              "v52_payment_amount": order["amount_toman"],
                              "v52_payment_pending": True})
    await _send_v52_page(query, checkout_ui.preview_receipt(ctx, attempt, order=order),
                         "فقط یک تصویر رسید برای همین سفارش ارسال کنید.")


def _v52_domain_draft(context) -> dict:
    """Read the in-progress domain-register draft for this actor."""
    draft = context.user_data.get("v52_domain_draft")
    return dict(draft) if isinstance(draft, dict) else {}


def _v52_set_domain_draft(context, draft: dict) -> None:
    context.user_data["v52_domain_draft"] = dict(draft)


def _v52_clear_domain_draft(context) -> None:
    context.user_data.pop("v52_domain_draft", None)


def _v52_actor_rep_id(ctx) -> int:
    """The representative bound to the acting persona, or a permission error."""
    try:
        selected = int(ctx.get("rep_id"))
    except (TypeError, ValueError):
        selected = 0
    if selected <= 0:
        raise PermissionError("این حساب به هیچ نماینده‌ای متصل نیست.")
    return selected


def _v52_run_domain_verify(domain_id: int):
    """Blocking DNS/TLS verification; callers must run it off the event loop."""
    import domain_service
    with database.get_db() as conn:
        domain_service.ensure_domain_columns(conn)
        return domain_service.verify_domain(conn, int(domain_id))


async def _dispatch_v52_callback(update, context, *, role=None, rep=None):
    query = getattr(update, "callback_query", None)
    if query is None:
        return False
    data, user = str(query.data or ""), update.effective_user
    selected_role = role or "user"
    if selected_role == "rep" and rep is None:
        current_role, current_rep = database.get_user_role(
            user.id, update.effective_chat.id,
            update.effective_message.message_thread_id if update.effective_message else None)
        if current_role == "rep":
            rep = current_rep
    persona = "admin" if selected_role == "super_admin" else "rep" if selected_role == "rep" else "user"
    ctx = _v52_actor_context(update, context, persona, rep)
    try:
        route = v52_router.route_callback(data, context=ctx)
    except Exception:
        route = None
    page = None
    match = re.fullmatch(r"v52:subreps:([1-9][0-9]{0,17})", data)
    if match:
        import rep_admin_ui
        page = rep_admin_ui.show_sub_reps(ctx, int(match.group(1)), persona=persona)
    else:
        match = re.fullmatch(r"v52:cfg:([1-9][0-9]{0,17})", data)
        if match:
            import config_delivery_ui
            page = await config_delivery_ui.show_configs(ctx, int(match.group(1)),
                        adapter_factory=admin_panel._get_admin_panel_adapter_factory(context))
        else:
            match = re.fullmatch(r"v52:qr:([1-9][0-9]{0,17}):([0-9]{1,4})", data)
            if match:
                import config_delivery_ui
                result = await config_delivery_ui.deliver_config_qr(
                    ctx, int(match.group(1)), int(match.group(2)),
                    adapter_factory=admin_panel._get_admin_panel_adapter_factory(context), message=query.message)
                await query.answer("QR کانفیگ ارسال شد." if result.status == "ok" else "شماره کانفیگ معتبر نیست.")
                return True
            match = re.fullmatch(r"v52:allcfg:([1-9][0-9]{0,17})", data)
            if match:
                import config_delivery_ui
                await config_delivery_ui.deliver_all_configs(
                    ctx, int(match.group(1)), adapter_factory=admin_panel._get_admin_panel_adapter_factory(context),
                    message=query.message)
                await query.answer("تحویل کانفیگ‌ها انجام شد.")
                return True
            if data == "v52:panels":
                from ui_v52 import render_page, button
                with database.get_db() as conn:
                    rows = conn.execute("SELECT id,name,panel_type,lifecycle,is_default FROM panels ORDER BY is_default DESC,id").fetchall()
                body = "\n".join(
                    f"پنل {int(row['id'])}: {row['name']} · {row['panel_type']} · {row['lifecycle']}"
                    for row in rows
                ) or "پنلی ثبت نشده است."
                buttons = [[button("مشاهده پنل " + str(int(row["id"])), "v52:panel:" + str(int(row["id"])))]
                           for row in rows]
                buttons.append([button("افزودن پنل", "v52:panelnew")])
                page = render_page("مدیریت پنل‌ها", body, persona="admin", rows=buttons)
            elif data == "v52:panelnew":
                import panel_admin_ui
                page = panel_admin_ui.begin_add_panel(ctx)
            elif data == "v52:bots":
                from ui_v52 import render_page, button
                reps = database.get_all_active_reps()
                body = "ثبت بات اختصاصی برای نماینده‌های فعال؛ توکن فقط در فرم خصوصی دریافت می‌شود."
                buttons = [[button("ثبت بات نماینده " + str(rep_item.get("id")), "v52:bot:" + str(int(rep_item["id"])))]
                           for rep_item in reps]
                buttons.insert(0, [button("ثبت بات اصلی", "v52:bot:primary")])
                page = render_page("مدیریت بات‌ها", body, persona="admin", rows=buttons)
            elif data == "v52:bot:primary":
                import bot_admin_ui
                page = bot_admin_ui.begin_register_bot(ctx, rep_id=None, is_primary=True, chat_type="private")
            elif data == "v52:cards":
                import finance_admin_ui
                page = finance_admin_ui.edit_bank_card(ctx)
            elif data == "v52:card:new":
                # S59 — the root card is collected by a wizard and confirmed on a
                # preview card, instead of one raw pipe-separated line.
                import finance_admin_ui
                from ui_v52 import wizard_render
                _wz_state = finance_admin_ui.start_finance_wizard(
                    context, query, finance_admin_ui.BANK_CARD_WIZARD_KEY,
                    parent_callback="v52:cards")
                await wizard_render(update, context, _wz_state)
                return True
            else:
                match = re.fullmatch(r"v52:card:([1-9][0-9]{0,17})", data)
                if match:
                    import finance_admin_ui
                    with database.get_db() as conn:
                        row = conn.execute("SELECT * FROM bank_cards WHERE id=?", (int(match.group(1)),)).fetchone()
                    page = finance_admin_ui.edit_bank_card(ctx, None if row is None else dict(row))
                elif data == "v52:plans":
                    # Both ``v52:plans`` and ``admin:plans`` render this one hub, so
                    # an operator can no longer edit half a tariff from one menu and
                    # the other half from another.
                    import finance_admin_ui
                    page = finance_admin_ui.tariff_hub(ctx, parent_callback="v52:home:admin")
                else:
                    match = re.fullmatch(r"v52:settle:([1-9][0-9]{0,17})", data)
                    if match:
                        from ui_v52 import render_error
                        page = render_error("snapshot بدهی همان edge موجود نیست؛ هیچ posting مالی انجام نشد.",
                                             persona=persona, parent_callback="v52:cards")
                    else:
                        match = re.fullmatch(r"v52:receipt:([0-9a-fA-F-]{16,40})", data)
                        if match:
                            try:
                                await _load_v52_receipt_page(update, context, match.group(1), ctx)
                            except Exception as exc:
                                from ui_v52 import render_error
                                await _send_v52_page(query, render_error(str(exc), persona=persona,
                                                                         parent_callback="v52:shop"))
                            return True

    if page is None and isinstance(route, v52_router.Route):
        name, params = route.handler, route.params
        try:
            if name.startswith("wizard_"):
                # The shared step-by-step wizard engine. It owns its own state and
                # renders into the message the wizard itself opened, so it returns a
                # page only when nothing is live any more.
                from ui_v52 import wizard_handle_callback, render_error
                action = name[len("wizard_"):]
                if action == "pick":
                    args = (int(params["step"]), int(params["choice"]))
                elif action == "generate":
                    action, args = "gen", (int(params["step"]),)
                elif action in ("skip", "done"):
                    args = (int(params["step"]),)
                else:
                    args = ()
                if await wizard_handle_callback(update, context, action, args):
                    return True
                page = render_error(
                    "این ویزارد دیگر فعال نیست؛ آن را از منو دوباره آغاز کنید.",
                    persona=persona, parent_callback=f"v52:home:{persona}")
            elif name == "show_panel":
                from ui_v52 import render_page, button, render_error
                panel_id = int(params["panel_id"])
                with database.get_db() as conn:
                    row = conn.execute("SELECT * FROM panels WHERE id=?", (panel_id,)).fetchone()
                if row is None:
                    page = render_error("پنل موردنظر وجود ندارد.", persona="admin", parent_callback="v52:panels")
                else:
                    panel = dict(row)
                    url = panel.get("raw_url") or panel.get("base_url") or "تنظیم نشده"
                    has_secret = bool(panel.get("auth_cipher") or panel.get("api_token_cipher"))
                    sub_url = str(panel.get("sub_base_url") or "").strip()
                    body = (f"کد: {panel.get('panel_code', panel_id)}\nنام: {panel.get('name', '')}\n"
                            f"نوع: {panel.get('panel_type', '')}\nوضعیت: {panel.get('lifecycle', '')}\n"
                            f"نشانی: {url}\nاعتبارنامه: {'ثبت شده (محرمانه)' if has_secret else 'ثبت نشده'}\n"
                            f"دامنهٔ سابسکریپشن: {sub_url if sub_url else 'ثبت نشده'}")
                    page = render_page("جزئیات پنل", body, persona="admin", rows=[
                        [button("ویرایش اتصال", f"v52:panelconfig:{panel_id}")],
                        [button("⚙️ تنظیمات پیشرفته پنل (ظرفیت، نام‌گذاری، تست، افزودنی)", f"v52:paneladv:{panel_id}")],
                        [button("🧪 آزمایش اتصال", f"v52:paneltest:{panel_id}")],
                        [button("⚡ تست اتصال و فعال‌سازی خودکار", f"v52:panelauto:{panel_id}")],
                        [button("🧩 انتخاب Inbound پیش‌فرض ساخت اکانت", f"v52:panelinbound:{panel_id}")],
                        [button("🟢 فعال‌سازی پس از آزمایش", f"v52:life:{panel_id}:active")],
                        [button("⬅️ بازگشت به فهرست", "v52:panels"),
                         button("🏠 بازگشت به پیشخوان", "v52:home:admin")],
                    ],
                    parent_callback="v52:panels")
            elif name == "choose_panel_inbound":
                if persona != "admin" or not ctx.get("is_root"):
                    raise PermissionError("انتخاب inbound پیش‌فرض فقط در اختیار root است.")
                from ui_v52 import render_page, button, render_error
                panel_id = int(params["panel_id"])
                listing = await asyncio.to_thread(xui_manager.list_panel_inbounds, panel_id)
                if listing.get("state") != "found":
                    page = render_error(
                        f"خواندن inboundهای پنل ممکن نشد ({listing.get('reason') or 'نامشخص'}).",
                        persona="admin", parent_callback=f"v52:panel:{panel_id}")
                else:
                    inbounds = listing.get("inbounds") or []
                    if not inbounds:
                        page = render_error(
                            "این پنل هیچ inbound فعالی برنگرداند.",
                            persona="admin", parent_callback=f"v52:panel:{panel_id}")
                    else:
                        lines = ["inboundهای خوانده‌شده از پنل:", ""]
                        rows = []
                        for item in inbounds[:20]:
                            lines.append(
                                f"• id={item.get('id')} | {item.get('protocol') or '—'} | "
                                f"پورت {item.get('port') or '—'} | {item.get('remark') or 'بدون نام'} | "
                                f"{'فعال' if item.get('enable') else 'غیرفعال'}"
                            )
                            rows.append([button(
                                f"انتخاب inbound {item.get('id')}",
                                f"v52:panelsetinbound:{panel_id}:{item.get('id')}")])
                        rows.append([button("بازگشت به پنل", f"v52:panel:{panel_id}")])
                        page = render_page(
                            "انتخاب Inbound پیش‌فرض ساخت اکانت", "\n".join(lines),
                            persona="admin", page_kind="form", rows=rows)
            elif name == "set_panel_inbound":
                if persona != "admin" or not ctx.get("is_root"):
                    raise PermissionError("ثبت inbound پیش‌فرض فقط در اختیار root است.")
                from ui_v52 import render_page
                panel_id = int(params["panel_id"])
                inbound_id = int(params["inbound_id"])
                with database.get_db() as conn:
                    exists = conn.execute("SELECT 1 FROM panels WHERE id=?", (panel_id,)).fetchone()
                    if exists is None:
                        raise ValueError("پنل موردنظر وجود ندارد.")
                    conn.execute(
                        "UPDATE panels SET default_inbound_ids_json=?, revision=revision+1 "
                        "WHERE id=?",
                        (json.dumps([inbound_id]), panel_id),
                    )
                    conn.commit()
                page = render_page(
                    "Inbound پیش‌فرض ثبت شد",
                    f"پنل {panel_id} اکنون اکانت‌های جدید را روی inbound {inbound_id} می‌سازد.",
                    persona="admin", page_kind="result",
                    parent_callback=f"v52:panel:{panel_id}")
            elif name == "begin_panel_config":
                if persona != "admin" or not ctx.get("is_root") or getattr(update.effective_chat, "type", "") != "private":
                    raise PermissionError("تنظیم اتصال پنل فقط در پیوی root مجاز است.")
                import panel_admin_ui
                panel_id = int(params["panel_id"])
                with database.get_db() as conn:
                    exists = conn.execute("SELECT 1 FROM panels WHERE id=?", (panel_id,)).fetchone()
                if exists is None:
                    raise ValueError("پنل موردنظر وجود ندارد.")
                # The edit path enters the very same step-1 builder as the add flow,
                # so add and edit can never diverge.
                page = panel_admin_ui.render_panel_type_step(ctx, panel_id=panel_id)
            elif name == "test_panel_connection":
                if persona != "admin" or not ctx.get("is_root"):
                    raise PermissionError("آزمایش اتصال پنل فقط در اختیار root است.")
                panel_id = int(params["panel_id"])
                with database.get_db() as conn:
                    row = conn.execute("SELECT * FROM panels WHERE id=?", (panel_id,)).fetchone()
                if row is None:
                    raise ValueError("پنل وجود ندارد.")
                panel_record = dict(row)
                kind = str(panel_record.get("panel_type") or "").lower().replace("_", "-")
                if kind not in {"3x-ui", "3xui", "xui"}:
                    raise ValueError("آزمایش آفلاین/آنلاین این نصب فعلاً برای پنل 3X-UI فعال است.")
                import requests
                from panel_adapters.xui_openapi import XUIOpenAPIAdapter
                adapter = XUIOpenAPIAdapter(panel_record, requests.Session(), transaction_probe=lambda: False)
                status = await adapter.verify_connection()
                reachable = bool(getattr(status, "reachable", False))
                body = "اتصال پنل موفق و پاسخ معتبر دریافت شد." if reachable else "پاسخ قابل‌اتکا دریافت نشد؛ وضعیت پنل تغییر نکرد."
                from ui_v52 import render_page, button
                if reachable:
                    rows = [[button("🟢 فعال‌سازی فوری پنل (Active)", f"v52:life:{panel_id}:active")]]
                else:
                    rows = [[button("🔁 آزمایش مجدد اتصال", f"v52:paneltest:{panel_id}")],
                            [button("✏️ ویرایش اتصال", f"v52:panelconfig:{panel_id}")]]
                rows.append([button("🖥 مشاهده کارت پنل", f"v52:panel:{panel_id}")])
                rows.append([button("🏠 بازگشت به منوی اصلی", "v52:home:admin")])
                page = render_page("نتیجه آزمایش اتصال", body, persona="admin", page_kind="result", rows=rows)
            elif name == "confirm_lifecycle":
                if persona != "admin" or not ctx.get("is_root"):
                    raise PermissionError("تغییر چرخه عمر پنل فقط در اختیار root است.")
                panel_id, state = int(params["panel_id"]), str(params["lifecycle"])
                if state == "active":
                    with database.get_db() as conn:
                        row = conn.execute("SELECT * FROM panels WHERE id=?", (panel_id,)).fetchone()
                    if row is None:
                        raise ValueError("پنل وجود ندارد.")
                    panel_record = dict(row)
                    kind = str(panel_record.get("panel_type") or "").lower().replace("_", "-")
                    if kind not in {"3x-ui", "3xui", "xui"}:
                        raise ValueError("آزمایش اتصال پیش از فعال‌سازی در این نسخه فقط برای 3X-UI پشتیبانی می‌شود.")
                    import requests
                    from panel_adapters.xui_openapi import XUIOpenAPIAdapter
                    adapter = XUIOpenAPIAdapter(panel_record, requests.Session(), transaction_probe=lambda: False)
                    status = await adapter.verify_connection()
                    if not bool(getattr(status, "reachable", False)):
                        raise RuntimeError("آزمایش اتصال موفق نبود؛ پنل در وضعیت قبلی باقی ماند.")
                result = database.set_panel_lifecycle(panel_id, state)
                from ui_v52 import render_page
                page = render_page(
                    "وضعیت پنل به‌روزرسانی شد",
                    f"پنل {panel_id} · وضعیت جدید: {result['lifecycle']}",
                    persona="admin", page_kind="result", parent_callback=f"v52:panel:{panel_id}",
                )
            elif name == "auto_activate_panel":
                # One-click test-then-activate: the panel is only promoted to
                # ``active`` after a verified read, and a failed probe leaves the
                # stored lifecycle untouched.
                if persona != "admin" or not ctx.get("is_root"):
                    raise PermissionError("آزمایش و فعال‌سازی پنل فقط در اختیار root است.")
                panel_id = int(params["panel_id"])
                with database.get_db() as conn:
                    row = conn.execute("SELECT * FROM panels WHERE id=?", (panel_id,)).fetchone()
                if row is None:
                    raise ValueError("پنل وجود ندارد.")
                panel_record = dict(row)
                kind = str(panel_record.get("panel_type") or "").lower().replace("_", "-")
                if kind not in {"3x-ui", "3xui", "xui"}:
                    raise ValueError("تست اتصال و فعال‌سازی خودکار در این نسخه فقط برای 3X-UI پشتیبانی می‌شود.")
                import requests
                from panel_adapters.xui_openapi import XUIOpenAPIAdapter
                from ui_v52 import render_page, button
                adapter = XUIOpenAPIAdapter(panel_record, requests.Session(), transaction_probe=lambda: False)
                status = await adapter.verify_connection()
                if not bool(getattr(status, "reachable", False)):
                    page = render_page(
                        "آزمایش اتصال ناموفق",
                        "پاسخ قابل‌اتکا دریافت نشد؛ وضعیت پنل تغییر نکرد و فعال‌سازی انجام نشد.",
                        persona="admin", page_kind="result",
                        parent_callback=f"v52:panel:{panel_id}",
                        rows=[[button("🔁 آزمایش مجدد", f"v52:panelauto:{panel_id}")],
                              [button("✏️ ویرایش اتصال", f"v52:panelconfig:{panel_id}")]],
                    )
                else:
                    result = database.set_panel_lifecycle(panel_id, "active")
                    page = render_page(
                        "پنل فعال شد",
                        f"آزمایش اتصال موفق بود و پنل {panel_id} به وضعیت "
                        f"{result['lifecycle']} تغییر یافت.",
                        persona="admin", page_kind="result",
                        parent_callback=f"v52:panel:{panel_id}",
                        rows=[[button("🖥 مشاهده کارت پنل", f"v52:panel:{panel_id}")],
                              [button("🏠 بازگشت به منوی اصلی", "v52:home:admin")]],
                    )
            elif name == "list_domains":
                import domain_ui
                page = domain_ui.domains_page(ctx, scope="admin" if ctx.get("is_root") else "rep")
            elif name == "show_domain":
                import domain_ui
                page = domain_ui.domain_detail_page(
                    int(params["domain_id"]), ctx=ctx, persona=persona,
                    parent_callback="v52:domains")
            elif name == "begin_domain_register":
                import domain_ui
                if ctx.get("is_root"):
                    page = domain_ui.rep_picker_page(ctx, persona=persona, parent_callback="v52:domains")
                else:
                    rep_id = _v52_actor_rep_id(ctx)
                    v52_router.DEFAULT_FSM.set(
                        ctx, "domain_hostname", payload={"rep_id": rep_id},
                        parent_callback="v52:domains", now=int(time.time()))
                    page = domain_ui.register_hostname_prompt(
                        ctx, rep_id, persona=persona, parent_callback="v52:domains")
            elif name == "domain_register_rep":
                import domain_ui
                rep_id = int(params["rep_id"])
                v52_router.DEFAULT_FSM.set(
                    ctx, "domain_hostname", payload={"rep_id": rep_id},
                    parent_callback="v52:domains", now=int(time.time()))
                page = domain_ui.register_hostname_prompt(
                    ctx, rep_id, persona=persona, parent_callback="v52:domains")
            elif name == "domain_register_role":
                import domain_ui
                draft = _v52_domain_draft(context)
                draft["role"] = str(params["role"])
                _v52_set_domain_draft(context, draft)
                page = domain_ui.register_panel_step(ctx, persona=persona, parent_callback="v52:domains")
            elif name == "domain_register_panel":
                import domain_ui
                draft = _v52_domain_draft(context)
                draft["panel_id"] = params.get("panel_id")
                _v52_set_domain_draft(context, draft)
                page = domain_ui.register_primary_step(ctx, persona=persona, parent_callback="v52:domains")
            elif name == "domain_register_primary":
                import domain_ui
                import domain_service
                draft = _v52_domain_draft(context)
                rep_id = int(draft.get("rep_id") or 0)
                hostname = str(draft.get("hostname") or "")
                if rep_id <= 0 or not hostname:
                    raise ValueError("پیش‌نویس ثبت دامنه ناقص است؛ از ابتدا شروع کنید.")
                with database.get_db() as conn:
                    result = domain_service.register_domain(
                        conn, int(ctx.get("actor_tg_id") or 0), rep_id, hostname,
                        role=str(draft.get("role") or "subscription"),
                        panel_id=draft.get("panel_id"),
                        is_primary=bool(params.get("is_primary")))
                _v52_clear_domain_draft(context)
                v52_router.DEFAULT_FSM.clear(ctx)
                page = domain_ui.show_verification(
                    result, persona=persona, parent_callback="v52:domains")
            elif name == "domain_verify":
                from ui_v52 import render_page
                import domain_service
                domain_id = int(params["domain_id"])
                with database.get_db() as conn:
                    domain_service.ensure_domain_columns(conn)
                    row = conn.execute(
                        "SELECT domain_id,rep_id,hostname,status FROM sub_domains WHERE domain_id=?",
                        (domain_id,)).fetchone()
                if row is None:
                    raise ValueError("دامنهٔ موردنظر ثبت نشده است.")
                if not ctx.get("is_root") and int(row["rep_id"]) != int(ctx.get("rep_id") or 0):
                    raise PermissionError("only the domain owner or root may verify this domain")
                try:
                    result = await asyncio.to_thread(_v52_run_domain_verify, domain_id)
                    page = render_page(
                        "بررسی وضعیت DNS و SSL دامنه",
                        f"دامنه: {row['hostname']}\nبررسی DNS/TLS موفق بود.\n"
                        f"وضعیت جدید: {result.status}",
                        persona=persona, page_kind="result",
                        parent_callback=f"v52:domaindetail:{domain_id}")
                except domain_service.DomainError as exc:
                    page = render_page(
                        "بررسی وضعیت DNS و SSL دامنه",
                        f"دامنه: {row['hostname']}\nبررسی ناموفق: {exc}\n"
                        "رکوردهای TXT/CNAME/A و گواهی TLS را اصلاح و دوباره تلاش کنید.",
                        persona=persona, page_kind="error",
                        parent_callback=f"v52:domaindetail:{domain_id}")
            elif name == "domainactivate":
                from ui_v52 import render_page
                import confirmation_service
                import domain_service
                domain_id = int(params["domain_id"])
                actor = int(ctx.get("actor_tg_id") or 0)
                bot_id = int(ctx.get("bot_id") or 0)
                now = int(time.time())
                with database.get_db() as conn:
                    domain_service.ensure_domain_columns(conn)
                    row = conn.execute(
                        "SELECT domain_id,rep_id,hostname,status,tls_checked_at "
                        "FROM sub_domains WHERE domain_id=?", (domain_id,)).fetchone()
                    if row is None:
                        raise ValueError("دامنهٔ موردنظر ثبت نشده است.")
                    if not ctx.get("is_root") and int(row["rep_id"]) != int(ctx.get("rep_id") or 0):
                        raise PermissionError("only the domain owner or root may activate this domain")
                    if row["status"] != "verified" or row["tls_checked_at"] is None:
                        raise domain_service.DomainProofError(
                            "دامنه باید پیش از فعال‌سازی وضعیت verified داشته باشد.")
                    if not conn.in_transaction:
                        conn.execute("BEGIN IMMEDIATE")
                    conf = confirmation_service.create_confirmation(
                        conn, actor_tg_id=actor, bot_id=bot_id, action="domain.activate",
                        target_type="sub_domain", target_id=domain_id,
                        expected_revision=int(row["tls_checked_at"]), payload={}, now=now)
                    result = domain_service.activate_domain(
                        conn, actor, domain_id, nonce=conf["nonce"], bot_id=bot_id, now=now)
                    conn.commit()
                page = render_page(
                    "دامنه فعال شد", f"دامنه {result.hostname} فعال شد.",
                    persona=persona, page_kind="result",
                    parent_callback=f"v52:domaindetail:{domain_id}")
            elif name == "preview_domain_lifecycle":
                import domain_ui
                page = domain_ui.preview_domain_lifecycle(
                    ctx, int(params["domain_id"]), state=params.get("state"),
                    persona=persona, parent_callback="v52:domains")
            elif name == "confirm_domain_lifecycle":
                from ui_v52 import render_page
                import domain_service
                domain_id = int(params["domain_id"])
                with database.get_db() as conn:
                    result = domain_service.set_domain_status(
                        conn, int(ctx.get("actor_tg_id") or 0), domain_id,
                        status=str(params["state"]))
                page = render_page(
                    "وضعیت دامنه به‌روزرسانی شد",
                    f"دامنه {result.hostname} · وضعیت جدید: {result.status}",
                    persona=persona, page_kind="result",
                    parent_callback=f"v52:domaindetail:{domain_id}")
            elif name == "preview_domain_delete":
                import domain_ui
                page = domain_ui.preview_domain_delete(
                    ctx, int(params["domain_id"]), persona=persona, parent_callback="v52:domains")
            elif name == "confirm_domain_delete":
                from ui_v52 import render_page
                import domain_service
                domain_id = int(params["domain_id"])
                with database.get_db() as conn:
                    result = domain_service.delete_domain(
                        conn, int(ctx.get("actor_tg_id") or 0), domain_id)
                page = render_page(
                    "دامنه حذف شد",
                    f"دامنه {result.hostname} برای همیشه حذف شد.",
                    persona=persona, page_kind="result", parent_callback="v52:domains")
            elif name == "list_tickets":
                import support_ui
                page = support_ui.tickets_page(
                    ctx, group=str(params.get("group") or "open"),
                    parent_callback="v52:home:" + persona,
                )
            elif name == "begin_new_ticket":
                import support_ui
                v52_router.DEFAULT_FSM.set(
                    ctx, "support_ticket_body",
                    payload={"kind": "ticket", "account_id": 0, "ticket_type": "general"},
                    parent_callback="v52:tickets", now=int(time.time()),
                )
                page = support_ui.begin_ticket(ctx, 0, submit=False, parent_callback="v52:tickets")
            elif name == "list_abuse":
                if persona != "admin" or not ctx.get("is_root"):
                    raise PermissionError("گزارش‌های تخلف فقط در اختیار مدیریت کل است.")
                import abuse_service
                from ui_v52 import render_page, button
                reports = abuse_service.list_abuse_reports(ctx)
                import profile_360
                rows = []
                for item in reports[:12]:
                    abuse_ticket = str(item["ticket_id"])
                    row = [button(f"🎫 {abuse_ticket[:8]} · {item['status']}",
                                  "v52:ticket:" + abuse_ticket)]
                    # The reporter's 360-degree card, remembering the report it was
                    # opened from. This surface is root-only, so the button can never
                    # be shown to a lower level.
                    sender = int(item.get("sender_tg_id") or 0)
                    if sender:
                        row.append(button("👤 پروفایل", profile_360.profile_callback(
                            "view", sender, "abuse", abuse_ticket)))
                    rows.append(row)
                page = render_page(
                    "🚨 گزارش‌های تخلف",
                    f"تعداد گزارش‌های ثبت‌شده: {len(reports)}\n"
                    "این مسیر کاملاً جدا از تیکت‌های پشتیبانی است و فقط برای مدیریت کل "
                    "نمایش داده می‌شود؛ هیچ نسخه‌ای به صندوق نماینده نمی‌رود.",
                    persona="admin", rows=rows, parent_callback="v52:home:admin",
                )
            elif name == "ticket_status_settings":
                import ticket_status_ui
                page = ticket_status_ui.ticket_status_settings(ctx, parent_callback="v52:tickets")
            elif name == "begin_ticket_status_new":
                import ticket_status_ui
                page = ticket_status_ui.begin_ticket_status_new(
                    ctx, parent_callback="v52:ticketsettings")
            elif name == "begin_ticket_status_edit":
                import ticket_status_ui
                page = ticket_status_ui.begin_ticket_status_edit(
                    ctx, str(params["code"]), parent_callback="v52:ticketsettings")
            elif name == "toggle_ticket_status":
                import ticket_status_ui
                page = ticket_status_ui.toggle_ticket_status(
                    ctx, str(params["code"]), parent_callback="v52:ticketsettings")
            elif name == "delete_ticket_status":
                import ticket_status_ui
                page = ticket_status_ui.delete_ticket_status(
                    ctx, str(params["code"]), parent_callback="v52:ticketsettings")
            elif name == "set_ticket_status":
                import ticket_status_ui
                selected_ticket = str(params["ticket_id"])
                page = ticket_status_ui.set_ticket_status(
                    ctx, selected_ticket, str(params["code"]),
                    parent_callback=f"v52:ticket:{selected_ticket}")
            elif name == "list_tiers":
                import tier_admin_ui
                page = tier_admin_ui.list_tiers(ctx)
            elif name == "begin_tier_new":
                import tier_admin_ui
                page = tier_admin_ui.begin_tier_new(ctx)
            elif name == "begin_tier_edit":
                import tier_admin_ui
                page = tier_admin_ui.begin_tier_edit(
                    ctx, str(params["actor_type"]), str(params["code"]))
            elif name == "delete_tier":
                import tier_admin_ui
                page = tier_admin_ui.delete_tier(
                    ctx, str(params["actor_type"]), str(params["code"]))
            elif name == "close_ticket":
                import support_ui
                page = support_ui.reply_or_escalate(
                    ctx, str(params["ticket_id"]), action="close",
                    confirm=True, parent_callback="v52:tickets",
                )
            elif name == "reopen_ticket":
                import support_ui
                page = support_ui.reply_or_escalate(
                    ctx, str(params["ticket_id"]), action="reopen",
                    confirm=True, parent_callback="v52:tickets",
                )
            elif name == "begin_ticket":
                import support_ui
                account_id = int(params["account_id"])
                v52_router.DEFAULT_FSM.set(
                    ctx, "support_ticket_body",
                    payload={"kind": "ticket", "account_id": account_id, "ticket_type": "general"},
                    parent_callback=data, now=int(time.time()),
                )
                page = support_ui.begin_ticket(ctx, account_id, submit=False, parent_callback="v52:tickets")
            elif name == "begin_abuse":
                import support_ui
                v52_router.DEFAULT_FSM.set(
                    ctx, "support_ticket_body", payload={"kind": "abuse"},
                    parent_callback=data, now=int(time.time()),
                )
                page = support_ui.begin_abuse(ctx, chat_type=ctx.get("chat_type"), parent_callback="v52:tickets")
            elif name == "reply_or_escalate":
                import support_service
                from ui_v52 import render_page, button
                if params.get("action") == "transfer":
                    page = _begin_ownership_transfer(ctx, int(params["account_id"]), persona)
                elif params.get("action") == "escalate":
                    ticket_id = str(params["ticket_id"])
                    import support_ui
                    page = support_ui.reply_or_escalate(
                        ctx, ticket_id, action="escalate", confirm=True, parent_callback="v52:tickets",
                    )
                else:
                    page = _v52_ticket_page(ctx, persona, str(params["ticket_id"]),
                                            fsm_parent=data)
            elif name == "p360_ticket":
                # A ticket opened from a 360-degree profile: the card carries an
                # explicit Back button to the profile it was opened from, and the
                # reply draft's stale-form escape hatch goes there too.
                import profile_360
                user_id = int(params["user_id"])
                origin, ref = str(params["origin"]), str(params["ref"])
                back = profile_360.profile_callback("view", user_id, origin, ref)
                page = _v52_ticket_page(ctx, persona, str(params["ticket_id"]),
                                        fsm_parent=back, page_parent=back)
            elif name == "p360_account_card":
                # The account card, with its Back button rebuilt from the profile it
                # was opened from. ``v52:sub:<id>`` alone had no registered parent,
                # so the operator who drilled in from a profile lost the card.
                import account_card_service
                import profile_360
                user_id = int(params["user_id"])
                origin, ref = str(params["origin"]), str(params["ref"])
                card = await account_card_service.build_account_card(
                    ctx, int(params["account_id"]),
                    adapter_factory=admin_panel._get_admin_panel_adapter_factory(context),
                    parent_callback=profile_360.profile_callback(
                        "view", user_id, origin, ref))
                page = type("V52Page", (), {"text": card.text,
                                            "reply_markup": card.reply_markup})()
            elif name in {"p360_issue_service", "p360_transfer_ownership"}:
                # The two management actions the 360-degree card offers at every
                # level. Both are re-authorised here: the card is not the only way
                # to reach a callback, and a scoped representative must not be able
                # to act on somebody who is not downstream of them.
                import profile_360
                from ui_v52 import render_page
                user_id = int(params["user_id"])
                origin, ref = str(params["origin"]), str(params["ref"])
                viewer_rep_id = _v52_viewer_rep_id(ctx)
                if viewer_rep_id is not None and not profile_360.scoped_rep_scope_holds(
                        user_id, viewer_rep_id):
                    raise PermissionError("این عملیات برای این کاربر مجاز نیست.")
                builder = (profile_360.build_issue_service_page
                           if name == "p360_issue_service"
                           else profile_360.build_transfer_ownership_page)
                title, body, rows = builder(
                    user_id, viewer_rep_id=viewer_rep_id, origin=origin, ref=ref)
                page = render_page(
                    title, body, persona=persona, rows=rows,
                    parent_callback=profile_360.profile_callback(
                        "view", user_id, origin, ref))
            elif name == "sync_topics":
                if persona != "admin" or not ctx.get("is_root"):
                    raise PermissionError("همگام‌سازی تاپیک‌ها فقط در اختیار root است.")
                main_group_id = database.get_main_group_id()
                if not main_group_id:
                    raise ValueError("شناسه سوپرگروه اصلی در تنظیمات ثبت نشده است.")
                result = await _sync_v52_forum_topics(
                    context.bot, int(context.bot.id), int(main_group_id),
                    owner_key=str(params.get("owner_key") or "root"),
                )
                from ui_v52 import render_page
                lines = [
                    f"وضعیت: {result.get('state')}",
                    f"سوپرگروه: `{main_group_id}`",
                    f"تاپیک‌های فعال: {result.get('active', 0)}/{result.get('topics', 0)}",
                    f"شناسه‌های ذخیره‌شده: {result.get('settings_written', 0)}",
                ]
                for item in (result.get("outcomes") or [])[:10]:
                    lines.append(
                        f"• {item.get('topic_key')}: {item.get('state')} "
                        f"(thread={item.get('thread_id', '-')})"
                    )
                if result.get("state") != "completed":
                    lines.append(f"جزئیات: {result.get('reason', 'ثبت شد')}")
                page = render_page(
                    "همگام‌سازی فروم", "\n".join(lines),
                    persona="admin", page_kind="result", parent_callback="v52:panels",
                )
            elif name == "route_home":
                target = params.get("persona")
                if target == "admin":
                    await admin_panel.show_admin_dashboard(update, context, edit_message=True)
                elif target == "rep":
                    active_rep = _resolve_persona_rep(user, rep)
                    if active_rep is None:
                        from ui_v52 import render_error
                        await _send_v52_page(query, render_error(
                            "این نقش برای حساب شما فعال نیست.", persona="rep",
                            parent_callback="v52:home:user"))
                        return True
                    await rep_panel.show_rep_dashboard(update, context, active_rep, edit_message=True)
                else:
                    await user_portal.show_user_portal(update, context, edit_message=True)
                await query.answer("منوی اصلی")
                return True
            elif name == "choose_rail":
                if params.get("rail") not in {"card", "card_to_card"}:
                    from ui_v52 import render_error
                    page = render_error("این پل پرداخت در W18-B01 فعال نشده است؛ هیچ برداشت یا تحویلی انجام نشد.",
                                        persona=persona, parent_callback="v52:shop")
                else:
                    import payment_service
                    import checkout_ui
                    order_id = str(params["order_id"])
                    with database.get_db() as conn:
                        order_row = conn.execute(
                            "SELECT order_id,actor_tg_id,bot_id,merchant_key,purpose,payment_rail,amount_toman,"
                            "status,received_toman,created_at,expires_at,revision FROM payment_orders WHERE order_id=?",
                            (order_id,),
                        ).fetchone()
                        if order_row is None:
                            raise ValueError("سفارش پرداخت پیدا نشد.")
                        order = {"order_id": str(order_row[0]), "actor_tg_id": int(order_row[1]),
                                 "bot_id": int(order_row[2]), "merchant_key": str(order_row[3]),
                                 "purpose": str(order_row[4]), "payment_rail": str(order_row[5]),
                                 "amount_toman": int(order_row[6]), "status": str(order_row[7]),
                                 "received_toman": int(order_row[8]), "created_at": int(order_row[9]),
                                 "expires_at": int(order_row[10]), "revision": int(order_row[11])}
                        if order["actor_tg_id"] != int(user.id) or order["bot_id"] != int(ctx["bot_id"]):
                            raise PermissionError("سفارش متعلق به این کاربر/بات نیست.")
                        attempt = payment_service.create_card_attempt(
                            conn, order_id=order_id, actor_tg_id=int(user.id),
                            bot_id=int(ctx["bot_id"]), now=int(time.time())
                        )
                    context.user_data.update({
                        "v52_payment_attempt_id": attempt["attempt_id"],
                        "v52_payment_order_id": order_id,
                        "v52_payment_amount": int(order["amount_toman"]),
                        "v52_payment_pending": True,
                    })
                    page = checkout_ui.preview_receipt(ctx, attempt, order=order)
            elif name == "preview_rotation":
                import account_actions_ui
                aid = params["account_id"]
                rtype = params["rotation_type"]
                page = account_actions_ui.preview_rotation(
                    ctx, aid, rtype,
                    parent_callback=f"v52:sub:{aid}",
                    next_callback=f"v52:rotate:confirm:{rtype}:{aid}",
                )
            elif name == "show_grants":
                import access_ui
                page = access_ui.show_grants(ctx, params["account_id"], persona=persona)
            elif name == "confirm_rotation":
                import account_actions_ui
                import confirmation_service
                import time
                aid = int(params["account_id"])
                rtype = str(params["rotation_type"])
                actor_id = int(ctx.get("actor_tg_id") or 0)
                bot_id = int(ctx.get("bot_id") or 0)
                now = int(time.time())
                action = "rotate_subscription_url" if rtype == "sub" else "rotate_connection_credentials"
                with database.get_db() as conn:
                    row = conn.execute(
                        "SELECT revision FROM accounts WHERE id=? AND deleted_at IS NULL",
                        (aid,),
                    ).fetchone()
                    if row is None:
                        raise ValueError("حساب برای چرخش پیدا نشد.")
                    revision = int(row["revision"])
                    conn.execute("BEGIN IMMEDIATE")
                    try:
                        nonce_data = confirmation_service.create_confirmation(
                            conn, actor_tg_id=actor_id, bot_id=bot_id,
                            action=action, target_type=rtype, target_id=str(aid),
                            expected_revision=revision, payload={}, now=now)
                        conn.commit()
                    except Exception:
                        if conn.in_transaction:
                            conn.rollback()
                        raise
                confirmation = {
                    "callback_ok": nonce_data["callback_ok"],
                    "callback_no": nonce_data["callback_no"],
                }
                page = account_actions_ui.preview_rotation(
                    ctx, aid, rtype, confirmation=confirmation,
                    parent_callback=f"v52:sub:{aid}",
                )
            elif name == "preview_revoke":
                import access_ui
                page = access_ui.preview_revoke(ctx, params["account_id"], params["grantee_tg_id"], persona=persona)
            elif name == "begin_register":
                import domain_ui
                page = domain_ui.begin_register(ctx, params["rep_id"])
            elif name == "begin_register_bot":
                import bot_admin_ui
                if params.get("kind") == "primary":
                    page = bot_admin_ui.begin_register_bot(
                        ctx, rep_id=None, is_primary=True, chat_type=ctx.get("chat_type"))
                    _v52_arm_bot_token_state(ctx, rep_id=None, is_primary=True)
                elif params.get("kind") == "rep":
                    from ui_v52 import render_page, button
                    reps = database.get_all_active_reps()
                    rows = [[button("ثبت بات نماینده " + str(int(item["id"])),
                                    "v52:bot:" + str(int(item["id"])))] for item in reps]
                    page = render_page(
                        "ثبت بات نماینده",
                        "نمایندهٔ مالک بات را انتخاب کنید. توکن فقط در پیوی root دریافت می‌شود.",
                        persona="admin", rows=rows, parent_callback="v52:bots",
                    )
                else:
                    page = bot_admin_ui.begin_register_bot(ctx, params["rep_id"], chat_type=ctx.get("chat_type"))
                    _v52_arm_bot_token_state(ctx, rep_id=int(params["rep_id"]),
                                             is_primary=False)
            elif name == "list_bots":
                import bot_admin_ui
                page = bot_admin_ui.build_bots_page(
                    ctx, database.list_registered_bots(),
                    reps=database.get_all_active_reps())
            elif name == "bot_view":
                import bot_admin_ui
                bot = _v52_load_bot(int(params["bot_id"]))
                page = (bot_admin_ui.build_bot_detail_page(ctx, bot) if bot is not None
                        else ui_v52.render_error("این بات در فهرست ثبت نشده است.",
                                                 persona="admin", parent_callback="v52:bots"))
            elif name in ("bot_enable", "bot_disable", "bot_delete"):
                import bot_admin_ui
                bot_id = int(params["bot_id"])
                bot = _v52_load_bot(bot_id)
                if bot is None:
                    page = ui_v52.render_error("این بات در فهرست ثبت نشده است.",
                                               persona="admin", parent_callback="v52:bots")
                else:
                    action = {"bot_enable": "bot.enable", "bot_disable": "bot.disable",
                              "bot_delete": "bot.delete"}[name]
                    prepared = _v52_prepare_bot_action(ctx, name, bot_id)
                    page = bot_admin_ui.build_bot_action_preview(
                        ctx, bot, prepared, action=action,
                        parent_callback="v52:bot:view:" + str(bot_id))
            elif name == "bot_reassign_pick":
                import bot_admin_ui
                bot = _v52_load_bot(int(params["bot_id"]))
                page = (bot_admin_ui.build_bot_reassign_picker(
                            ctx, bot, database.get_all_active_reps())
                        if bot is not None
                        else ui_v52.render_error("این بات در فهرست ثبت نشده است.",
                                                 persona="admin", parent_callback="v52:bots"))
            elif name == "bot_reassign":
                import bot_admin_ui
                bot_id = int(params["bot_id"])
                raw = str(params.get("new_rep") or "root")
                bot = _v52_load_bot(bot_id)
                if bot is None:
                    page = ui_v52.render_error("این بات در فهرست ثبت نشده است.",
                                               persona="admin", parent_callback="v52:bots")
                else:
                    prepared = _v52_prepare_bot_action(
                        ctx, "bot_reassign", bot_id,
                        new_rep_id=None if raw == "root" else int(raw))
                    page = bot_admin_ui.build_bot_action_preview(
                        ctx, bot, prepared, action="bot.reassign",
                        parent_callback="v52:bot:view:" + str(bot_id))
            elif name == "show_sub_reps":
                import rep_admin_ui
                page = rep_admin_ui.show_sub_reps(ctx, int(params["rep_id"]), persona=persona)
            elif name == "list_orders":
                page = _v52_orders_page(ctx, persona)
            elif name == "show_finance":
                page = _v52_finance_page(ctx)
            elif name == "list_panels":
                from ui_v52 import render_page, button
                with database.get_db() as conn:
                    panel_rows = conn.execute(
                        "SELECT id,name,panel_type,lifecycle,is_default FROM panels "
                        "ORDER BY is_default DESC,id").fetchall()
                body = "\n".join(
                    f"پنل {int(row['id'])}: {row['name']} · {row['panel_type']} · {row['lifecycle']}"
                    for row in panel_rows
                ) or "پنلی ثبت نشده است."
                buttons = [[button("مشاهده پنل " + str(int(row["id"])), "v52:panel:" + str(int(row["id"])))]
                           for row in panel_rows]
                buttons.append([button("افزودن پنل", "v52:panelnew")])
                page = render_page("مدیریت پنل‌ها", body, persona="admin", rows=buttons)
            elif name == "begin_add_panel":
                import panel_admin_ui
                page = panel_admin_ui.begin_add_panel(ctx)
            elif name == "begin_panel_draft":
                page = _v52_begin_panel_draft(ctx, str(params["panel_type"]))
            elif name == "set_panel_type":
                if persona != "admin" or not ctx.get("is_root"):
                    raise PermissionError("ویرایش پنل فقط در اختیار root است.")
                page = _v52_set_panel_type(ctx, int(params["panel_id"]), str(params["panel_type"]))
            elif name == "begin_panel_name":
                if persona != "admin" or not ctx.get("is_root"):
                    raise PermissionError("افزودن/ویرایش پنل فقط در اختیار root است.")
                page = _v52_begin_panel_name(ctx, int(params["panel_id"]))
            elif name == "begin_panel_url":
                if persona != "admin" or not ctx.get("is_root"):
                    raise PermissionError("افزودن/ویرایش پنل فقط در اختیار root است.")
                page = _v52_begin_panel_url(ctx, int(params["panel_id"]))
            elif name == "begin_panel_credential":
                if persona != "admin" or not ctx.get("is_root"):
                    raise PermissionError("افزودن/ویرایش پنل فقط در اختیار root است.")
                page = _v52_begin_panel_credential(ctx, int(params["panel_id"]))
            elif name == "begin_panel_suburl":
                if persona != "admin" or not ctx.get("is_root"):
                    raise PermissionError("افزودن/ویرایش پنل فقط در اختیار root است.")
                from panel_adapters.url_parser import parse_panel_url
                pid = int(params["panel_id"])
                v52_router.DEFAULT_FSM.set(
                    ctx, "panel_connection_suburl", payload={"panel_id": pid},
                    parent_callback=f"v52:panelcred:{pid}", now=int(time.time()))
                import panel_admin_ui
                page = panel_admin_ui.render_panel_suburl_step(ctx, pid)
            elif name == "confirm_panel_draft":
                page = _v52_confirm_panel_draft(ctx, str(params["step"]))
            elif name == "preview_panel_policy":
                page = _v52_preview_panel_policy(ctx, int(params["panel_id"]))
            elif name == "open_receipt":
                await _load_v52_receipt_page(update, context, str(params["attempt_id"]), ctx)
                return True
            elif name == "preview_lifecycle":
                import panel_admin_ui
                page = panel_admin_ui.preview_lifecycle(ctx, params["panel_id"], params["lifecycle"])
            elif name == "open_account_profiles":
                import account_card_service
                card = await account_card_service.build_account_card(
                    ctx, params["account_id"], adapter_factory=admin_panel._get_admin_panel_adapter_factory(context))
                page = type("V52Page", (), {"text": card.text, "reply_markup": card.reply_markup})()
            elif name == "list_profiles":
                import subscription_ui
                page = subscription_ui.list_profiles(ctx)
            elif name == "show_profile":
                import subscription_ui
                page = subscription_ui.show_profile(ctx, params["profile_id"])
            elif name == "show_catalog":
                import goods_ui
                from ui_v52 import render_page
                if persona == "admin":
                    text, rows = goods_ui.admin_shop_page(ctx)
                    page = render_page("🛍 مدیریت فروشگاه", text, persona="admin",
                                       rows=rows, parent_callback="v52:home:admin")
                else:
                    page = goods_ui.show_catalog(
                        ctx, _load_digital_products(), stock_counts=_load_digital_stock_counts())
            elif name == "view_catalog":
                import goods_ui
                page = goods_ui.show_catalog(
                    ctx, _load_digital_products(), stock_counts=_load_digital_stock_counts())
            elif name == "begin_product_wizard":
                import goods_ui
                from ui_v52 import render_page
                text, rows = goods_ui.begin_product_wizard(ctx)
                page = render_page("➕ افزودن محصول", text, persona="admin", rows=rows)
            elif name == "product_wizard_choice":
                import goods_ui
                from ui_v52 import render_page
                text, rows = goods_ui.handle_product_wizard_choice(
                    ctx, str(params["field"]), str(params["value"]))
                page = render_page("➕ افزودن محصول", text, persona="admin", rows=rows)
            elif name == "admin_product_detail":
                import goods_ui
                from ui_v52 import render_page
                text, rows = goods_ui.admin_product_detail(ctx, int(params["product_id"]))
                page = render_page("📦 جزئیات محصول", text, persona="admin", rows=rows)
            elif name == "admin_catalog_page":
                import goods_ui
                from ui_v52 import render_page
                text, rows = goods_ui.show_admin_product_catalog_page(
                    ctx, int(params["page"]))
                page = render_page("📋 کاتالوگ مدیریت کالا", text, persona="admin", rows=rows)
            elif name == "shop_categories":
                import goods_ui
                from ui_v52 import render_page
                text, rows = goods_ui.show_category_management(ctx)
                page = render_page("🏷 مدیریت دسته‌بندی‌ها", text, persona="admin", rows=rows)
            elif name == "shop_sales_report":
                import goods_ui
                from ui_v52 import render_page
                text, rows = goods_ui.show_sales_report(
                    ctx, int(params["product_id"]), int(params["page"]))
                page = render_page("🛍 گزارش فروش", text, persona="admin", rows=rows)
            elif name == "product_wizard_category":
                import goods_ui
                from ui_v52 import render_page
                text, rows = goods_ui.handle_product_wizard_choice(
                    ctx, "category", str(params["value"]))
                page = render_page("➕ افزودن محصول", text, persona="admin", rows=rows)
            elif name == "begin_product_stock":
                import goods_ui
                from ui_v52 import render_page
                text, rows = goods_ui.begin_stock_import(ctx, int(params["product_id"]))
                page = render_page("📦 ورود موجودی", text, persona="admin", rows=rows)
            elif name == "begin_product_stock_mode":
                import goods_ui
                from ui_v52 import render_page
                text, rows = goods_ui.begin_stock_import_mode(
                    ctx, int(params["product_id"]), str(params["mode"]))
                page = render_page("📦 ورود موجودی", text, persona="admin", rows=rows)
            elif name == "begin_structured_item":
                # S60 — the guided builder for a multi-line credential card. It is a
                # ``ui_v52`` wizard, so it renders into its own message and commits
                # through the flow's own confirmation card.
                import inventory_admin_ui
                from ui_v52 import wizard_render
                _wz_state = inventory_admin_ui.begin_structured_item(
                    context, query, int(params["product_id"]))
                await wizard_render(update, context, _wz_state)
                return True
            elif name == "begin_product_edit":
                import goods_ui
                from ui_v52 import render_page
                text, rows = goods_ui.begin_product_edit(ctx, int(params["product_id"]))
                page = render_page("✏️ ویرایش محصول", text, persona="admin", rows=rows)
            elif name == "toggle_product":
                import goods_ui
                from ui_v52 import render_page
                text, rows = goods_ui.toggle_product_active(ctx, int(params["product_id"]))
                page = render_page("📦 جزئیات محصول", text, persona="admin", rows=rows)
            elif name == "begin_product_price":
                import goods_ui
                from ui_v52 import render_page
                text, rows = goods_ui.begin_product_price(ctx, int(params["product_id"]))
                page = render_page("💵 تغییر قیمت", text, persona="admin", rows=rows)
            elif name == "begin_product_guide":
                import goods_ui
                from ui_v52 import render_page
                text, rows = goods_ui.begin_product_guide(ctx, int(params["product_id"]))
                page = render_page("📘 راهنمای استفاده محصول", text, persona="admin", rows=rows)
            elif name == "clear_product_guide":
                import goods_ui
                from ui_v52 import render_page
                text, rows = goods_ui.clear_product_guide(ctx, int(params["product_id"]))
                page = render_page("📘 راهنمای استفاده محصول", text, persona="admin", rows=rows)
            elif name == "preview_product_delete":
                import goods_ui
                from ui_v52 import render_page
                text, rows = goods_ui.preview_product_delete(ctx, int(params["product_id"]))
                page = render_page("🗑 حذف محصول", text, persona="admin", rows=rows)
            elif name == "confirm_product_delete":
                import goods_ui
                from ui_v52 import render_page
                text, rows = goods_ui.confirm_product_delete(ctx, int(params["product_id"]))
                page = render_page("🗑 حذف محصول", text, persona="admin", rows=rows)
            elif name == "preview_product":
                import goods_ui
                from ui_v52 import render_error
                product = _load_digital_product(int(params["product_id"]))
                if product is None:
                    page = render_error("کالای موردنظر یافت نشد.", persona=persona,
                                        parent_callback="v52:shop")
                else:
                    page = goods_ui.preview_product(ctx, product)
            elif name == "add_member":
                import subscription_ui
                page = subscription_ui.add_member(ctx, int(params["profile_id"]), int(params["account_id"]))
            elif name == "preview_remove":
                import subscription_ui
                page = subscription_ui.preview_remove(
                    ctx, int(params["profile_id"]), int(params["account_id"]), now=int(time.time()))
            elif name == "begin_create":
                import subscription_ui
                page = subscription_ui.begin_create(ctx, int(params.get("account_id") or 0))
            elif name == "show_link":
                import subscription_ui
                page = subscription_ui.show_link(ctx, int(params["profile_id"]))
            elif name == "show_qr":
                import subscription_ui
                page = subscription_ui.show_qr(ctx, int(params["profile_id"]))
            elif name in {"preview_rotate", "preview_delete", "preview_clear", "preview_enabled"}:
                import subscription_ui
                actions = {"preview_rotate": "rotate", "preview_delete": "delete", "preview_clear": "clear"}
                if name == "preview_enabled":
                    action = "resume" if params.get("enabled") else "pause"
                else:
                    action = actions[name]
                page = subscription_ui.preview_profile_action(
                    ctx, int(params["profile_id"]), action,
                    now=int(time.time()), chat_type=ctx.get("chat_type"))
            elif name == "begin_rename":
                import subscription_ui
                page = subscription_ui.begin_rename(
                    ctx, int(params["profile_id"]), chat_type=ctx.get("chat_type"))
            elif name == "begin_traffic":
                import account_actions_ui
                page = account_actions_ui.begin_traffic(
                    ctx, int(params["account_id"]),
                    parent_callback=f"v52:sub:{int(params['account_id'])}")
            elif name == "begin_expiry":
                import account_actions_ui
                page = account_actions_ui.begin_expiry(
                    ctx, int(params["account_id"]),
                    parent_callback=f"v52:sub:{int(params['account_id'])}")
            elif name == "begin_grant":
                import access_ui
                page = access_ui.begin_grant(
                    ctx, int(params["account_id"]),
                    access_level=str(params.get("level") or "viewer"), persona=persona)
            elif name == "preview_buy":
                page = _v52_quote_page(ctx, persona, plan_code=str(params["plan_code"]), account_id=None)
            elif name == "preview_renew":
                page = _v52_quote_page(ctx, persona, plan_code=str(params["plan_code"]),
                                       account_id=int(params["account_id"]))
            elif name == "show_renewal_menu":
                page = _v52_show_renewal_menu(ctx, persona, int(params["account_id"]))
            elif name == "begin_order":
                page = _v52_begin_order(ctx, persona, str(params["quote_id"]))
            elif name == "begin_addon":
                import checkout_ui
                page = checkout_ui.begin_addon(
                    ctx, int(params["account_id"]), str(params.get("addon_type") or "gib"))
            elif name == "preview_receipt":
                page = _v52_receipt_review_page(ctx, persona, str(params.get("receipt_id") or ""))
            elif name == "prepare_manual_delivery":
                page = _v52_manual_delivery_page(ctx, persona, str(params["order_id"]))
            elif name == "show_operations":
                import operations_ui
                page = operations_ui.show_operations(
                    ctx, _load_panel_operations(ctx, params.get("operation_id")))
            elif name == "preview_refund":
                import operations_ui
                from ui_v52 import render_error
                refund = _load_refund(str(params["refund_id"]))
                if refund is None:
                    page = render_error("درخواست بازگشت وجه یافت نشد.", persona=persona,
                                        parent_callback="v52:ops")
                else:
                    page = operations_ui.preview_refund(ctx, refund)
            elif name == "begin_manual_import":
                page = _v52_begin_manual_import(ctx, int(params["panel_id"]))
            elif name == "begin_digital_import":
                page = _v52_begin_digital_import(ctx, int(params["product_id"]))
            elif name == "confirm_digital_import":
                import inventory_admin_ui
                page = inventory_admin_ui.confirm_digital_import(
                    ctx, str(params["token"]), parent_callback="v52:shop")
            elif name == "cancel_digital_import":
                import inventory_admin_ui
                page = inventory_admin_ui.cancel_digital_import(
                    ctx, str(params["token"]), parent_callback="v52:shop")
            elif name == "confirm_manual_import":
                import inventory_admin_ui
                page = inventory_admin_ui.confirm_manual_import(
                    ctx, str(params["token"]), parent_callback="v52:panels")
            elif name == "cancel_manual_import":
                import inventory_admin_ui
                page = inventory_admin_ui.cancel_manual_import(
                    ctx, str(params["token"]), parent_callback="v52:panels")
            elif name == "resolve_quote_preview":
                page = _v52_confirmation_page(ctx, persona, str(params["nonce"]))
            elif name == "consume_and_dispatch":
                page = _v52_consume_confirmation(ctx, persona, str(params["nonce"]))
            elif name == "cancel_confirmation":
                page = _v52_cancel_confirmation(ctx, persona, str(params["nonce"]))
            elif name == "plan_hub":
                import finance_admin_ui
                page = finance_admin_ui.tariff_hub(ctx)
            elif name == "plan_detail":
                import finance_admin_ui
                page = finance_admin_ui.plan_detail(ctx, str(params["plan_code"]))
            elif name == "plan_categories":
                import finance_admin_ui
                page = finance_admin_ui.plan_categories_page(ctx)
            elif name == "begin_plan_category_new":
                # S59 — the pipe-separated single line is replaced by a wizard that ends
                # on a preview card. ``begin_plan_category_new`` stays available for the
                # legacy text path and the S57 tests.
                import finance_admin_ui
                from ui_v52 import wizard_render
                _wz_state = finance_admin_ui.start_finance_wizard(
                    context, query, finance_admin_ui.PLAN_CATEGORY_WIZARD_KEY,
                    parent_callback="v52:plancats")
                await wizard_render(update, context, _wz_state)
                return True
            elif name == "list_cards":
                # S59 registered ``v52:cards`` in the router but never added the
                # branch, so the button dead-ended in the W18 bridge error page.
                import finance_admin_ui
                page = finance_admin_ui.edit_bank_card(ctx, parent_callback="v52:finance")
            elif name == "begin_bank_card_new":
                # Same gap: ``v52:card:new`` resolved to a handler that did not exist.
                import finance_admin_ui
                from ui_v52 import wizard_render
                _wz_state = finance_admin_ui.start_finance_wizard(
                    context, query, finance_admin_ui.BANK_CARD_WIZARD_KEY,
                    parent_callback="v52:cards")
                await wizard_render(update, context, _wz_state)
                return True
            elif name == "begin_plan_category_edit":
                import finance_admin_ui
                page = finance_admin_ui.begin_plan_category_edit(ctx, str(params["category_key"]))
            elif name == "delete_plan_category":
                import finance_admin_ui
                page = finance_admin_ui.delete_plan_category(ctx, str(params["category_key"]))
            elif name == "toggle_plan_category":
                import finance_admin_ui
                page = finance_admin_ui.toggle_plan_category(
                    ctx, str(params["plan_code"]), str(params["category_key"]))
            elif name == "toggle_plan_panel":
                import finance_admin_ui
                page = finance_admin_ui.toggle_plan_panel(
                    ctx, str(params["plan_code"]), int(params["panel_id"]))
            elif name == "toggle_plan_audience":
                import finance_admin_ui
                page = finance_admin_ui.toggle_plan_audience(
                    ctx, str(params["plan_code"]), str(params["audience"]))
            elif name == "set_plan_kind":
                import finance_admin_ui
                page = finance_admin_ui.apply_plan_kind(
                    ctx, str(params["plan_code"]), str(params["kind"]))
            elif name == "tier_price_matrix":
                import tier_admin_ui
                page = tier_admin_ui.tier_price_matrix(
                    ctx, str(params["actor_type"]), str(params["code"]))
            elif name == "begin_tier_plan_price":
                import tier_admin_ui
                page = tier_admin_ui.begin_tier_plan_price(
                    ctx, str(params["actor_type"]), str(params["code"]), str(params["plan_code"]))
            elif name == "clear_tier_plan_price":
                import tier_admin_ui
                page = tier_admin_ui.clear_tier_plan_price(
                    ctx, str(params["actor_type"]), str(params["code"]), str(params["plan_code"]))
            elif name == "list_coupons":
                import finance_admin_ui
                page = finance_admin_ui.coupons_page(ctx)
            elif name == "begin_coupon_new":
                # S59 — same treatment as the plan-category form above.
                import finance_admin_ui
                from ui_v52 import wizard_render
                _wz_state = finance_admin_ui.start_finance_wizard(
                    context, query, finance_admin_ui.COUPON_WIZARD_KEY,
                    parent_callback="v52:coupons")
                await wizard_render(update, context, _wz_state)
                return True
            elif name == "begin_coupon_edit":
                import finance_admin_ui
                page = finance_admin_ui.begin_coupon_edit(ctx, str(params["code"]))
            elif name == "toggle_coupon":
                import finance_admin_ui
                page = finance_admin_ui.toggle_coupon(ctx, str(params["code"]))
            elif name == "delete_coupon":
                import finance_admin_ui
                page = finance_admin_ui.delete_coupon(ctx, str(params["code"]))
            elif name == "list_refunds":
                import finance_admin_ui
                page = finance_admin_ui.refunds_page(ctx)
            elif name == "preview_order_refund":
                import finance_admin_ui
                page = finance_admin_ui.preview_order_refund(ctx, str(params["order_id"]))
            elif name == "begin_refund_destination":
                import finance_admin_ui
                page = finance_admin_ui.begin_refund_destination(ctx, str(params["order_id"]))
            elif name == "panel_advanced":
                import panel_admin_ui
                page = panel_admin_ui.panel_advanced_settings(ctx, int(params["panel_id"]))
            elif name == "begin_panel_advanced":
                import panel_admin_ui
                page = panel_admin_ui.begin_panel_advanced(
                    ctx, int(params["panel_id"]), str(params["field"]))
            elif name == "edit_plan_all_explicit":
                page = _v52_edit_plan(ctx, persona, str(params["plan_code"]), confirm_all=True)
            elif name == "switch_persona":
                return await _switch_v52_persona(update, context, str(params["persona"]), role, rep)
            else:
                from ui_v52 import render_error
                page = render_error(
                    f"مسیر «{name}» در این نسخه به Bridge متصل نیست؛ عملیات انجام نشد.",
                    persona=persona, parent_callback="v52:home:" + persona)
        except Exception as exc:
            logger.exception("v5.2 bridge dispatch failed for %s", data)
            page = _v52_error_page(exc, persona, data)
    if page is None:
        from ui_v52 import render_error
        page = render_error("callback در registry یا Bridge مجاز W18 شناخته نشد؛ عملیات قدیمی اجرا نشد.",
                            persona=persona, parent_callback="v52:home:" + persona)
    await _send_v52_page(query, page)
    return True


async def _show_legacy_reset_preview(update, context, role, rep=None):
    import account_actions_ui
    from ui_v52 import render_error
    query = update.callback_query
    parts = str(query.data or "").split(":")
    code = parts[-1] if len(parts) > 1 else ""
    persona = "admin" if role == "super_admin" else "rep" if role == "rep" else "user"
    try:
        account = database.get_account_by_code_or_email(code)
        if not account or not account.get("id"):
            raise ValueError("حساب برای پیش‌نمایش پیدا نشد.")
        page = account_actions_ui.preview_rotation(_v52_actor_context(update, context, persona, rep),
                                                   int(account["id"]), "sub", parent_callback="v52:home:" + persona)
    except Exception:
        page = render_error("درخواست قدیمی بازنشانی اجرا نشد؛ شناسه یا مجوز قابل تأیید نیست.",
                            persona=persona, parent_callback="v52:home:" + persona)
    await _send_v52_page(query, page, "پیش‌نمایش؛ هیچ تغییری در پنل انجام نشد.")


async def _submit_v52_order_receipt(update, context, role):
    state, message, user = getattr(context, "user_data", {}) or {}, update.message, update.effective_user
    attempt_id = state.get("v52_payment_attempt_id")
    if not attempt_id or not state.get("v52_payment_pending") or message is None or not message.photo:
        return False
    if role not in {"end_user", "rep"} or database.is_user_blocked(user.id):
        await message.reply_text(
            "⛔️ ثبت رسید برای این نقش/حساب مجاز نیست.",
            reply_markup=build_receipt_navigation_keyboard(role),
        )
        return True
    amount = state.get("v52_payment_amount")
    if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
        await message.reply_text(
            "⚠️ مبلغ ثابت سفارش قابل تأیید نیست؛ درخواست امن متوقف شد.",
            reply_markup=build_receipt_navigation_keyboard(role),
        )
        return True
    try:
        import payment_service
        with database.get_db() as conn:
            result = payment_service.submit_receipt(conn, attempt_id=str(attempt_id),
                actor_tg_id=int(user.id), bot_id=int(context.bot.id), amount_toman=int(amount),
                receipt_photo_id=str(message.photo[-1].file_id), now=int(time.time()),
                media_group_id=message.media_group_id)
    except Exception:
        logger.exception("Order-bound v5.2 receipt submission failed")
        await message.reply_text(
            "❌ رسید سفارش ثبت نشد؛ وضعیت سفارش و درخواست پرداخت محفوظ ماند.",
            reply_markup=build_receipt_navigation_keyboard(role),
        )
        return True
    for key in ("v52_payment_attempt_id", "v52_payment_order_id", "v52_payment_amount", "v52_payment_pending"):
        state.pop(key, None)
    notice = "رسید قبلاً ثبت شده بود؛ ثبت تکراری انجام نشد." if result.get("replayed") else "رسید به سفارش متصل و برای بررسی ثبت شد."
    await message.reply_text(
        f"✅ {notice}\nشناسه رسید: {result.get('receipt_id')}\n"
        f"وضعیت سفارش: {result.get('order_status', 'pending_review')}",
        reply_markup=build_receipt_navigation_keyboard(role),
    )
    try:
        await process_receipt_notification_outbox(context.bot, limit=10)
    except Exception:
        logger.exception("Order receipt notification remains queued")
    return True


async def _route_v52_fsm_text(update, context):
    user, chat, message = update.effective_user, update.effective_chat, update.message
    bot_api = getattr(context, "bot", None)
    if not all((user, chat, getattr(bot_api, "id", None), message)):
        return False
    identity = {"bot_id": int(bot_api.id), "actor_tg_id": int(user.id), "chat_id": int(chat.id)}
    route = v52_router.route_text(identity, message.text or "", now=int(time.time()))
    if route is False:
        return False
    payload = dict(route.params.get("payload") or {})
    role, rep_actor = _persona_aware_role(
        user, chat, getattr(message, "message_thread_id", None),
    )
    persona = "admin" if role == "super_admin" else "rep" if role == "rep" else "user"
    ctx = _v52_actor_context(update, context, persona, rep_actor)
    text_value = str(route.params.get("text") or "").strip()
    try:
        if route.name in {"panel_connection_name", "panel_connection_url", "panel_connection_token"}:
            if persona != "admin" or not ctx.get("is_root") or chat.type != "private":
                raise PermissionError("تنظیم اتصال پنل فقط در پیوی root مجاز است.")
        if route.name == "bot_register_token":
            import bot_admin_ui, bot_registry_service
            if persona != "admin" or not ctx.get("is_root") or chat.type != "private":
                raise PermissionError("ثبت بات فقط در پیوی root مجاز است.")
            is_primary = bool(payload.get("is_primary"))
            rep_id = payload.get("rep_id")
            rep_id = None if rep_id is None else int(rep_id)
            actor_id = int(user.id)

            async def _verify(*, bot_id, token, is_primary, rep_id):
                """Run the real getMe outside the transaction, as the service requires."""
                from telegram import Bot as _TelegramBot

                async def _get_me(value):
                    return await _TelegramBot(token=value).get_me()

                with database.get_db() as conn:
                    return await bot_registry_service.verify_bot_registration(
                        conn, actor_tg_id=actor_id, bot_id=int(bot_id), token=token,
                        get_me=_get_me, now=int(time.time()),
                        rep_id=None if rep_id is None else int(rep_id),
                        is_primary=bool(is_primary))

            page = await bot_admin_ui.begin_registration_from_text(
                ctx, text_value, verify=_verify, rep_id=rep_id, is_primary=is_primary,
                chat_type=chat.type, parent_callback="v52:bots")
            # The answer carried a live token; it must not linger in the chat history.
            try:
                await message.delete()
            except Exception:
                pass
            await message.reply_text(page.text, reply_markup=page.reply_markup,
                                     parse_mode="HTML")
            return True
        if route.name == "panel_connection_name":
            import panel_admin_ui
            pid = int(payload["panel_id"])
            display_name = text_value
            if not display_name or len(display_name) > 100:
                v52_router.DEFAULT_FSM.set(
                    ctx, "panel_connection_name", payload={"panel_id": pid},
                    parent_callback=f"v52:panelconfig:{pid}", now=int(time.time()),
                )
                page = panel_admin_ui.render_panel_name_step(
                    ctx, pid, error="نام پنل باید بین ۱ تا ۱۰۰ نویسه باشد.")
            else:
                with database.get_db() as conn:
                    conn.execute("UPDATE panels SET name=?, revision=revision+1 WHERE id=?",
                                 (display_name, pid))
                    conn.commit()
                v52_router.DEFAULT_FSM.set(
                    ctx, "panel_connection_url", payload={"panel_id": pid},
                    parent_callback=f"v52:panelname:{pid}", now=int(time.time()),
                )
                page = panel_admin_ui.render_panel_url_step(ctx, pid)
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        if route.name == "panel_connection_url":
            import panel_admin_ui
            from panel_adapters.url_parser import parse_panel_url
            pid = int(payload["panel_id"])
            try:
                endpoint = parse_panel_url(text_value)
            except ValueError:
                v52_router.DEFAULT_FSM.set(
                    ctx, "panel_connection_url", payload={"panel_id": pid},
                    parent_callback=f"v52:panelname:{pid}", now=int(time.time()),
                )
                page = panel_admin_ui.render_panel_url_step(
                    ctx, pid,
                    error="نشانی نامعتبر است؛ یک نشانی کامل http یا https بدون query/نام کاربری بفرستید.")
                await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
                return True
            v52_router.DEFAULT_FSM.set(
                ctx, "panel_connection_token",
                payload={"panel_id": pid, "normalized_url": endpoint.normalized_url},
                parent_callback=f"v52:panelurl:{pid}", now=int(time.time()),
            )
            page = panel_admin_ui.render_panel_credential_step(ctx, pid)
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        if route.name == "panel_connection_token":
            import panel_admin_ui
            try:
                await message.delete()
            except Exception:
                logger.debug("Could not delete the private panel credential message")
            pid = int(payload["panel_id"])
            normalized_url = str(payload.get("normalized_url") or "")
            try:
                database.update_panel_connection(pid, normalized_url, text_value)
            except ValueError as exc:
                # A malformed credential is a correctable step, not a dead end:
                # re-render step 4 with the concrete reason and keep the flow armed.
                v52_router.DEFAULT_FSM.set(
                    ctx, "panel_connection_token",
                    payload={"panel_id": pid, "normalized_url": normalized_url},
                    parent_callback=f"v52:panelurl:{pid}", now=int(time.time()),
                )
                page = panel_admin_ui.render_panel_credential_step(
                    ctx, pid, error=str(exc) or "اعتبارنامه نامعتبر است.")
                await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
                return True
            # Step 5: subscription domain (after credential, before probe)
            v52_router.DEFAULT_FSM.set(
                ctx, "panel_connection_suburl",
                payload={"panel_id": pid},
                parent_callback=f"v52:panelcred:{pid}", now=int(time.time()),
            )
            page = panel_admin_ui.render_panel_suburl_step(ctx, pid)
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        if route.name == "panel_connection_suburl":
            import panel_admin_ui
            pid = int(payload["panel_id"])
            raw = text_value.strip()
            if raw.lower() in ("/skip", "لغو", "skip", "-"):
                # Skip suburl — go straight to probe
                pass
            else:
                from panel_adapters.url_parser import urlsplit
                try:
                    parsed = urlsplit(raw)
                except Exception:
                    v52_router.DEFAULT_FSM.set(
                        ctx, "panel_connection_suburl", payload={"panel_id": pid},
                        parent_callback=f"v52:panelcred:{pid}", now=int(time.time()))
                    page = panel_admin_ui.render_panel_suburl_step(
                        ctx, pid, error="نشانی نامعتبر است. یک URL معتبر با https بفرستید یا /skip را بزنید.")
                    await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
                    return True
                if parsed.scheme and parsed.scheme != "https" and parsed.scheme != "http":
                    v52_router.DEFAULT_FSM.set(
                        ctx, "panel_connection_suburl", payload={"panel_id": pid},
                        parent_callback=f"v52:panelcred:{pid}", now=int(time.time()))
                    page = panel_admin_ui.render_panel_suburl_step(
                        ctx, pid, error="فقط نشانی https یا http معتبر است.")
                    await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
                    return True
                if not parsed.hostname:
                    v52_router.DEFAULT_FSM.set(
                        ctx, "panel_connection_suburl", payload={"panel_id": pid},
                        parent_callback=f"v52:panelcred:{pid}", now=int(time.time()))
                    page = panel_admin_ui.render_panel_suburl_step(
                        ctx, pid, error="نشانی باید شامل نام میزبان باشد.")
                    await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
                    return True
                with database.get_db() as conn:
                    conn.execute("UPDATE panels SET sub_base_url=? WHERE id=?",
                                 (raw, pid))
                    conn.commit()
            # After suburl (or skip), run the connection probe
            with database.get_db() as conn:
                type_row = conn.execute("SELECT panel_type FROM panels WHERE id=?", (pid,)).fetchone()
            panel_type = str(type_row["panel_type"]) if type_row is not None else ""
            reachable, reason = await _v52_probe_panel_connection(pid)
            if reachable is None:
                page = panel_admin_ui.render_panel_connection_saved(ctx, pid, panel_type=panel_type)
            else:
                page = panel_admin_ui.render_panel_connection_result(
                    ctx, pid, panel_type=panel_type, reachable=reachable, reason=reason)
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        if route.name == "domain_hostname":
            import domain_service
            import domain_ui
            rep_id = int(payload.get("rep_id") or 0)
            if rep_id <= 0:
                raise ValueError("نمایندهٔ مالک مشخص نشده است؛ از ابتدا شروع کنید.")
            hostname = domain_service.normalize_hostname(text_value)
            _v52_set_domain_draft(context, {"rep_id": rep_id, "hostname": hostname})
            page = domain_ui.register_role_step(ctx, persona=persona, parent_callback="v52:domains")
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        if route.name == "support_ticket_body":
            import support_ui
            if payload.get("kind") == "abuse":
                page = support_ui.begin_abuse(ctx, text_value, chat_type=chat.type, parent_callback="v52:tickets")
            else:
                page = support_ui.begin_ticket(
                    ctx, int(payload.get("account_id", 0)), body=text_value,
                    ticket_type=str(payload.get("ticket_type", "general")),
                    submit=True, parent_callback="v52:tickets",
                )
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        if route.name == "support_reply":
            import support_service
            result = support_service.reply_ticket(
                ctx, str(payload["ticket_id"]), text_value,
                visibility="customer", now=int(time.time()),
            )
            await message.reply_text(
                f"✅ پاسخ ثبت شد. شناسه پیام: {result['message_id']} · نمایش: {result['visibility']}",
                reply_markup=build_v52_fsm_error_keyboard(
                    persona, f"v52:ticket:{payload['ticket_id']}"),
            )
            return True
        if route.name == "ownership_transfer":
            import ownership_service
            from ui_v52 import render_page, button
            target = text_value.strip()
            if not target.isdigit() or int(target) <= 0:
                raise ValueError("شناسه تلگرام مالک جدید باید یک عدد مثبت باشد.")
            account_id = int(payload.get("account_id", 0))
            result = ownership_service.preview_transfer(
                ctx, account_id, int(target), reason="v52 root transfer",
                bot_id=ctx.get("bot_id"),
            )
            preview = render_page(
                "پیش‌نمایش انتقال مالکیت",
                f"اکانت {result['account_id']} → مالک {int(target)}\n"
                f"مالک فعلی: {result['before'].get('owner_tg_id')}\n"
                f"انقضا: {result['expires_at']}",
                persona=persona, page_kind="preview",
                parent_callback=f"v52:sub:{account_id}",
                rows=[[button("تأیید انتقال", result["callback_ok"]),
                       button("لغو", result["callback_no"])]],
            )
            await message.reply_text(preview.text, reply_markup=preview.reply_markup, parse_mode="HTML")
            return True
        if route.name == "goods_manual_secret":
            import goods_ui
            order = _load_v52_order(str(payload.get("order_id") or ""))
            if order is None:
                raise ValueError("سفارش موردنظر یافت نشد.")
            v52_router.DEFAULT_FSM.set(
                ctx, "goods_manual_secret",
                payload={"order_id": str(payload.get("order_id")), "secret": text_value},
                parent_callback="v52:shop", now=int(time.time()))
            page = goods_ui.prepare_manual_delivery(ctx, order, confirm=False)
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        if route.name == "inventory_manual_batch":
            import inventory_admin_ui
            parts = [chunk.strip() for chunk in text_value.split("|") if chunk.strip()]
            if len(parts) < 2:
                raise ValueError("قالب نامعتبر است؛ «identifier | uri1 | uri2» را ارسال کنید.")
            page = inventory_admin_ui.begin_manual_import(
                ctx, int(payload.get("panel_id", 0)), parts[0], parts[1:],
                parent_callback="v52:panels")
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        if route.name == "inventory_digital_batch":
            import inventory_admin_ui
            # The raw text goes to the parser rather than a pre-split list, so the
            # --- / ### separators can group multi-line items into one record. The
            # mode the operator picked in the chooser is honoured; ``auto`` is the
            # fallback for the bare route.
            page = inventory_admin_ui.begin_digital_import(
                ctx, int(payload.get("product_id", 0)), text_value,
                mode=str(payload.get("mode") or "auto"),
                parent_callback="v52:shop")
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        if route.name == "goods_product_draft":
            import goods_ui
            from ui_v52 import render_page
            text, rows = goods_ui.build_product_wizard_step(
                ctx, payload, value=text_value,
                parent_callback=str(route.params.get("parent_callback") or "v52:shop"))
            page = render_page("➕ افزودن محصول", text, persona="admin", rows=rows)
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        if route.name == "goods_product_price":
            import goods_ui
            from ui_v52 import render_page
            text, rows = goods_ui.apply_product_price(
                ctx, payload, text_value,
                parent_callback=str(route.params.get("parent_callback") or "v52:shop"))
            page = render_page("💵 تغییر قیمت", text, persona="admin", rows=rows)
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        if route.name == "goods_product_guide":
            import goods_ui
            from ui_v52 import render_page
            text, rows = goods_ui.apply_product_guide(
                ctx, payload, text_value,
                parent_callback=str(route.params.get("parent_callback") or "v52:shop"))
            page = render_page("📘 راهنمای استفاده محصول", text, persona="admin", rows=rows)
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        if route.name == "ticket_status_new":
            import ticket_status_ui
            page = ticket_status_ui.apply_ticket_status_new(
                ctx, text_value,
                parent_callback=str(route.params.get("parent_callback") or "v52:ticketsettings"))
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        if route.name == "ticket_status_edit":
            import ticket_status_ui
            page = ticket_status_ui.apply_ticket_status_edit(
                ctx, payload, text_value,
                parent_callback=str(route.params.get("parent_callback") or "v52:ticketsettings"))
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        if route.name == "tier_new":
            import tier_admin_ui
            page = tier_admin_ui.apply_tier_new(
                ctx, text_value,
                parent_callback=str(route.params.get("parent_callback") or "v52:tiers"))
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        if route.name == "tier_edit":
            import tier_admin_ui
            page = tier_admin_ui.apply_tier_edit(
                ctx, payload, text_value,
                parent_callback=str(route.params.get("parent_callback") or "v52:tiers"))
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        if route.name == "tier_plan_price":
            import tier_admin_ui
            page = tier_admin_ui.apply_tier_plan_price(
                ctx, payload, text_value,
                parent_callback=str(route.params.get("parent_callback") or "v52:tiers"))
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        if route.name == "plan_category_new":
            import finance_admin_ui
            page = finance_admin_ui.apply_plan_category_new(
                ctx, text_value,
                parent_callback=str(route.params.get("parent_callback") or "v52:plancats"))
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        if route.name == "plan_category_edit":
            import finance_admin_ui
            page = finance_admin_ui.apply_plan_category_edit(
                ctx, payload, text_value,
                parent_callback=str(route.params.get("parent_callback") or "v52:plancats"))
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        if route.name == "coupon_new":
            import finance_admin_ui
            page = finance_admin_ui.apply_coupon_new(
                ctx, text_value,
                parent_callback=str(route.params.get("parent_callback") or "v52:coupons"))
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        if route.name == "coupon_edit":
            import finance_admin_ui
            page = finance_admin_ui.apply_coupon_edit(
                ctx, payload, text_value,
                parent_callback=str(route.params.get("parent_callback") or "v52:coupons"))
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        if route.name == "refund_destination":
            import finance_admin_ui
            page = finance_admin_ui.apply_refund_destination(
                ctx, payload, text_value,
                parent_callback=str(route.params.get("parent_callback") or "v52:refunds"))
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        if route.name == "panel_advanced":
            import panel_admin_ui
            page = panel_admin_ui.apply_panel_advanced(
                ctx, payload, text_value,
                parent_callback=str(route.params.get("parent_callback") or "v52:panels"))
            await message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
            return True
        await message.reply_text(
            "متن draft دریافت شد؛ مسیر قدیمی NLP آن را دوباره مصرف نمی‌کند.",
            reply_markup=build_v52_fsm_error_keyboard(
                persona, route.params.get("parent_callback")),
        )
    except Exception as exc:
        logger.exception("v5.2 text workflow failed for state=%s", route.name)
        await message.reply_text(
            f"❌ عملیات v5.2 انجام نشد: {exc}",
            reply_markup=build_v52_fsm_error_keyboard(
                persona, route.params.get("parent_callback")),
        )
    return True

async def _route_v52_fsm_photo(update, context):
    user, chat, bot, message = update.effective_user, update.effective_chat, getattr(context, "bot", None), update.message
    if not all((user, chat, getattr(bot, "id", None), message, message.photo)):
        return False
    route = v52_router.route_photo({"bot_id": int(bot.id), "actor_tg_id": int(user.id), "chat_id": int(chat.id)},
                                   str(message.photo[-1].file_id), now=int(time.time()))
    if route is False:
        return False
    role, _rep_actor = _persona_aware_role(user, chat, getattr(message, "message_thread_id", None))
    await message.reply_text(
        "تصویر به draft پشتیبانی v5.2 متصل شد؛ مسیر رسید legacy اجرا نشد.",
        reply_markup=build_v52_fsm_error_keyboard(
            _persona_for_role(role), route.params.get("parent_callback")),
    )
    return True

async def link_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    thread_id = update.message.message_thread_id if update.message else None
    role, rep = _persona_aware_role(user, chat, thread_id)
    if role != "end_user":
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
    import account_link_service
    link_context = {"actor_tg_id": user.id, "telegram_id": user.id}
    candidates = account_link_service.resolve_link_candidates(link_context, code)
    if candidates.get("status") == "ambiguous":
        import access_ui
        page = access_ui.show_link_candidates(link_context, code, persona="user", parent_callback="user:main")
        await update.message.reply_text(page.text, reply_markup=page.reply_markup, parse_mode="HTML")
        return
    if candidates.get("status") != "resolved" or len(candidates.get("account_ids", [])) != 1:
        await update.message.reply_text(f"❌ حساب مجاز و یکتایی با کد `{code}` پیدا نشد.", parse_mode=ParseMode.MARKDOWN)
        return
    result = account_link_service.claim_or_access(link_context, int(candidates["account_ids"][0]))
    if result.get("status") in {"claimed", "owner", "grant_access", "root_access"}:
        await update.message.reply_text(f"✅ دسترسی حساب `{code}` برای این حساب تلگرام تأیید شد.\nاکنون می‌توانید سرویس را از پورتال مشاهده کنید.",
                                        parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text("⛔️ این حساب قابل اتصال یا مشاهده نیست.", parse_mode=ParseMode.MARKDOWN)

async def pricing_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    thread_id = update.message.message_thread_id if update.message else None
    role, rep = _persona_aware_role(user, chat, thread_id)
    if role == "unauthorized_group" or (role == "super_admin" and chat.type != "private"):
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
    if not _is_private_super_admin(update):
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
    role, rep = _persona_aware_role(user, chat, thread_id)
    if role not in ("super_admin", "rep"):
        return

    # Global financial aggregates are private-chat-only.
    if role == "super_admin" and rep is None and chat.type != "private":
        return

    # If Super Admin in private DM (without specific rep context), show aggregated summary of all reps
    if role == "super_admin" and rep is None and chat.type != "private":
        return
    if role not in ("super_admin", "rep") or (role == "rep" and not rep):
        return

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
    if not _is_private_super_admin(update):
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
    role, rep = _persona_aware_role(user, chat, thread_id)
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
    role, rep = _persona_aware_role(user, chat, thread_id)
    if role not in ("end_user", "rep") or (role == "rep" and not rep):
        await update.message.reply_text(
            "ℹ️ ثبت فیش و شارژ کیف پول فقط از پورتال کاربری یا میزکار نمایندگی انجام می‌شود.",
            reply_markup=build_actor_navigation_keyboard(role),
        )
        return

    args = context.args or []
    amount = 0
    if args and args[0].isdigit():
        amount = int(args[0])

    await _cleanup_deposit_amount_prompt(context, chat.id)
    if amount <= 0:
        context.user_data["fsm_state"] = finance_manager.STATE_AWAITING_RECEIPT_AMOUNT
        context.user_data["deposit_role"] = "rep" if role == "rep" else "end_user"
        prompt_message = await update.message.reply_text(
            "💳 **لطفاً مبلغ واریزی مورد نظر را به تومان وارد کنید:**\n"
            "مثال: `/deposit 500000` یا ارسال عدد `500000`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_finance_fsm_keyboard(role),
        )
        context.user_data["deposit_form_state"] = {
            "prompt_msg_id": getattr(prompt_message, "message_id", None)
        }
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
    if not _is_private_super_admin(update):
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

async def operations_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show persistent or unexpired external-operation locks to the Super Admin."""
    user = update.effective_user
    if not _is_private_super_admin(update):
        return
    locks = await asyncio.to_thread(database.get_operation_locks)
    if not locks:
        await update.message.reply_text("✅ هیچ عملیات معلق یا قفل فعالی برای تطبیق وجود ندارد.")
        return
    lines = [
        "⚠️ **عملیات‌های قفل‌شده / نیازمند تطبیق**",
        "این قفل‌ها مانع اجرای دوباره عملیات خارجی می‌شوند. پیش از آزادسازی، وضعیت دقیق اکانت را در 3X-UI بررسی کنید.",
        ""
    ]
    for item in locks[:30]:
        lines.append(
            f"• `{item['lock_key']}`\n"
            f"  ایجاد: `{item['created_at']}` | انقضا: `{item['expires_at']}`"
        )
        if item.get("intent_id"):
            owner_type = item.get("intent_owner_type")
            action_type = item.get("action_type")
            if action_type == "reservation_activation":
                owner_label = "نماینده" if owner_type == "representative" else "مدیریت کل"
                command = "/resolvereservation"
            elif action_type == "admin_renewal":
                owner_label, command = "مدیریت کل", "/resolveadminrenewal"
            elif owner_type == "representative" and action_type == "renew":
                owner_label, command = "نماینده", "/resolvereprenewal"
            elif owner_type == "representative":
                owner_label, command = "نماینده", "/resolverepintent"
            elif owner_type == "end_user":
                owner_label, command = "کاربر", "/resolveintent"
            else:
                owner_label, command = "نامشخص", "/unlock"
            if action_type == "reservation_activation":
                lines.append(
                    f"  ⚠️ رزرو: `{item.get('reservation_id')}` | {owner_label} | "
                    f"Actor Telegram ID: `{item.get('user_id')}` | وضعیت: {item.get('intent_status')}"
                )
                lines.append(
                    f"  تعیین تکلیف پس از تطبیق X-UI: {command} <reservation-id> APPLIED: ... "
                    f"یا {command} <reservation-id> NOT_APPLIED: ..."
                )
            else:
                lines.append(
                    f"  ⚠️ Intent: `{item['intent_id']}` | نوع: {action_type} "
                    f"| {owner_label} Telegram ID: `{item.get('user_id')}` | وضعیت: {item.get('intent_status')}"
                )
                lines.append(
                    f"  تعیین تکلیف مالی: {command} <intent-id> APPLIED: ... "
                    f"یا {command} <intent-id> REFUND_NOT_APPLIED: ..."
                )
    lines.append(
        "\nقفل‌های عادی را فقط پس از بررسی پنل و دیتابیس با /unlock حل کنید. "
        "قفل‌های Intent کاربر با /resolveintent، صدور نماینده با /resolverepintent، تمدید نماینده با /resolvereprenewal، تمدید مستقیم مدیریت کل با /resolveadminrenewal و فعال‌سازی رزرو با /resolvereservation تعیین تکلیف می‌شوند."
    )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def unlock_operation_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Release an unresolved-operation lock only with explicit admin evidence."""
    user = update.effective_user
    if not _is_private_super_admin(update):
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "فرمت دستور:\n"
            "`/unlock <operation-key> VERIFIED_NOT_APPLIED: توضیح بررسی پنل`\n"
            "یا\n"
            "`/unlock <operation-key> RECONCILED: توضیح اصلاح وضعیت محلی`\n\n"
            "پیش از این دستور حتماً وضعیت همان اکانت را در 3X-UI و دیتابیس تطبیق دهید."
        )
        return
    lock_key = args[0].strip()
    evidence = " ".join(args[1:]).strip()
    if not (
        evidence.startswith("VERIFIED_NOT_APPLIED:")
        or evidence.startswith("RECONCILED:")
    ):
        await update.message.reply_text(
            "⛔️ مدرک باید با VERIFIED_NOT_APPLIED: یا RECONCILED: شروع شود."
        )
        return
    try:
        resolved = await asyncio.to_thread(
            database.resolve_operation_lock,
            lock_key, user.id, user.full_name or "Super Admin", evidence
        )
    except ValueError as exc:
        await update.message.reply_text(f"❌ آزادسازی انجام نشد: {exc}")
        return
    if resolved:
        await update.message.reply_text(
            f"✅ قفل `{lock_key}` با ثبت مدرک و هویت مدیر آزاد شد.\n"
            "این دستور هیچ تغییری در 3X-UI، موجودی یا وضعیت رزرو ایجاد نمی‌کند."
        )
    else:
        await update.message.reply_text("ℹ️ چنین قفل فعالی پیدا نشد.")


async def resolve_user_intent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Evidence-gated resolution of a user purchase/renewal with atomic wallet semantics."""
    user = update.effective_user
    if not _is_private_super_admin(update):
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "فرمت دستور (فقط پس از بررسی مستقیم اکانت و دیتابیس):\n"
            "`/resolveintent <intent-id> APPLIED: مدرک انجام عملیات در 3X-UI`\n"
            "یا\n"
            "`/resolveintent <intent-id> REFUND_NOT_APPLIED: مدرک عدم انجام عملیات در 3X-UI`\n\n"
            "APPLIED قفل را آزاد و Intent را موفق ثبت می‌کند؛ REFUND_NOT_APPLIED مبلغ را به‌صورت اتمیک مسترد و Intent را refunded می‌کند."
        )
        return

    intent_id = args[0].strip()
    evidence = " ".join(args[1:]).strip()
    if evidence.startswith("APPLIED:"):
        resolution = "APPLIED"
    elif evidence.startswith("REFUND_NOT_APPLIED:"):
        resolution = "REFUND_NOT_APPLIED"
    else:
        await update.message.reply_text(
            "⛔️ مدرک باید با APPLIED: یا REFUND_NOT_APPLIED: شروع شود."
        )
        return

    try:
        result = await asyncio.to_thread(
            database.resolve_user_action_intent,
            intent_id,
            user.id,
            user.full_name or "Super Admin",
            resolution,
            evidence,
        )
    except ValueError as exc:
        await update.message.reply_text(f"❌ تعیین تکلیف انجام نشد: {exc}")
        return

    if result["status"] == "succeeded":
        await update.message.reply_text(
            f"✅ Intent `{intent_id}` به‌عنوان انجام‌شده ثبت شد و قفل `{result['lock_key']}` آزاد شد.\n"
            "برداشت مالی قبلی حفظ شد؛ هیچ درخواست خارجی جدیدی ارسال نشد."
        )
    elif result["status"] == "refunded":
        await update.message.reply_text(
            f"✅ Intent `{intent_id}` مسترد شد؛ مبلغ {result['amount']:,} تومان به کیف پول کاربر "
            f"`{result['user_id']}` بازگشت و موجودی جدید {result['new_balance']:,} تومان است.\n"
            f"قفل `{result['lock_key']}` نیز در همان تراکنش آزاد شد."
        )


async def resolve_rep_intent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Evidence-gated resolution of a representative issuance precharge."""
    user = update.effective_user
    if not _is_private_super_admin(update):
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "فرمت دستور (پس از تطبیق مستقیم اکانت در 3X-UI و دیتابیس):\n"
            "`/resolverepintent <intent-id> APPLIED: مدرک وجود اکانت و ثبت محلی`\n"
            "یا\n"
            "`/resolverepintent <intent-id> REFUND_NOT_APPLIED: مدرک عدم صدور اکانت`\n\n"
            "APPLIED پیش‌پرداخت را حفظ و Intent را موفق ثبت می‌کند؛ "
            "REFUND_NOT_APPLIED پیش‌پرداخت را به‌صورت اتمیک به تراز نماینده برمی‌گرداند."
        )
        return

    intent_id = args[0].strip()
    evidence = " ".join(args[1:]).strip()
    if evidence.startswith("APPLIED:"):
        resolution = "APPLIED"
    elif evidence.startswith("REFUND_NOT_APPLIED:"):
        resolution = "REFUND_NOT_APPLIED"
    else:
        await update.message.reply_text("⛔️ مدرک باید با APPLIED: یا REFUND_NOT_APPLIED: شروع شود.")
        return

    try:
        result = await asyncio.to_thread(
            database.resolve_rep_action_intent,
            intent_id,
            user.id,
            user.full_name or "Super Admin",
            resolution,
            evidence,
        )
    except ValueError as exc:
        await update.message.reply_text(f"❌ تعیین تکلیف انجام نشد: {exc}")
        return

    if result["status"] == "succeeded":
        await update.message.reply_text(
            f"✅ Intent نماینده `{intent_id}` به‌عنوان انجام‌شده ثبت شد؛ "
            f"قفل `{result['lock_key']}` آزاد شد. پیش‌پرداخت قبلی حفظ شد و درخواست خارجی جدیدی ارسال نشد."
        )
    else:
        await update.message.reply_text(
            f"✅ پیش‌پرداخت Intent `{intent_id}` مسترد شد؛ "
            f"{result['amount']:,} تومان به تراز نماینده {result['rep_id']} برگشت. "
            f"مانده جدید: {result['new_balance']:,} تومان؛ قفل نیز آزاد شد."
        )


async def resolve_admin_renewal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Evidence-gated resolution of an ambiguous Super Admin quick renewal."""
    user = update.effective_user
    if not _is_private_super_admin(update):
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "فرمت دستور (پس از تطبیق مستقیم اکانت در 3X-UI و دیتابیس):\n"
            "`/resolveadminrenewal <confirmation-id> APPLIED: مدرک تمدید موفق`\n"
            "یا\n"
            "`/resolveadminrenewal <confirmation-id> NOT_APPLIED: مدرک عدم اعمال تمدید`\n\n"
            "این دستور فقط وضعیت تأیید و قفل را تعیین تکلیف می‌کند و هیچ تغییر مالی ایجاد نمی‌کند."
        )
        return
    confirmation_id = args[0].strip()
    evidence = " ".join(args[1:]).strip()
    if evidence.startswith("APPLIED:"):
        resolution = "APPLIED"
    elif evidence.startswith("NOT_APPLIED:"):
        resolution = "NOT_APPLIED"
    else:
        await update.message.reply_text("⛔️ مدرک باید با APPLIED: یا NOT_APPLIED: شروع شود.")
        return
    try:
        result = await asyncio.to_thread(
            database.resolve_admin_renewal_confirmation,
            confirmation_id, user.id, user.full_name or "Super Admin", resolution, evidence,
        )
    except ValueError as exc:
        await update.message.reply_text(f"❌ تعیین تکلیف تمدید مدیریت کل انجام نشد: {exc}")
        return
    state_text = "موفق" if result["status"] == "succeeded" else "ناموفق و عدم اعمال تمدید"
    await update.message.reply_text(
        f"✅ تأیید تمدید `{confirmation_id}` با مدرک ثبت شد: {state_text}. "
        f"قفل `{result['lock_key']}` آزاد شد؛ عملیات خارجی جدیدی ارسال نشد."
    )


async def resolve_reservation_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Evidence-gated resolution of an ambiguous reserved-package activation."""
    user = update.effective_user
    if not _is_private_super_admin(update):
        return
    args = context.args or []
    if len(args) < 2 or not args[0].isdigit():
        await update.message.reply_text(
            "فرمت دستور (پس از تطبیق وضعیت همان اکانت در 3X-UI):\n"
            "`/resolvereservation <reservation-id> APPLIED: مدرک فعال شدن بسته`\n"
            "یا\n"
            "`/resolvereservation <reservation-id> NOT_APPLIED: مدرک عدم اعمال تمدید`\n\n"
            "APPLIED رزرو را فعال و قفل را آزاد می‌کند. NOT_APPLIED فقط قفل را آزاد می‌کند و رزرو در صف باقی می‌ماند؛ در صورت نیاز می‌توانید بعداً آن را لغو و وجه را مسترد کنید."
        )
        return

    res_id = int(args[0])
    evidence = " ".join(args[1:]).strip()
    if evidence.startswith("APPLIED:"):
        resolution = "APPLIED"
    elif evidence.startswith("NOT_APPLIED:"):
        resolution = "NOT_APPLIED"
    else:
        await update.message.reply_text("⛔️ مدرک باید با APPLIED: یا NOT_APPLIED: شروع شود.")
        return

    try:
        result = await asyncio.to_thread(
            database.resolve_reservation_activation,
            res_id, user.id, user.full_name or "Super Admin", resolution, evidence,
        )
    except ValueError as exc:
        await update.message.reply_text(f"❌ تعیین تکلیف رزرو انجام نشد: {exc}")
        return

    if result["status"] == "activated":
        await update.message.reply_text(
            f"✅ رزرو `{res_id}` به‌عنوان فعال‌شده ثبت شد؛ قفل `{result['lock_key']}` آزاد شد. پیش‌پرداخت قبلی بدون تغییر باقی ماند."
        )
    else:
        await update.message.reply_text(
            f"✅ مدرک عدم اعمال تمدید برای رزرو `{res_id}` ثبت شد؛ قفل آزاد شد و رزرو همچنان در صف باقی ماند. برای لغو و استرداد از دکمه لغو رزرو استفاده کنید."
        )


async def resolve_rep_renewal_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Evidence-gated resolution of an uncertain representative renewal precharge."""
    user = update.effective_user
    if not _is_private_super_admin(update):
        return
    args = context.args or []
    if len(args) < 2:
        await update.message.reply_text(
            "فرمت دستور (پس از تطبیق مستقیم اکانت در 3X-UI و دیتابیس):\n"
            "`/resolvereprenewal <intent-id> APPLIED: مدرک تمدید موفق`\n"
            "یا\n"
            "`/resolvereprenewal <intent-id> REFUND_NOT_APPLIED: مدرک عدم اعمال تمدید`\n\n"
            "APPLIED پیش‌پرداخت را حفظ می‌کند؛ REFUND_NOT_APPLIED اعتبار نماینده را به‌صورت اتمیک مسترد می‌کند. هر دو مسیر قفل دقیق عملیات را آزاد می‌کنند."
        )
        return

    intent_id = args[0].strip()
    evidence = " ".join(args[1:]).strip()
    if evidence.startswith("APPLIED:"):
        resolution = "APPLIED"
    elif evidence.startswith("REFUND_NOT_APPLIED:"):
        resolution = "REFUND_NOT_APPLIED"
    else:
        await update.message.reply_text("⛔️ مدرک باید با APPLIED: یا REFUND_NOT_APPLIED: شروع شود.")
        return

    try:
        result = await asyncio.to_thread(
            database.resolve_rep_renewal_intent,
            intent_id, user.id, user.full_name or "Super Admin", resolution, evidence,
        )
    except ValueError as exc:
        await update.message.reply_text(f"❌ تعیین تکلیف تمدید انجام نشد: {exc}")
        return

    if result["status"] == "succeeded":
        await update.message.reply_text(
            f"✅ تمدید نماینده `{intent_id}` به‌عنوان انجام‌شده ثبت شد؛ پیش‌پرداخت حفظ و قفل `{result['lock_key']}` آزاد شد. درخواست خارجی جدیدی ارسال نشد."
        )
    else:
        await update.message.reply_text(
            f"✅ پیش‌پرداخت تمدید `{intent_id}` به مبلغ {result['amount']:,} تومان مسترد شد. "
            f"نماینده: {result['rep_id']} | مانده جدید: {result['new_balance']:,} تومان | قفل آزاد شد."
        )


async def sync_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_private_super_admin(update):
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
    role, rep = _persona_aware_role(user, chat, thread_id)
    if role == "unauthorized_group":
        return
    if role == "super_admin" and rep is None and chat.type != "private":
        return
    if role not in ("super_admin", "rep") or (role == "rep" and not rep):
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
    user = update.effective_user
    _reset_all_conversation_states(getattr(user, "id", 0), _bot_id_of(context), context)
    role, _rep = _persona_aware_role(user, update.effective_chat, None)
    await update.message.reply_text(
        "✅ تمام وضعیت‌های تعاملی و فرم‌های فعال لغو شدند.",
        reply_markup=build_actor_navigation_keyboard(role),
    )

# --- Renewal & Reserve Queue Logic ---


def _runtime_flag(name: str) -> bool:
    try:
        value = database.get_setting(name, default="0")
    except Exception:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


# Conversation state lives in two independent stores: the per-actor
# ``context.user_data`` drafts and the TTL-bound ``v52_router.DEFAULT_FSM``. A
# stale entry in either one consumes the next text message, so a ticket draft
# would swallow the amount the customer types into the wallet top-up. They are
# therefore always cleared together, never one at a time.
CONVERSATION_STATE_KEYS = (
    "user_state", "admin_state", "rep_state", "fsm_state",
    "deposit_role", "deposit_amount", "deposit_form_state",
    "active_receipt_code", "transfer_confirm", "referral_pending",
    # The step-by-step wizard is a conversation state like any other: leaving it
    # live would let it swallow the next message of whatever workflow the actor
    # started instead.
    ui_v52.WIZARD_STATE_KEY,
)

# Callbacks that mean "I am leaving whatever I was doing": every main-menu entry,
# the persona switcher and the home/back rows of any page.
_NAVIGATION_CALLBACKS = frozenset({
    "user:main", "user:services", "user:wallet", "user:pricing", "user:receipts",
    "user:pending", "user:prompt_link", "admin:main", "admin:settings",
    "rep:main", "rep:menu", "v52:shop",
})
_NAVIGATION_CALLBACK_PREFIXES = (
    "v52:home:", "v52:back:", "v52:persona:", "v52:tickets",
    "v52:profiles", "v52:finance", "v52:orders", "v52:panels", "v52:bots",
    "v52:domains", "v52:cards", "v52:plans", "v52:ops", "v52:abuse:list",
)

# Callbacks that start a fresh workflow: any pending draft from another module
# must be dropped before this one begins collecting input.
_WORKFLOW_START_PREFIXES = (
    "user:deposit_start", "user:buy_service", "user:do_buy", "user:do_renew",
    "user:renew_plans", "rep:deposit_start", "rep:issue_acc", "rep:renew",
    "v52:newticket", "v52:abuse", "v52:support:",
    "admin:prompt_", "rep:prompt_", "v52:panelnew", "v52:panelconfig:",
    "v52:domain:", "v52:domainnew", "v52:bot:", "v52:inventory:", "v52:poolimport:",
    "v52:shop:new",
)


async def _notify_registration_owner(update, context, user_row, rep_id) -> None:
    """Send the approval card to the tenant owner: the rep of this bot, else root.

    A representative's own bot must never route a pending approval into the root
    queue, so the destination is derived from the bot/rep scope, not from a
    global admin id.
    """
    import registration_ui
    try:
        target_chat = None
        if rep_id:
            rep_info = database.get_rep_by_id(int(rep_id))
            if rep_info and rep_info.get("telegram_id"):
                target_chat = int(rep_info["telegram_id"])
        if target_chat is None:
            target_chat = int(database.get_setting(
                "admin_telegram_id", default=ADMIN_TELEGRAM_ID, cast_type=int) or 0)
        if not target_chat:
            # A stored 0 means "not configured", not "send nowhere": fall back to
            # the deployment's configured root so the queue can never be silent.
            target_chat = int(ADMIN_TELEGRAM_ID or 0)
        if not target_chat:
            return
        if rep_id:
            text, markup = registration_ui.build_rep_approval_card(user_row)
        else:
            text, markup = registration_ui.build_approval_card(user_row)
        await context.bot.send_message(
            chat_id=target_chat, text=text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=markup,
        )
    except Exception:
        logger.exception("Could not deliver the registration approval card")


def _is_privileged_actor(actor_id, rep=None) -> bool:
    """Root and any active representative are provisioned, never queued.

    Two independent signals so the bypass cannot depend on a single stale row:
    the configured root id, and an active ``representatives`` row for this actor.
    """
    try:
        if actor_id is not None and int(actor_id) == int(ADMIN_TELEGRAM_ID):
            return True
    except (TypeError, ValueError):
        pass
    if isinstance(rep, dict) and rep.get("id"):
        return True
    try:
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT 1 FROM representatives WHERE telegram_id=? AND is_active=1 "
                "AND deleted_at IS NULL LIMIT 1", (int(actor_id),),
            ).fetchone()
        return row is not None
    except Exception:
        logger.debug("Privileged-actor probe failed", exc_info=True)
        return False


def _ticket_row(ticket_id: str) -> dict | None:
    """Read one support ticket row for the lifecycle view."""
    try:
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT ticket_id,sender_tg_id,status,assigned_key FROM support_tickets "
                "WHERE ticket_id=?", (str(ticket_id),)).fetchone()
        return dict(row) if row is not None else None
    except Exception:
        logger.debug("Ticket row read failed", exc_info=True)
        return None


def _v52_viewer_rep_id(ctx) -> int | None:
    """The representative a v5.2 surface is acting as, or ``None`` for the root.

    ``profile_360`` renders its scoped control set only for a representative, so
    the bridge has to translate its own actor context into that one value instead
    of passing a persona string the profile would have to re-derive.
    """
    if ctx.get("is_root"):
        return None
    try:
        rep_id = int(ctx.get("rep_id") or 0)
    except (TypeError, ValueError):
        return None
    return rep_id if rep_id > 0 else None


def _v52_retarget_back_row(rows, callback: str, replacement: str, label: str):
    """Point a page's own generic exit at the surface it was actually opened from.

    ``support_ui`` closes a ticket with «بازگشت به تیکت‌ها» → ``v52:tickets``, and the
    v5.2 footer deliberately never adds a second back control to a page that already
    ships one. A ticket opened from a 360-degree profile therefore has to *retarget*
    that row, or the operator is dropped on the generic list instead of the card
    they came from.
    """
    retargeted = []
    for row in rows:
        new_row = []
        for item in row:
            if str(getattr(item, "callback_data", "") or "") == callback:
                new_row.append(InlineKeyboardButton(label, callback_data=replacement))
            else:
                new_row.append(item)
        retargeted.append(new_row)
    return retargeted


def _v52_ticket_page(ctx, persona: str, ticket_id: str, *,
                     fsm_parent: str | None = None, page_parent: str | None = None):
    """The ticket card and its reply form, shared by the plain and contextual routes.

    ``fsm_parent`` is what the reply draft offers as its way back when the form
    expires; ``page_parent`` is the Back button on the card itself. A ticket opened
    from the 360-degree profile passes the profile for both, so a reply that lands
    after the form went stale still returns to the person it was about.
    """
    import support_service
    import support_ui
    from ui_v52 import button as _button, render_page
    ticket = _ticket_row(ticket_id)
    if ticket is None:
        raise ValueError("این تیکت دیگر در دسترس نیست.")
    messages = support_service.list_ticket_messages(ctx, ticket_id)
    summary = "\n".join(
        f"{item.get('visibility', 'customer')}: {str(item.get('body') or '')[:180]}"
        for item in messages[-4:]
    ) or "هنوز پیامی برای این تیکت ثبت نشده است."
    is_author = int(ticket.get("sender_tg_id") or 0) == int(ctx.get("actor_tg_id") or 0)
    is_operator = persona in ("admin", "rep")
    status = str(ticket.get("status") or "").strip().lower()
    is_closed = status == "closed"
    rows = support_ui.ticket_page_rows(
        ticket_id, ticket.get("status"),
        is_operator=is_operator, is_author=is_author,
        sender_tg_id=int(ticket.get("sender_tg_id") or 0) or None)
    if page_parent:
        rows = _v52_retarget_back_row(rows, "v52:tickets", page_parent,
                                      "🔙 بازگشت به پروفایل ۳۶۰ درجه")
    if is_closed and not is_operator:
        # Closed ticket for an end-user: the reply FSM would dead-end, so show a
        # new-ticket door instead. The reopen button is already in lifecycle_rows.
        body = support_ui.ticket_status_header(ticket.get("status"), ticket_id)
        body += "\n\n" + summary
        body += "\n\nاین تیکت بسته شده است. در صورت نیاز یک تیکت جدید ثبت کنید."
        rows.append([_button("➕ ثبت تیکت جدید", "v52:newticket")])
        return render_page(
            "🎫 تیکت " + support_service.ticket_code_label(ticket_id),
            body, persona=persona, page_kind="page", parent_callback=page_parent,
            rows=rows,
        )
    v52_router.DEFAULT_FSM.set(
        ctx, "support_reply", payload={"ticket_id": ticket_id},
        parent_callback=fsm_parent, now=int(time.time()),
    )
    return render_page(
        "🎫 تیکت " + support_service.ticket_code_label(ticket_id),
        support_ui.ticket_status_header(ticket.get("status"), ticket_id)
        + "\n\n" + summary
        + "\n\nپاسخ خود را در پیام بعدی ارسال کنید.",
        persona=persona, page_kind="form", parent_callback=page_parent,
        rows=rows,
    )


def _rep_may_open_profile(data: str, rep) -> bool:
    """True when a representative may reach this admin callback.

    A representative gets exactly one admin surface: the profile of a user inside
    their own scope. Everything else in the ``admin:`` namespace stays root-only,
    so widening this guard can never widen a rep's reach.
    """
    if not rep:
        return False
    parts = str(data or "").split(":")
    if len(parts) < 3 or parts[1] != "user_detail":
        return False
    try:
        target = int(parts[2])
    except (TypeError, ValueError):
        return False
    return _rep_owns_user(target, rep)


def _rep_owns_user(target_user_id: int, rep) -> bool:
    """Scope check: is this user inside the representative's own tenant?

    The user must be attached to the rep (``assigned_rep_id``) or have arrived
    through one of the rep's own bots (``origin_bot_id``). An unattached user is
    never in scope, so the check fails closed.
    """
    try:
        rep_id = int(rep.get("id"))
    except (TypeError, ValueError, AttributeError):
        return False
    try:
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT 1 FROM bot_users u WHERE u.telegram_id=? AND ("
                "u.assigned_rep_id=? OR u.origin_bot_id IN "
                "(SELECT bot_id FROM bot_registry WHERE rep_id=?)) LIMIT 1",
                (int(target_user_id), rep_id, rep_id),
            ).fetchone()
        return row is not None
    except Exception:
        logger.debug("Representative profile scope check failed", exc_info=True)
        return False


async def _apply_registration_gate(
    update, context, role, rep, *, base_role=None, base_rep=None,
) -> bool:
    """Apply the tenant registration policy; True when the actor is held back.

    Root and representatives are provisioned by the operator and never queued.
    The bypass reads the actor's PERMANENT role (``base_role``), not the resolved
    persona: a super admin who switched into the end-user workspace used to be
    asked for a referral code by his own bot. Every other actor is classified by
    the tenant's mode, and a not-yet-approved actor sees the holding page instead
    of the portal.
    """
    import registration_service
    import registration_ui
    user = update.effective_user
    chat = update.effective_chat
    permanent_role = base_role if base_role is not None else role
    if permanent_role in ("super_admin", "rep", "unauthorized_group") or user is None:
        return False
    if _is_privileged_actor(user.id, base_rep if base_rep is not None else rep):
        return False
    bot_id = _bot_id_of(context)
    rep_id = rep.get("id") if isinstance(rep, dict) else None
    invite_code = registration_service.parse_start_payload(getattr(context, "args", None))
    typed_code = context.user_data.pop("referral_pending_code", "")
    try:
        database.ensure_bot_user(user.id, user.username or "", user.full_name or "", role="end_user")
        with database.get_db() as conn:
            result = registration_service.register(
                conn, user_id=user.id, username=user.username or "",
                full_name=user.full_name or "", bot_id=bot_id or None,
                rep_id=rep_id, invite_code=invite_code or None,
                typed_code=typed_code or None, now=int(time.time()),
            )
            conn.commit()
    except registration_service.RegistrationError:
        text, markup = registration_ui.build_registration_waiting_page(
            registration_service.STATUS_AWAITING_REFERRAL)
        await update.message.reply_text(
            "❌ کد معرف وارد‌شده معتبر نیست.\n\n" + text,
            parse_mode=ParseMode.MARKDOWN, reply_markup=markup,
        )
        return True
    except Exception:
        logger.exception("Registration policy evaluation failed; access is held back")
        await update.message.reply_text(
            "⛔️ بررسی وضعیت عضویت ممکن نشد؛ لطفاً بعداً دوباره تلاش کنید.",
            reply_markup=build_actor_navigation_keyboard(role),
        )
        return True

    status = str(result["status"])
    if status == registration_service.STATUS_APPROVED:
        return False
    if status == registration_service.STATUS_PENDING and result.get("changed"):
        row = database.get_bot_user_by_tg_id(user.id) or {}
        await _notify_registration_owner(update, context, row, rep_id)
    if status == registration_service.STATUS_REJECTED:
        text, markup = registration_ui.build_registration_rejected_page()
    else:
        text, markup = registration_ui.build_registration_waiting_page(
            status, has_referrer=bool(result.get("referrer")))
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=markup)
    return True


async def _handle_registration_admin(update, context, *, scope: str, action: str, sub_action: str) -> bool:
    """Shared registration-policy and approval-queue handler for admin and rep.

    One implementation serves both entry points so a representative's bot can
    never diverge from the root behaviour, and the tenant scope is what decides
    which queue is read and written.
    """
    import registration_service
    import registration_ui

    query = update.callback_query
    user = update.effective_user
    prefix = "admin" if scope == "admin" else "rep"
    rep_id = None
    if scope == "rep":
        active_rep = _resolve_persona_rep(user)
        rep_id = active_rep.get("id") if isinstance(active_rep, dict) else None
        if rep_id is None:
            await query.answer("⛔️ میزکار نمایندگی برای حساب شما فعال نیست.", show_alert=True)
            return True
    bot_id = _bot_id_of(context) or None

    def _read_policy():
        with database.get_db() as conn:
            mode = registration_service.resolve_mode(conn, bot_id=bot_id, rep_id=rep_id)
            pending = registration_service.pending_count(conn, bot_id=bot_id, rep_id=rep_id)
        requires = str(database.get_setting(
            "referral_requires_approval", "0")).strip().lower() in {"1", "true", "yes", "on", "enabled"}
        return mode, requires, pending

    # ``message_edit.render_page`` keeps this correct on every source message: a
    # receipt-style photo message cannot be edited as text, and pressing the same
    # button twice is a normal action whose «Message is not modified» is benign.
    # A raw ``edit_message_text`` turned both into «❌ خطای غیرمنتظره رخ داد».
    async def _render_settings():
        import message_edit
        mode, requires, pending = _read_policy()
        text, markup = registration_ui.build_registration_settings_page(
            mode=mode, requires_approval=requires, pending=pending, scope=scope)
        await message_edit.render_page(
            query, text=text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)

    async def _render_queue():
        import message_edit
        with database.get_db() as conn:
            pending = registration_service.list_pending(conn, bot_id=bot_id, rep_id=rep_id)
        text, markup = registration_ui.build_pending_registrations_page(pending, scope=scope)
        await message_edit.render_page(
            query, text=text, reply_markup=markup, parse_mode=ParseMode.MARKDOWN)

    if action == "reg_settings":
        await _render_settings()
        await query.answer("سیاست ثبت‌نام")
        return True

    if action == "reg_mode":
        try:
            with database.get_db() as conn:
                registration_service.set_mode(
                    conn, mode=sub_action, bot_id=bot_id, rep_id=rep_id)
                conn.commit()
        except registration_service.RegistrationError as exc:
            await query.answer(f"❌ حالت نامعتبر: {exc}", show_alert=True)
            return True
        await _render_settings()
        await query.answer("حالت ثبت‌نام ذخیره شد.")
        return True

    if action == "reg_toggle_approval":
        current = str(database.get_setting(
            "referral_requires_approval", "0")).strip().lower() in {"1", "true", "yes", "on", "enabled"}
        database.set_setting(
            "referral_requires_approval", "0" if current else "1",
            actor_id=user.id, actor_name=user.full_name or "Admin")
        await _render_settings()
        await query.answer("تنظیم ذخیره شد.")
        return True

    if action == "pending_reg":
        await _render_queue()
        await query.answer("درخواست‌های عضویت")
        return True

    if action == "reg_approve_all":
        with database.get_db() as conn:
            pending = registration_service.list_pending(conn, bot_id=bot_id, rep_id=rep_id)
        changed = 0
        for item in pending:
            target = int(item["telegram_id"])
            try:
                with database.get_db() as conn:
                    outcome = registration_service.approve(conn, user_id=target, actor_id=user.id)
                    conn.commit()
            except Exception:
                logger.exception("Bulk approval failed for %s", target)
                continue
            if outcome.get("changed"):
                changed += 1
                await _notify_registration_decision(context, target, approved=True)
                registration_service.pay_signup_bonus(
                    target, actor_id=user.id, actor_name=user.full_name or "Admin")
        await _render_queue()
        await query.answer(f"✅ {changed} درخواست تایید شد.")
        return True

    if action in {"reg_approve", "reg_reject"}:
        try:
            target = int(str(sub_action))
        except (TypeError, ValueError):
            await query.answer("شناسه کاربر نامعتبر است.", show_alert=True)
            return True
        approve = action == "reg_approve"
        try:
            with database.get_db() as conn:
                decision = (registration_service.approve if approve else registration_service.reject)
                outcome = decision(conn, user_id=target, actor_id=user.id)
                conn.commit()
        except registration_service.RegistrationError as exc:
            await query.answer(f"❌ تعیین تکلیف انجام نشد: {exc}", show_alert=True)
            return True
        if outcome.get("changed"):
            await _notify_registration_decision(context, target, approved=approve)
            if approve:
                reward = registration_service.pay_signup_bonus(
                    target, actor_id=user.id, actor_name=user.full_name or "Admin")
                await _notify_referrer_of_reward(context, reward, target)
        await _render_queue()
        await query.answer("✅ تایید شد." if approve else "❌ رد شد.")
        return True
    return False


async def _notify_registration_decision(context, target_user_id: int, *, approved: bool) -> None:
    """Tell the applicant the outcome and give them the way back in."""
    try:
        if approved:
            text = ("🎉 **حساب شما تایید شد!**\n\n"
                    "از این پس می‌توانید از منوی اصلی ربات استفاده کنید.")
            label = "🏠 ورود به منوی اصلی"
        else:
            text = ("❌ **درخواست عضویت شما تایید نشد.**\n\n"
                    "در صورت نیاز با پشتیبانی تماس بگیرید.")
            label = "🔄 بررسی وضعیت تایید"
        callback = "user:main" if approved else "user:reg_status"
        await context.bot.send_message(
            chat_id=int(target_user_id), text=text, parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=callback)]]),
        )
    except Exception:
        logger.exception("Could not notify user %s about the registration decision", target_user_id)


async def _notify_referrer_of_reward(context, reward, referred_user_id: int) -> None:
    """Congratulate the referrer when a reward actually lands in their wallet."""
    if not isinstance(reward, dict) or reward.get("status") != "credited":
        return
    referrer = int(reward.get("referrer_user_id") or 0)
    if not referrer:
        return
    try:
        await context.bot.send_message(
            chat_id=referrer,
            text=(
                "🎁 **پاداش دعوت شما واریز شد!**\n\n"
                f"• مبلغ: **{int(reward.get('amount_toman') or 0):,} تومان**\n"
                f"• موجودی جدید کیف پول: **{int(reward.get('new_balance') or 0):,} تومان**\n\n"
                "از دعوت دوستانتان سپاسگزاریم."
            ),
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("💳 کیف پول من", callback_data="user:wallet"),
                InlineKeyboardButton("🎁 دعوت از دوستان", callback_data="user:referral"),
            ]]),
        )
    except Exception:
        logger.exception("Could not notify referrer %s about the reward", referrer)


def _bot_id_of(context) -> int:
    """Telegram bot id from a real or fake context; 0 when it cannot be resolved."""
    try:
        return int(getattr(getattr(context, "bot", None), "id", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _is_navigation_callback(data: str) -> bool:
    text = str(data or "")
    return text in _NAVIGATION_CALLBACKS or text.startswith(_NAVIGATION_CALLBACK_PREFIXES)


def _is_workflow_start_callback(data: str) -> bool:
    return str(data or "").startswith(_WORKFLOW_START_PREFIXES)


def _reset_all_conversation_states(user_id, bot_id, context) -> dict:
    """Drop every pending conversation state for one actor in both stores.

    Called on /start, /cancel, every main-menu/persona/home/back button and at the
    start of each new workflow, so no module can steal another module's input.
    """
    cleared: list = []
    if context is not None and getattr(context, "user_data", None) is not None:
        for key in CONVERSATION_STATE_KEYS:
            if context.user_data.pop(key, None) is not None:
                cleared.append(key)
    fsm_cleared = False
    try:
        actor = int(user_id or 0)
        bot = int(bot_id or 0)
        if actor > 0 and bot > 0:
            fsm_cleared = bool(v52_router.DEFAULT_FSM.clear(
                {"bot_id": bot, "actor_tg_id": actor, "chat_id": actor}
            ))
    except Exception:
        logger.exception("Could not clear the v5.2 draft FSM for actor %s", user_id)
    return {"cleared_keys": cleared, "fsm_cleared": fsm_cleared}


def _is_root_personal_renewal(user, role, account) -> bool:
    """Root exemption requires explicit personal purpose and exact root ownership."""
    if str(role or "").strip().lower() != "super_admin" or user is None:
        return False
    if not isinstance(account, dict):
        return False
    owner = account.get("owner_tg_id")
    return (
        bool(account.get("is_personal_use"))
        and owner is not None
        and str(owner) == str(getattr(user, "id", None))
        and str(account.get("seller_key") or "root") == "root"
    )


async def _create_v52_renewal_checkout(
    user, role, rep, acc_record, gb, days, plan_code, bot_id,
    *, root_personal=False,
):
    """Persist an immutable quote/order and render its before/after renewal preview."""
    if isinstance(bot_id, bool) or not isinstance(bot_id, int) or bot_id <= 0:
        raise ValueError("شناسه معتبر بات برای checkout در دسترس نیست.")
    account_id = acc_record.get("id")
    if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
        raise ValueError("شناسه داخلی حساب برای تمدید v5.2 موجود نیست.")

    binding = database.get_account_panel_binding(account_id=account_id)
    if not binding or binding.get("panel_id") is None:
        raise ValueError("حساب فاقد اتصال یکتای پنل است؛ عملیات انجام نشد.")
    if str(binding.get("lifecycle") or "") not in {"active", "capacity"}:
        raise ValueError("چرخه عمر پنل اجازه تمدید را نمی‌دهد.")

    adapter = database.get_panel_adapter_for_account(account_id)
    lookup = await adapter.get_client(str(binding["email"]))
    if getattr(lookup, "state", None) != "found" or getattr(lookup, "client", None) is None:
        raise ValueError("وضعیت فعلی حساب از پنل قابل تأیید نیست.")
    current = lookup.client

    from renewal_calculator import calculate_renewal
    from pricing_service import price_chain
    from quote_service import create_quote
    from order_service import create_order
    import checkout_ui

    now = int(time.time())
    plan_bytes = int(round(float(gb) * (1024 ** 3)))
    if plan_bytes <= 0 or int(days) <= 0:
        raise ValueError("مقدار پلن تمدید معتبر نیست.")
    before = {
        "quota_mode": current.quota_mode,
        "quota_bytes": current.quota_bytes,
        "used_bytes": current.used_bytes,
        "time_mode": current.time_mode,
        "expires_at": current.expires_at,
        "enabled": current.enabled,
    }
    if current.quota_mode != "finite" or current.time_mode != "dated":
        raise ValueError("حالت حجم/زمان پنل برای تمدید خودکار پشتیبانی‌شده نیست.")

    remaining_bytes = max(int(current.quota_bytes or 0) - int(current.used_bytes or 0), 0)
    remaining_days = max(int(current.expires_at or now) - now, 0) / 86400
    modes = database.get_account_renewal_modes(str(binding["account_code"]), int(user.id))
    rollover_gb = database.get_setting("rollover_max_gb", default=2.0, cast_type=float)
    rollover_days = database.get_setting("rollover_max_days", default=2.0, cast_type=float)
    if root_personal or not bool(modes.get("rollover_enabled")):
        method = "reset_volume_time"
    elif remaining_bytes <= int(float(rollover_gb) * (1024 ** 3)) and remaining_days <= float(rollover_days):
        method = "add_both"
    else:
        method = "reset_volume_add_time"
    after = calculate_renewal(
        current, {"quota_bytes": plan_bytes, "days": int(days)}, method, now,
    )
    public_after = {
        key: after[key] for key in
        ("quota_mode", "quota_bytes", "used_bytes", "time_mode", "expires_at", "enabled")
    }

    seller_key = str(binding.get("seller_key") or (
        f"rep:{int(binding['rep_id'])}" if binding.get("rep_id") is not None else "root"
    ))
    payer_tg_id = int(user.id) if root_personal or binding.get("owner_tg_id") is None else int(binding["owner_tg_id"])
    with database.get_db() as conn:
        bot = conn.execute(
            "SELECT rep_id,is_primary,status FROM bot_registry WHERE bot_id=?", (bot_id,)
        ).fetchone()
        if bot is None or str(bot["status"]).lower() != "active":
            raise ValueError("هویت این بات در registry فعال نیست.")
        bot_seller = (
            "root" if bool(bot["is_primary"]) and bot["rep_id"] is None
            else f"rep:{int(bot['rep_id'])}" if bot["rep_id"] is not None else ""
        )
        # The primary bot may host a representative desk; the rep's own account
        # still prices against that representative's registered merchant edge.
        if str(role).lower() == "rep":
            import bot_context_service
            route = bot_context_service.authorize_checkout_route(
                conn, SimpleNamespace(bot=SimpleNamespace(id=bot_id)),
                actor_tg_id=int(user.id), action="rep_renew", account_id=account_id,
            )
            seller_key = str(route["seller_key"])
        elif bot_seller != seller_key:
            raise ValueError("فروشنده حساب با هویت ثبت‌شده بات مطابقت ندارد.")

        pricing = price_chain(
            conn, plan_code=plan_code, seller_key=seller_key,
            buyer_tg_id=payer_tg_id,
            purpose="personal_use" if root_personal else "vpn_renew",
            now=now,
        )
        quote = create_quote(
            conn, actor_tg_id=int(user.id), bot_id=bot_id,
            merchant_key=seller_key, purpose="vpn_renew",
            amount_toman=int(pricing["amount_toman"]),
            spec={
                "purpose": "vpn_renew", "payer_tg_id": payer_tg_id,
                "before": before, "after": public_after,
                "renewal_method": method, "days": int(days),
                "plan_quota_bytes": plan_bytes,
            },
            price_edges=list(pricing["price_edges"]),
            account_id=account_id, plan_code=plan_code,
            panel_id=int(binding["panel_id"]), now=now,
        )
        rail = "exempt" if int(pricing["amount_toman"]) == 0 else "card_to_card"
        order = create_order(
            conn, quote_id=quote["quote_id"], actor_tg_id=int(user.id),
            bot_id=bot_id, payment_rail=rail, now=now,
        )

    persona = "admin" if role == "super_admin" else "rep" if role == "rep" else "user"
    ctx = {
        "actor_tg_id": int(user.id), "telegram_id": int(user.id),
        "bot_id": bot_id, "persona": persona,
        "rep_id": rep.get("id") if isinstance(rep, dict) else None,
        "is_root": int(user.id) == int(ADMIN_TELEGRAM_ID),
    }
    redirect = (
        f"v52:pay:{order['order_id']}:card"
        if rail == "card_to_card" else f"v52:home:{persona}"
    )
    page = checkout_ui.preview_renew(
        ctx, account_id, plan_code, quote=quote,
        redirect_callback=redirect,
        parent_callback=f"v52:home:{persona}",
        persona=persona,
    )
    return page.text, page.reply_markup

async def process_account_renewal(
    user, role, rep, acc_record, client_info, gb: float, days: float,
    plan_code: str, effective_first: bool, curr_comment: str, comment_updated: bool = False,
    confirmation_id: str | None = None,
    bot_id: int | None = None,
):
    account_code = acc_record["account_code"]
    email = acc_record["email"]
    modes = database.get_account_renewal_modes(account_code, user.id)
    effective_first = bool(effective_first or modes["first_connect_mode"])
    rollover_enabled = bool(modes["rollover_enabled"])
    is_super_admin = (role == "super_admin")
    scope_flt = "all" if is_super_admin else (rep["scope_filter"] if rep else "all")
    by_code_only = False if is_super_admin else True

    # Validate the approved pricing snapshot before acquiring even the short anti-spam lock.
    # Invalid/free-form requests must not create operation state or reach X-UI.
    plan = database.get_plan(plan_code)
    early_keyboard = _build_account_keyboard(
        account_code, user_id=user.id,
        is_enabled=bool(client_info.get("enabled", True)),
        role=role,
        pending_res=database.get_pending_reservation(account_code),
    )
    if not plan or not bool(plan.get("is_active", 1)):
        return False, "⛔️ پلن تمدید تعریف نشده یا غیرفعال است؛ لطفاً یکی از پلن‌های فعال و مصوب را انتخاب کنید.", early_keyboard
    try:
        requested_gb = float(gb)
        requested_days = float(days)
        plan_gb = float(plan["gb"])
        plan_days = int(plan["days"])
        charge = int(plan["rep_price"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return False, "⛔️ اطلاعات پلن تمدید معتبر نیست؛ عملیات انجام نشد.", early_keyboard
    if (
        requested_gb <= 0
        or requested_days <= 0
        or requested_days != plan_days
        or abs(requested_gb - plan_gb) > 1e-9
    ):
        return False, "⛔️ مشخصات حجم یا مدت با پلن مصوب مطابقت ندارد؛ عملیات انجام نشد.", early_keyboard
    plan_name = str(plan["name"])
    target_rep_id = acc_record.get("rep_id")
    root_personal_exempt = _is_root_personal_renewal(user, role, acc_record)
    # A confirmation issued before this bridge stays on its original terminal
    # intent path. It is not a new checkout and must not be charged a second time.
    legacy_admin_confirmation = is_super_admin and confirmation_id is not None
    is_admin_exempt = root_personal_exempt or legacy_admin_confirmation

    # New requests enter the immutable quote/order pipeline. A legacy confirmation
    # remains on its legacy intent path and is never silently converted to a second charge.
    if _runtime_flag("v52_renewal_bridge_enabled") and confirmation_id is None:
        try:
            preview_text, preview_keyboard = await _create_v52_renewal_checkout(
                user, role, rep, acc_record, gb, days, plan_code, bot_id,
                root_personal=root_personal_exempt,
            )
            return True, preview_text, preview_keyboard
        except Exception as exc:
            logger.exception("v5.2 renewal quote preparation failed for %s", account_code)
            return False, f"⛔️ پیش‌نمایش تمدید امن ایجاد نشد: {exc}", early_keyboard

    # A commercial root operation cannot use the legacy no-debit confirmation.
    # It must be reopened as a quoted order; root personal use remains exempt.
    if is_super_admin and not is_admin_exempt:
        return False, (
            "⛔️ تمدید تجاری مدیریت کل معاف از صورت‌حساب نیست؛ "
            "تأیید legacy اجرا نشد و باید checkout جدید ساخته شود."
        ), early_keyboard
    if role == "rep" and target_rep_id is None:
        return False, "⛔️ حساب به نمایندگی مشخصی متصل نیست؛ برداشت انجام نشد.", early_keyboard
    if not is_admin_exempt and charge <= 0:
        return False, "⛔️ قیمت پلن برای نمایندگی معتبر نیست؛ با مدیریت تماس بگیرید.", early_keyboard

    # Read dynamic system settings
    rollover_max_gb = database.get_setting("rollover_max_gb", default=2.0, cast_type=float)
    rollover_max_days = database.get_setting("rollover_max_days", default=2.0, cast_type=float)
    lock_ttl = database.get_setting("renewal_lock_ttl_sec", default=20, cast_type=int)

    # 1. Anti-spam / duplicate click lock
    if not database.acquire_renewal_lock(account_code, ttl_seconds=lock_ttl):
        pending_res = database.get_pending_reservation(account_code)
        keyboard = _build_account_keyboard(account_code, user_id=user.id, is_enabled=True, role=role, pending_res=pending_res)
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



    # Representative Credit Limit Enforcement
    if not is_admin_exempt and target_rep_id:
        allowed, curr_debt, credit_limit = database.check_rep_credit_limit(target_rep_id, charge)
        if not allowed:
            pending_res = database.get_pending_reservation(account_code)
            keyboard = _build_account_keyboard(account_code, user_id=user.id, is_enabled=True, role=role, pending_res=pending_res)
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
        operation_lock_key = f"account-renewal:{account_code.lower()}"
        operation_lock_token = None
        rep_renewal_intent_id = None
        admin_renewal_confirmation_id = None
        new_bal = None

        if not is_admin_exempt:
            try:
                started = database.begin_rep_renewal_intent(
                    rep_id=target_rep_id,
                    actor_tg_id=user.id,
                    account_code=account_code,
                    account_email=email,
                    plan_code=plan_code,
                    plan_name=plan_name,
                    gb=gb,
                    days=days,
                    amount=charge,
                    confirmation_id=confirmation_id,
                    actor_role=role,
                )
            except ValueError as exc:
                pending_res = database.get_pending_reservation(account_code)
                keyboard = _build_account_keyboard(
                    account_code, user_id=user.id, is_enabled=is_enabled,
                    role=role, pending_res=pending_res
                )
                return False, f"⛔️ ثبت مالی تمدید انجام نشد: {exc}", keyboard

            if started.get("status") != "started":
                pending_res = database.get_pending_reservation(account_code)
                keyboard = _build_account_keyboard(
                    account_code, user_id=user.id, is_enabled=is_enabled,
                    role=role, pending_res=pending_res
                )
                return False, "⏳ درخواست تمدید قبلی هنوز در حال پردازش یا نیازمند تطبیق است؛ از اجرای دوباره خودداری کنید.", keyboard

            rep_renewal_intent_id = started["intent_id"]
            operation_lock_key = started["lock_key"]
            operation_lock_token = started["lock_token"]
            new_bal = started["new_balance"]
        else:
            if role == "super_admin" and confirmation_id:
                try:
                    admin_started = database.begin_admin_renewal_confirmation(
                        confirmation_id=confirmation_id,
                        actor_id=user.id,
                        actor_role=role,
                        confirmation_rep_id=rep["id"] if rep else None,
                        account_code=account_code,
                        account_email=email,
                        plan_code=plan_code,
                        plan_name=plan_name,
                        gb=gb,
                        days=days,
                        amount=charge,
                    )
                except ValueError as exc:
                    pending_res = database.get_pending_reservation(account_code)
                    keyboard = _build_account_keyboard(
                        account_code, user_id=user.id, is_enabled=is_enabled,
                        role=role, pending_res=pending_res
                    )
                    return False, f"⛔️ تأیید تمدید مدیریت کل قابل ثبت نیست: {exc}", keyboard
                if admin_started.get("status") != "started":
                    pending_res = database.get_pending_reservation(account_code)
                    keyboard = _build_account_keyboard(
                        account_code, user_id=user.id, is_enabled=is_enabled,
                        role=role, pending_res=pending_res
                    )
                    return False, "⏳ درخواست تمدید قبلی هنوز در حال پردازش یا نیازمند تطبیق است؛ از اجرای دوباره خودداری کنید.", keyboard
                admin_renewal_confirmation_id = confirmation_id
                operation_lock_key = admin_started["lock_key"]
                operation_lock_token = admin_started["lock_token"]
            else:
                operation_lock_token = database.acquire_operation_lock(operation_lock_key, ttl_seconds=None)
                if not operation_lock_token:
                    pending_res = database.get_pending_reservation(account_code)
                    keyboard = _build_account_keyboard(
                        account_code, user_id=user.id, is_enabled=is_enabled,
                        role=role, pending_res=pending_res
                    )
                    return False, "⏳ درخواست تمدید قبلی هنوز در حال پردازش یا نیازمند تطبیق است؛ از اجرای دوباره خودداری کنید.", keyboard

        try:
            ren_res = await asyncio.to_thread(
                xui_manager.renew_client,
                email, gb, days, comment=curr_comment, rollover=rollover_enabled, countdown=countdown_param,
                scope=scope_flt, by_code_only=by_code_only
            )
        except Exception:
            logger.exception("X-UI renewal outcome is unknown for %s", account_code)
            # Keep the debit hold and exact operation lease for evidence-gated reconciliation.
            if rep_renewal_intent_id:
                try:
                    database.mark_rep_renewal_for_reconciliation(
                        rep_renewal_intent_id, target_rep_id, user.id,
                        "X-UI renewal raised an exception; external outcome is unknown",
                    )
                except Exception:
                    logger.exception("Could not mark representative renewal for reconciliation: %s", account_code)
            elif admin_renewal_confirmation_id:
                try:
                    database.mark_admin_renewal_confirmation_for_reconciliation(
                        admin_renewal_confirmation_id, user.id,
                        "X-UI renewal raised an exception; external outcome is unknown",
                    )
                except Exception:
                    logger.exception("Could not mark Super Admin renewal for reconciliation: %s", account_code)
            pending_res = database.get_pending_reservation(account_code)
            keyboard = _build_account_keyboard(
                account_code, user_id=user.id, is_enabled=is_enabled,
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
                        database.refund_rep_renewal_intent(
                            rep_renewal_intent_id, target_rep_id, user.id,
                            f"استرداد پیش‌پرداخت به دلیل رد قطعی تمدید {account_code}",
                        )
                    except Exception:
                        logger.exception("Could not refund representative renewal intent for %s", account_code)
                        try:
                            database.mark_rep_renewal_for_reconciliation(
                                rep_renewal_intent_id, target_rep_id, user.id,
                                "X-UI explicitly rejected renewal but atomic refund failed",
                            )
                        except Exception:
                            logger.exception("Could not mark failed refund for reconciliation: %s", account_code)
                        pending_res = database.get_pending_reservation(account_code)
                        keyboard = _build_account_keyboard(
                            account_code, user_id=user.id, is_enabled=is_enabled,
                            role=role, pending_res=pending_res
                        )
                        return False, "⚠️ پنل تمدید را رد کرد اما استرداد اتمیک انجام نشد؛ قفل و پیش‌پرداخت برای تطبیق مدیریت حفظ شدند.", keyboard
                elif admin_renewal_confirmation_id:
                    database.fail_admin_renewal_confirmation(
                        admin_renewal_confirmation_id, user.id,
                        f"X-UI explicitly rejected renewal for {account_code}",
                    )
                else:
                    database.release_operation_lock(operation_lock_key, operation_lock_token)
                pending_res = database.get_pending_reservation(account_code)
                keyboard = _build_account_keyboard(
                    account_code, user_id=user.id, is_enabled=is_enabled,
                    role=role, pending_res=pending_res
                )
                return False, (
                    "❌ پنل تمدید را رد کرد؛ هیچ تمدیدی انجام نشد و وجه/اعتبار پیش‌پرداخت‌شده مسترد شد."
                ), keyboard

            # Unknown response: do not refund a possibly completed external operation.
            if rep_renewal_intent_id:
                try:
                    database.mark_rep_renewal_for_reconciliation(
                        rep_renewal_intent_id, target_rep_id, user.id,
                        f"X-UI returned an ambiguous response: {str(ren_res)[:300]}",
                    )
                except Exception:
                    logger.exception("Could not mark ambiguous representative renewal: %s", account_code)
            elif admin_renewal_confirmation_id:
                try:
                    database.mark_admin_renewal_confirmation_for_reconciliation(
                        admin_renewal_confirmation_id, user.id,
                        f"X-UI returned an ambiguous response: {str(ren_res)[:300]}",
                    )
                except Exception:
                    logger.exception("Could not mark ambiguous Super Admin renewal: %s", account_code)
            pending_res = database.get_pending_reservation(account_code)
            keyboard = _build_account_keyboard(
                account_code, user_id=user.id, is_enabled=is_enabled,
                role=role, pending_res=pending_res
            )
            return False, (
                "⚠️ پاسخ پنل موفقیت یا شکست قطعی را مشخص نمی‌کند؛ مبلغ محفوظ مانده و بررسی دستی لازم است."
            ), keyboard

        if rep_renewal_intent_id:
            try:
                database.complete_rep_renewal_intent(
                    rep_renewal_intent_id, target_rep_id, user.id,
                    "X-UI explicitly confirmed renewal success",
                )
            except Exception:
                logger.exception("X-UI renewal succeeded but local intent finalization failed for %s", account_code)
                try:
                    database.mark_rep_renewal_for_reconciliation(
                        rep_renewal_intent_id, target_rep_id, user.id,
                        "X-UI explicitly succeeded but local finalization failed",
                    )
                except Exception:
                    logger.exception("Could not mark successful external renewal for reconciliation: %s", account_code)
                pending_res = database.get_pending_reservation(account_code)
                keyboard = _build_account_keyboard(
                    account_code, user_id=user.id, is_enabled=is_enabled,
                    role=role, pending_res=pending_res
                )
                return False, "⚠️ پنل تمدید را با موفقیت انجام داد، اما ثبت نهایی محلی کامل نشد؛ قفل برای تطبیق حفظ شد.", keyboard
        elif admin_renewal_confirmation_id:
            try:
                database.complete_admin_renewal_confirmation(
                    admin_renewal_confirmation_id, user.id,
                    "X-UI explicitly confirmed Super Admin renewal success",
                )
            except Exception:
                logger.exception("Super Admin renewal succeeded but local confirmation finalization failed for %s", account_code)
                try:
                    database.mark_admin_renewal_confirmation_for_reconciliation(
                        admin_renewal_confirmation_id, user.id,
                        "X-UI explicitly succeeded but local confirmation finalization failed",
                    )
                except Exception:
                    logger.exception("Could not mark successful Super Admin renewal for reconciliation: %s", account_code)
                pending_res = database.get_pending_reservation(account_code)
                keyboard = _build_account_keyboard(
                    account_code, user_id=user.id, is_enabled=is_enabled,
                    role=role, pending_res=pending_res
                )
                return False, "⚠️ پنل تمدید را با موفقیت انجام داد، اما ثبت نهایی محلی کامل نشد؛ قفل برای تطبیق حفظ شد.", keyboard
        else:
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

        keyboard = _build_account_keyboard(account_code, user_id=user.id, is_enabled=True, role=role, pending_res=None)

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
            keyboard = _build_account_keyboard(account_code, user_id=user.id, is_enabled=is_enabled, role=role, pending_res=existing_res)
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
            on_first_connect=effective_first,
            charge_amount=res_charge,
            actor_id=user.id,
            actor_name=user.full_name or user.username,
            confirmation_id=confirmation_id,
            actor_role=role,
            confirmation_rep_id=rep["id"] if rep else None,
        )

        if is_admin_exempt:
            fin_info = "• وضعیت مالی: 👑 **عملیات مستقیم مدیریت کل (معاف از صورت‌حساب نمایندگی)**"
        else:
            # Reservation and representative debit were committed atomically by create_reservation.
            new_bal = database.get_rep_balance(target_rep_id)
            fin_info = f"• فاکتور رزرو: `{charge:,} تومان` | **مانده بدهی نماینده:** `{new_bal:,} تومان`"

        pending_dict = database.get_reservation_by_id(res_id)
        keyboard = _build_account_keyboard(account_code, user_id=user.id, is_enabled=is_enabled, role=role, pending_res=pending_dict)

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

def build_receipt_navigation_keyboard(role: str) -> InlineKeyboardMarkup | None:
    """Build role-scoped navigation buttons for receipt success and recoverable errors."""
    if role == "rep":
        buttons = [
            [InlineKeyboardButton("💰 امور مالی و فیش‌ها", callback_data="rep:finance")],
            [InlineKeyboardButton("🏠 بازگشت به پیشخوان نماینده", callback_data="rep:main")],
        ]
    elif role == "end_user":
        buttons = [
            [
                InlineKeyboardButton("💳 کیف پول و شارژ", callback_data="user:wallet"),
                InlineKeyboardButton("📜 سوابق فیش‌های من", callback_data="user:receipts"),
            ],
            [InlineKeyboardButton("🏠 بازگشت به پورتال", callback_data="user:main")],
        ]
    else:
        return None
    return InlineKeyboardMarkup(buttons)


def _active_persona_wallet_owner_id(user, role, rep):
    """Resolve the wallet target for a persona acting on another linked identity."""
    if role == "end_user":
        return int(user.id)
    if role == "rep" and rep:
        # When a root/rep switches into the representative persona, receipts
        # must still belong to the authenticated actor (payment_receipts.user_id)
        # while credit/debt attribution uses the linked representative id.
        return int(user.id)
    return int(user.id)


def build_finance_fsm_keyboard(role: str) -> InlineKeyboardMarkup:
    """Navigation for the wallet-amount FSM.

    Every branch of the amount-entry state returns this keyboard, so a rejected
    amount, a role mismatch or a stale prompt never strands the actor in a chat
    with no way back to the wallet or the dashboard.
    """
    if role == "rep":
        buttons = [
            [InlineKeyboardButton("💰 امور مالی و فیش‌ها", callback_data="rep:finance")],
            [InlineKeyboardButton("🏠 بازگشت به پیشخوان نماینده", callback_data="rep:main")],
        ]
    elif role == "end_user":
        buttons = [
            [InlineKeyboardButton("💳 کیف پول و شارژ", callback_data="user:wallet")],
            [InlineKeyboardButton("🏠 بازگشت به پورتال", callback_data="user:main")],
        ]
    else:
        buttons = [
            [InlineKeyboardButton("🏠 بازگشت به منوی مدیریت", callback_data="admin:main")],
        ]
    return InlineKeyboardMarkup(buttons)


def _persona_for_role(role: str) -> str:
    """Map a resolved role to its v5.2 persona name."""
    return "admin" if role == "super_admin" else "rep" if role == "rep" else "user"


def build_v52_fsm_error_keyboard(persona: str, parent_callback=None) -> InlineKeyboardMarkup:
    """Navigation for a failed or refused v5.2 text workflow.

    Every answer the text router produces carries a way back, so a rejected
    draft never leaves the actor with a message and no buttons.
    """
    rows = []
    if isinstance(parent_callback, str) and parent_callback.startswith("v52:"):
        rows.append([InlineKeyboardButton("↩ بازگشت", callback_data=parent_callback)])
    rows.append([InlineKeyboardButton("🏠 منوی اصلی", callback_data=f"v52:home:{persona}")])
    return InlineKeyboardMarkup(rows)


def build_panel_credential_wizard_keyboard(panel_id: int) -> InlineKeyboardMarkup:
    """Escape hatch while the panel credential step is awaiting input."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ انصراف از تنظیم اتصال", callback_data=f"v52:panel:{panel_id}")],
        [InlineKeyboardButton("🏠 بازگشت به منوی اصلی", callback_data="v52:home:admin")],
    ])


def build_panel_wizard_keyboard(panel_id: int, *, reachable: bool = False) -> InlineKeyboardMarkup:
    """Next-step buttons for the panel wizard.

    Saving a connection used to answer with bare text, so the admin had to hunt
    the panel card for the test and activation buttons. Every wizard answer now
    carries the whole next step, including a one-click test-and-activate.
    """
    activate_label = ("🟢 فعال‌سازی فوری پنل (Active)" if reachable
                      else "🟢 تغییر وضعیت به فعال (Active)")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🧪 آزمایش اتصال پنل " + str(panel_id),
                              callback_data=f"v52:paneltest:{panel_id}")],
        [InlineKeyboardButton("⚡ تست اتصال و فعال‌سازی خودکار",
                              callback_data=f"v52:panelauto:{panel_id}")],
        [InlineKeyboardButton(activate_label, callback_data=f"v52:life:{panel_id}:active")],
        [InlineKeyboardButton("🖥 مشاهده کارت پنل " + str(panel_id),
                              callback_data=f"v52:panel:{panel_id}")],
        [InlineKeyboardButton("🏠 بازگشت به منوی اصلی", callback_data="v52:home:admin")],
    ])


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    user = update.effective_user
    chat = update.effective_chat
    thread_id = update.message.message_thread_id
    role, rep = _persona_aware_role(user, chat, thread_id)
    if role == "unauthorized_group":
        return
    if database.is_user_blocked(user.id):
        await update.message.reply_text("⛔️ دسترسی شما به این بات مسدود شده است.")
        return
    # A root actor may have switched persona after starting the top-up; the
    # persisted workflow role owns the continuation, so adopt it before the role
    # guard runs instead of refusing the receipt the actor just asked for.
    resolved = _resolve_finance_continuation(user, context, role, rep)
    if resolved is not None:
        role, rep = resolved
    if role not in ("end_user", "rep") or (role == "rep" and not rep):
        await update.message.reply_text(
            "⛔️ ارسال فیش فقط برای نقش کاربر نهایی یا نماینده مجاز است.",
            reply_markup=build_receipt_navigation_keyboard(role)
            or build_actor_navigation_keyboard(role),
        )
        return

    # Order-bound attempts are handled before legacy wallet receipts.
    if await _submit_v52_order_receipt(update, context, role):
        return
    if await _route_v52_fsm_photo(update, context):
        return

    navigation_keyboard = build_receipt_navigation_keyboard(role)
    if not update.message.photo:
        await update.message.reply_text(
            "⚠️ فرمت تصویر ارسالی برای ثبت فیش پشتیبانی نمی‌شود. لطفاً تصویر را به‌صورت Photo ارسال کنید، نه به‌شکل فایل/Document.",
            reply_markup=navigation_keyboard,
        )
        return

    photo_id = update.message.photo[-1].file_id
    caption = (update.message.caption or "").strip()
    tr_code = context.user_data.get("active_receipt_code")
    amount = int(context.user_data.get("deposit_amount") or 0)
    # Same persona-continuation rule as the amount step: the persisted workflow
    # role owns the receipt, so a root actor who switched persona mid-flow still
    # completes it while a genuine role change is refused.
    resolved = _resolve_finance_continuation(user, context, role, rep)
    if (
        resolved is None
        or resolved[0] not in ("end_user", "rep")
        or (resolved[0] == "rep" and not resolved[1])
    ):
        await update.message.reply_text(
            "⛔️ این فرایند ارسال فیش با نقش فعلی شما آغاز نشده است؛ از پنل همان نقش دوباره اقدام کنید.",
            reply_markup=navigation_keyboard,
        )
        return
    role, rep = resolved
    navigation_keyboard = build_receipt_navigation_keyboard(role)
    dep_role = role
    target_rep_id = rep["id"] if role == "rep" else None

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
            "⚠️ مبلغ فیش مشخص نیست. ابتدا از گزینه ثبت فیش یا دستور /deposit مبلغ را ثبت کنید.",
            reply_markup=navigation_keyboard,
        )
        return

    admin_tg_id = database.get_setting("admin_telegram_id", default=ADMIN_TELEGRAM_ID, cast_type=int)
    admin_dm_top = database.get_setting("admin_dm_topic_id", default=ADMIN_DM_LOGS_TOPIC_ID, cast_type=int)
    try:
        # Receipt submission and durable admin-notification enqueue share one DB transaction.
        receipt = database.submit_payment_receipt(
            user_id=user.id,
            role=dep_role,
            amount_toman=amount,
            photo_id=photo_id,
            rep_id=target_rep_id,
            receipt_id_or_code=tr_code,
            note=caption,
            recipient_chat_id=admin_tg_id if admin_tg_id and int(admin_tg_id) > 0 else None,
            recipient_thread_id=admin_dm_top if admin_dm_top and int(admin_dm_top) > 0 else None,
            sender_name=user.full_name or "",
            sender_username=user.username or "",
        )
        tr_code = receipt["tracking_code"]
    except PermissionError:
        logger.warning("Rejected receipt submission with mismatched owner/role for user %s", user.id)
        await update.message.reply_text(
            "⛔️ فیش در انتظار ارسال برای این حساب یافت نشد یا دیگر قابل ویرایش نیست.",
            reply_markup=navigation_keyboard,
        )
        return
    except Exception:
        logger.exception("Could not persist incoming payment receipt and notification outbox")
        await update.message.reply_text(
            "❌ ثبت فیش انجام نشد. لطفاً دوباره تلاش کنید یا با پشتیبانی تماس بگیرید.",
            reply_markup=navigation_keyboard,
        )
        return

    for key in ("fsm_state", "active_receipt_code", "deposit_amount", "deposit_role"):
        context.user_data.pop(key, None)

    amount_disp = f"**{receipt['amount_toman']:,} تومان**"
    await update.message.reply_text(
        f"🚀 **رسید پرداخت شما با موفقیت دریافت و ثبت گردید.**\n\n"
        f"• کد پیگیری: `{receipt['tracking_code']}`\n"
        f"• مبلغ واریزی: {amount_disp}\n\n"
        "به محض بررسی و تایید توسط مدیریت، حساب شما شارژ یا تسویه خواهد شد.",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=navigation_keyboard,
    )

    # The durable outbox already exists; this is only the first delivery attempt.
    # The background worker will retry after process restarts or delivery failures.
    await process_receipt_notification_outbox(context.bot, limit=10)

# --- Message & NLP Router ---

def _p360_scoped_note_pending(context) -> bool:
    """True while a scoped representative's 360-profile note form owns the next message.

    The note writer lives in ``admin_panel`` (``action == "profile_note"``) and is
    reached through the same ``admin_state`` FSM the super admin uses, so the form is
    handed to it rather than duplicated. The marker is written by
    ``profile_360._begin_note_input`` and only for a scoped representative, so no
    other admin form action becomes reachable for a non-root actor.
    """
    state = (getattr(context, "user_data", None) or {}).get("admin_state") or {}
    return state.get("action") == "profile_note" and bool(state.get("scoped_rep_id"))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
        
    user = update.effective_user
    chat = update.effective_chat
    thread_id = update.message.message_thread_id
    msg_text = update.message.text or ""
    
    role, rep = _persona_aware_role(user, chat, thread_id)
    if role == "unauthorized_group":
        return

    if database.is_user_blocked(user.id):
        await update.message.reply_text("⛔️ دسترسی شما به این بات توسط مدیریت مسدود شده است.")
        return
    if role == "super_admin" and chat.type != "private":
        return

    # A user held at the referral gate may send their code as plain text. It is
    # captured before any other handler so the code is never mistaken for an
    # amount, a ticket body or a search term.
    if role == "end_user" and context.user_data.get("referral_pending"):
        import registration_service
        import registration_ui
        code = registration_service.normalize_referral_code(msg_text)
        context.user_data.pop("referral_pending", None)
        if code:
            context.user_data["referral_pending_code"] = code
            await start_cmd(update, context)
        else:
            text, markup = registration_ui.build_referral_code_prompt()
            await update.message.reply_text(
                "❌ کد معرف نامعتبر است.\n\n" + text,
                parse_mode=ParseMode.MARKDOWN, reply_markup=markup,
            )
        return

    # A live step-by-step wizard owns the next text message. It is consulted before
    # the draft FSM and the legacy per-module forms because it is the most recently
    # started conversation state, and it keeps the chat on a single screen.
    from ui_v52 import wizard_handle_text
    if await wizard_handle_text(update, context):
        return

    if await _route_v52_fsm_text(update, context):
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

    # Check Admin Interactive Input State (FSM) - STRICTLY in private DM only.
    # A scoped representative's own 360-profile note form is the one admin form
    # action they may complete, and it is recognised by its explicit marker.
    if chat.type == "private" and (role == "super_admin"
                                   or _p360_scoped_note_pending(context)):
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
        # The persona can change between entering the amount prompt and sending
        # the amount. Route this continuation by the persisted workflow role,
        # but keep the root actor's Telegram id as the receipt owner.
        resolved = _resolve_finance_continuation(user, context, role, rep)
        if (
            resolved is None
            or resolved[0] not in ("end_user", "rep")
            or (resolved[0] == "rep" and not resolved[1])
        ):
            await update.message.reply_text(
                "⛔️ این فرایند شارژ متعلق به نقش فعلی شما نیست؛ از پنل همان نقش دوباره اقدام کنید.",
                reply_markup=build_finance_fsm_keyboard(role),
            )
            return
        role, rep = resolved
        text_clean = msg_text.replace(",", "").strip()
        if text_clean.isdigit() and int(text_clean) > 0:
            amount = int(text_clean)
            context.user_data["deposit_amount"] = amount
            card_info = database.get_card_info()
            dep_role = role
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
            await _cleanup_deposit_amount_prompt(context, chat.id)
            return
        else:
            await update.message.reply_text(
                "⚠️ لطفاً مبلغ را به صورت عدد معتبر لاتین وارد کنید (مثال: `500000`).",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_finance_fsm_keyboard(role),
            )
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
            await update.message.reply_text(
                "❌ حجم و مدت اکانت باید مثبت باشند.",
                reply_markup=build_actor_navigation_keyboard(role),
            )
            return

        target_rep_id = rep["id"] if rep else None
        is_exempt = role == "super_admin" or target_rep_id is None
        plan_code = f"{int(gb)}gb_{int(days)}d"
        plan = database.get_plan(plan_code)
        if not is_exempt and not plan:
            await update.message.reply_text(
                "❌ این ترکیب حجم/مدت، تعرفه مصوب نمایندگی ندارد. لطفاً از فهرست پلن‌های تاییدشده برای صدور استفاده کنید.",
                reply_markup=build_actor_navigation_keyboard(role),
            )
            return
        charge = int(plan["rep_price"]) if plan else 0
        if not is_exempt and charge <= 0:
            await update.message.reply_text(
                "❌ قیمت صدور اکانت معتبر نیست؛ با مدیریت تماس بگیرید.",
                reply_markup=build_actor_navigation_keyboard(role),
            )
            return

        # Representative NLP issuance is handed off to the same durable,
        # actor-bound rep_action_intents flow used by the dashboard.
        if role == "rep":
            try:
                intent_id = database.create_rep_action_intent(
                    rep_id=rep["id"], actor_tg_id=user.id,
                    plan_code=plan["plan_code"], plan_name=plan["name"],
                    gb=plan["gb"], days=plan["days"], amount=charge,
                )
                if comment:
                    database.set_rep_action_intent_customer_name(
                        intent_id, rep["id"], user.id, comment
                    )
            except Exception:
                logger.exception("Could not create durable NLP representative issuance intent")
                await update.message.reply_text(
                    "❌ ثبت درخواست صدور ممکن نشد؛ لطفاً از پنل نمایندگی دوباره اقدام کنید.",
                    reply_markup=build_actor_navigation_keyboard(role),
                )
                return

            confirm_keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton(
                    f"✅ تأیید صدور با هزینه {charge:,} تومان",
                    callback_data=f"rep:issue_acc_direct:{intent_id}:noname"
                )],
                [InlineKeyboardButton("❌ انصراف", callback_data="rep:new_acc_plans")]
            ])
            await update.message.reply_text(
                f"🆕 **درخواست صدور ثبت شد**\n\n"
                f"• پلن: **{plan['name']}** ({plan['gb']} گیگ / {plan['days']} روز)\n"
                f"• هزینه: **{charge:,} تومان**\n"
                f"• نام مشتری: {comment or 'ثبت نشده'}\n\n"
                "با تأیید دکمه زیر، پیش‌پرداخت و قفل پایدار به‌صورت اتمیک ثبت و سپس پنل فراخوانی می‌شود.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=confirm_keyboard,
            )
            return

        lock_key = f"new-account:{role}:{user.id}:{plan_code}"
        lock_token = database.acquire_operation_lock(lock_key, ttl_seconds=None)
        if not lock_token:
            await update.message.reply_text(
                "⏳ درخواست صدور دیگری در حال پردازش است یا نتیجه قبلی نیازمند بررسی است؛ لطفاً دوباره اجرا نکنید.",
                reply_markup=build_actor_navigation_keyboard(role),
            )
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
                await update.message.reply_text(
                    f"⛔️ صدور اکانت انجام نشد: {exc}",
                    reply_markup=build_actor_navigation_keyboard(role),
                )
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
                "⚠️ پاسخ پنل نامشخص است؛ مبلغ/اعتبار در وضعیت محفوظ باقی مانده و بررسی دستی لازم است. لطفاً درخواست صدور دیگری نفرستید.",
                reply_markup=build_actor_navigation_keyboard(role),
            )
            return

        if classify_xui_result(alloc_res) == XUIResult.SUCCESS:
            email = alloc_res.get("email")
            if not email:
                logger.error("X-UI reports account allocation success without email: %r", alloc_res)
                await update.message.reply_text(
                    "⚠️ پنل موفقیت را گزارش کرد اما شناسه اکانت ناقص است؛ مبلغ برای تطبیق محفوظ ماند.",
                    reply_markup=build_actor_navigation_keyboard(role),
                )
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
                    "⚠️ اکانت در پنل ایجاد شد اما ثبت محلی کامل نشد؛ مبلغ محفوظ است و تطبیق پنل/دیتابیس لازم است.",
                    reply_markup=build_actor_navigation_keyboard(role),
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
            keyboard = _build_account_keyboard(code, user_id=user.id, is_enabled=True, role=role)
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

        if classify_xui_result(alloc_res) == XUIResult.FAILURE:
            if not is_exempt:
                try:
                    database.register_rep_payment(
                        target_rep_id, charge,
                        f"استرداد پیش‌پرداخت به دلیل شکست قطعی صدور {plan_code}",
                        actor_id=user.id, actor_name=user.full_name or user.username
                    )
                except Exception:
                    logger.exception("Failed to reverse representative account precharge")
                    await update.message.reply_text(
                        "⚠️ پنل صدور را رد کرد اما استرداد مالی ثبت نشد؛ مدیریت باید بررسی کند.",
                        reply_markup=build_actor_navigation_keyboard(role),
                    )
                    return
            database.release_operation_lock(lock_key, lock_token)
            await update.message.reply_text(
                f"❌ پنل صدور اکانت را رد کرد؛ {'پیش‌پرداخت مسترد شد.' if not is_exempt else 'هزینه‌ای ثبت نشد.'} "
                f"{alloc_res.get('message', '')}",
                reply_markup=build_actor_navigation_keyboard(role),
            )
            return

        logger.error("Ambiguous X-UI allocation response for %s: %r", user.id, alloc_res)
        await update.message.reply_text(
            "⚠️ پاسخ پنل برای تأیید یا رد صدور کافی نیست؛ مبلغ/اعتبار محفوظ است و بررسی دستی لازم است.",
            reply_markup=build_actor_navigation_keyboard(role),
        )
        return
    # 2. Extract account code from reply or message
    account_code = nlp_matcher.extract_account_code(reply_text, msg_text)

    if not account_code:
        if chat.id < 0:
            return
        await update.message.reply_text(
            "⚠️ شناسه یا کد اکانت یافت نشد.\nلطفاً روی پیام اکانت ریپلای کنید یا کد/نام کاربری آن را بفرستید.",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_actor_navigation_keyboard(role),
        )
        return

    # Check End-User access restriction
    if role == "end_user":
        owned_account = database.get_user_owned_account(user.id, account_code)
        if not owned_account:
            await update.message.reply_text(
                "⛔️ شما به این اکانت دسترسی ندارید. ابتدا با دستور `/link <کد_اکانت>` آن را ثبت کنید.",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_actor_navigation_keyboard(role),
            )
            return
        account_code = owned_account["email"]

    # Super Admin vs Rep Scope Configuration
    is_super_admin = (role == "super_admin")
    scope_flt = "all" if is_super_admin else (rep["scope_filter"] if rep else "all")
    by_code_only = False if is_super_admin else True

    # 1. Lookup in local database accounts
    acc_record = None
    if is_super_admin:
        with database.get_db() as conn:
            row = conn.cursor().execute(
                "SELECT * FROM accounts WHERE LOWER(account_code) = ? OR LOWER(email) = ? LIMIT 1",
                (account_code.lower(), account_code.lower())
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
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_actor_navigation_keyboard(role),
            )
            return
        await update.message.reply_text(
            f"⚠️ **اکانت `{account_code}` در سرور ۳X-UI یافت نشد.**",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_actor_navigation_keyboard(role),
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
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=build_actor_navigation_keyboard(role),
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

    # Read persisted per-account policy for message-driven cards and renewals.
    modes = database.get_account_renewal_modes(account_code, user.id)
    on_first = modes["first_connect_mode"]

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
            on_first = modes["first_connect_mode"]
            keyboard = _build_account_keyboard(account_code, user_id=user.id, is_enabled=False, role=role)
            await update.message.reply_text(text_out, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            database.log_audit(
                rep_id=acc_record.get("rep_id"), account_code=account_code, email=email,
                action="suspend_account", actor_id=user.id, actor_name=user.full_name or user.username,
                details={"rem_gb": rem_gb}, result_status="success"
            )
            await asyncio.to_thread(send_admin_log, f"🛑 **قطع اتصال اکانت:** `{email}` توسط {user.full_name}\nحجم محفوظ: {rem_gb:.2f} GB")
        else:
            await update.message.reply_text(
                f"❌ خطا در غیرفعال‌سازی اکانت: {res.get('message')}",
                reply_markup=build_actor_navigation_keyboard(role),
            )

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
            on_first = modes["first_connect_mode"]
            keyboard = _build_account_keyboard(account_code, user_id=user.id, is_enabled=True, role=role)
            await update.message.reply_text(text_out, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
            database.log_audit(
                rep_id=acc_record["rep_id"], account_code=account_code, email=email,
                action="activate_account", actor_id=user.id, actor_name=user.full_name or user.username,
                details={"rem_gb": rem_gb}, result_status="success"
            )
            await asyncio.to_thread(send_admin_log, f"✅ **برقراری مجدد اتصال اکانت:** `{email}` توسط {user.full_name}")
        else:
            await update.message.reply_text(
                f"❌ خطا در برقراری اتصال اکانت: {res.get('message')}",
                reply_markup=build_actor_navigation_keyboard(role),
            )

    elif intent == "renew" and role in ("super_admin", "rep"):
        gb = params.get("gb", 20.0)
        days = params.get("days", 30.0)
        first_connect_req = params.get("first_connect", False)
        mode_first = modes["first_connect_mode"]
        effective_first = first_connect_req or mode_first
        plan_code = f"{int(gb)}gb_{int(days)}d"

        ok, msg_out, keyboard = await process_account_renewal(
            user=user, role=role, rep=rep, acc_record=acc_record, client_info=client_info,
            gb=gb, days=days, plan_code=plan_code, effective_first=effective_first,
            curr_comment=curr_comment, comment_updated=comment_updated,
            bot_id=getattr(getattr(context, "bot", None), "id", None),
        )
        await update.message.reply_text(msg_out, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

    elif intent == "deliverable":
        links = await asyncio.to_thread(xui_manager.get_client_links, email)
        sub_id = client_info.get("sub_id", "")
        sub_url = xui_manager.get_sub_url(sub_id)
        qr_needed = params.get("qr", False)
        conf_qr = params.get("conf_qr", False)
        
        on_first = modes["first_connect_mode"]
        pending_res = database.get_pending_reservation(account_code)
        keyboard = _build_account_keyboard(account_code, user_id=user.id, is_enabled=is_enabled, role=role, pending_res=pending_res)
        
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
            
        on_first = modes["first_connect_mode"]
        keyboard = _build_account_keyboard(account_code, user_id=user.id, is_enabled=is_enabled, role=role, pending_res=pending_res)
        await update.message.reply_text(status_text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

# --- Interactive Callback Handler ---

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    thread_id = update.effective_message.message_thread_id if update.effective_message else None
    
    role, rep = _persona_aware_role(user, chat, thread_id)
    if role == "unauthorized_group":
        await query.answer("⛔️ دسترسی غیرمجاز در این تاپیک.", show_alert=True)
        return

    if database.is_user_blocked(user.id):
        await query.answer("⛔️ دسترسی شما به این بات مسدود شده است.", show_alert=True)
        return
    data = query.data or ""
    # A receipt is delivered as a PHOTO into the finance topic of the supergroup,
    # so the root operator must be able to settle it right there. Only the
    # receipt-scoped callbacks are admitted outside the private chat; every other
    # admin surface still requires the DM.
    if (role == "super_admin" and chat.type != "private"
            and not _may_use_admin_callback_in_group(user, chat, thread_id, data)):
        await query.answer(
            "⛔️ عملیات مدیریت کل فقط در گفت‌وگوی خصوصی ربات مجاز است.",
            show_alert=True
        )
        return
    # Leaving a workflow (menu/home/back/persona) or entering a new one must drop
    # every pending draft in both state stores, or the previous module steals the
    # next text message the customer sends.
    if _is_navigation_callback(data) or _is_workflow_start_callback(data):
        _reset_all_conversation_states(user.id, _bot_id_of(context), context)
    if data.startswith("v52:"):
        await _dispatch_v52_callback(update, context, role=role, rep=rep)
        return
    # The 360-degree profile is a legacy-surface command centre reachable from
    # tickets, receipts, locked transactions and the user/rep lists. It renders
    # through message_edit, so opening it from a receipt PHOTO works.
    if data.startswith("p360:"):
        await profile_360.handle_profile_callback(update, context, role=role, rep=rep)
        return
    v52_enabled = False
    try:
        v52_enabled = str(database.get_setting("v52_ui_enabled", "0")).strip().lower() in {"1", "true", "yes", "on", "enabled"}
    except Exception:
        pass
    if v52_enabled and (data.startswith("reset_sub:") or data.startswith("confirm_reset_sub:") or data.startswith("do_reset_sub:")):
        await _show_legacy_reset_preview(update, context, role, rep)
        return

    # Admin views and mutations can expose financial/personal records; they are
    # restricted to the bot's private chat even when the super-admin ID is used
    # from an otherwise authorized group topic.
    admin_callback = (
        data.startswith("admin:")
        or data.startswith("admin_approve_rcp_")
        or data.startswith("admin_reject_rcp_")
        or data.startswith("admin_detail_rcp_")
    )
    if (admin_callback and (not chat or chat.type != "private")
            and not _may_use_admin_callback_in_group(user, chat, thread_id, data)):
        await query.answer(
            "⛔️ عملیات پنل مدیریت فقط در گفت‌وگوی خصوصی ربات مجاز است.",
            show_alert=True
        )
        return

    # A settled receipt's status chip is inert by design: pressing it must never
    # re-run the review or query the panel again.
    if data == "noop":
        await query.answer("این فیش قبلاً تعیین تکلیف شده است.")
        return

    # Admin Receipt Verification Callbacks
    if data.startswith("admin_approve_rcp_"):
        if _base_role_of(user, chat, thread_id)[0] != "super_admin":
            await query.answer("⛔️ این عملیات فقط برای ادمین کل سیستم مجاز است.", show_alert=True)
            return
        raw_id = data.replace("admin_approve_rcp_", "")
        target_rcp = int(raw_id) if raw_id.isdigit() else raw_id
        try:
            rcp = database.approve_payment_receipt(target_rcp, user.id, user.full_name or "Admin")
            if rcp.get("status") != "approved":
                status = rcp.get("status", "unknown")
                await _settle_receipt_message(query, rcp, status, user)
                await query.answer(
                    f"⚠️ این فیش قبلاً بررسی شده است؛ وضعیت فعلی: {status}",
                    show_alert=True
                )
                return
            await _settle_receipt_message(query, rcp, "approved", user)
            await query.answer(f"✅ فیش {rcp['tracking_code']} تایید شد.")
            
            try:
                role_fa = "نماینده" if rcp["role"] == "rep" else "کاربر"
                notify_txt = (
                    f"🎉 **تایید فیش پرداختی ({role_fa}):**\n\n"
                    f"• کد رهگیری: `{rcp['tracking_code']}`\n"
                    f"• مبلغ: **{rcp['amount_toman']:,} تومان**\n"
                    f"• وضعیت: **تایید شد و حساب شما شارژ/تسویه گردید.**"
                )
                await context.bot.send_message(
                    chat_id=rcp["user_id"], text=notify_txt, parse_mode=ParseMode.MARKDOWN,
                    reply_markup=build_actor_navigation_keyboard(
                        "rep" if rcp["role"] == "rep" else "end_user"),
                )
            except Exception as e:
                logger.error(f"Failed to notify user of receipt approval: {e}")
        except Exception as e:
            await query.answer(f"❌ خطا در تایید: {e}", show_alert=True)
        return

    if data.startswith("admin_reject_rcp_"):
        if _base_role_of(user, chat, thread_id)[0] != "super_admin":
            await query.answer("⛔️ این عملیات فقط برای ادمین کل سیستم مجاز است.", show_alert=True)
            return
        raw_id = data.replace("admin_reject_rcp_", "")
        target_rcp = int(raw_id) if raw_id.isdigit() else raw_id
        # First check if receipt is still pending
        rcp = database.get_payment_receipt(target_rcp)
        if not rcp:
            await query.answer("❌ فیش یافت نشد.", show_alert=True)
            return
        if rcp.get("status") != "pending":
            await query.answer(f"⚠️ این فیش قبلاً بررسی شده است؛ وضعیت فعلی: {rcp.get('status', 'نامشخص')}", show_alert=True)
            await _settle_receipt_message(query, rcp, rcp["status"], user)
            return
        # Store pending rejection state and ask for reason
        context.user_data["admin_state"] = {
            "action": "receipt_reject_reason",
            "receipt_id": target_rcp,
            "receipt_code": rcp.get("tracking_code", "?"),
            "ts": time.time(),
            "return_callback": f"admin_detail_rcp_{raw_id}",
        }
        await query.edit_message_text(
            "❌ **رد فیش**\n\n"
            f"• کد رهگیری: `{rcp.get('tracking_code', '?')}`\n"
            f"• مبلغ: **{int(rcp.get('amount_toman', 0)):,} تومان**\n\n"
            "لطفاً دلیل رد فیش را در پیام بعدی ارسال کنید.\n"
            "مثال: تصویر ناخواناست، مبلغ مغایرت دارد، شماره کارت اشتباه است\n\n"
            "💡 برای انصراف، «لغو» را ارسال کنید.",
            parse_mode=ParseMode.MARKDOWN,
        )
        await query.answer("لطفاً دلیل رد را ارسال کنید.")
        return

    if data.startswith("admin_detail_rcp_"):
        if _base_role_of(user, chat, thread_id)[0] != "super_admin":
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
        note_txt = html.escape(str(rcp.get("receipt_note") or "ندارد")[:400])
        text = (
            f"🧾 <b>شناسنامه و جزئیات فیش پرداختی:</b>\n\n"
            f"• کد پیگیری: <code>{html.escape(str(rcp['tracking_code']))}</code>\n"
            f"• نقش: <b>{html.escape(role_fa)}</b>\n"
            f"• شناسه کاربر: <code>{int(rcp['user_id'])}</code>\n"
            f"• مبلغ واریزی: <b>{int(rcp['amount_toman']):,} تومان</b>\n"
            f"• وضعیت فعلی: <b>{html.escape(status_fa)}</b>\n"
            f"• تاریخ ارسال: <code>{html.escape(str(rcp['created_at'])[:19])}</code>\n"
            f"• توضیحات فرستنده: <i>{note_txt}</i>\n"
        )
        if rcp["status"] == "pending":
            btns = [
                [
                    InlineKeyboardButton("✅ تایید فیش", callback_data=f"admin_approve_rcp_{rcp['id']}"),
                    InlineKeyboardButton("❌ رد فیش", callback_data=f"admin_reject_rcp_{rcp['id']}")
                ],
                [InlineKeyboardButton(
                    "👤 مشاهده پروفایل کاربر",
                    callback_data=f"admin:user_detail:{int(rcp['user_id'])}:rcp:{int(rcp['id'])}")],
            ]
        else:
            btns = [
                [InlineKeyboardButton(status_fa, callback_data="noop")],
                [InlineKeyboardButton(
                    "👤 مشاهده پروفایل کاربر",
                    callback_data=f"admin:user_detail:{int(rcp['user_id'])}:rcp:{int(rcp['id'])}")],
                [InlineKeyboardButton("👑 بازگشت به میز مدیر کل", callback_data="admin:main")],
            ]
        await finance_manager.reply_with_receipt_detail(
            query.message,
            rcp,
            text,
            reply_markup=InlineKeyboardMarkup(btns),
        )
        await query.answer()
        return

    if data.startswith("send_receipt_"):
        tr_code = data.replace("send_receipt_", "")
        if role not in ("end_user", "rep") or (role == "rep" and not rep):
            await query.answer("⛔️ بارگذاری فیش فقط از مسیر کیف پول نقش معتبر مجاز است.", show_alert=True)
            return
        receipt = database.get_payment_receipt(tr_code)
        if (
            not receipt or int(receipt["user_id"]) != int(user.id)
            or receipt["status"] != "pending"
            or receipt.get("role") != role
            or (
                role == "rep"
                and int(receipt.get("rep_id") or -1) != int(rep["id"])
            )
        ):
            await query.answer("⛔️ این فیش متعلق به نقش/حساب فعلی شما نیست یا دیگر در انتظار ارسال نیست.", show_alert=True)
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
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_finance_fsm_keyboard(role),
        )
        return

    if data in ("wallet_charge_start", "rep_deposit_start", "user_deposit_start"):
        expected_role = "rep" if data == "rep_deposit_start" else "end_user"
        if role != expected_role or (expected_role == "rep" and not rep):
            await query.answer(
                "⛔️ این دکمه شارژ متعلق به نقش دیگری است؛ از پنل حساب فعلی خود اقدام کنید.",
                show_alert=True
            )
            return
        await _cleanup_deposit_amount_prompt(context, chat.id)
        context.user_data["fsm_state"] = finance_manager.STATE_AWAITING_RECEIPT_AMOUNT
        context.user_data["deposit_role"] = expected_role
        await query.answer("مبلغ را وارد کنید.")
        prompt_message = await query.message.reply_text(
            "💳 **لطفاً مبلغ مورد نظر جهت پرداخت را به تومان وارد کنید:**\n"
            "مثال: `500000`",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_finance_fsm_keyboard(role),
        )
        context.user_data["deposit_form_state"] = {
            "prompt_msg_id": getattr(prompt_message, "message_id", None)
        }
        return

    if data == "cancel_payment" or data.startswith("cancel_payment:"):
        # New buttons carry their exact receipt code. The legacy static callback
        # may cancel only the pre-receipt amount-entry state; it must not guess
        # which persisted receipt an old/stale message refers to.
        target_code = data.split(":", 1)[1] if data.startswith("cancel_payment:") else None
        active_code = context.user_data.get("active_receipt_code")

        if target_code:
            if role not in ("end_user", "rep") or (role == "rep" and not rep):
                await query.answer(
                    "⛔️ این درخواست پرداخت متعلق به نقش فعلی شما نیست.",
                    show_alert=True
                )
                return
            stored_receipt = database.get_payment_receipt(target_code)
            if (
                not stored_receipt
                or int(stored_receipt.get("user_id") or -1) != int(user.id)
                or stored_receipt.get("role") != role
                or (
                    role == "rep"
                    and int(stored_receipt.get("rep_id") or -1) != int(rep["id"])
                )
            ):
                await query.answer(
                    "⛔️ این درخواست پرداخت متعلق به نقش/حساب فعلی شما نیست.",
                    show_alert=True
                )
                return
            try:
                receipt = database.cancel_payment_receipt(target_code, user.id)
            except (ValueError, PermissionError):
                await query.answer(
                    "⛔️ این درخواست پرداخت یافت نشد یا متعلق به شما نیست.",
                    show_alert=True
                )
                return

            if receipt.get("status") == "cancelled":
                if active_code == target_code:
                    for key in ("fsm_state", "active_receipt_code", "deposit_amount", "deposit_role"):
                        context.user_data.pop(key, None)
                await query.answer("درخواست پرداخت لغو و در سامانه ثبت شد.")
                await safe_edit_message(query, "❌ درخواست پرداخت لغو شد.")
                return

            if receipt.get("status") == "pending" and receipt.get("receipt_photo_id"):
                await query.answer(
                    "این فیش قبلاً ارسال شده و برای بررسی مدیریت قابل لغو نیست.",
                    show_alert=True
                )
                return

            current_status = receipt.get("status", "unknown")
            await query.answer(
                f"این درخواست دیگر قابل لغو نیست؛ وضعیت فعلی: {current_status}",
                show_alert=True
            )
            return

        if active_code:
            await query.answer(
                "این دکمه قدیمی است و شناسه فیش را ندارد. از پیام جدید پرداخت استفاده کنید.",
                show_alert=True
            )
            return

        if context.user_data.get("fsm_state") == finance_manager.STATE_AWAITING_RECEIPT_AMOUNT:
            expected_role = context.user_data.get("deposit_role") or role
            if role not in ("end_user", "rep") or expected_role != role or (role == "rep" and not rep):
                await query.answer(
                    "⛔️ این فرایند شارژ متعلق به نقش دیگری است.",
                    show_alert=True
                )
                return
            await _cleanup_deposit_amount_prompt(context, chat.id)
            for key in ("fsm_state", "deposit_amount", "deposit_role"):
                context.user_data.pop(key, None)
            await query.answer("فرایند ورود مبلغ لغو شد.")
            await safe_edit_message(query, "❌ فرایند پرداخت لغو شد.")
            return

        await query.answer("درخواست پرداخت قابل شناسایی نیست؛ وضعیت فیش تغییر نکرد.", show_alert=True)
        return

    parts = data.split(":")
    action = parts[0]

    # Old unprefixed account-card reservation callbacks executed immediately.
    # Representatives must use the new rep:confirm_* → rep:do_* flow instead.
    if role in ("rep", "super_admin") and action in ("force_res", "cancel_res"):
        await query.answer(
            "⚠️ این دکمه رزرو قدیمی است و اجرا نشد؛ کارت اکانت را تازه کنید و تأیید دو مرحله‌ای را انجام دهید.",
            show_alert=True
        )
        return

    renewal_confirmation_id = None
    # Reserve callbacks carry <reservation_id>:<account_email>; other account
    # callbacks carry the account identifier in parts[1].
    account_code = (
        parts[2] if action in ("force_res", "cancel_res") and len(parts) > 2
        else (parts[1] if len(parts) > 1 else "")
    )

    # One-time confirmation for a quick-renew action. The token is owner-bound
    # and consumed before the existing renewal/reservation pipeline is entered.
    if action == "qrenew_confirm":
        if role not in ("super_admin", "rep"):
            await query.answer("⛔️ تمدید سریع فقط برای نماینده و مدیریت مجاز است.", show_alert=True)
            return
        intent_id = parts[1] if len(parts) > 1 else ""
        intent = database.get_account_renewal_confirmation(intent_id, user.id)
        if not intent:
            await query.answer("⛔️ درخواست تمدید یافت نشد یا متعلق به شما نیست.", show_alert=True)
            return
        if intent["actor_role"] != role:
            await query.answer("⛔️ سطح دسترسی این درخواست تغییر کرده است؛ از کارت اکانت دوباره اقدام کنید.", show_alert=True)
            return

        if role == "rep":
            if not rep or int(intent.get("rep_id") or -1) != int(rep["id"]):
                await query.answer("⛔️ این درخواست متعلق به محدوده نمایندگی شما نیست.", show_alert=True)
                return
            scoped = database.is_account_in_rep_scope(
                rep["id"], intent.get("account_code") or intent.get("account_email") or ""
            )
            if (
                not scoped
                or str(scoped.get("email", "")).lower() != str(intent["account_email"]).lower()
            ):
                await query.answer("⛔️ مالکیت یا محدوده اکانت برای این تمدید قابل تأیید نیست.", show_alert=True)
                return

        if intent.get("status") != "pending":
            await query.answer(
                "⛔️ این تأیید تمدید قبلاً وارد مرحله پردازش شده یا تعیین تکلیف شده است.",
                show_alert=True
            )
            return

        # Do not consume the token here. The downstream database transaction
        # claims it atomically with a rep renewal intent, reservation, or admin lock.
        renewal_confirmation_id = intent_id
        confirmed = intent
        account_code = confirmed["account_email"]
        parts = ["qrenew_execute", account_code, confirmed["plan_code"]]
        action = "qrenew_execute"

    # 0. Admin Navigation
    #
    # The guard reads the actor's PERMANENT role, never the resolved persona: a
    # super admin who switched into the representative or end-user workspace must
    # still reach the admin surfaces (a receipt's profile button used to dead-end
    # with «دسترسی فقط برای ادمین کل سیستم مجاز است»). A representative is admitted
    # only for the scoped profile view of their own downstream users.
    if action == "admin":
        base_role, base_rep = _base_role_of(user, chat, thread_id)
        if base_role == "super_admin":
            await admin_panel.handle_admin_callbacks(update, context)
            return
        if base_role == "rep" and _rep_may_open_profile(data, base_rep):
            await admin_panel.handle_admin_callbacks(update, context)
            return
        await query.answer("⛔️ دسترسی فقط برای ادمین کل سیستم مجاز است.", show_alert=True)
        return

    # 0.1 Rep Dashboard Navigation
    if action == "rep":
        if role == "rep" and rep:
            await rep_panel.handle_rep_callbacks(update, context, rep)
            return
        await query.answer(
            "⛔️ برای ورود به پنل نمایندگی باید در تاپیک اختصاصی همان نماینده باشید.",
            show_alert=True
        )
        return

    # 0.2 End-User Portal Navigation
    if action == "user":
        # A super-admin or representative whose active persona is the end-user view
        # must be able to drive the portal; only genuinely foreign roles are denied.
        if not user_portal.user_portal_access(role):
            await query.answer(
                "⛔️ پورتال کاربری فقط برای نقش کاربر نهایی مجاز است.",
                show_alert=True
            )
            return
        await user_portal.handle_user_callbacks(update, context)
        return

    # Strict End-User Isolation in Callbacks
    if role == "end_user":
        if action in ("suspend", "activate", "qrenew", "qrenew_confirm", "qrenew_execute", "force_res", "cancel_res", "toggle_mode", "set_mode", "set_rollover", "more_plans"):
            await query.answer("⛔️ این عملیات فقط برای نماینده و مدیریت مجاز است.", show_alert=True)
            return

        owned_account = database.get_user_owned_account(user.id, account_code)
        if not owned_account:
            await query.answer("⛔️ شما به این اکانت دسترسی ندارید. ابتدا با دستور /link آن را ثبت کنید.", show_alert=True)
            return
        account_code = owned_account["email"]
    
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
        acc_rec = database.is_account_in_rep_scope(rep["id"], account_code) if rep else None
        is_en = bool(acc_rec.get("enable", 1)) if acc_rec else True
        keyboard = _build_account_keyboard(account_code, user_id=user.id, is_enabled=is_en, role=role)
        try:
            await query.edit_message_reply_markup(reply_markup=keyboard)
        except BadRequest:
            pass
        await query.answer("↩️ بازگشت به منوی اصلی")
        return

    # 3. Legacy toggle callback is read-only: it carries no desired state.
    if action == "toggle_mode":
        await query.answer(
            "⚠️ این دکمه قدیمی است و تغییری اعمال نشد؛ کارت اکانت را تازه کنید.",
            show_alert=True
        )
        return

    # 3. Explicit first-connect mode: persist on the account and retain legacy actor sync.
    if action == "set_mode":
        desired_mode = parts[2] if len(parts) > 2 else ""
        if desired_mode not in ("0", "1"):
            await query.answer("⛔️ حالت درخواستی معتبر نیست؛ کارت اکانت را تازه کنید.", show_alert=True)
            return
        if not _can_manage_account_policy(role, rep, account_code):
            await query.answer("⛔️ این اکانت در محدوده مجاز شما نیست.", show_alert=True)
            return
        updated = database.set_account_first_connect_mode(
            account_code, desired_mode == "1", actor_id=user.id
        )
        if not updated:
            await query.answer("❌ ذخیره حالت شروع برای این اکانت انجام نشد.", show_alert=True)
            return
        acc_rec = database.is_account_in_rep_scope(rep["id"], account_code) if rep else None
        is_en = bool(acc_rec.get("enable", 1)) if acc_rec else True
        keyboard = _build_account_keyboard(
            account_code, user_id=user.id, is_enabled=is_en, role=role
        )
        try:
            await query.edit_message_reply_markup(reply_markup=keyboard)
        except BadRequest:
            pass
        mode_label = "شروع با اولین اتصال" if desired_mode == "1" else "شروع از زمان تمدید"
        await query.answer(f"⚙️ حالت شروع اکانت روی «{mode_label}» تنظیم شد.", show_alert=True)
        return

    # 3.1 Independent per-account rollover toggle.
    if action == "set_rollover":
        desired_rollover = parts[2] if len(parts) > 2 else ""
        if desired_rollover not in ("0", "1"):
            await query.answer("⛔️ وضعیت رول‌اوور معتبر نیست؛ کارت اکانت را تازه کنید.", show_alert=True)
            return
        if not _can_manage_account_policy(role, rep, account_code):
            await query.answer("⛔️ این اکانت در محدوده مجاز شما نیست.", show_alert=True)
            return
        updated = database.set_account_rollover_mode(
            account_code, desired_rollover == "1"
        )
        if not updated:
            await query.answer("❌ ذخیره وضعیت رول‌اوور انجام نشد.", show_alert=True)
            return
        acc_rec = database.is_account_in_rep_scope(rep["id"], account_code) if rep else None
        is_en = bool(acc_rec.get("enable", 1)) if acc_rec else True
        keyboard = _build_account_keyboard(
            account_code, user_id=user.id, is_enabled=is_en, role=role
        )
        try:
            await query.edit_message_reply_markup(reply_markup=keyboard)
        except BadRequest:
            pass
        rollover_label = "فعال" if desired_rollover == "1" else "غیرفعال"
        await query.answer(f"⚙️ رول‌اوور اکانت «{rollover_label}» شد.", show_alert=True)
        return

    # Account Verification
    is_super_admin = (role == "super_admin")
    scope_flt = "all" if is_super_admin else (rep["scope_filter"] if rep else "all")
    by_code_only = False if is_super_admin else True

    acc_record = None
    if is_super_admin:
        with database.get_db() as conn:
            row = conn.cursor().execute(
                "SELECT * FROM accounts WHERE LOWER(account_code) = ? OR LOWER(email) = ? LIMIT 1",
                (account_code.lower(), account_code.lower())
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
    modes = database.get_account_renewal_modes(account_code, user.id)
    on_first = modes["first_connect_mode"]

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
        keyboard = _build_account_keyboard(account_code, user_id=user.id, is_enabled=is_enabled, role=role, pending_res=pending_res)
        await safe_edit_message(query, status_text, reply_markup=keyboard, delete_transient_photo=True)
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
            keyboard = _build_account_keyboard(account_code, user_id=user.id, is_enabled=False, role=role, pending_res=pending_res)
            await safe_edit_message(query, text_out, reply_markup=keyboard, delete_transient_photo=True)
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
            keyboard = _build_account_keyboard(account_code, user_id=user.id, is_enabled=True, role=role, pending_res=pending_res)
            await safe_edit_message(query, text_out, reply_markup=keyboard, delete_transient_photo=True)
            database.log_audit(
                rep_id=target_rep_id, account_code=account_code, email=email,
                action="activate_account", actor_id=user.id, actor_name=user.full_name or user.username,
                details={"rem_gb": rem_gb}, result_status="success"
            )
            await query.answer("✅ اتصال اکانت مجدداً برقرار گردید.")
            await asyncio.to_thread(send_admin_log, f"✅ برقراری اتصال اکانت `{email}` از دکمه شیشه‌ای")

    # 7. Quick Renewal button opens an expiring one-time confirmation.
    elif action == "qrenew":
        if role not in ("super_admin", "rep"):
            await query.answer("⛔️ عدم دسترسی", show_alert=True)
            return

        plan_code = parts[2] if len(parts) > 2 else "20gb_30d"
        plan = database.get_plan(plan_code)
        if not plan or not plan.get("is_active", 1):
            await query.answer("❌ پلن مورد نظر تعریف نشده یا غیرفعال است.", show_alert=True)
            return
        try:
            confirmation_id = database.create_account_renewal_confirmation(
                actor_id=user.id,
                actor_role=role,
                rep_id=rep["id"] if rep else None,
                account_code=acc_record.get("account_code") or email,
                account_email=email,
                plan_code=plan["plan_code"],
                plan_name=plan["name"],
                gb=plan["gb"],
                days=plan["days"],
                amount=plan["rep_price"],
            )
        except ValueError as exc:
            await query.answer(f"❌ ایجاد تأیید تمدید ممکن نشد: {exc}", show_alert=True)
            return

        if role == "rep" and acc_record.get("rep_id") is not None:
            charge_line = f"• هزینه نمایندگی: **{plan['rep_price']:,} تومان**\n"
        else:
            charge_line = "• هزینه: **بدون صورت‌حساب نمایندگی (مدیریت کل)**\n"
        confirmation_text = (
            f"⚡️ **تأیید تمدید سرویس {email}**\n\n"
            f"• بسته: **{plan['name']}** ({plan['gb']} گیگابایت / {plan['days']} روز)\n"
            f"{charge_line}"
            "این تأیید تا ۱۰ دقیقه معتبر است و فقط یک بار قابل اجراست.\n"
            "در صورت تمایل، عملیات تمدید یا ثبت رزرو را تأیید کنید."
        )
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton(
                "✅ تأیید تمدید",
                callback_data=f"qrenew_confirm:{confirmation_id}"
            )],
            [InlineKeyboardButton(
                "❌ انصراف",
                callback_data=f"main_menu:{email}"
            )]
        ])
        await safe_edit_message(query, confirmation_text, reply_markup=keyboard, delete_transient_photo=True)
        await query.answer("تأیید یک‌بارمصرف تمدید ایجاد شد.")

    # qrenew_confirm has consumed its one-time token before reaching this branch.
    # The existing direct-vs-reserve decision and accounting pipeline are preserved.
    elif action == "qrenew_execute":
        if role not in ("super_admin", "rep"):
            await query.answer("⛔️ عدم دسترسی", show_alert=True)
            return
        if not renewal_confirmation_id:
            await query.answer(
                "⛔️ این دکمه قدیمی یا فاقد تأیید یک‌بارمصرف معتبر است؛ از کارت اکانت دوباره اقدام کنید.",
                show_alert=True
            )
            return
        plan_code = parts[2] if len(parts) > 2 else ""
        plan = database.get_plan(plan_code)
        if not plan:
            await query.answer("❌ پلن مورد نظر دیگر در دسترس نیست.", show_alert=True)
            return

        ok, msg_out, keyboard = await process_account_renewal(
            user=user, role=role, rep=rep, acc_record=acc_record, client_info=client_info,
            gb=plan["gb"], days=plan["days"], plan_code=plan_code, effective_first=on_first,
            curr_comment=curr_comment, comment_updated=False,
            confirmation_id=renewal_confirmation_id,
            bot_id=getattr(getattr(context, "bot", None), "id", None),
        )
        await safe_edit_message(query, msg_out, reply_markup=keyboard, delete_transient_photo=True)
        await query.answer("عملیات تمدید / صف رزرو پردازش شد.")

    # 8. Force Activate Reserved Package
    elif action == "force_res":
        res_id = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        res_item = database.get_reservation_by_id(res_id)
        if (
            not res_item or res_item.get("status") != "pending"
            or str(res_item.get("email", "")).lower() != str(email).lower()
            or (rep and res_item.get("rep_id") != rep.get("id"))
        ):
            await query.answer("⚠️ رزرو در انتظار متعلق به اکانت یا نمایندگی شما نیست.", show_alert=True)
            return

        lock_key = f"reservation-activation:{res_id}"
        lock_token = database.acquire_operation_lock(lock_key, ttl_seconds=None)
        if not lock_token:
            await query.answer("⏳ این رزرو در حال فعال‌سازی است یا نتیجه قبلی نیازمند بررسی است.", show_alert=True)
            return
        # Re-check after acquiring the lease so a concurrent cancellation cannot race the X-UI call.
        res_item = database.get_reservation_by_id(res_id)
        if (
            not res_item or res_item.get("status") != "pending"
            or str(res_item.get("email", "")).lower() != str(email).lower()
            or (rep and res_item.get("rep_id") != rep.get("id"))
        ):
            database.release_operation_lock(lock_key, lock_token)
            await query.answer("⚠️ وضعیت رزرو تغییر کرده است؛ عملیات متوقف شد.", show_alert=True)
            return

        res_email = res_item["email"]
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
            await query.answer("⚠️ پاسخ پنل نامشخص است؛ رزرو و قفل تطبیق حفظ شدند.", show_alert=True)
            return

        if _xui_operation_failed(ren_res):
            database.release_operation_lock(lock_key, lock_token)
            await query.answer("❌ پنل فعال‌سازی رزرو را رد کرد؛ رزرو در صف باقی ماند.", show_alert=True)
            return
        if not _xui_operation_succeeded(ren_res):
            await query.answer("⚠️ پاسخ پنل مبهم است؛ رزرو و قفل تطبیق حفظ شدند.", show_alert=True)
            return

        try:
            activated = database.activate_reservation(res_id, actor_id=user.id, actor_name=user.full_name or user.username)
        except Exception:
            logger.exception("Reservation persistence failed after confirmed panel renewal: %s", res_id)
            await query.answer("⚠️ پنل بسته را فعال کرد اما ثبت محلی انجام نشد؛ قفل برای بررسی محفوظ است.", show_alert=True)
            return
        if not activated:
            await query.answer("⚠️ پنل عملیات را انجام داد اما ثبت فعال‌سازی ناموفق بود؛ قفل تطبیق حفظ شد.", show_alert=True)
            return
        database.release_operation_lock(lock_key, lock_token)
        report_text = ren_res.get("report", "")
        report_text = f"🚀 **بسته رزرو به صورت فوری فعال شد (اعتبار قبلی صفر و دوره جدید آغاز گردید):**\n\n" + report_text
        keyboard = _build_account_keyboard(account_code, user_id=user.id, is_enabled=True, role=role, pending_res=None)
        await safe_edit_message(query, report_text, reply_markup=keyboard, delete_transient_photo=True)
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
        keyboard = _build_account_keyboard(account_code, user_id=user.id, is_enabled=is_enabled, role=role, pending_res=None)
        await safe_edit_message(query, msg_out, reply_markup=keyboard, delete_transient_photo=True)
        await query.answer("🗑 بسته رزرو لغو و وجه آن عودت داده شد.", show_alert=True)
        await asyncio.to_thread(
            send_admin_log,
            f"🗑 **لغو بسته رزرو و عودت وجه برای اکانت:** `{res_data['email']}` توسط {user.full_name}\n"
            f"• مبلغ عودت: {res_data['charge_amount']:,} تومان | مانده بدهی: {new_bal:,} تومان"
        )

    # 8. Subscription Link Only
    elif action == "deliv_sub":
        await query.answer()
        sub_id = client_info.get("sub_id", "")
        sub_url = xui_manager.get_sub_url(sub_id)
        msg_out = f"🔗 **لینک سابسکریپشن اکانت `{account_code}`:**\n\n`{sub_url}`"
        keyboard = _build_account_keyboard(account_code, user_id=user.id, is_enabled=is_enabled, role=role)
        await safe_edit_message(query, msg_out, reply_markup=keyboard, delete_transient_photo=True)

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
        keyboard = _build_account_keyboard(account_code, user_id=user.id, is_enabled=is_enabled, role=role)
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
    elif action in ("deliv_cfg", "deliv_conf_qr"):
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
        keyboard = _build_account_keyboard(account_code, user_id=user.id, is_enabled=is_enabled, role=role)
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

    # 11. Reset SubId — share the verified reset/persistence flow with the user portal.
    elif action == "reset_sub":
        lock_key = f"subscription-reset:{str(account_code).lower()}"
        lock_token = database.acquire_operation_lock(lock_key, ttl_seconds=None)
        if not lock_token:
            await query.answer("⏳ بازنشانی لینک این اکانت در حال انجام یا نیازمند تطبیق است؛ دوباره تلاش نکنید.", show_alert=True)
            return
        try:
            reset_result = await asyncio.to_thread(
                sub_manager.reset_client_subscription, xui_manager, email, scope="all"
            )
        except Exception:
            logger.exception("Account-card subscription reset outcome is unknown: %s", account_code)
            await query.answer(
                "⚠️ نتیجه بازنشانی پنل مشخص نیست؛ قفل برای بررسی مدیریت نگه داشته شد. فعلاً دوباره اقدام نکنید.",
                show_alert=True,
            )
            return

        if reset_result.get("status") == "unknown":
            await query.answer(
                "⚠️ وضعیت شناسه در پنل قابل تأیید نیست؛ قفل تطبیق حفظ شد. فعلاً دوباره تلاش نکنید.",
                show_alert=True,
            )
            return
        if reset_result.get("status") not in ("ok", "partial") or not reset_result.get("new_sub_id"):
            database.release_operation_lock(lock_key, lock_token)
            await query.answer(
                f"❌ خطا در بازنشانی لینک ساب: {reset_result.get('message', 'بازنشانی تأیید نشد')}",
                show_alert=True,
            )
            return

        try:
            database.upsert_account_record(
                rep_id=acc_record.get("rep_id"),
                email=email,
                account_code=account_code,
                sub_id=reset_result["new_sub_id"],
            )
        except Exception:
            logger.exception(
                "Subscription ID rotated remotely for %s but local persistence failed",
                email,
            )
            await query.answer(
                "⚠️ شناسه در پنل تغییر کرده اما ثبت محلی انجام نشد؛ قفل تطبیق حفظ شد و بررسی مدیریت لازم است.",
                show_alert=True,
            )
            return

        try:
            target_rep_id = acc_record.get("rep_id") or (rep["id"] if rep else None)
            database.log_audit(
                rep_id=target_rep_id, account_code=account_code, email=email,
                action="reset_sub_id", actor_id=user.id,
                actor_name=user.full_name or user.username,
                details={"new_sub_id": reset_result["new_sub_id"]},
                result_status="success",
            )
        except Exception:
            logger.exception("Subscription ID changed and persisted but audit write failed for %s", email)

        database.release_operation_lock(lock_key, lock_token)
        if reset_result.get("status") == "ok" and reset_result.get("new_sub_url"):
            out_text = (
                f"🔄 **لینک سابسکریپشن اکانت `{account_code}` با موفقیت بازنشانی شد.**\\n\\n"
                f"⚠️ لینک قبلی باطل گردید.\\n\\n"
                f"🔗 **لینک جدید سابسکریپشن:**\\n`{reset_result['new_sub_url']}`"
            )
            keyboard = _build_account_keyboard(
                account_code, user_id=user.id, is_enabled=is_enabled, role=role
            )
            await safe_edit_message(query, out_text, reply_markup=keyboard, delete_transient_photo=True)
            await query.answer("✅ لینک سابسکریپشن بازنشانی شد.")
            await asyncio.to_thread(
                send_admin_log, f"بازنشانی لینک ساب اکانت `{email}` توسط `{user.id}`", target_rep_id
            )
        else:
            await query.answer(
                "⚠️ شناسه جدید در پنل و دیتابیس ثبت شد، اما لینک عمومی ساخته نشد. تنظیمات دامنه را بررسی کنید.",
                show_alert=True,
            )

# --- Background Reserve Queue Watchdog Task ---


async def _watchdog_cycle(application: Application, *, now: int | None = None) -> dict:
    """One panel-scoped reservation sweep; all panel I/O occurs outside DB transactions."""
    import reservation_service
    import scheduler_service
    from renewal_calculator import calculate_renewal

    timestamp = int(time.time()) if now is None else int(now)
    trigger_gb = database.get_setting("reserve_trigger_gb", default=0.15, cast_type=float)
    pending = database.get_all_pending_reservations()
    result = {"read": 0, "triggered": 0, "activated": 0, "maintenance_skipped": 0,
              "unknown": 0, "failed": 0, "expiry_warnings": 0}
    period = str(timestamp // 300)

    for reservation in pending:
        reservation_id = int(reservation["id"])
        binding = database.get_account_panel_binding(
            account_id=reservation.get("account_id"),
            email=reservation.get("email"),
            account_code=reservation.get("account_code"),
        )
        if not binding or not binding.get("panel_id"):
            logger.warning("Watchdog skipped reservation %s: account/panel binding is ambiguous", reservation_id)
            continue

        try:
            adapter = database.get_panel_adapter_for_account(int(binding["id"]))
            lookup = await adapter.get_client(str(binding["email"]))
        except Exception:
            logger.exception("Watchdog panel read failed for reservation %s", reservation_id)
            continue
        if getattr(lookup, "state", None) != "found" or getattr(lookup, "client", None) is None:
            continue

        client = lookup.client
        result["read"] += 1
        if client.time_mode == "on_hold" or client.time_mode == "unknown" or client.quota_mode == "unknown":
            continue

        trigger = client.enabled is False
        reason = "غیرفعال بودن سرویس"
        if client.time_mode == "dated" and client.expires_at is not None and int(client.expires_at) <= timestamp:
            trigger, reason = True, "پایان اعتبار زمانی"
        if client.quota_mode == "finite" and client.quota_bytes is not None and client.used_bytes is not None:
            remaining_bytes = max(int(client.quota_bytes) - int(client.used_bytes), 0)
            if remaining_bytes <= int(float(trigger_gb) * (1024 ** 3)):
                trigger, reason = True, f"اتمام حجم (باقیمانده: {remaining_bytes / (1024 ** 3):.2f} GB)"
        if not trigger:
            continue
        result["triggered"] += 1

        # Read/reconciliation above is allowed in maintenance. No activation write
        # is attempted until the account's own panel is explicitly active.
        if str(binding.get("lifecycle") or "") != "active":
            result["maintenance_skipped"] += 1
            continue

        try:
            with database.get_db() as conn:
                claim = scheduler_service.claim_period_job(
                    conn, kind="reservation_watchdog",
                    target=f"{int(binding['panel_id'])}:{reservation_id}",
                    period=period, due_at=timestamp, now=timestamp,
                    lease_seconds=300,
                )
            if claim is None:
                continue
            with database.get_db() as conn:
                prepared = reservation_service.prepare_activation(
                    conn, int(binding["id"]), reservation_id, now=timestamp
                )
            if prepared is None:
                continue
        except Exception:
            logger.exception("Watchdog could not claim activation for reservation %s", reservation_id)
            result["failed"] += 1
            continue

        outcome = "unknown"
        try:
            # First-connect countdown cannot be represented by a verified adapter
            # target yet. Stop safely instead of silently changing its semantics.
            if bool(reservation.get("on_first_connect")):
                outcome = "definite_failure"
                logger.warning("Watchdog requires first-connect adapter evidence for reservation %s", reservation_id)
            else:
                target = calculate_renewal(
                    client,
                    {"quota_bytes": int(round(float(reservation["gb"]) * (1024 ** 3))),
                     "days": int(reservation["days"])},
                    "reset_volume_time",
                    timestamp,
                )
                lock_token = prepared["locks"][1]["token"]
                mutation = await adapter.renew_client(
                    str(binding["email"]), target,
                    f"reservation-{reservation_id}-{lock_token[:16]}",
                )
                state = getattr(mutation, "state", None)
                outcome = state if state in {"success", "definite_failure", "unknown"} else "unknown"
        except Exception:
            logger.exception("Watchdog activation result is unknown for reservation %s", reservation_id)
            outcome = "unknown"

        try:
            with database.get_db() as conn:
                finalized = reservation_service.finish_activation(conn, prepared, outcome=outcome)
        except Exception:
            # A failed local finalization after panel success remains locked for reconciliation.
            logger.exception("Watchdog could not finalize reservation %s", reservation_id)
            result["unknown"] += 1
            continue

        if outcome == "success" and finalized.get("state") == "success":
            result["activated"] += 1
            rep_info = database.get_rep_by_id(reservation.get("rep_id"))
            message = (
                f"🚀 **فعال‌سازی خودکار بسته رزرو (Watchdog):**\n\n"
                f"• اکانت: `{binding['email']}` (`{reservation.get('account_code')}`)\n"
                f"• نماینده: **{rep_info['name'] if rep_info else 'مدیریت کل'}**\n"
                f"• علت فعال‌سازی: **{reason}**\n"
                f"• بسته فعال‌شده: **{reservation.get('plan_name')}** "
                f"({reservation.get('gb')} گیگ / {reservation.get('days')} روز)"
            )
            await asyncio.to_thread(send_admin_log, message)
            if rep_info and rep_info.get("group_id") and rep_info.get("topic_id"):
                try:
                    await application.bot.send_message(
                        chat_id=rep_info["group_id"], message_thread_id=rep_info["topic_id"],
                        text=message, parse_mode=ParseMode.MARKDOWN,
                    )
                except Exception:
                    logger.exception("Watchdog representative notification failed for reservation %s", reservation_id)
        elif outcome == "unknown" or finalized.get("locks_retained"):
            result["unknown"] += 1
        else:
            result["failed"] += 1

    # Expiring services receive one early warning per Tehran day. The claim lives in
    # ``scheduled_jobs`` (kind ``expiry_warning``), so a restart cannot repeat a
    # warning and a missed cycle is not lost. A delivery failure is logged and never
    # retried against the same claim, because a duplicate warning is worse than a
    # missing one for a customer who has already been told.
    try:
        with database.get_db() as conn:
            warnings = scheduler_service.due_expiry_warnings(conn, now=timestamp)
        result["expiry_warnings"] = len(warnings)
        for warning in warnings:
            await _notify_expiry_warning(application, warning)
    except Exception:
        logger.exception("Watchdog expiry-warning sweep failed")
    return result


async def _notify_expiry_warning(application: Application, warning: dict) -> None:
    """Deliver one early expiry warning to the service owner and to the admin log."""
    days_left = int(warning.get("days_left") or 0)
    when = "امروز" if days_left <= 0 else f"تا {days_left} روز دیگر"
    label = warning.get("account_code") or warning.get("email") or warning.get("account_id")
    body = (
        f"⏳ **هشدار نزدیک شدن پایان اعتبار سرویس:**\n\n"
        f"• کد سرویس: {label}\n"
        f"• اعتبار: {when}\n\n"
        "برای جلوگیری از قطعی سرویس، از منوی «📱 سرویس‌های من» آن را تمدید کنید."
    )
    owner = warning.get("owner_tg_id")
    if owner:
        try:
            await application.bot.send_message(
                chat_id=int(owner), text=body, parse_mode=ParseMode.MARKDOWN)
        except Exception:
            logger.exception(
                "Expiry warning delivery failed for account %s", warning.get("account_id"))
    try:
        await asyncio.to_thread(
            send_admin_log,
            f"⏳ هشدار انقضای سرویس {label} ارسال شد (تا {days_left} روز).")
    except Exception:
        logger.exception("Expiry warning admin log failed for account %s",
                         warning.get("account_id"))


async def watchdog_worker(application: Application):
    logger.info("Starting panel-scoped reservation watchdog (300-second interval)...")
    while True:
        try:
            await _watchdog_cycle(application)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Watchdog cycle failed")
        await asyncio.sleep(300)

async def receipt_notification_worker(application: Application):
    """Retry durable admin receipt notifications independently of the X-UI watchdog."""
    logger.info("Starting durable receipt notification outbox worker...")
    while True:
        try:
            result = await process_receipt_notification_outbox(application.bot, limit=10)
            if result.get("attempted"):
                logger.info(
                    "Receipt notification worker attempted=%s sent=%s",
                    result.get("attempted"), result.get("sent")
                )
        except Exception:
            logger.exception("Receipt notification worker cycle failed")
        await asyncio.sleep(30)



async def _sync_v52_forum_topics(bot_api, bot_id: int, group_chat_id: int, *, owner_key: str = "root") -> dict:
    """Bridge synchronous forum_manager calls to the async Telegram Bot API."""
    import forum_manager
    loop = asyncio.get_running_loop()

    class TelegramSyncBridge:
        def _call(self, method_name, **kwargs):
            future = asyncio.run_coroutine_threadsafe(getattr(bot_api, method_name)(**kwargs), loop)
            return future.result(timeout=45)
        def get_chat(self, **kwargs): return self._call("get_chat", **kwargs)
        def get_me(self, **kwargs): return self._call("get_me", **kwargs)
        def get_chat_member(self, **kwargs): return self._call("get_chat_member", **kwargs)
        def create_forum_topic(self, **kwargs): return self._call("create_forum_topic", **kwargs)
        def edit_forum_topic(self, **kwargs): return self._call("edit_forum_topic", **kwargs)

    def sync():
        api = TelegramSyncBridge()
        permission = forum_manager.check_forum_permissions(api, int(group_chat_id), bot_id=int(bot_id))
        if not permission.get("allowed"):
            return {"state": "skipped", "reason": permission.get("reason"), "active": 0}
        with database.get_db() as conn:
            result = forum_manager.provision_standard_topics(
                conn, api, bot_id=int(bot_id), group_chat_id=int(group_chat_id),
                now=int(time.time()), owner_key=owner_key,
            )
        # Persist the real message_thread_id so log/receipt/backup routing targets
        # the topic Telegram actually created instead of a hard-coded guess.
        written = database.apply_forum_topic_settings(result.get("settings") or {})
        return {
            "state": "completed",
            "topics": result.get("total", 0),
            "active": result.get("active", 0),
            "settings_written": written,
            "thread_ids": result.get("settings") or {},
            "outcomes": result.get("outcomes") or [],
        }

    return await asyncio.to_thread(sync)


async def _bootstrap_v52_startup(application: Application) -> None:
    """Verify Telegram getMe then seed the v5.2 installation and synchronize report topics."""
    if not config.read_v52_settings().migrations_enabled:
        return
    me = await application.bot.get_me()
    result = await asyncio.to_thread(
        database.bootstrap_v52_installation,
        bot_id=int(me.id), username=str(getattr(me, "username", "") or ""), token=BOT_TOKEN,
    )
    logger.info("v5.2 bootstrap completed: %s", result)
    main_group_id = database.get_main_group_id()
    if main_group_id:
        try:
            topic_result = await _sync_v52_forum_topics(
                application.bot, int(me.id), int(main_group_id), owner_key="root",
            )
            logger.info("v5.2 startup forum synchronization: %s", topic_result)
        except Exception:
            logger.exception("Forum synchronization deferred; the sales bot will continue startup")


def main():
    database.init_db()
    if _runtime_flag("v52_multi_bot_enabled"):
        startup_sync = database.sync_all_active_reps_detailed()
        if startup_sync.get("status") != "success":
            logger.error("Initial panel-scoped database sync failed: %s", startup_sync.get("message"))
        elif startup_sync.get("skipped"):
            logger.warning("Panel-scoped sync skipped %s panel(s): %s",
                           len(startup_sync["skipped"]), startup_sync["skipped"])
        try:
            asyncio.run(_run_multi_bot_runtime())
        except KeyboardInterrupt:
            logger.info("Multi-bot runtime stopped.")
        return

    startup_sync = database.sync_all_active_reps_detailed()
    if startup_sync.get("status") != "success":
        logger.error("Initial panel-scoped database sync failed: %s", startup_sync.get("message"))
    elif startup_sync.get("skipped"):
        # A half-configured or unreachable panel is skipped, never fatal; the
        # remaining panels are still imported. Surface it without an ERROR.
        logger.warning("Panel-scoped sync skipped %s panel(s): %s",
                       len(startup_sync["skipped"]), startup_sync["skipped"])

    async def post_init(application: Application):
        await _bootstrap_v52_startup(application)
        # Single-bot compatibility mode keeps exactly one watchdog/outbox worker.
        asyncio.create_task(watchdog_worker(application))
        asyncio.create_task(receipt_notification_worker(application))

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    _register_application_handlers(app)
    logger.info("Starting upgraded HstAgentBot polling with Watchdog...")
    # Never discard pending Telegram updates during startup.
    app.run_polling(drop_pending_updates=False)


if __name__ == "__main__":
    main()
