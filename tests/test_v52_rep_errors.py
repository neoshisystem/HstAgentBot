"""W18.10: representative text-form failures always have a valid exit path."""
from types import SimpleNamespace
import asyncio
import time

import rep_panel


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


def test_all_text_failures_have_navigation(monkeypatch):
    inspections = iter([
        {"status": "not_found"},
        {"status": "ok", "data": {"email": "foreign@example.invalid",
                                  "raw": {"enable": True}}},
    ])
    monkeypatch.setattr(
        rep_panel, "xui_manager",
        SimpleNamespace(inspect_client=lambda *_a, **_kw: next(inspections)),
    )
    monkeypatch.setattr(rep_panel.database, "is_account_in_rep_scope", lambda *_: None)

    rep = {"id": 202, "scope_filter": "all"}
    cases = [
        ("unknown code", {"action": "awaiting_search_query", "rep_id": 202,
                          "ts": time.time(), "attempts": 0,
                          "return_callback": "rep:acc_list:all:1"}),
        ("out of scope", {"action": "awaiting_search_query", "rep_id": 202,
                          "ts": time.time(), "attempts": 0,
                          "return_callback": "rep:main"}),
        ("invalid amount", {"action": "awaiting_price_amount", "rep_id": 202,
                            "ts": time.time(), "attempts": 0,
                            "return_callback": "rep:new_acc_plans"}),
        ("cancel", {"action": "awaiting_search_query", "rep_id": 202,
                    "ts": time.time(), "attempts": 0,
                    "return_callback": "rep:main"}),
    ]

    for text, state in cases:
        message = _Message("لغو" if text == "cancel" else text)
        update = SimpleNamespace(
            message=message,
            effective_user=SimpleNamespace(id=7202),
            effective_chat=SimpleNamespace(id=7202),
        )
        context = SimpleNamespace(
            user_data={"rep_state": state},
            bot=SimpleNamespace(id=9002),
        )
        assert asyncio.run(rep_panel.process_rep_text_input(update, context, rep)) is True
        assert message.replies
        callbacks = _callbacks(message.replies[-1][1].get("reply_markup"))
        assert any(value.startswith("rep:") for value in callbacks)
        assert any("main" in value for value in callbacks) or state.get("return_callback") in callbacks
        assert not any(value.startswith("admin:") or value.startswith("v52:ops") for value in callbacks)
        if text == "invalid amount":
            assert "rep_state" not in context.user_data
        if text == "cancel":
            assert "rep_state" not in context.user_data
