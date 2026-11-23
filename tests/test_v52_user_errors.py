"""W18.11: end-user link/search failures and foreign FSM state remain navigable."""
from types import SimpleNamespace
import asyncio
import time

import user_portal


class _Message:
    def __init__(self, text):
        self.text = text
        self.replies = []

    async def reply_text(self, value, **kwargs):
        self.replies.append((value, kwargs))


def _callbacks(markup):
    if markup is None:
        return []
    return [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]


def test_search_and_form_errors_exit(monkeypatch):
    database_results = iter([
        {"status": "not_found"},
        {"status": "out_of_scope"},
    ])

    def link_result(*_args, **_kwargs):
        return next(database_results)

    monkeypatch.setattr(user_portal.database, "link_user_to_account", link_result)
    for value in ("unknown code", "out of scope"):
        message = _Message(value)
        update = SimpleNamespace(
            message=message,
            effective_user=SimpleNamespace(id=7101, username="user", full_name="User"),
            effective_chat=SimpleNamespace(id=7101),
        )
        context = SimpleNamespace(
            user_data={"user_state": {
                "action": "awaiting_link_code", "ts": time.time(), "attempts": 0,
                "return_callback": "user:services", "bot_id": 9001, "actor_tg_id": 7101,
            }},
            bot=SimpleNamespace(id=9001),
        )
        assert asyncio.run(user_portal.process_user_text_input(update, context)) is True
        assert message.replies
        callbacks = _callbacks(message.replies[-1][1].get("reply_markup"))
        assert "user:prompt_link" in callbacks
        assert "user:main" in callbacks
        assert "user:services" in callbacks
        assert not any(value.startswith("admin:") or value.startswith("v52:ops") for value in callbacks)

    # A Telegram/database exception is reported once and does not retry the mutation.
    calls = []
    def telegram_error(*_args, **_kwargs):
        calls.append("once")
        raise RuntimeError("synthetic Telegram/database failure")
    monkeypatch.setattr(user_portal.database, "link_user_to_account", telegram_error)
    message = _Message("telegram error")
    update = SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=7101, username="user", full_name="User"),
        effective_chat=SimpleNamespace(id=7101),
    )
    context = SimpleNamespace(
        user_data={"user_state": {
            "action": "awaiting_link_code", "ts": time.time(), "attempts": 0,
            "return_callback": "user:services", "bot_id": 9001, "actor_tg_id": 7101,
        }},
        bot=SimpleNamespace(id=9001),
    )
    assert asyncio.run(user_portal.process_user_text_input(update, context)) is True
    assert calls == ["once"]
    assert "user:services" in _callbacks(message.replies[-1][1].get("reply_markup"))

    # A form issued by another Telegram bot is rejected, cleared and routed home.
    message = _Message("foreign bot state")
    update.message = message
    context = SimpleNamespace(
        user_data={"user_state": {
            "action": "awaiting_link_code", "ts": time.time(), "attempts": 0,
            "return_callback": "user:services", "bot_id": 9002, "actor_tg_id": 7101,
        }},
        bot=SimpleNamespace(id=9001),
    )
    assert asyncio.run(user_portal.process_user_text_input(update, context)) is True
    assert "user_state" not in context.user_data
    callbacks = _callbacks(message.replies[-1][1].get("reply_markup"))
    assert "user:services" in callbacks
    assert not any(value.startswith("admin:") for value in callbacks)

    # A forged actor ID is rejected before it can reuse another user's link form.
    message = _Message("foreign actor state")
    update.message = message
    context = SimpleNamespace(
        user_data={"user_state": {
            "action": "awaiting_link_code", "ts": time.time(), "attempts": 0,
            "return_callback": "user:services", "bot_id": 9001, "actor_tg_id": 9999,
        }},
        bot=SimpleNamespace(id=9001),
    )
    assert asyncio.run(user_portal.process_user_text_input(update, context)) is True
    assert "user_state" not in context.user_data
    assert "user:services" in _callbacks(message.replies[-1][1].get("reply_markup"))
