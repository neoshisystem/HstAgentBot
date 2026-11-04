import asyncio
from types import SimpleNamespace

import rep_bot
import rep_panel
import nlp_matcher


class FakeMessage:
    def __init__(self, text=None):
        self.text = text
        self.message_thread_id = None
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


class FakeQuery:
    def __init__(self, data=""):
        self.data = data
        self.answers = []
        self.edits = []
        self.message = FakeMessage()

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


def make_callback(data, user_id=88001):
    query = FakeQuery(data)
    user = SimpleNamespace(id=user_id, full_name="Representative", username="rep")
    chat = SimpleNamespace(id=-1001234, type="supergroup")
    update = SimpleNamespace(
        callback_query=query,
        effective_user=user,
        effective_chat=chat,
        effective_message=SimpleNamespace(message_thread_id=52),
        message=None,
    )
    return update, query


def make_rep(rep_id=5):
    return {
        "id": rep_id,
        "name": f"Rep {rep_id}",
        "scope_filter": "rep-scope",
        "is_active": 1,
    }


def pending_reservation(rep_id=5, res_id=7, account_code="ABCD"):
    return {
        "id": res_id,
        "rep_id": rep_id,
        "status": "pending",
        "account_code": account_code,
        "email": "abcd@example.test",
        "plan_name": "20 GB / 30 days",
        "gb": 20,
        "days": 30,
        "charge_amount": 400_000,
        "on_first_connect": 0,
    }


def test_rep_account_card_reservation_actions_use_two_step_callbacks(monkeypatch):
    monkeypatch.setattr(nlp_matcher.database, "get_pricing_plans", lambda *args, **kwargs: [])
    keyboard = nlp_matcher.build_account_keyboard(
        "ABCD",
        role="rep",
        pending_res=pending_reservation(),
    )
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert "rep:confirm_force_res:7:ABCD" in callbacks
    assert "rep:confirm_cancel_res:7:ABCD" in callbacks
    assert "force_res:7:ABCD" not in callbacks
    assert "cancel_res:7:ABCD" not in callbacks


def test_rep_reservation_queue_buttons_open_confirmation_screens(monkeypatch):
    monkeypatch.setattr(
        rep_panel.database, "get_rep_reservations",
        lambda rep_id: [{**pending_reservation(rep_id=rep_id), "created_at": "2026-11-03 17:00"}],
    )
    update, query = make_callback("rep:reserves")
    asyncio.run(rep_panel.handle_rep_callbacks(update, SimpleNamespace(user_data={}), make_rep()))

    keyboard = query.edits[0][1]["reply_markup"]
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert "rep:confirm_force_res:7:ABCD" in callbacks
    assert "rep:confirm_cancel_res:7:ABCD" in callbacks


def test_first_reservation_confirmation_click_has_no_external_or_financial_side_effects(monkeypatch):
    rep = make_rep()
    monkeypatch.setattr(
        rep_panel.database, "get_reservation_by_id",
        lambda _res_id: pending_reservation(),
    )
    monkeypatch.setattr(
        rep_panel.database, "acquire_operation_lock",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("first click must not acquire lock")),
    )
    monkeypatch.setattr(
        rep_panel.database, "cancel_reservation",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("first click must not refund")),
    )
    monkeypatch.setattr(
        rep_panel, "xui_manager",
        SimpleNamespace(renew_client=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("first click must not call X-UI")
        )),
    )

    for action, expected_action in (
        ("confirm_force_res", "do_force_res"),
        ("confirm_cancel_res", "do_cancel_res"),
    ):
        update, query = make_callback(f"rep:{action}:7:ABCD")
        asyncio.run(rep_panel.handle_rep_callbacks(update, SimpleNamespace(user_data={}), rep))
        keyboard = query.edits[0][1]["reply_markup"]
        assert keyboard.inline_keyboard[0][0].callback_data == f"rep:{expected_action}:7:ABCD"
        assert query.answers and query.answers[0][1] is False


def test_legacy_prefixed_one_click_reservation_callbacks_are_rejected(monkeypatch):
    monkeypatch.setattr(
        rep_panel.database, "get_reservation_by_id",
        lambda *_: (_ for _ in ()).throw(AssertionError("legacy callback must stop before DB action")),
    )
    for action in ("force_res", "cancel_res"):
        update, query = make_callback(f"rep:{action}:7:ABCD")
        asyncio.run(rep_panel.handle_rep_callbacks(update, SimpleNamespace(user_data={}), make_rep()))
        assert "قدیمی" in query.answers[0][0]
        assert query.answers[0][1] is True


def test_legacy_unprefixed_account_card_reservation_callbacks_are_rejected_for_reps(monkeypatch):
    rep = make_rep()
    monkeypatch.setattr(rep_bot.database, "get_user_role", lambda *args, **kwargs: ("rep", rep))
    monkeypatch.setattr(rep_bot.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(
        rep_bot.database, "get_reservation_by_id",
        lambda *_: (_ for _ in ()).throw(AssertionError("legacy callback must stop before DB action")),
    )
    update, query = make_callback("force_res:7:ABCD")

    asyncio.run(rep_bot.handle_callback(update, SimpleNamespace(user_data={})))

    assert query.answers and query.answers[0][1] is True
    assert "قدیمی" in query.answers[0][0]


def test_second_stage_reservation_cancel_revalidates_rep_and_account_before_refund(monkeypatch):
    rep = make_rep()
    stored = pending_reservation()
    calls = []
    monkeypatch.setattr(rep_panel.database, "get_reservation_by_id", lambda _rid: stored)
    monkeypatch.setattr(
        rep_panel.database, "cancel_reservation",
        lambda res_id, **kwargs: (calls.append((res_id, kwargs)) or (stored, 100_000)),
    )
    update, query = make_callback("rep:do_cancel_res:7:ABCD")

    asyncio.run(rep_panel.handle_rep_callbacks(update, SimpleNamespace(user_data={}), rep))

    assert calls and calls[0][0] == 7
    assert "400,000" in query.answers[0][0]


def test_second_stage_reservation_cancel_rejects_different_rep(monkeypatch):
    stored = pending_reservation(rep_id=99)
    monkeypatch.setattr(rep_panel.database, "get_reservation_by_id", lambda _rid: stored)
    monkeypatch.setattr(
        rep_panel.database, "cancel_reservation",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("other rep must not refund")),
    )
    update, query = make_callback("rep:do_cancel_res:7:ABCD")

    asyncio.run(rep_panel.handle_rep_callbacks(update, SimpleNamespace(user_data={}), make_rep(5)))

    assert query.answers and query.answers[0][1] is True
    assert "محدوده" in query.answers[0][0]


def test_second_stage_force_activation_uses_existing_lock_and_success_path(monkeypatch):
    rep = make_rep()
    stored = pending_reservation()
    events = []
    monkeypatch.setattr(rep_panel.database, "get_reservation_by_id", lambda _rid: stored)
    monkeypatch.setattr(rep_panel.database, "acquire_operation_lock", lambda *a, **k: "lock-token")
    monkeypatch.setattr(rep_panel.database, "release_operation_lock", lambda *a, **k: events.append("release"))
    monkeypatch.setattr(rep_panel.database, "activate_reservation", lambda *a, **k: events.append("activate") or stored)
    monkeypatch.setattr(
        rep_panel, "xui_manager",
        SimpleNamespace(renew_client=lambda *args, **kwargs: {"status": "success", "report": "renewed"}),
    )
    update, query = make_callback("rep:do_force_res:7:ABCD")

    asyncio.run(rep_panel.handle_rep_callbacks(update, SimpleNamespace(user_data={}), rep))

    assert events == ["activate", "release"]
    assert "فعال شد" in query.answers[0][0]


def test_rep_view_account_requires_exact_local_rep_scope_before_xui(monkeypatch):
    monkeypatch.setattr(rep_panel.database, "is_account_in_rep_scope", lambda *_: None)
    monkeypatch.setattr(
        rep_panel, "xui_manager",
        SimpleNamespace(inspect_client=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("X-UI must not be queried for an unassigned account")
        )),
    )
    update, query = make_callback("rep:view_acc:FOREIGN")
    asyncio.run(rep_panel.handle_rep_callbacks(update, SimpleNamespace(user_data={}), make_rep()))

    assert query.answers and query.answers[0][1] is True
    assert "محدوده ثبت‌شده" in query.answers[0][0]


def test_rep_view_account_uses_canonical_locally_assigned_email(monkeypatch):
    owned = {
        "rep_id": 5,
        "account_code": "ABCD",
        "email": "abcd@example.test",
    }
    inspected = []
    keyboards = []
    monkeypatch.setattr(rep_panel.database, "is_account_in_rep_scope", lambda *_: owned)
    monkeypatch.setattr(rep_panel.database, "get_pending_reservation", lambda _code: None)
    monkeypatch.setattr(
        rep_panel, "xui_manager",
        SimpleNamespace(
            inspect_client=lambda identifier, **kwargs: (
                inspected.append((identifier, kwargs))
                or {"status": "ok", "data": {"email": identifier, "raw": {"enable": True}}}
            ),
            format_status_report=lambda *_args, **_kwargs: "account report",
        ),
    )
    monkeypatch.setattr(
        rep_panel.nlp_matcher, "build_account_keyboard",
        lambda code, **kwargs: keyboards.append((code, kwargs)) or "keyboard",
    )
    update, query = make_callback("rep:view_acc:ABCD")

    asyncio.run(rep_panel.handle_rep_callbacks(update, SimpleNamespace(user_data={}), make_rep()))

    assert inspected[0][0] == "abcd@example.test"
    assert keyboards[0][0] == "ABCD"
    assert query.message.replies[0][1]["reply_markup"] == "keyboard"


def test_rep_search_fsm_rejects_xui_result_not_assigned_to_current_rep(monkeypatch):
    message = FakeMessage(text="FOREIGN-ACCOUNT")
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=88001))
    context = SimpleNamespace(user_data={
        "rep_state": {"action": "awaiting_search_query", "rep_id": 5, "ts": 9999999999}
    })
    monkeypatch.setattr(
        rep_panel, "xui_manager",
        SimpleNamespace(
            inspect_client=lambda *args, **kwargs: {
                "status": "ok",
                "data": {"email": "foreign@example.test"},
            },
            format_status_report=lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("foreign account details must not be shown")
            ),
        ),
    )
    monkeypatch.setattr(rep_panel.database, "is_account_in_rep_scope", lambda *_: None)

    handled = asyncio.run(rep_panel.process_rep_text_input(update, context, make_rep()))

    assert handled is True
    assert "محدوده ثبت‌شده" in message.replies[0][0]


def test_rep_fsm_cannot_be_consumed_under_a_different_rep_context(monkeypatch):
    message = FakeMessage(text="FOREIGN-ACCOUNT")
    update = SimpleNamespace(message=message, effective_user=SimpleNamespace(id=88001))
    context = SimpleNamespace(user_data={
        "rep_state": {"action": "awaiting_search_query", "rep_id": 99, "ts": 9999999999}
    })
    monkeypatch.setattr(
        rep_panel, "xui_manager",
        SimpleNamespace(inspect_client=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("stale rep state must stop before X-UI")
        )),
    )

    handled = asyncio.run(rep_panel.process_rep_text_input(update, context, make_rep(5)))

    assert handled is True
    assert "rep_state" not in context.user_data
    assert "نمایندگی دیگری" in message.replies[0][0]



def test_rep_finance_navigation_alias_opens_wallet_and_receipt_history(monkeypatch):
    monkeypatch.setattr(
        rep_panel.database,
        "get_rep_financial_summary",
        lambda _rep_id: {
            "balance_toman": 125_000,
            "total_debits": 500_000,
            "total_credits": 375_000,
            "tx_count": 3,
        },
    )
    monkeypatch.setattr(rep_panel.database, "get_rep_credit_limit", lambda _rep_id: 1_000_000)
    monkeypatch.setattr(rep_panel.database, "get_recent_ledger_entries", lambda **_kwargs: [])

    update, query = make_callback("rep:finance")
    asyncio.run(rep_panel.handle_rep_callbacks(update, SimpleNamespace(user_data={}), make_rep()))

    text = query.edits[0][0]
    keyboard = query.edits[0][1]["reply_markup"]
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]
    assert "کیف پول و تراز مالی" in text
    assert "rep:receipts_history" in callbacks
    assert "rep:main" in callbacks
