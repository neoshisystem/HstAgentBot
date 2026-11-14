import asyncio
import inspect
from types import SimpleNamespace

import nlp_matcher
import rep_bot
import database


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
    def __init__(self, text=None):
        self.text = text
        self.message_thread_id = None
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


def make_update(data="", chat_type="private", user_id=None, text=None):
    user_id = rep_bot.ADMIN_TELEGRAM_ID if user_id is None else user_id
    query = FakeQuery(data)
    message = FakeMessage(text)
    chat = SimpleNamespace(
        id=123456 if chat_type == "private" else -100123456,
        type=chat_type,
    )
    user = SimpleNamespace(id=user_id, full_name="Admin", username="admin")
    update = SimpleNamespace(
        callback_query=query,
        message=message,
        effective_message=SimpleNamespace(message_thread_id=52 if chat_type != "private" else None),
        effective_user=user,
        effective_chat=chat,
    )
    return update, query, message


def test_admin_command_guard_requires_private_chat_and_configured_admin():
    private, _, _ = make_update(chat_type="private")
    group, _, _ = make_update(chat_type="supergroup")
    stranger, _, _ = make_update(chat_type="private", user_id=12345)

    assert rep_bot._is_private_super_admin(private) is True
    assert rep_bot._is_private_super_admin(group) is False
    assert rep_bot._is_private_super_admin(stranger) is False


def test_all_sensitive_admin_commands_use_private_admin_guard():
    command_names = (
        "setprice_cmd", "pay_cmd", "card_cmd", "operations_cmd",
        "unlock_operation_cmd", "resolve_user_intent_cmd",
        "resolve_rep_intent_cmd", "resolve_admin_renewal_cmd",
        "resolve_reservation_cmd", "resolve_rep_renewal_cmd", "sync_cmd",
    )
    for name in command_names:
        source = inspect.getsource(getattr(rep_bot, name))
        assert "_is_private_super_admin(update)" in source, name


def test_setprice_command_does_not_mutate_from_group_even_for_admin_id(monkeypatch):
    update, _, message = make_update(chat_type="supergroup", text="/setprice")
    context = SimpleNamespace(args=["plan", "100", "200"])
    monkeypatch.setattr(
        rep_bot.database, "update_plan_price",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("group command must not mutate pricing")),
    )

    asyncio.run(rep_bot.setprice_cmd(update, context))

    assert message.replies == []


def test_super_admin_cannot_enter_first_representative_panel_by_implicit_fallback(monkeypatch):
    update, query, _ = make_update("rep:main", chat_type="private")
    monkeypatch.setattr(rep_bot.database, "get_user_role", lambda *args, **kwargs: ("super_admin", None))
    monkeypatch.setattr(rep_bot.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(rep_bot.database, "get_all_reps", lambda: [{"id": 1, "name": "First"}])
    monkeypatch.setattr(
        rep_bot.rep_panel, "handle_rep_callbacks",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not impersonate first rep")),
    )

    asyncio.run(rep_bot.handle_callback(update, SimpleNamespace(user_data={})))

    assert query.answers and query.answers[0][1] is True
    assert "تاپیک اختصاصی" in query.answers[0][0]


def test_super_admin_account_card_reservation_buttons_require_second_confirmation(monkeypatch):
    monkeypatch.setattr(nlp_matcher.database, "get_pricing_plans", lambda *args, **kwargs: [])
    pending = {
        "id": 7, "status": "pending", "account_code": "ABCD",
        "email": "abcd@example.test", "plan_name": "Test",
        "gb": 20, "days": 30, "charge_amount": 250000,
    }
    keyboard = nlp_matcher.build_account_keyboard("ABCD", role="super_admin", pending_res=pending)
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]

    assert "admin:confirm_force_res:7:ABCD" in callbacks
    assert "admin:confirm_cancel_res:7:ABCD" in callbacks
    assert "force_res:7:ABCD" not in callbacks
    assert "cancel_res:7:ABCD" not in callbacks


def test_legacy_one_click_reservation_callbacks_are_rejected_for_super_admin(monkeypatch):
    update, query, _ = make_update("force_res:7:ABCD")
    monkeypatch.setattr(rep_bot.database, "get_user_role", lambda *args, **kwargs: ("super_admin", None))
    monkeypatch.setattr(rep_bot.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(
        rep_bot.database, "get_reservation_by_id",
        lambda *_: (_ for _ in ()).throw(AssertionError("legacy action must stop before reservation lookup")),
    )

    asyncio.run(rep_bot.handle_callback(update, SimpleNamespace(user_data={})))

    assert query.answers and query.answers[0][1] is True
    assert "قدیمی" in query.answers[0][0]


def test_link_command_is_end_user_only(monkeypatch):
    update, _, message = make_update(chat_type="private", user_id=77001, text="/link ABCD")
    context = SimpleNamespace(args=["ABCD"])
    monkeypatch.setattr(rep_bot.database, "get_user_role", lambda *args, **kwargs: ("rep", {"id": 5}))
    monkeypatch.setattr(
        rep_bot.database, "link_user_to_account",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("representative must not claim end-user link")),
    )

    asyncio.run(rep_bot.link_cmd(update, context))

    assert message.replies == []


def test_stats_command_rejects_end_user_without_rep_context(monkeypatch):
    update, _, message = make_update(chat_type="private", user_id=77001, text="/stats")
    context = SimpleNamespace()
    monkeypatch.setattr(rep_bot.database, "get_user_role", lambda *args, **kwargs: ("end_user", None))
    monkeypatch.setattr(
        rep_bot.database, "get_all_active_reps",
        lambda: (_ for _ in ()).throw(AssertionError("end user must not reach admin stats")),
    )

    asyncio.run(rep_bot.stats_cmd(update, context))

    assert message.replies == []


def test_global_billing_aggregate_is_not_disclosed_in_group(monkeypatch):
    update, _, message = make_update(chat_type="supergroup", text="/billing")
    monkeypatch.setattr(rep_bot.database, "get_user_role", lambda *args, **kwargs: ("super_admin", None))
    monkeypatch.setattr(
        rep_bot.database, "get_all_active_reps",
        lambda: (_ for _ in ()).throw(AssertionError("group must not read global financial aggregate")),
    )

    asyncio.run(rep_bot.billing_cmd(update, SimpleNamespace()))

    assert message.replies == []


def test_super_admin_cannot_open_end_user_or_rep_deposit_command(monkeypatch):
    update, _, message = make_update(chat_type="private", text="/deposit 500000")
    context = SimpleNamespace(args=["500000"], user_data={})
    monkeypatch.setattr(rep_bot.database, "get_user_role", lambda *args, **kwargs: ("super_admin", None))
    monkeypatch.setattr(
        rep_bot.database, "create_payment_receipt",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("super admin is not a deposit role")),
    )

    asyncio.run(rep_bot.deposit_cmd(update, context))

    assert message.replies == []
    assert context.user_data == {}


def test_super_admin_global_text_routes_are_silent_outside_private_chat(monkeypatch):
    update, _, message = make_update(chat_type="supergroup", text="ABCD")
    monkeypatch.setattr(rep_bot.database, "get_user_role", lambda *args, **kwargs: ("super_admin", None))
    monkeypatch.setattr(rep_bot.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(
        rep_bot.nlp_matcher, "detect_intent",
        lambda *_: (_ for _ in ()).throw(AssertionError("global admin group text must stop before intent routing")),
    )

    asyncio.run(rep_bot.handle_message(update, SimpleNamespace(user_data={})))

    assert message.replies == []


def test_super_admin_global_callbacks_are_rejected_outside_private_chat(monkeypatch):
    update, query, _ = make_update("status:ABCD", chat_type="supergroup")
    monkeypatch.setattr(rep_bot.database, "get_user_role", lambda *args, **kwargs: ("super_admin", None))
    monkeypatch.setattr(rep_bot.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(rep_bot, "xui_manager", SimpleNamespace(
        inspect_client=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("global admin callback must stop before X-UI"))
    ))

    asyncio.run(rep_bot.handle_callback(update, SimpleNamespace(user_data={})))

    assert query.answers and query.answers[0][1] is True
    assert "خصوصی" in query.answers[0][0]
