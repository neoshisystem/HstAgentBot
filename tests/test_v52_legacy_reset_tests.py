"""Regression for preview-first legacy reset callbacks and retained financial assertions."""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import rep_bot
import user_portal


class _Query:
    def __init__(self, data):
        self.data = data
        self.answers = []
        self.edits = []
        self.message = SimpleNamespace(message_thread_id=None)

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


def _update(data, user_id=73001):
    query = _Query(data)
    user = SimpleNamespace(
        id=user_id, username="w18-user", full_name="W18 user",
        first_name="W18",
    )
    chat = SimpleNamespace(id=user_id, type="private")
    message = SimpleNamespace(message_thread_id=None)
    return SimpleNamespace(
        callback_query=query, effective_user=user, effective_chat=chat,
        effective_message=message, message=query.message,
    ), query


def test_legacy_reset_suite_keeps_financial_assertions(monkeypatch):
    events = []
    user_update, user_query = _update("user:reset_sub:ACC-1", 73001)
    rep_update, rep_query = _update("reset_sub:ACC-1", 88001)

    monkeypatch.setattr(user_portal.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(
        user_portal.database, "get_user_role",
        lambda *args, **kwargs: ("end_user", None),
    )
    monkeypatch.setattr(
        user_portal.database, "get_user_owned_account",
        lambda *_args: {
            "id": 501, "rep_id": 202, "account_code": "ACC-1",
            "email": "acc-1@example.invalid", "sub_id": "old-sub",
            "owner_tg_id": 73001,
        },
    )
    monkeypatch.setattr(user_portal, "_v52_ui_enabled", lambda: True)

    async def user_preview(*_args, **_kwargs):
        events.append("user-preview")
    monkeypatch.setattr(user_portal, "_show_legacy_reset_preview", user_preview)

    monkeypatch.setattr(
        user_portal.database, "acquire_operation_lock",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("preview acquired a mutation lock")),
    )
    monkeypatch.setattr(
        user_portal.sub_manager, "reset_client_subscription",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("preview called panel mutation")),
    )
    asyncio.run(user_portal.handle_user_callbacks(user_update, SimpleNamespace(user_data={})))
    assert events == ["user-preview"]
    assert user_query.answers == []

    monkeypatch.setattr(rep_bot.database, "get_user_role", lambda *_args, **_kwargs: (
        "rep", {"id": 202, "name": "W18 rep", "scope_filter": "all"}
    ))
    monkeypatch.setattr(rep_bot.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(rep_bot.database, "get_setting", lambda key, default="0": "1" if key == "v52_ui_enabled" else default)

    async def rep_preview(*_args, **_kwargs):
        events.append("rep-preview")
    monkeypatch.setattr(rep_bot, "_show_legacy_reset_preview", rep_preview)

    asyncio.run(rep_bot.handle_callback(rep_update, SimpleNamespace(user_data={})))
    assert events == ["user-preview", "rep-preview"]
    assert rep_query.answers == []

    source = Path(__file__).with_name("test_subscription_callback_routing.py").read_text(encoding="utf-8")
    required = (
        "test_user_reset_partial_result_persists_new_sub_id_and_releases_lock",
        "test_user_reset_unknown_result_keeps_lock_and_does_not_persist_candidate",
        "test_user_reset_local_write_failure_keeps_lock",
        "test_rep_account_card_reset_partial_persists_id_and_releases_shared_lock",
        "test_rep_account_card_reset_unknown_keeps_lock",
        "test_rep_account_card_reset_local_persistence_failure_keeps_lock",
    )
    assert all(name in source for name in required)
    assert "pytest.skip(" not in source
    assert "del test_" not in source
