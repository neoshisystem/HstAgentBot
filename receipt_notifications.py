"""Durable, receipt-scoped delivery for Super Admin payment notifications."""

import logging

from telegram.constants import ParseMode
from telegram.error import BadRequest

import database
import finance_manager

logger = logging.getLogger(__name__)


async def _deliver_admin_receipt_notification(bot, item: dict) -> str:
    chat_id = item.get("recipient_chat_id")
    if not chat_id:
        from config import ADMIN_TELEGRAM_ID
        chat_id = database.get_setting(
            "admin_telegram_id", default=ADMIN_TELEGRAM_ID, cast_type=int
        )
    if not chat_id or int(chat_id) <= 0:
        raise RuntimeError("Super Admin Telegram destination is not configured")

    chat_id = int(chat_id)
    thread_id = item.get("recipient_thread_id")
    if thread_id:
        thread_id = int(thread_id)
    else:
        thread_id = None
    # A positive Telegram chat ID is a private chat; forum topics only apply
    # to supergroups and must never be sent to a private admin DM.
    if chat_id > 0:
        thread_id = None

    receipt = {
        "id": item["receipt_id"],
        "tracking_code": item["tracking_code"],
        "role": item["role"],
        "user_id": item["user_id"],
        "amount_toman": item["amount_toman"],
        "receipt_note": item.get("receipt_note"),
        "created_at": item.get("created_at") or "",
    }
    text = finance_manager.format_admin_receipt_card(
        receipt,
        item.get("sender_name") or "",
        item.get("sender_username") or "",
    )
    keyboard = finance_manager.build_admin_receipt_review_keyboard(
        int(item["receipt_id"])
    )
    common = {
        "chat_id": chat_id,
        "message_thread_id": thread_id,
        "parse_mode": ParseMode.MARKDOWN,
        "reply_markup": keyboard,
    }

    photo_error = None
    try:
        await bot.send_photo(
            **common,
            photo=item["receipt_photo_id"],
            caption=text,
        )
        return "photo"
    except BadRequest as error:
        photo_error = error
        error_text = str(error).lower()
        is_topic_error = "thread not found" in error_text or "topic" in error_text
        if is_topic_error:
            # Retry the same stored photo in the same chat, explicitly outside
            # the forum topic. Do not create or mutate another receipt.
            retry_common = {**common, "message_thread_id": None}
            try:
                await bot.send_photo(
                    **retry_common,
                    photo=item["receipt_photo_id"],
                    caption=text,
                )
                return "photo"
            except Exception as retry_error:
                logger.warning(
                    "Receipt %s photo retry without topic failed in chat %s: %s",
                    item.get("tracking_code"),
                    chat_id,
                    retry_error,
                )

            # If the target chat still rejects the photo, try the configured
            # Super Admin private chat once, provided it is a distinct target.
            try:
                from config import ADMIN_TELEGRAM_ID
                admin_chat_id = int(ADMIN_TELEGRAM_ID or 0)
            except (ImportError, TypeError, ValueError):
                admin_chat_id = 0

            if admin_chat_id > 0 and admin_chat_id != chat_id:
                admin_common = {
                    **common,
                    "chat_id": admin_chat_id,
                    "message_thread_id": None,
                }
                try:
                    await bot.send_photo(
                        **admin_common,
                        photo=item["receipt_photo_id"],
                        caption=text,
                    )
                    return "photo"
                except Exception as admin_error:
                    logger.warning(
                        "Receipt %s photo fallback to Super Admin DM %s failed: %s",
                        item.get("tracking_code"),
                        admin_chat_id,
                        admin_error,
                    )
    except Exception as error:
        photo_error = error

    logger.warning(
        "Receipt %s photo notification failed; trying durable text fallback: %s",
        item.get("tracking_code"),
        photo_error,
    )
    try:
        await bot.send_message(
            **common,
            text=text + "\n\n⚠️ ارسال تصویر ناموفق بود؛ فیش در پایگاه داده ثبت است.",
        )
        return "text_fallback"
    except Exception as fallback_error:
        raise RuntimeError(
            "photo delivery failed: "
            f"{type(photo_error).__name__}: {photo_error}; "
            "text fallback failed: "
            f"{type(fallback_error).__name__}: {fallback_error}"
        ) from fallback_error


async def process_receipt_notification_outbox(bot, limit: int = 10, now: float | None = None) -> dict:
    """Attempt due notifications; failures remain durable with exponential backoff."""
    attempted = 0
    sent = 0
    for _ in range(max(1, int(limit))):
        item = database.claim_next_receipt_notification(now=now)
        if not item:
            break
        attempted += 1
        try:
            method = await _deliver_admin_receipt_notification(bot, item)
            if database.mark_receipt_notification_sent(
                item["notification_id"], item["lease_token"], method
            ):
                sent += 1
            else:
                logger.error(
                    "Receipt notification %s was sent but its lease could not be finalized",
                    item["notification_id"],
                )
        except Exception as error:
            try:
                rescheduled = database.retry_receipt_notification(
                    item["notification_id"], item["lease_token"], str(error), now=now
                )
                if not rescheduled:
                    logger.error(
                        "Could not reschedule receipt notification %s after delivery failure",
                        item["notification_id"],
                    )
                else:
                    logger.warning(
                        "Receipt notification %s remains pending for retry: %s",
                        item["notification_id"],
                        error,
                    )
            except Exception:
                logger.exception(
                    "Failed to persist retry state for receipt notification %s",
                    item["notification_id"],
                )
    return {"attempted": attempted, "sent": sent}
