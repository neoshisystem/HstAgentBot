import asyncio
from types import SimpleNamespace

import nlp_matcher
import rep_bot
import rep_panel


class FakeMessage:
    def __init__(self):
        self.photo = None
        self.message_thread_id = None
        self.message_id = 123
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

    async def edit_message_reply_markup(self, **kwargs):
        self.edits.append(kwargs)

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


def make_update(data, user_id=88001):
    query = FakeQuery(data)
    user = SimpleNamespace(id=user_id, username="rep", full_name="Representative")
    chat = SimpleNamespace(id=user_id, type="private")
    return SimpleNamespace(
        callback_query=query,
        effective_user=user,
        effective_chat=chat,
        effective_message=SimpleNamespace(message_thread_id=None),
        message=query.message,
    ), query


def test_account_keyboard_has_independent_policy_toggles_delivery_row_and_rep_back(monkeypatch):
    monkeypatch.setattr(nlp_matcher.database, "get_pricing_plans", lambda *a, **k: [])
    keyboard = nlp_matcher.build_account_keyboard(
        "ACC-1", is_enabled=True, on_first_connect=True, role="rep",
        pending_res={}, rollover_enabled=False,
    )
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    by_callback = {button.callback_data: button for button in buttons}
    assert by_callback["set_mode:ACC-1:0"].text == "⏱ شروع: با اولین اتصال"
    assert by_callback["set_rollover:ACC-1:1"].text == "⏹ رول‌اوور: غیرفعال"
    delivery_row = next(row for row in keyboard.inline_keyboard if any(
        button.callback_data == "deliv_qr:ACC-1" for button in row
    ))
    assert len(delivery_row) == 2
    assert [button.callback_data for button in delivery_row] == [
        "deliv_qr:ACC-1", "deliv_cfg:ACC-1"
    ]
    assert not any(button.callback_data == "deliv_sub:ACC-1" for button in buttons)
    assert keyboard.inline_keyboard[-1][0].callback_data == "rep:main"


def test_admin_account_keyboard_has_admin_back_and_active_rollover_label(monkeypatch):
    monkeypatch.setattr(nlp_matcher.database, "get_pricing_plans", lambda *a, **k: [])
    keyboard = nlp_matcher.build_account_keyboard(
        "ADMIN-1", role="super_admin", pending_res={}, rollover_enabled=True
    )
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    assert any(
        button.callback_data == "set_rollover:ADMIN-1:0"
        and button.text == "🔄 رول‌اوور: فعال"
        for button in buttons
    )
    assert keyboard.inline_keyboard[-1][0].callback_data == "admin:main"


def test_expanded_plans_are_single_column(monkeypatch):
    monkeypatch.setattr(
        nlp_matcher.database, "get_pricing_plans",
        lambda **kwargs: [
            {"is_quick": 0, "name": "Long Plan 1", "plan_code": "p1"},
            {"is_quick": 0, "name": "Long Plan 2", "plan_code": "p2"},
        ],
    )
    keyboard = nlp_matcher.build_more_plans_keyboard("ACC-1")
    assert [len(row) for row in keyboard.inline_keyboard] == [1, 1, 1]
    assert keyboard.inline_keyboard[-1][0].callback_data == "main_menu:ACC-1"


def test_rep_issuance_plans_are_single_column(monkeypatch):
    monkeypatch.setattr(
        rep_panel.database, "get_pricing_plans",
        lambda **kwargs: [
            {"name": "Long tariff one", "rep_price": 1000, "plan_code": "p1"},
            {"name": "Long tariff two", "rep_price": 2000, "plan_code": "p2"},
        ],
    )
    update, query = make_update("rep:new_acc_plans")
    asyncio.run(rep_panel.handle_rep_callbacks(
        update, SimpleNamespace(user_data={}),
        {"id": 5, "name": "Rep", "scope_filter": "all"},
    ))
    keyboard = query.edits[0][1]["reply_markup"]
    assert [len(row) for row in keyboard.inline_keyboard] == [1, 1, 1]
    assert keyboard.inline_keyboard[0][0].callback_data == "rep:new_plan_chosen:p1"


def _patch_rep_callback_preflight(monkeypatch):
    rep = {"id": 5, "name": "Rep", "scope_filter": "all"}
    account = {
        "id": 1, "rep_id": 5, "account_code": "ACC-1",
        "email": "acc-1@example.test", "sub_id": "old-sub",
    }
    modes = {"first_connect_mode": False, "rollover_enabled": True}
    monkeypatch.setattr(rep_bot.database, "get_user_role", lambda *a, **k: ("rep", rep))
    monkeypatch.setattr(rep_bot.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(rep_bot.database, "is_account_in_rep_scope", lambda *_: account)
    monkeypatch.setattr(rep_bot.database, "get_account_renewal_modes", lambda *_: dict(modes))
    monkeypatch.setattr(
        rep_bot, "xui_manager",
        SimpleNamespace(inspect_client=lambda *_a, **_k: {
            "status": "ok",
            "data": {
                "raw": {"email": "acc-1@example.test", "enable": True, "comment": ""},
                "email": "acc-1@example.test", "sub_id": "old-sub",
            },
        }),
    )
    monkeypatch.setattr(rep_bot.nlp_matcher, "build_account_keyboard", lambda *a, **k: "keyboard")
    return account, modes


def test_set_rollover_callback_persists_account_value_and_refreshes_card(monkeypatch):
    _account, modes = _patch_rep_callback_preflight(monkeypatch)
    writes = []
    def save_rollover(code, enabled):
        writes.append((code, enabled))
        modes["rollover_enabled"] = enabled
        return True
    monkeypatch.setattr(rep_bot.database, "set_account_rollover_mode", save_rollover)
    update, query = make_update("set_rollover:ACC-1:0")
    asyncio.run(rep_bot.handle_callback(update, SimpleNamespace(user_data={})))

    assert writes == [("ACC-1", False)]
    assert query.edits and query.edits[0]["reply_markup"] == "keyboard"
    assert query.answers and "غیرفعال" in query.answers[-1][0]


def test_set_mode_callback_persists_account_value_and_actor_id(monkeypatch):
    _account, modes = _patch_rep_callback_preflight(monkeypatch)
    writes = []
    def save_first_connect(code, enabled, actor_id=None):
        writes.append((code, enabled, actor_id))
        modes["first_connect_mode"] = enabled
        return True
    monkeypatch.setattr(rep_bot.database, "set_account_first_connect_mode", save_first_connect)
    update, query = make_update("set_mode:ACC-1:1")
    asyncio.run(rep_bot.handle_callback(update, SimpleNamespace(user_data={})))

    assert writes == [("ACC-1", True, 88001)]
    assert query.edits and query.answers and "اولین اتصال" in query.answers[-1][0]


def _patch_card_delivery(monkeypatch):
    _patch_rep_callback_preflight(monkeypatch)
    return SimpleNamespace(
        inspect_client=lambda *_a, **_k: {
            "status": "ok",
            "data": {
                "raw": {"email": "acc-1@example.test", "enable": True, "comment": ""},
                "email": "acc-1@example.test", "sub_id": "old-sub",
            },
        },
        get_sub_url=lambda _sub_id: "https://sub.example.test/token",
        get_client_links=lambda _email: [],
    )


def test_new_and_legacy_config_delivery_callbacks_are_both_supported(monkeypatch):
    monkeypatch.setattr(rep_bot, "xui_manager", _patch_card_delivery(monkeypatch))
    for callback in ("deliv_cfg:ACC-1", "deliv_conf_qr:ACC-1"):
        update, query = make_update(callback)
        asyncio.run(rep_bot.handle_callback(update, SimpleNamespace(user_data={})))
        assert query.answers and query.answers[-1][1] is True
        assert "کانفیگی" in query.answers[-1][0]


def test_legacy_deliv_sub_backend_acknowledges_immediately_and_only_once(monkeypatch):
    monkeypatch.setattr(rep_bot, "xui_manager", _patch_card_delivery(monkeypatch))
    update, query = make_update("deliv_sub:ACC-1")
    asyncio.run(rep_bot.handle_callback(update, SimpleNamespace(user_data={})))

    assert query.answers == [(None, False)]
    assert query.edits and query.edits[0][0].startswith("🔗 **لینک سابسکریپشن")
