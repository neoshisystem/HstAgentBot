"""S50 regression suite for the defects found in the live ``@HstAB_bot`` Batch #02 test.

BUG-FIN-001  a panel rejection that never reached the panel locked the customer's money
BUG-FIN-002  no administrative queue for locked transactions and no manual refund
BUG-FIN-003  the receipt review guard read the active persona instead of the base role
BUG-SYS-001  a bare ``100…`` supergroup id was stored unsigned, so no topic could exist
BUG-SYS-002  the backup destination was read from an env constant, never from settings
BUG-SYS-003  the settings centre was a flat wall of keys and lost the caller's category
BUG-FSM-001  a stale conversation state in either store stole the next text message

Every assertion is offline: a temporary database, fake Telegram objects and fake
panel adapters. No socket is opened and no real panel is contacted.
"""
from __future__ import annotations

import asyncio
import base64
import pathlib
import time
from types import SimpleNamespace

import pytest

from migrations_runner import apply_migrations

import admin_panel
import backup
import config
import database
import finance_manager
import forum_manager
import internal_panel_compat
import rep_bot
import user_portal
import v52_router
from xui_result import XUIResult, classify_xui_result

PRODUCT_ROOT = pathlib.Path(__file__).resolve().parents[1]
# A fixed synthetic root: the CI runner has no .env, so config.ADMIN_TELEGRAM_ID
# is unset there and the suite must not depend on the deployment's value.
ROOT_TG_ID = 101258148
PAYER_TG_ID = 202500001
BOT_ID = 9002
NOW = 1794600000


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

class FakeMessage:
    def __init__(self, text=None, photo=None, caption=None):
        self.text = text
        self.photo = photo or []
        self.caption = caption
        self.message_thread_id = None
        self.replies = []
        self.deleted = False

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return SimpleNamespace(message_id=len(self.replies))

    async def reply_photo(self, photo=None, caption=None, **kwargs):
        self.replies.append((caption, kwargs))
        return SimpleNamespace(message_id=100 + len(self.replies))

    async def delete(self):
        self.deleted = True


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

    async def edit_message_reply_markup(self, reply_markup=None, **kwargs):
        self.edits.append(("", {"reply_markup": reply_markup}))


class FakeBot:
    def __init__(self, bot_id=BOT_ID):
        self.id = bot_id
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=1)

    async def send_photo(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=2)


def make_update(*, text=None, data="", user_id=None, chat_type="private"):
    user_id = ROOT_TG_ID if user_id is None else int(user_id)
    query = FakeQuery(data)
    message = FakeMessage(text=text)
    query.message = message
    chat = SimpleNamespace(id=user_id, type=chat_type)
    user = SimpleNamespace(id=user_id, full_name="Root", username="root", first_name="Root")
    return SimpleNamespace(
        callback_query=query,
        message=message,
        effective_message=SimpleNamespace(message_thread_id=None),
        effective_user=user,
        effective_chat=chat,
    ), query, message


def make_context(user_data=None, bot=None):
    return SimpleNamespace(user_data=dict(user_data or {}), bot=bot or FakeBot(), args=[])


def _callbacks(markup):
    return [item.callback_data for row in markup.inline_keyboard for item in row]


@pytest.fixture()
def s50(tmp_path, monkeypatch):
    """Temporary product database with the v5.2 migrations applied."""
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    for module in (config, database, rep_bot):
        monkeypatch.setattr(module, "ADMIN_TELEGRAM_ID", ROOT_TG_ID)
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "s50.sqlite3"))
    # The env constant must stay unset so the settings-driven branch is exercised.
    monkeypatch.setattr(backup, "BACKUPS_TOPIC_ID", 0)
    database.init_db()
    with database.get_db() as conn:
        apply_migrations(conn, PRODUCT_ROOT / "migrations", now=NOW)
        conn.execute(
            "INSERT INTO system_settings(key,value,label,description) "
            "VALUES('v52_persona_switcher_enabled','1','persona','persona switcher') "
            "ON CONFLICT(key) DO UPDATE SET value='1'"
        )
        conn.execute(
            "INSERT INTO bot_users(telegram_id,role,username,full_name,is_blocked) "
            "VALUES(?,'super_admin','root','Root',0) "
            "ON CONFLICT(telegram_id) DO UPDATE SET role='super_admin'",
            (ROOT_TG_ID,),
        )
        conn.execute(
            "INSERT INTO bot_users(telegram_id,role,username,full_name,is_blocked) "
            "VALUES(?,'end_user','payer','Payer',0) ON CONFLICT(telegram_id) DO NOTHING",
            (PAYER_TG_ID,),
        )
        conn.execute(
            "INSERT INTO user_wallets(user_id,balance_toman) VALUES(?,0) "
            "ON CONFLICT(user_id) DO UPDATE SET balance_toman=0",
            (ROOT_TG_ID,),
        )
        conn.execute(
            "INSERT INTO user_wallets(user_id,balance_toman) VALUES(?,0) "
            "ON CONFLICT(user_id) DO UPDATE SET balance_toman=0",
            (PAYER_TG_ID,),
        )
        conn.execute(
            "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,"
            "verified_at,revision) VALUES(?,NULL,1,NULL,'rootbot','active',?,1) "
            "ON CONFLICT(bot_id) DO NOTHING",
            (BOT_ID, NOW),
        )
        conn.execute(
            "INSERT INTO pricing_plans(plan_code,name,gb,days,user_price,rep_price,is_active) "
            "VALUES('P20-30','۲۰ گیگ ۱ ماهه',20,30,450000,400000,1) "
            "ON CONFLICT(plan_code) DO UPDATE SET is_active=1"
        )
        conn.commit()
    for actor in (ROOT_TG_ID, PAYER_TG_ID):
        v52_router.DEFAULT_FSM.clear({"bot_id": BOT_ID, "actor_tg_id": actor, "chat_id": actor})
    yield
    for actor in (ROOT_TG_ID, PAYER_TG_ID):
        v52_router.DEFAULT_FSM.clear({"bot_id": BOT_ID, "actor_tg_id": actor, "chat_id": actor})


def _set_persona(persona: str, user_id: int = ROOT_TG_ID) -> None:
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO user_settings(user_id,active_persona) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET active_persona=excluded.active_persona",
            (int(user_id), persona),
        )
        conn.commit()


def _credit(user_id: int, amount: int) -> int:
    with database.get_db() as conn:
        conn.execute(
            "UPDATE user_wallets SET balance_toman = balance_toman + ? WHERE user_id = ?",
            (int(amount), int(user_id)),
        )
        conn.commit()
    return database.get_user_wallet_balance(int(user_id))


def _locked_intent(user_id: int, amount: int = 450000) -> str:
    """Create one intent and drive it into the locked (processing) state."""
    intent_id = database.create_user_action_intent(
        user_id=user_id, action_type="buy", plan_code="P20-30", plan_name="۲۰ گیگ ۱ ماهه",
        gb=20, days=30, amount=amount,
    )
    started = database.begin_user_action_intent(intent_id, user_id, "buy")
    assert started["status"] == "started", started
    return intent_id


# --------------------------------------------------------------------------- #
# BUG-FIN-001 — a pre-dispatch rejection is a definite failure, never a lock
# --------------------------------------------------------------------------- #

def test_not_dispatched_response_is_classified_as_a_definite_failure():
    assert classify_xui_result(
        {"status": "unknown", "state": "unknown", "dispatched": False,
         "reason": "account_binding_missing_or_ambiguous"}
    ) == XUIResult.FAILURE


def test_genuinely_ambiguous_response_stays_unknown():
    # No ``dispatched`` marker: the request may have reached the panel.
    assert classify_xui_result({"status": "unknown", "state": "unknown"}) == XUIResult.UNKNOWN
    assert classify_xui_result(
        {"status": "failed", "success": True}
    ) == XUIResult.UNKNOWN


def test_legacy_pool_allocation_signature_reports_not_dispatched(s50):
    # The historical call shape ``allocate_pool_client(gb, days)`` cannot resolve
    # an account binding, so nothing is ever sent to the panel.
    result = internal_panel_compat.allocate_pool_client(20.0, 30, comment="x", scope="all")
    assert result["dispatched"] is False
    assert classify_xui_result(result) == XUIResult.FAILURE


def test_renewal_without_an_operation_id_reports_not_dispatched(s50):
    result = internal_panel_compat.renew_client("no-such-account", 20, 30)
    assert result["dispatched"] is False
    assert classify_xui_result(result) == XUIResult.FAILURE


def test_user_purchase_that_never_reached_the_panel_refunds_atomically(s50, monkeypatch):
    """The exact live defect: 450,000 locked with no service and no refund."""
    _credit(PAYER_TG_ID, 500000)
    intent_id = database.create_user_action_intent(
        user_id=PAYER_TG_ID, action_type="buy", plan_code="P20-30",
        plan_name="۲۰ گیگ ۱ ماهه", gb=20, days=30, amount=450000,
    )
    monkeypatch.setattr(
        user_portal.xui_manager, "allocate_pool_client",
        lambda *a, **k: {"status": "unknown", "state": "unknown", "dispatched": False,
                         "reason": "account_binding_missing_or_ambiguous"},
    )
    update, query, _message = make_update(data=f"user:do_buy:{intent_id}", user_id=PAYER_TG_ID)
    asyncio.run(user_portal.handle_user_callbacks(update, make_context()))

    assert database.get_user_wallet_balance(PAYER_TG_ID) == 500000
    assert database.get_user_locked_amount(PAYER_TG_ID) == 0
    assert any("بازگشت داده شد" in str(text) for text, _kw in query.message.replies)


def test_wallet_view_separates_spendable_balance_from_the_escrowed_amount(s50):
    _credit(PAYER_TG_ID, 500000)
    intent_id = _locked_intent(PAYER_TG_ID)
    assert database.get_user_locked_amount(PAYER_TG_ID) == 450000

    update, query, _message = make_update(data="user:wallet", user_id=PAYER_TG_ID)
    asyncio.run(user_portal.handle_user_callbacks(update, make_context()))

    text, kwargs = query.edits[-1]
    assert "موجودی قابل برداشت" in text
    assert "مبلغ محفوظ در تراکنش معلق" in text
    assert "user:pending" in _callbacks(kwargs["reply_markup"])

    update, query, _message = make_update(data="user:pending", user_id=PAYER_TG_ID)
    asyncio.run(user_portal.handle_user_callbacks(update, make_context()))
    text, _kwargs = query.edits[-1]
    assert intent_id[:8] in text
    assert "۴۵۰" not in text  # the raw number is rendered with ASCII digits
    assert "450,000" in text


def test_purchase_confirmation_page_carries_standard_navigation(s50):
    _credit(PAYER_TG_ID, 500000)
    intent_id = database.create_user_action_intent(
        user_id=PAYER_TG_ID, action_type="buy", plan_code="P20-30",
        plan_name="۲۰ گیگ ۱ ماهه", gb=20, days=30, amount=450000,
    )
    update, query, _message = make_update(
        data=f"user:confirm_buy:{intent_id}", user_id=PAYER_TG_ID)
    asyncio.run(user_portal.handle_user_callbacks(update, make_context()))

    _text, kwargs = query.edits[-1]
    callbacks = _callbacks(kwargs["reply_markup"])
    assert "user:buy_service" in callbacks
    assert "user:main" in callbacks


# --------------------------------------------------------------------------- #
# BUG-FIN-002 — the administrative queue for locked money and manual refund
# --------------------------------------------------------------------------- #

def test_pending_transaction_queue_lists_every_locked_intent(s50):
    _credit(PAYER_TG_ID, 500000)
    intent_id = _locked_intent(PAYER_TG_ID)

    update, query, _message = make_update(data="admin:pending_tx")
    asyncio.run(admin_panel.handle_admin_callbacks(update, make_context()))

    text, kwargs = query.edits[-1]
    assert intent_id[:10] in text
    assert "450,000" in text
    assert f"admin:pending_tx:{intent_id}" in _callbacks(kwargs["reply_markup"])


def test_pending_transaction_card_offers_profile_reconcile_apply_and_refund(s50):
    _credit(PAYER_TG_ID, 500000)
    intent_id = _locked_intent(PAYER_TG_ID)

    update, query, _message = make_update(data=f"admin:pending_tx:{intent_id}")
    asyncio.run(admin_panel.handle_admin_callbacks(update, make_context()))

    callbacks = _callbacks(query.edits[-1][1]["reply_markup"])
    # S52 made the profile callback carry the surface it was opened from so its
    # back button returns to this very transaction.
    assert any(cb.startswith(f"admin:user_detail:{PAYER_TG_ID}") for cb in callbacks)
    assert f"admin:pending_recheck:{intent_id}" in callbacks
    assert f"admin:pending_confirm:{intent_id}" in callbacks
    assert f"admin:pending_refund:{intent_id}" in callbacks


def test_manual_refund_returns_the_money_and_releases_the_lock(s50):
    _credit(PAYER_TG_ID, 500000)
    intent_id = _locked_intent(PAYER_TG_ID)
    assert database.get_user_wallet_balance(PAYER_TG_ID) == 50000

    update, query, _message = make_update(data=f"admin:pending_refund:{intent_id}")
    asyncio.run(admin_panel.handle_admin_callbacks(update, make_context()))
    callbacks = _callbacks(query.edits[-1][1]["reply_markup"])
    assert f"admin:pending_do_refund:{intent_id}" in callbacks

    update, query, _message = make_update(data=f"admin:pending_do_refund:{intent_id}")
    context = make_context()
    asyncio.run(admin_panel.handle_admin_callbacks(update, context))

    assert database.get_user_wallet_balance(PAYER_TG_ID) == 500000
    assert database.get_user_locked_amount(PAYER_TG_ID) == 0
    with database.get_db() as conn:
        lock = conn.execute(
            "SELECT 1 FROM operation_locks WHERE lock_key = ?", (f"user-buy:{PAYER_TG_ID}",)
        ).fetchone()
    assert lock is None
    assert context.bot.sent, "the payer must be notified about the refund"


def test_confirmed_delivery_keeps_the_debit_and_releases_the_lock(s50):
    _credit(PAYER_TG_ID, 500000)
    intent_id = _locked_intent(PAYER_TG_ID)

    update, query, _message = make_update(data=f"admin:pending_do_apply:{intent_id}")
    asyncio.run(admin_panel.handle_admin_callbacks(update, make_context()))

    assert database.get_user_wallet_balance(PAYER_TG_ID) == 50000
    assert database.get_user_locked_amount(PAYER_TG_ID) == 0


def test_reconcile_button_never_claims_a_panel_query(s50):
    _credit(PAYER_TG_ID, 500000)
    intent_id = _locked_intent(PAYER_TG_ID)

    update, query, _message = make_update(data=f"admin:pending_recheck:{intent_id}")
    asyncio.run(admin_panel.handle_admin_callbacks(update, make_context()))

    text = query.edits[-1][0]
    assert "هیچ درخواستی به پنل" in text


# --------------------------------------------------------------------------- #
# BUG-FIN-003 — receipt review follows the permanent role, not the persona
# --------------------------------------------------------------------------- #

def test_root_in_the_representative_persona_can_still_review_a_receipt(s50):
    _set_persona("rep")
    code = database.create_payment_receipt(
        user_id=PAYER_TG_ID, role="end_user", amount_toman=100000,
    )
    with database.get_db() as conn:
        receipt_id = int(conn.execute(
            "SELECT id FROM payment_receipts WHERE tracking_code = ?", (code,)
        ).fetchone()["id"])

    update, query, _message = make_update(data=f"admin_approve_rcp_{receipt_id}")
    asyncio.run(rep_bot.handle_callback(update, make_context()))

    answers = [str(text or "") for text, _alert in query.answers]
    assert not any("فقط برای ادمین کل سیستم" in text for text in answers), answers


def test_settled_receipt_status_chip_only_reports_the_decision(s50):
    update, query, _message = make_update(data="noop")
    asyncio.run(rep_bot.handle_callback(update, make_context()))
    assert query.answers[-1][0] == "این فیش قبلاً تعیین تکلیف شده است."


def test_reviewed_receipt_keyboard_keeps_profile_and_admin_home():
    markup = finance_manager.build_admin_reviewed_keyboard("approved", PAYER_TG_ID)
    callbacks = _callbacks(markup)
    assert "noop" in callbacks
    # S52 made the profile callback carry the surface it was opened from so its
    # back button returns to this very transaction.
    assert any(cb.startswith(f"admin:user_detail:{PAYER_TG_ID}") for cb in callbacks)
    assert "admin:main" in callbacks


# --------------------------------------------------------------------------- #
# BUG-SYS-001 — signed supergroup id and real topic provisioning
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ("1004377897090", -1004377897090),
    ("-1004377897090", -1004377897090),
    (" -1004377897090 ", -1004377897090),
    ("123456789", -123456789),
    ("0", 0),
])
def test_group_id_normalization(raw, expected):
    assert forum_manager.normalize_group_chat_id(raw) == expected
    assert database.normalize_group_chat_id(raw) == expected


def test_saving_a_bare_supergroup_id_stores_the_signed_form(s50):
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO system_settings(key,value,label,description) "
            "VALUES('main_group_id','0','گروه','group') ON CONFLICT(key) DO NOTHING"
        )
        conn.commit()
    database.set_setting("main_group_id", "1004377897090")
    assert database.get_setting("main_group_id") == "-1004377897090"
    assert database.get_main_group_id() == -1004377897090


class FakeForumApi:
    """Minimal Telegram Bot API surface used by ``sync_report_topics``."""

    def __init__(self, chat_id):
        self.chat_id = chat_id
        self.created = []
        self.edited = []
        self._next_thread = 100

    def get_chat(self, chat_id=None):
        return {"id": chat_id, "type": "supergroup", "is_forum": True}

    def get_me(self):
        return {"id": BOT_ID}

    def get_chat_member(self, chat_id=None, user_id=None):
        return {"status": "administrator", "can_manage_topics": True, "can_pin_messages": True}

    def create_forum_topic(self, chat_id=None, name=None):
        self._next_thread += 1
        self.created.append((chat_id, name))
        return {"message_thread_id": self._next_thread}

    def edit_forum_topic(self, chat_id=None, message_thread_id=None, name=None):
        self.edited.append((chat_id, message_thread_id, name))
        return True


def test_provisioning_creates_the_five_standard_topics_and_persists_thread_ids(s50):
    group_id = -1004377897090
    api = FakeForumApi(group_id)
    with database.get_db() as conn:
        result = forum_manager.provision_standard_topics(
            conn, api, bot_id=BOT_ID, group_chat_id=group_id, now=NOW,
        )
    assert result["total"] == 6
    assert result["active"] == 6
    assert len(api.created) == 6
    assert set(result["settings"]) == {
        "operational_logs_topic_id", "pending_receipts_topic_id",
        "backups_topic_id", "rep_logs_topic_id", "support_tickets_topic_id",
        "error_logs_topic_id",
    }
    written = database.apply_forum_topic_settings(result["settings"])
    assert written == 6
    for key, value in result["settings"].items():
        assert database.get_setting(key) == value


def test_every_standard_topic_key_is_routable(s50):
    import report_router
    for topic_key in forum_manager.STANDARD_TOPIC_KEYS:
        assert topic_key in report_router._GROUP_TOPICS, topic_key
        assert topic_key in forum_manager.TOPIC_TITLES, topic_key


# --------------------------------------------------------------------------- #
# BUG-SYS-002 — the backup destination comes from settings
# --------------------------------------------------------------------------- #

def test_backup_destination_uses_the_configured_group_topic(s50):
    database.set_setting("main_group_id", "1004377897090")
    database.set_setting("backups_topic_id", "104")
    destination = backup.resolve_backup_destination()
    assert destination["configured"] is True
    assert destination["target"] == "group_topic"
    assert destination["chat_id"] == -1004377897090
    assert destination["topic_id"] == 104


def test_backup_destination_falls_back_to_the_admin_dm(s50):
    database.set_setting("main_group_id", "1004377897090")
    database.set_setting("backups_topic_id", "0")
    database.set_setting("admin_telegram_id", str(ROOT_TG_ID))
    destination = backup.resolve_backup_destination()
    assert destination["configured"] is True
    assert destination["target"] == "admin_dm"
    assert destination["chat_id"] == ROOT_TG_ID


def test_backup_destination_reports_the_missing_configuration(s50):
    database.set_setting("main_group_id", "0")
    database.set_setting("backups_topic_id", "0")
    database.set_setting("admin_telegram_id", "0")
    destination = backup.resolve_backup_destination()
    assert destination["configured"] is False
    assert "not configured" in destination["reason"]


# --------------------------------------------------------------------------- #
# BUG-SYS-003 — categorised settings that return to their own category
# --------------------------------------------------------------------------- #

def test_every_live_setting_belongs_to_exactly_one_category(s50):
    known = set(admin_panel.SETTINGS_CATEGORY_OF)
    categories = [key for key, _title, _keys in admin_panel.SETTINGS_CATEGORIES]
    assert len(categories) == len(set(categories)) == 5
    for setting in database.get_all_settings():
        key = setting["key"]
        if key in known:
            assert admin_panel.settings_category_of(key) in categories
        else:
            assert admin_panel.settings_category_of(key) == "other"


def test_settings_menu_offers_the_five_categories(s50):
    update, query, _message = make_update(data="admin:settings")
    asyncio.run(admin_panel.handle_admin_callbacks(update, make_context()))
    callbacks = _callbacks(query.edits[-1][1]["reply_markup"])
    for cat_key, _title, _keys in admin_panel.SETTINGS_CATEGORIES:
        assert f"admin:settings_cat:{cat_key}" in callbacks


def test_settings_category_page_lists_its_parameters(s50):
    update, query, _message = make_update(data="admin:settings_cat:group")
    asyncio.run(admin_panel.handle_admin_callbacks(update, make_context()))
    text, kwargs = query.edits[-1]
    assert "سوپرگروه" in text
    assert "admin:prompt_setting:main_group_id:group" in _callbacks(kwargs["reply_markup"])


def test_editing_a_setting_returns_to_the_same_category(s50):
    update, _query, message = make_update(text="300")
    context = make_context({
        "admin_state": {"action": "edit_setting", "key": "watchdog_interval_sec",
                        "label": "فاصله بررسی دیده‌بان (ثانیه)", "category": "monitoring",
                        "ts": time.time()},
    })
    asyncio.run(admin_panel.process_admin_text_input(update, context))

    text, kwargs = message.replies[-1]
    assert database.get_setting("watchdog_interval_sec") == "300"
    assert "دیده‌بان" in text
    # The reply re-renders the SAME category, not the admin main menu.
    callbacks = _callbacks(kwargs["reply_markup"])
    assert "admin:prompt_setting:watchdog_interval_sec:monitoring" in callbacks
    assert "admin:prompt_setting:pool_warning_threshold:monitoring" in callbacks
    assert "admin:settings" in callbacks


def test_editing_the_group_id_provisions_topics_and_normalizes_the_value(s50, monkeypatch):
    async def fake_sync(_bot, _bot_id, group_chat_id, *, owner_key="root"):
        assert group_chat_id == -1004377897090
        return {"state": "completed", "topics": 5, "active": 5, "settings_written": 5}

    monkeypatch.setattr(rep_bot, "_sync_v52_forum_topics", fake_sync)
    update, _query, message = make_update(text="1004377897090")
    context = make_context({
        "admin_state": {"action": "edit_setting", "key": "main_group_id",
                        "label": "شناسه گروه اصلی", "category": "group",
                        "ts": time.time()},
    })
    asyncio.run(admin_panel.process_admin_text_input(update, context))

    assert database.get_setting("main_group_id") == "-1004377897090"
    text, _kwargs = message.replies[-1]
    assert "همگام‌سازی تاپیک‌ها" in text


# --------------------------------------------------------------------------- #
# BUG-FSM-001 — no stale conversation state may steal the next message
# --------------------------------------------------------------------------- #

def test_reset_clears_both_state_stores(s50):
    context = make_context({"user_state": {"action": "x"}, "fsm_state": "STATE"})
    v52_router.DEFAULT_FSM.set(
        {"bot_id": BOT_ID, "actor_tg_id": PAYER_TG_ID, "chat_id": PAYER_TG_ID},
        "support_ticket_body", payload={"account_id": 1}, now=NOW,
    )
    result = rep_bot._reset_all_conversation_states(PAYER_TG_ID, BOT_ID, context)

    assert result["fsm_cleared"] is True
    assert set(result["cleared_keys"]) == {"user_state", "fsm_state"}
    assert context.user_data == {}
    assert v52_router.DEFAULT_FSM.peek(
        {"bot_id": BOT_ID, "actor_tg_id": PAYER_TG_ID, "chat_id": PAYER_TG_ID},
        now=NOW,
    ) is None


def test_starting_the_wallet_topup_drops_an_abandoned_ticket_draft(s50):
    """The live defect: the amount was recorded as a ticket body instead."""
    _set_persona("end_user")
    v52_router.DEFAULT_FSM.set(
        {"bot_id": BOT_ID, "actor_tg_id": ROOT_TG_ID, "chat_id": ROOT_TG_ID},
        "support_ticket_body", payload={"account_id": 1}, now=int(__import__("time").time()),
    )
    update, _query, _message = make_update(data="user:deposit_start")
    asyncio.run(rep_bot.handle_callback(update, make_context()))

    assert v52_router.DEFAULT_FSM.peek(
        {"bot_id": BOT_ID, "actor_tg_id": ROOT_TG_ID, "chat_id": ROOT_TG_ID},
        now=int(__import__("time").time()),
    ) is None


def test_cancel_command_clears_every_pending_state(s50):
    update, _query, message = make_update(text="/cancel")
    context = make_context({"user_state": {"a": 1}, "rep_state": {"b": 2}, "fsm_state": "S"})
    asyncio.run(rep_bot.cancel_cmd(update, context))

    assert context.user_data == {}
    assert message.replies, "cancellation must always answer"


def test_workflow_start_and_navigation_callbacks_are_recognised():
    assert rep_bot._is_navigation_callback("user:main") is True
    assert rep_bot._is_navigation_callback("v52:persona:rep") is True
    assert rep_bot._is_navigation_callback("user:do_buy:abc") is False
    assert rep_bot._is_workflow_start_callback("user:deposit_start") is True
    assert rep_bot._is_workflow_start_callback("v52:newticket") is True
    assert rep_bot._is_workflow_start_callback("user:wallet") is False
