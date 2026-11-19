import asyncio
from types import SimpleNamespace

import admin_panel
import rep_bot


class FakeQuery:
    def __init__(self, data=""):
        self.data = data
        self.answers = []
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeMessage:
    def __init__(self):
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


def make_update(data="", chat_type="private", user_id=9000):
    query = FakeQuery(data)
    message = FakeMessage()
    chat = SimpleNamespace(id=123456 if chat_type == "private" else -100123456, type=chat_type)
    user = SimpleNamespace(id=user_id, full_name="Super Admin", username="admin")
    update = SimpleNamespace(
        callback_query=query,
        message=message,
        effective_message=SimpleNamespace(message_thread_id=None),
        effective_user=user,
        effective_chat=chat,
    )
    return update, query, message


def test_admin_dashboard_is_private_chat_only(monkeypatch):
    update, query, message = make_update(chat_type="supergroup")
    monkeypatch.setattr(
        admin_panel.database, "get_user_role",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must stop before role/data lookup")),
    )
    asyncio.run(admin_panel.show_admin_dashboard(update, SimpleNamespace(), edit_message=False))
    assert message.replies == []
    assert query.answers and query.answers[0][1] is True
    assert "خصوصی" in query.answers[0][0]


def test_admin_callback_handler_rejects_group_before_processing(monkeypatch):
    update, query, _ = make_update("admin:users", chat_type="supergroup")
    monkeypatch.setattr(
        admin_panel.database, "get_user_role",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("group callback must be stopped first")),
    )
    asyncio.run(admin_panel.handle_admin_callbacks(update, SimpleNamespace(user_data={})))
    assert query.answers and query.answers[0][1] is True
    assert "خصوصی" in query.answers[0][0]
    assert query.edits == []


def test_rep_bot_blocks_admin_receipt_callbacks_in_group(monkeypatch):
    update, query, _ = make_update("admin_approve_rcp_RCP-1", chat_type="supergroup")
    monkeypatch.setattr(rep_bot.database, "get_user_role", lambda *args, **kwargs: ("super_admin", None))
    monkeypatch.setattr(rep_bot.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(
        rep_bot.database, "approve_payment_receipt",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("receipt approval must not run in group")),
    )

    asyncio.run(rep_bot.handle_callback(update, SimpleNamespace(user_data={})))
    assert query.answers and query.answers[0][1] is True
    assert "خصوصی" in query.answers[0][0]


def _pending_reservation():
    return {
        "id": 7,
        "status": "pending",
        "account_code": "ABCD",
        "email": "abcd@example.test",
        "plan_name": "Test Plan",
        "gb": 20,
        "days": 30,
        "charge_amount": 250000,
        "on_first_connect": 0,
    }


def _patch_private_admin(monkeypatch):
    monkeypatch.setattr(admin_panel.database, "get_user_role", lambda *args, **kwargs: ("super_admin", None))
    monkeypatch.setattr(admin_panel.database, "get_reservation_by_id", lambda _rid: _pending_reservation())


def test_reservation_force_and_cancel_require_a_second_confirmation(monkeypatch):
    _patch_private_admin(monkeypatch)
    update_force, query_force, _ = make_update("admin:confirm_force_res:7:ABCD")
    update_cancel, query_cancel, _ = make_update("admin:confirm_cancel_res:7:ABCD")
    context = SimpleNamespace(user_data={})

    # Any accidental side effect during the first click fails the test.
    monkeypatch.setattr(
        admin_panel.database, "acquire_operation_lock",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("first click must not acquire lock")),
    )
    monkeypatch.setattr(
        admin_panel.database, "cancel_reservation",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("first click must not cancel/refund")),
    )
    monkeypatch.setattr(
        admin_panel, "xui_manager",
        SimpleNamespace(renew_client=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("side effect before confirmation"))),
    )

    asyncio.run(admin_panel.handle_admin_callbacks(update_force, context))
    asyncio.run(admin_panel.handle_admin_callbacks(update_cancel, context))

    force_keyboard = query_force.edits[0][1]["reply_markup"]
    cancel_keyboard = query_cancel.edits[0][1]["reply_markup"]
    assert force_keyboard.inline_keyboard[0][0].callback_data == "admin:do_force_res:7:ABCD"
    assert cancel_keyboard.inline_keyboard[0][0].callback_data == "admin:do_cancel_res:7:ABCD"
    assert "X-UI" in query_force.edits[0][0]
    assert "عودت" in query_cancel.edits[0][0]


def test_legacy_one_click_reservation_actions_are_rejected(monkeypatch):
    _patch_private_admin(monkeypatch)
    update_force, query_force, _ = make_update("admin:force_res:7:ABCD")
    update_cancel, query_cancel, _ = make_update("admin:cancel_res:7:ABCD")
    monkeypatch.setattr(
        admin_panel.database, "cancel_reservation",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy callback must not cancel")),
    )
    monkeypatch.setattr(
        admin_panel.database, "acquire_operation_lock",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("legacy callback must not activate")),
    )

    asyncio.run(admin_panel.handle_admin_callbacks(update_force, SimpleNamespace(user_data={})))
    asyncio.run(admin_panel.handle_admin_callbacks(update_cancel, SimpleNamespace(user_data={})))
    assert "قدیمی" in query_force.answers[0][0]
    assert "قدیمی" in query_cancel.answers[0][0]


def test_second_confirmation_executes_reservation_cancellation_only_after_revalidation(monkeypatch):
    _patch_private_admin(monkeypatch)
    update, query, _ = make_update("admin:do_cancel_res:7:ABCD")
    calls = []
    monkeypatch.setattr(
        admin_panel.database, "cancel_reservation",
        lambda res_id, **kwargs: (calls.append((res_id, kwargs)) or (_pending_reservation(), 100000)),
    )

    asyncio.run(admin_panel.handle_admin_callbacks(update, SimpleNamespace(user_data={})))
    assert calls and calls[0][0] == 7
    assert "250,000" in query.answers[0][0]


def test_reservation_execution_rejects_stale_account_binding(monkeypatch):
    _patch_private_admin(monkeypatch)
    update, query, _ = make_update("admin:do_cancel_res:7:OTHER")
    monkeypatch.setattr(
        admin_panel.database, "cancel_reservation",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("mismatched account must not cancel")),
    )

    asyncio.run(admin_panel.handle_admin_callbacks(update, SimpleNamespace(user_data={})))
    assert "تغییر کرده" in query.answers[0][0]


def test_all_admin_prompt_callbacks_have_a_matching_text_fsm_consumer():
    import inspect

    callback_source = inspect.getsource(admin_panel._handle_admin_callbacks_impl)
    fsm_source = inspect.getsource(admin_panel.process_admin_text_input)
    prompt_to_state = {
        "prompt_card_settings": "edit_card_settings",
        "prompt_charge_user": "charge_user",
        "prompt_edit_credit": "edit_credit_limit",
        "prompt_setting": "edit_setting",
        "prompt_pay": "pay",
        "prompt_add_rep": "add_rep",
        "prompt_edit_plan": "edit_plan",
        "prompt_transfer": "transfer",
        "prompt_inspect": "inspect",
        "prompt_add_plan": "add_plan",
        "prompt_link_user": "link_user",
        "prompt_search_user": "search_user",
        "prompt_edit_user_id": "edit_user_id",
        "prompt_link_user_id": "link_user_account",
        "prompt_unlink_user_acc": "unlink_user_account",
        "prompt_edit_rep": "edit_rep",
        "prompt_edit_plan_specs": "edit_plan_specs",
    }
    for prompt, state in prompt_to_state.items():
        assert prompt in callback_source
        assert state in callback_source
        assert state in fsm_source



def test_escape_markdown_escapes_classic_markdown_metacharacters():
    actual = admin_panel.escape_markdown("A_B*[x]" + chr(96))
    expected = (
        "A" + chr(92) + "_B"
        + chr(92) + "*"
        + chr(92) + "[x]"
        + chr(92) + chr(96)
    )
    assert actual == expected


def test_admin_users_list_escapes_dynamic_full_name_before_markdown_rendering(monkeypatch):
    update, query, _ = make_update("admin:users")
    monkeypatch.setattr(admin_panel.database, "get_user_role", lambda *_args, **_kwargs: ("super_admin", None))
    monkeypatch.setattr(admin_panel.database, "get_all_bot_users", lambda: [{
        "telegram_id": 501,
        "full_name": "Alice_Test",
        "username": "alice",
        "is_blocked": 0,
        "linked_accounts_count": 1,
    }])

    asyncio.run(admin_panel.handle_admin_callbacks(update, SimpleNamespace(user_data={})))

    assert query.edits
    assert "Alice" + chr(92) + "_Test" in query.edits[0][0]


def test_admin_callback_render_exception_answers_query_and_releases_spinner(monkeypatch):
    update, query, _ = make_update("admin:users")
    monkeypatch.setattr(admin_panel.database, "get_user_role", lambda *_args, **_kwargs: ("super_admin", None))
    monkeypatch.setattr(
        admin_panel.database, "get_all_bot_users",
        lambda: (_ for _ in ()).throw(RuntimeError("database unavailable")),
    )

    asyncio.run(admin_panel.handle_admin_callbacks(update, SimpleNamespace(user_data={})))

    assert query.answers
    assert query.answers[-1][1] is True
    assert "خطای غیرمنتظره" in query.answers[-1][0]


def test_admin_back_keyboard_has_parent_and_root_rows_but_root_dashboard_has_no_back():
    nested = admin_panel.build_back_to_admin_keyboard(parent_cb="admin:users:1")
    callbacks = [
        button.callback_data
        for row in nested.inline_keyboard
        for button in row
    ]
    assert callbacks == ["admin:users:1", "admin:main"]

    submenu = admin_panel.build_back_to_admin_keyboard()
    submenu_callbacks = [
        button.callback_data
        for row in submenu.inline_keyboard
        for button in row
    ]
    assert submenu_callbacks == ["admin:main"]

    root_callbacks = [
        button.callback_data
        for row in admin_panel.build_admin_main_keyboard().inline_keyboard
        for button in row
    ]
    assert "admin:main" not in root_callbacks


def test_set_plan_active_callback_updates_explicit_state_and_refreshes_plan_card(monkeypatch):
    update, query, _ = make_update("admin:set_plan_active:plan_alpha:0")
    monkeypatch.setattr(admin_panel.database, "get_user_role", lambda *_args, **_kwargs: ("super_admin", None))
    calls = []
    monkeypatch.setattr(
        admin_panel.database, "set_plan_active_status",
        lambda code, active, **kwargs: calls.append((code, active, kwargs)) or int(active),
    )
    monkeypatch.setattr(admin_panel.database, "get_plan", lambda _code: {
        "plan_code": "plan_alpha",
        "name": "Plan_Test",
        "gb": 20,
        "days": 30,
        "rep_price": 100_000,
        "user_price": 150_000,
        "is_quick": 1,
        "is_active": 0,
    })

    asyncio.run(admin_panel.handle_admin_callbacks(update, SimpleNamespace(user_data={})))

    assert calls and calls[0][0:2] == ("plan_alpha", False)
    assert "🔴 غیرفعال" in query.edits[0][0]
    keyboard = query.edits[0][1]["reply_markup"]
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert "admin:set_plan_active:plan_alpha:1" in callbacks
    assert callbacks[-2:] == ["admin:plans", "admin:main"]



def test_admin_recent_tx_escapes_database_names_and_description(monkeypatch):
    update, query, _ = make_update("admin:recent_tx:1")
    monkeypatch.setattr(admin_panel.database, "get_user_role", lambda *_args, **_kwargs: ("super_admin", None))
    monkeypatch.setattr(admin_panel.database, "get_financial_ledger_paginated", lambda **_kwargs: ([{
        "transaction_type": "credit",
        "rep_id": 8,
        "rep_name": "Rep_Name",
        "rep_code": "rep_code",
        "user_id": None,
        "actor_id": 9000,
        "actor_name": "Admin",
        "created_at": "2026-11-04 12:00:00",
        "amount": 10_000,
        "description": "memo_with_underscore",
        "balance_after": 10_000,
    }], 1, 1))

    asyncio.run(admin_panel.handle_admin_callbacks(update, SimpleNamespace(user_data={})))

    rendered = query.edits[0][0]
    assert "Rep" + chr(92) + "_Name" in rendered
    assert "memo" + chr(92) + "_with" + chr(92) + "_underscore" in rendered


def test_backup_document_message_contains_admin_return_keyboard(monkeypatch, tmp_path):
    import sys
    from types import SimpleNamespace

    update, query, _ = make_update("admin:backup")
    monkeypatch.setattr(admin_panel.database, "get_user_role", lambda *_args, **_kwargs: ("super_admin", None))
    archive = tmp_path / "db-backup.zip"
    archive.write_bytes(b"backup")
    monkeypatch.setitem(sys.modules, "backup", SimpleNamespace(
        run_backup=lambda: {
            "archive": str(archive),
            "size_kb": 1,
            "telegram_upload_status": "sent",
        }
    ))
    sent = []

    async def send_document(**kwargs):
        sent.append(kwargs)

    update.effective_chat.send_document = send_document
    asyncio.run(admin_panel.handle_admin_callbacks(update, SimpleNamespace(user_data={})))

    assert sent
    keyboard = sent[0]["reply_markup"]
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert callbacks == ["admin:main"]
