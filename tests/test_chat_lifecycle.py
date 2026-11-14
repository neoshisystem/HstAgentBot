import asyncio
from types import SimpleNamespace

import pytest

import admin_panel
import rep_bot
import rep_panel
import user_portal


class FakeBot:
    def __init__(self):
        self.deleted = []

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True


class FakePromptMessage:
    def __init__(self, message_id=501):
        self.message_id = message_id
        self.message_thread_id = None
        self.photo = []
        self.caption = None
        self.edits = []

    async def reply_text(self, text, **kwargs):
        return SimpleNamespace(message_id=self.message_id + 1000, text=text)

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))

    async def delete(self):
        self.deleted = True


class FakePhotoMessage(FakePromptMessage):
    def __init__(self, caption, delete_fails=False):
        super().__init__(message_id=701)
        self.photo = [object()]
        self.caption = caption
        self.deleted = False
        self.delete_fails = delete_fails

    async def reply_text(self, text, **kwargs):
        return {"text": text, "kwargs": kwargs}

    async def delete(self):
        if self.delete_fails:
            raise RuntimeError("delete forbidden")
        self.deleted = True


class FakePhotoQuery:
    def __init__(self, message):
        self.message = message

    async def edit_message_caption(self, **kwargs):
        raise rep_bot.BadRequest("Bad Request: message cannot be edited")


class FakeCallbackQuery:
    def __init__(self, data, message_id=801):
        self.data = data
        self.message = FakePromptMessage(message_id)
        self.answers = []

    async def edit_message_text(self, text, **kwargs):
        await self.message.edit_message_text(text, **kwargs)

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


def text_update(text, uid):
    message = SimpleNamespace(text=text, replies=[])

    async def reply_text(body, **kwargs):
        message.replies.append((body, kwargs))
        return {"text": body}

    message.reply_text = reply_text
    update = SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(
            id=uid, full_name="Tester", first_name="Test", username="tester"
        ),
        effective_chat=SimpleNamespace(id=uid, type="private"),
        effective_message=message,
    )
    return update, message


def callback_update(query, uid):
    return SimpleNamespace(
        callback_query=query,
        message=None,
        effective_user=SimpleNamespace(
            id=uid, full_name="Tester", first_name="Test", username="tester"
        ),
        effective_chat=SimpleNamespace(id=uid, type="private"),
        effective_message=query.message,
    )


def test_safe_edit_deletes_only_opted_in_transient_qr_after_text_fallback():
    old = FakePhotoMessage("📱 QR Code لینک سابسکریپشن اکانت 1001")
    result = asyncio.run(
        rep_bot.safe_edit_message(
            FakePhotoQuery(old), "منوی متنی", delete_transient_photo=True
        )
    )
    assert result["text"] == "منوی متنی"
    assert old.deleted is True


def test_safe_edit_preserves_receipt_photo_even_with_delete_opt_in():
    old = FakePhotoMessage("📎 فیش بانکی ارسالی کاربر — در انتظار بررسی")
    result = asyncio.run(
        rep_bot.safe_edit_message(
            FakePhotoQuery(old), "نمایش متنی", delete_transient_photo=True
        )
    )
    assert result["text"] == "نمایش متنی"
    assert old.deleted is False


def test_safe_edit_does_not_delete_qr_photo_without_explicit_opt_in():
    old = FakePhotoMessage("🔀 QR Code اتصال مستقیم به کانفیگ VLESS")
    asyncio.run(rep_bot.safe_edit_message(FakePhotoQuery(old), "نمایش متنی"))
    assert old.deleted is False


def test_safe_edit_returns_fallback_when_best_effort_delete_fails():
    old = FakePhotoMessage(
        "📱 QR Code لینک سابسکریپشن اکانت 1001", delete_fails=True
    )
    result = asyncio.run(
        rep_bot.safe_edit_message(
            FakePhotoQuery(old), "متن جایگزین", delete_transient_photo=True
        )
    )
    assert result["text"] == "متن جایگزین"


@pytest.mark.parametrize(
    "module",
    [admin_panel, rep_panel, user_portal],
    ids=["admin", "representative", "end-user"],
)
def test_form_prompt_id_is_remembered_and_best_effort_cleaned(module):
    query = SimpleNamespace(message=FakePromptMessage(message_id=905))
    state = {}
    module._remember_form_prompt(state, query)
    assert state["prompt_msg_id"] == 905
    bot = FakeBot()
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=93010))
    context = SimpleNamespace(bot=bot)
    asyncio.run(module._cleanup_form_prompt(update, context, state))
    assert bot.deleted == [(93010, 905)]
    assert "prompt_msg_id" not in state


@pytest.mark.parametrize(
    "module",
    [admin_panel, rep_panel, user_portal],
    ids=["admin", "representative", "end-user"],
)
def test_form_prompt_is_not_deleted_when_parent_callback_reuses_same_message(module):
    state = {"prompt_msg_id": 906}
    bot = FakeBot()
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=93011))
    context = SimpleNamespace(bot=bot)
    asyncio.run(
        module._cleanup_form_prompt(
            update, context, state, preserve_message_id=906
        )
    )
    assert bot.deleted == []
    assert "prompt_msg_id" not in state


def test_valid_user_input_cleans_recorded_prompt(monkeypatch):
    uid = 93012
    update, _ = text_update("1001", uid)
    state = {
        "action": "awaiting_link_code", "user_id": uid,
        "ts": user_portal.time.time(), "prompt_msg_id": 907,
        "return_callback": "user:main",
    }
    monkeypatch.setattr(
        user_portal.database, "link_user_to_account",
        lambda *args, **kwargs: {"status": "ok"},
    )
    bot = FakeBot()
    context = SimpleNamespace(user_data={"user_state": state}, bot=bot)
    assert asyncio.run(user_portal.process_user_text_input(update, context)) is True
    assert bot.deleted == [(uid, 907)]
    assert "user_state" not in context.user_data


def test_valid_admin_input_cleans_recorded_prompt(monkeypatch):
    uid = 93013
    update, _ = text_update("25000", uid)
    state = {
        "action": "charge_user", "target_user_id": 12,
        "ts": admin_panel.time.time(), "prompt_msg_id": 908,
        "return_callback": "admin:users:1",
    }
    monkeypatch.setattr(
        admin_panel.database, "update_user_wallet_manual",
        lambda *a, **k: 25000,
    )
    bot = FakeBot()
    context = SimpleNamespace(user_data={"admin_state": state}, bot=bot)
    assert asyncio.run(admin_panel.process_admin_text_input(update, context)) is True
    assert bot.deleted == [(uid, 908)]
    assert "admin_state" not in context.user_data


def test_valid_representative_input_cleans_recorded_prompt(monkeypatch):
    uid = 93014
    update, _ = text_update("Customer Example", uid)
    state = {
        "action": "awaiting_new_acc_name", "intent_id": "intent-1",
        "rep_id": 8, "ts": rep_panel.time.time(), "prompt_msg_id": 909,
        "return_callback": "rep:new_acc_plans",
    }
    monkeypatch.setattr(
        rep_panel.database, "get_rep_action_intent",
        lambda *a, **k: {
            "status": "pending", "amount": 120000,
            "plan_name": "Monthly", "gb": 20, "days": 30,
        },
    )
    monkeypatch.setattr(
        rep_panel.database, "set_rep_action_intent_customer_name",
        lambda *a, **k: True,
    )
    bot = FakeBot()
    context = SimpleNamespace(user_data={"rep_state": state}, bot=bot)
    rep = {"id": 8, "scope_filter": "all"}
    assert asyncio.run(rep_panel.process_rep_text_input(update, context, rep)) is True
    assert bot.deleted == [(uid, 909)]
    assert "rep_state" not in context.user_data


def test_invalid_user_input_keeps_prompt_for_retry(monkeypatch):
    uid = 93015
    update, _ = text_update("missing-account", uid)
    state = {
        "action": "awaiting_link_code", "user_id": uid,
        "ts": user_portal.time.time(), "prompt_msg_id": 910,
        "return_callback": "user:main",
    }
    monkeypatch.setattr(
        user_portal.database, "link_user_to_account",
        lambda *a, **k: {"status": "not_found"},
    )
    bot = FakeBot()
    context = SimpleNamespace(user_data={"user_state": state}, bot=bot)
    assert asyncio.run(user_portal.process_user_text_input(update, context)) is True
    assert context.user_data["user_state"] is state
    assert state["prompt_msg_id"] == 910
    assert bot.deleted == []


def test_admin_prompt_handler_stores_prompt_message_id(monkeypatch):
    uid = 93016
    monkeypatch.setattr(
        admin_panel.database, "get_user_role", lambda *a, **k: ("super_admin", None)
    )
    monkeypatch.setattr(
        admin_panel.database, "get_card_info",
        lambda: {"card_number": "6037991122334455", "card_holder": "Tester", "bank_name": "Test Bank"},
    )
    query = FakeCallbackQuery("admin:prompt_card_settings", message_id=911)
    context = SimpleNamespace(user_data={}, bot=FakeBot())
    asyncio.run(admin_panel.handle_admin_callbacks(callback_update(query, uid), context))
    assert context.user_data["admin_state"]["prompt_msg_id"] == 911


def test_representative_prompt_handler_stores_prompt_message_id():
    uid = 93017
    query = FakeCallbackQuery("rep:prompt_search", message_id=912)
    context = SimpleNamespace(user_data={}, bot=FakeBot())
    asyncio.run(
        rep_panel.handle_rep_callbacks(
            callback_update(query, uid), context, {"id": 18, "scope_filter": "all"}
        )
    )
    assert context.user_data["rep_state"]["prompt_msg_id"] == 912


def test_user_prompt_handler_stores_prompt_message_id(monkeypatch):
    uid = 93018
    monkeypatch.setattr(user_portal.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(
        user_portal.database, "get_user_role", lambda *a, **k: ("end_user", None)
    )
    query = FakeCallbackQuery("user:prompt_link", message_id=913)
    context = SimpleNamespace(user_data={}, bot=FakeBot())
    asyncio.run(user_portal.handle_user_callbacks(callback_update(query, uid), context))
    assert context.user_data["user_state"]["prompt_msg_id"] == 913


def test_cancel_button_cleans_previous_prompt_before_reusing_callback_message(monkeypatch):
    uid = 93019
    monkeypatch.setattr(user_portal.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(
        user_portal.database, "get_user_role", lambda *a, **k: ("end_user", None)
    )
    called = []

    async def show_portal(update, context, edit_message=False):
        called.append(edit_message)

    monkeypatch.setattr(user_portal, "show_user_portal", show_portal)
    query = FakeCallbackQuery("user:cancel_form", message_id=915)
    bot = FakeBot()
    context = SimpleNamespace(
        user_data={"user_state": {
            "action": "awaiting_link_code",
            "prompt_msg_id": 914,
            "return_callback": "user:main",
        }},
        bot=bot,
    )
    asyncio.run(user_portal.handle_user_callbacks(callback_update(query, uid), context))
    assert bot.deleted == [(uid, 914)]
    assert query.data == "user:main"
    assert called == [True]
    assert "user_state" not in context.user_data



def test_deposit_command_stores_amount_prompt_message_id(monkeypatch):
    uid = 93020
    message = FakePromptMessage(message_id=920)
    update = SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=uid, full_name="Tester", username="tester"),
        effective_chat=SimpleNamespace(id=uid, type="private"),
    )
    monkeypatch.setattr(
        rep_bot.database, "get_user_role", lambda *a, **k: ("end_user", None)
    )
    context = SimpleNamespace(args=[], user_data={}, bot=FakeBot())
    asyncio.run(rep_bot.deposit_cmd(update, context))
    assert context.user_data["fsm_state"] == rep_bot.finance_manager.STATE_AWAITING_RECEIPT_AMOUNT
    assert context.user_data["deposit_form_state"]["prompt_msg_id"] == 1920


def test_wallet_callback_stores_amount_prompt_message_id(monkeypatch):
    uid = 93021
    monkeypatch.setattr(
        rep_bot.database, "get_user_role", lambda *a, **k: ("end_user", None)
    )
    monkeypatch.setattr(rep_bot.database, "is_user_blocked", lambda _uid: False)
    query = FakeCallbackQuery("user_deposit_start", message_id=921)
    bot = FakeBot()
    context = SimpleNamespace(user_data={}, bot=bot)
    asyncio.run(rep_bot.handle_callback(callback_update(query, uid), context))
    assert context.user_data["fsm_state"] == rep_bot.finance_manager.STATE_AWAITING_RECEIPT_AMOUNT
    assert context.user_data["deposit_form_state"]["prompt_msg_id"] == 1921


def _deposit_amount_update(text, uid):
    message = FakePromptMessage(message_id=922)
    message.text = text
    update = SimpleNamespace(
        message=message,
        effective_user=SimpleNamespace(id=uid, full_name="Tester", username="tester"),
        effective_chat=SimpleNamespace(id=uid, type="private"),
        effective_message=message,
    )
    return update


def test_valid_deposit_amount_cleans_prompt_after_payment_card_is_sent(monkeypatch):
    uid = 93022
    update = _deposit_amount_update("500000", uid)
    monkeypatch.setattr(
        rep_bot.database, "get_user_role", lambda *a, **k: ("end_user", None)
    )
    monkeypatch.setattr(rep_bot.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(rep_bot.database, "get_card_info", lambda: {})
    monkeypatch.setattr(
        rep_bot.database, "create_payment_receipt", lambda *a, **k: "TRX-93022"
    )
    monkeypatch.setattr(
        rep_bot.finance_manager, "format_card_instructions",
        lambda *a, **k: "card instructions",
    )
    monkeypatch.setattr(
        rep_bot.finance_manager, "build_card_payment_keyboard",
        lambda *a, **k: object(),
    )
    bot = FakeBot()
    context = SimpleNamespace(
        user_data={
            "fsm_state": rep_bot.finance_manager.STATE_AWAITING_RECEIPT_AMOUNT,
            "deposit_role": "end_user",
            "deposit_form_state": {"prompt_msg_id": 1922},
        },
        bot=bot,
    )
    asyncio.run(rep_bot.handle_message(update, context))
    assert bot.deleted == [(uid, 1922)]
    assert "deposit_form_state" not in context.user_data
    assert context.user_data["fsm_state"] == rep_bot.finance_manager.STATE_AWAITING_RECEIPT_PHOTO


def test_invalid_deposit_amount_keeps_prompt_for_retry(monkeypatch):
    uid = 93023
    update = _deposit_amount_update("invalid amount", uid)
    monkeypatch.setattr(
        rep_bot.database, "get_user_role", lambda *a, **k: ("end_user", None)
    )
    monkeypatch.setattr(rep_bot.database, "is_user_blocked", lambda _uid: False)
    bot = FakeBot()
    state = {"prompt_msg_id": 1923}
    context = SimpleNamespace(
        user_data={
            "fsm_state": rep_bot.finance_manager.STATE_AWAITING_RECEIPT_AMOUNT,
            "deposit_role": "end_user",
            "deposit_form_state": state,
        },
        bot=bot,
    )
    asyncio.run(rep_bot.handle_message(update, context))
    assert context.user_data["deposit_form_state"] is state
    assert state["prompt_msg_id"] == 1923
    assert bot.deleted == []


def test_cancel_payment_cleans_amount_prompt_without_deleting_receipt_photo(monkeypatch):
    uid = 93024
    monkeypatch.setattr(
        rep_bot.database, "get_user_role", lambda *a, **k: ("end_user", None)
    )
    monkeypatch.setattr(rep_bot.database, "is_user_blocked", lambda _uid: False)
    query = FakeCallbackQuery("cancel_payment", message_id=925)
    bot = FakeBot()
    context = SimpleNamespace(
        user_data={
            "fsm_state": rep_bot.finance_manager.STATE_AWAITING_RECEIPT_AMOUNT,
            "deposit_role": "end_user",
            "deposit_form_state": {"prompt_msg_id": 1925},
        },
        bot=bot,
    )
    asyncio.run(rep_bot.handle_callback(callback_update(query, uid), context))
    assert bot.deleted == [(uid, 1925)]
    assert "fsm_state" not in context.user_data
    assert "deposit_form_state" not in context.user_data
    assert getattr(query.message, "deleted", False) is False
