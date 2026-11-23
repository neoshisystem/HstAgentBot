"""Type-aware Telegram message editing for caption-bearing messages.

A receipt notification is a **photo** message: its body lives in ``caption`` and
``message.text`` is ``None``. Calling ``edit_message_text`` on it raises
``BadRequest: There is no text in the message to edit``, which the outer
dispatcher guard converts into a generic «❌ خطای غیرمنتظره رخ داد» popup — the
operator sees a crash on a button whose handler is perfectly fine.

Two distinct operations are needed, and conflating them is what produced the bug:

* ``update_body`` — change the body of the message the button sits on while
  keeping its type. A photo stays a photo (``edit_message_caption``); a text
  message stays text (``edit_message_text``). This is what a receipt status
  change needs: the caption must actually read «✅ تایید شده» afterwards.
* ``render_page`` — show a *different* page. Telegram cannot turn a photo
  message into a text message, so when the source carries media the new page is
  sent as a fresh message instead of being edited onto it.

Both swallow the benign ``Message is not modified`` rejection (pressing the same
button twice is a normal user action) and never swallow anything else.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: Outcomes returned by the helpers, so a caller (and a test) can assert which
#: transport path was taken instead of guessing.
EDITED_TEXT = "edited_text"
EDITED_CAPTION = "edited_caption"
SENT_NEW = "sent_new"
UNCHANGED = "unchanged"


def is_media_message(message: Any) -> bool:
    """True when the message body is a caption rather than ``text``.

    Telegram exposes ``photo``/``document``/``video``/``animation``/``audio`` for
    caption-bearing messages and leaves ``text`` as ``None``. Any of them makes
    ``edit_message_text`` illegal.
    """
    if message is None:
        return False
    for attribute in ("photo", "document", "video", "animation", "audio", "voice"):
        if getattr(message, attribute, None):
            return True
    return getattr(message, "text", None) is None and getattr(message, "caption", None) is not None


def _bot_of(query: Any) -> Any:
    for attribute in ("get_bot",):
        getter = getattr(query, attribute, None)
        if callable(getter):
            try:
                return getter()
            except Exception:  # pragma: no cover - defensive, PTB versions differ
                pass
    message = getattr(query, "message", None)
    getter = getattr(message, "get_bot", None)
    if callable(getter):
        return getter()
    return getattr(query, "bot", None)


def _is_not_modified(error: Exception) -> bool:
    return "not modified" in str(error).lower()


async def update_body(
    query: Any,
    *,
    text: str,
    reply_markup: Any = None,
    parse_mode: Any = None,
) -> str:
    """Change the body of the callback's own message, preserving its type.

    Returns one of ``EDITED_CAPTION`` / ``EDITED_TEXT`` / ``UNCHANGED``. A photo
    message is updated through ``edit_message_caption`` so the receipt's status
    line really changes; a text message through ``edit_message_text``.
    """
    message = getattr(query, "message", None)
    kwargs: dict[str, Any] = {"reply_markup": reply_markup}
    if parse_mode is not None:
        kwargs["parse_mode"] = parse_mode
    try:
        if is_media_message(message):
            await query.edit_message_caption(caption=text, **kwargs)
            return EDITED_CAPTION
        await query.edit_message_text(text=text, **kwargs)
        return EDITED_TEXT
    except Exception as error:
        if _is_not_modified(error):
            return UNCHANGED
        raise


async def render_page(
    query: Any,
    *,
    text: str,
    reply_markup: Any = None,
    parse_mode: Any = None,
) -> str:
    """Show a new page in response to a callback, whatever the source message is.

    A text message is edited in place. A photo/document message cannot be turned
    into a text message, so the page is delivered as a fresh message in the same
    chat and topic — the operator always sees the page instead of a crash.
    """
    message = getattr(query, "message", None)
    kwargs: dict[str, Any] = {"reply_markup": reply_markup}
    if parse_mode is not None:
        kwargs["parse_mode"] = parse_mode

    if not is_media_message(message):
        try:
            await query.edit_message_text(text=text, **kwargs)
            return EDITED_TEXT
        except Exception as error:
            if _is_not_modified(error):
                return UNCHANGED
            # A text message whose body cannot be edited (e.g. an inline message
            # from a channel) still gets the page rather than a dead button.
            logger.debug("In-place text edit failed; sending a new message", exc_info=True)

    bot = _bot_of(query)
    if bot is None:
        raise RuntimeError("callback has no bot to deliver the page with")
    send_kwargs: dict[str, Any] = {
        "chat_id": getattr(message, "chat_id", None),
        "text": text,
    }
    thread_id = getattr(message, "message_thread_id", None)
    if thread_id:
        send_kwargs["message_thread_id"] = thread_id
    send_kwargs.update(kwargs)
    await bot.send_message(**send_kwargs)
    return SENT_NEW


async def edit_reply_markup(query: Any, reply_markup: Any) -> bool:
    """Swap only the keyboard. Legal on photo and text messages alike.

    Returns False when Telegram reports the markup as unchanged, so a caller can
    distinguish a real update from an idempotent no-op.
    """
    try:
        await query.edit_message_reply_markup(reply_markup=reply_markup)
        return True
    except Exception as error:
        if _is_not_modified(error):
            return False
        raise


__all__ = [
    "EDITED_CAPTION", "EDITED_TEXT", "SENT_NEW", "UNCHANGED",
    "edit_reply_markup", "is_media_message", "render_page", "update_body",
]
