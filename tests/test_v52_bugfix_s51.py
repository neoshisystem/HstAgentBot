"""S51 regression suite for the registration/referral module and real panel creation.

BUG-REG-001  three-mode registration, referral codes, rewards and tenant isolation
MISSION-2    a real 3X-UI client is created on an active panel when the pool is empty

Every assertion is offline: a temporary database, fake Telegram objects and a fake
panel adapter. No socket is opened and no real panel is contacted.
"""
from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import time
from types import SimpleNamespace

import pytest

from migrations_runner import apply_migrations

import admin_panel
import config
import database
import panel_provisioning_service as provisioning
import registration_service as reg
import registration_ui
import rep_bot
import user_portal
import v52_router

PRODUCT_ROOT = pathlib.Path(__file__).resolve().parents[1]
ROOT_TG_ID = 101258148
BOT_ID = 9003
REP_BOT_ID = 9004
JOINER_TG_ID = 303500001
REFERRER_TG_ID = 303500002
NOW = 1794600000


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #

class FakeMessage:
    def __init__(self, text=None):
        self.text = text
        self.message_thread_id = None
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return SimpleNamespace(message_id=len(self.replies))


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
    def __init__(self, bot_id=BOT_ID, username="HstAB_bot"):
        self.id = bot_id
        self.username = username
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=1)


def make_update(*, text=None, data="", user_id=None, bot_id=BOT_ID, args=None):
    user_id = ROOT_TG_ID if user_id is None else int(user_id)
    query = FakeQuery(data)
    message = FakeMessage(text=text)
    query.message = message
    chat = SimpleNamespace(id=user_id, type="private")
    user = SimpleNamespace(id=user_id, full_name="Root", username="root", first_name="Root")
    return SimpleNamespace(
        callback_query=query,
        message=message,
        effective_message=SimpleNamespace(message_thread_id=None),
        effective_user=user,
        effective_chat=chat,
    ), query, message


def make_context(user_data=None, bot=None, args=None):
    return SimpleNamespace(
        user_data=dict(user_data or {}), bot=bot or FakeBot(), args=list(args or []))


def _callbacks(markup):
    return [item.callback_data for row in markup.inline_keyboard for item in row]


@pytest.fixture()
def s51(tmp_path, monkeypatch):
    """Temporary product database with the v5.2 migrations applied."""
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    for module in (config, database, rep_bot):
        monkeypatch.setattr(module, "ADMIN_TELEGRAM_ID", ROOT_TG_ID)
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "s51.sqlite3"))
    database.init_db()
    with database.get_db() as conn:
        apply_migrations(conn, PRODUCT_ROOT / "migrations", now=NOW)
        conn.execute(
            "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,"
            "verified_at,revision) VALUES(?,NULL,1,NULL,'HstAB_bot','active',?,1) "
            "ON CONFLICT(bot_id) DO NOTHING", (BOT_ID, NOW),
        )
        conn.execute(
            "INSERT INTO representatives(rep_code,name,telegram_id,scope_filter,is_active) "
            "VALUES('rep_s51','Rep S51',?, 'all', 1) ON CONFLICT(rep_code) DO NOTHING",
            (ROOT_TG_ID + 5,),
        )
        rep_id = int(conn.execute(
            "SELECT id FROM representatives WHERE rep_code='rep_s51'").fetchone()["id"])
        conn.execute(
            "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,"
            "verified_at,revision) VALUES(?,?,0,NULL,'RepS51_bot','active',?,1) "
            "ON CONFLICT(bot_id) DO NOTHING", (REP_BOT_ID, rep_id, NOW),
        )
        for actor, role in ((ROOT_TG_ID, "super_admin"), (REFERRER_TG_ID, "end_user"),
                            (JOINER_TG_ID, "end_user")):
            conn.execute(
                "INSERT INTO bot_users(telegram_id,role,username,full_name,is_blocked) "
                "VALUES(?,?,?,?,0) ON CONFLICT(telegram_id) DO UPDATE SET role=excluded.role",
                (actor, role, f"u{actor}", f"User {actor}"),
            )
            conn.execute(
                "INSERT INTO user_wallets(user_id,balance_toman) VALUES(?,0) "
                "ON CONFLICT(user_id) DO UPDATE SET balance_toman=0", (actor,),
            )
        # DEFAULT_SETTINGS is built at import time from the deployment's .env, so
        # the CI runner (which has none) would seed a zero admin id. Pin it.
        conn.execute(
            "INSERT INTO system_settings(key,value,label,description) "
            "VALUES('admin_telegram_id',?,'','') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(ROOT_TG_ID),))
        conn.execute(
            "INSERT INTO panels(id,panel_code,name,panel_type,lifecycle,is_hidden,"
            "for_end_users,for_reps,default_inbound_ids_json,revision,created_at) "
            "VALUES(1,'xui-1','Panel 1','3x-ui','active',0,1,1,'[]',1,?) "
            "ON CONFLICT(id) DO NOTHING", (NOW,),
        )
        conn.commit()
    yield {"rep_id": rep_id}
    for actor in (ROOT_TG_ID, REFERRER_TG_ID, JOINER_TG_ID):
        v52_router.DEFAULT_FSM.clear(
            {"bot_id": BOT_ID, "actor_tg_id": actor, "chat_id": actor})


def _set_setting(key: str, value: str) -> None:
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO system_settings(key,value,label,description) VALUES(?,?,'','') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))
        conn.commit()


def _status_of(user_id: int) -> str:
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT registration_status FROM bot_users WHERE telegram_id=?", (int(user_id),)
        ).fetchone()
    return str(row["registration_status"]) if row else ""


def _code_of(user_id: int) -> str:
    with database.get_db() as conn:
        code = reg.generate_referral_code(conn, int(user_id))
        conn.commit()
    return code


# --------------------------------------------------------------------------- #
# BUG-REG-001 — mode resolution and tenant isolation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("raw,expected", [
    ("open", "open"), ("APPROVAL_REQUIRED", "approval_required"),
    ("referral_only", "referral_only"), ("inherit", "inherit"), ("", "inherit"),
])
def test_normalize_mode(raw, expected):
    assert reg.normalize_mode(raw) == expected


def test_invalid_mode_is_rejected():
    with pytest.raises(reg.RegistrationError):
        reg.normalize_mode("anything_else")


def test_referral_code_normalization_strips_and_uppercases():
    assert reg.normalize_referral_code(" ab23cd45 ") == "AB23CD45"
    assert reg.normalize_referral_code("") == ""
    with pytest.raises(reg.RegistrationError):
        reg.normalize_referral_code("bad-code!")


def test_mode_resolution_prefers_rep_then_bot_then_global(s51):
    _set_setting("registration_mode", "open")
    with database.get_db() as conn:
        assert reg.resolve_mode(conn, bot_id=BOT_ID, rep_id=s51["rep_id"]) == "open"
        # A rep override wins over everything.
        reg.set_mode(conn, mode="referral_only", rep_id=s51["rep_id"])
        conn.commit()
        assert reg.resolve_mode(conn, bot_id=BOT_ID, rep_id=s51["rep_id"]) == "referral_only"
        # The bot override applies when no rep scope is supplied.
        reg.set_mode(conn, mode="approval_required", bot_id=BOT_ID)
        conn.commit()
        assert reg.resolve_mode(conn, bot_id=BOT_ID) == "approval_required"
        assert reg.resolve_mode(conn) == "open"
        # A rep bot inherits its own rep scope, never the primary bot's.
        assert reg.resolve_mode(conn, bot_id=REP_BOT_ID, rep_id=s51["rep_id"]) == "referral_only"


# --------------------------------------------------------------------------- #
# BUG-REG-001 — the three registration modes
# --------------------------------------------------------------------------- #

def test_open_mode_admits_a_new_user_immediately(s51):
    _set_setting("registration_mode", "open")
    with database.get_db() as conn:
        result = reg.register(conn, user_id=JOINER_TG_ID, bot_id=BOT_ID, now=NOW)
        conn.commit()
    assert result["status"] == reg.STATUS_APPROVED
    assert result["mode"] == "open"


def test_approval_required_mode_queues_the_user(s51):
    _set_setting("registration_mode", "approval_required")
    with database.get_db() as conn:
        result = reg.register(conn, user_id=JOINER_TG_ID, bot_id=BOT_ID, now=NOW)
        conn.commit()
    assert result["status"] == reg.STATUS_PENDING
    assert _status_of(JOINER_TG_ID) == reg.STATUS_PENDING


def test_referral_only_mode_waits_for_a_code_then_admits(s51):
    _set_setting("registration_mode", "referral_only")
    _set_setting("referral_requires_approval", "0")
    code = _code_of(REFERRER_TG_ID)
    with database.get_db() as conn:
        first = reg.register(conn, user_id=JOINER_TG_ID, bot_id=BOT_ID, now=NOW)
        conn.commit()
        assert first["status"] == reg.STATUS_AWAITING_REFERRAL
        second = reg.register(
            conn, user_id=JOINER_TG_ID, bot_id=BOT_ID, invite_code=code, now=NOW)
        conn.commit()
    assert second["status"] == reg.STATUS_APPROVED
    assert second["referrer"]["telegram_id"] == REFERRER_TG_ID
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT referred_by_user_id, referred_by_code FROM bot_users WHERE telegram_id=?",
            (JOINER_TG_ID,)).fetchone()
    assert int(row["referred_by_user_id"]) == REFERRER_TG_ID
    assert str(row["referred_by_code"]) == code


def test_referral_requires_approval_switch_queues_even_with_a_valid_code(s51):
    _set_setting("registration_mode", "referral_only")
    _set_setting("referral_requires_approval", "1")
    code = _code_of(REFERRER_TG_ID)
    with database.get_db() as conn:
        result = reg.register(
            conn, user_id=JOINER_TG_ID, bot_id=BOT_ID, invite_code=code, now=NOW)
        conn.commit()
    assert result["status"] == reg.STATUS_PENDING


def test_an_invalid_referral_code_is_rejected(s51):
    _set_setting("registration_mode", "referral_only")
    with database.get_db() as conn:
        with pytest.raises(reg.RegistrationError):
            reg.register(conn, user_id=JOINER_TG_ID, bot_id=BOT_ID,
                         invite_code="ZZZZZZZZ", now=NOW)


def test_a_user_cannot_refer_themselves(s51):
    _set_setting("registration_mode", "referral_only")
    code = _code_of(JOINER_TG_ID)
    with database.get_db() as conn:
        with pytest.raises(reg.RegistrationError):
            reg.register(conn, user_id=JOINER_TG_ID, bot_id=BOT_ID,
                         invite_code=code, now=NOW)


def test_legacy_rows_default_to_approved(s51):
    """An existing customer must never be locked out by the new column."""
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO bot_users(telegram_id,role,username,full_name,is_blocked) "
            "VALUES(777000111,'end_user','legacy','Legacy',0) ON CONFLICT(telegram_id) DO NOTHING")
        conn.commit()
    assert _status_of(777000111) == reg.STATUS_APPROVED


def test_parse_start_payload_reads_the_referral_deeplink():
    assert reg.parse_start_payload(["ref_AB23CD45"]) == "AB23CD45"
    assert reg.parse_start_payload(["ref_ab23cd45"]) == "AB23CD45"
    assert reg.parse_start_payload(["hello"]) == ""
    assert reg.parse_start_payload([]) == ""


def test_invite_link_shape():
    assert reg.invite_link("@HstAB_bot", "AB23CD45") == "https://t.me/HstAB_bot?start=ref_AB23CD45"
    assert reg.invite_link("", "AB23CD45") == ""


# --------------------------------------------------------------------------- #
# BUG-REG-001 — approval queue and tenant isolation
# --------------------------------------------------------------------------- #

def test_pending_queue_is_scoped_per_tenant(s51):
    _set_setting("registration_mode", "approval_required")
    with database.get_db() as conn:
        reg.register(conn, user_id=JOINER_TG_ID, bot_id=BOT_ID, now=NOW)
        for actor in (JOINER_TG_ID + 1, JOINER_TG_ID + 2):
            conn.execute(
                "INSERT INTO bot_users(telegram_id,role,username,full_name,is_blocked) "
                "VALUES(?, 'end_user','r','Rep user',0) ON CONFLICT(telegram_id) DO NOTHING",
                (actor,))
        conn.commit()
        reg.register(conn, user_id=JOINER_TG_ID + 1, bot_id=REP_BOT_ID,
                     rep_id=s51["rep_id"], now=NOW)
        conn.commit()
        root_queue = reg.list_pending(conn, bot_id=BOT_ID)
        rep_queue = reg.list_pending(conn, rep_id=s51["rep_id"])
    root_ids = {int(item["telegram_id"]) for item in root_queue}
    rep_ids = {int(item["telegram_id"]) for item in rep_queue}
    assert JOINER_TG_ID in root_ids
    assert JOINER_TG_ID not in rep_ids
    assert JOINER_TG_ID + 1 in rep_ids


def test_approve_and_reject_are_idempotent(s51):
    _set_setting("registration_mode", "approval_required")
    with database.get_db() as conn:
        reg.register(conn, user_id=JOINER_TG_ID, bot_id=BOT_ID, now=NOW)
        conn.commit()
        first = reg.approve(conn, user_id=JOINER_TG_ID, actor_id=ROOT_TG_ID)
        conn.commit()
        second = reg.approve(conn, user_id=JOINER_TG_ID, actor_id=ROOT_TG_ID)
        conn.commit()
    assert first["changed"] is True
    assert second["changed"] is False
    assert _status_of(JOINER_TG_ID) == reg.STATUS_APPROVED


def test_rejected_user_stays_rejected_after_a_new_start(s51):
    _set_setting("registration_mode", "open")
    with database.get_db() as conn:
        reg.reject(conn, user_id=JOINER_TG_ID, actor_id=ROOT_TG_ID)
        conn.commit()
        again = reg.register(conn, user_id=JOINER_TG_ID, bot_id=BOT_ID, now=NOW)
        conn.commit()
    assert again["status"] == reg.STATUS_REJECTED


# --------------------------------------------------------------------------- #
# BUG-REG-001 — referral rewards
# --------------------------------------------------------------------------- #

def test_reward_plan_combines_fixed_and_percentage_rewards(s51):
    _set_setting("referral_signup_bonus", "20000")
    _set_setting("referral_first_purchase_bonus", "5000")
    _set_setting("referral_purchase_reward_percent", "10")
    with database.get_db() as conn:
        plan = reg.reward_plan(conn, order_amount=450000)
    assert plan["signup_bonus"] == 20000
    assert plan["first_purchase_bonus"] == 5000
    assert plan["purchase_amount"] == 45000


def test_signup_bonus_credits_the_referrer_wallet_once(s51):
    _set_setting("referral_signup_bonus", "20000")
    code = _code_of(REFERRER_TG_ID)
    with database.get_db() as conn:
        reg.register(conn, user_id=JOINER_TG_ID, bot_id=BOT_ID, invite_code=code, now=NOW)
        conn.commit()
    first = reg.pay_signup_bonus(JOINER_TG_ID, actor_id=ROOT_TG_ID)
    second = reg.pay_signup_bonus(JOINER_TG_ID, actor_id=ROOT_TG_ID)
    assert first["status"] == "credited"
    assert first["amount_toman"] == 20000
    assert second["status"] == "already_paid"
    assert database.get_user_wallet_balance(REFERRER_TG_ID) == 20000


def test_first_purchase_reward_is_paid_once_and_recorded_in_the_ledger(s51):
    _set_setting("referral_first_purchase_bonus", "5000")
    _set_setting("referral_purchase_reward_percent", "10")
    code = _code_of(REFERRER_TG_ID)
    with database.get_db() as conn:
        reg.register(conn, user_id=JOINER_TG_ID, bot_id=BOT_ID, invite_code=code, now=NOW)
        conn.commit()
    first = reg.pay_first_purchase_reward(
        JOINER_TG_ID, order_amount=450000, order_id="order-1", actor_id=ROOT_TG_ID)
    second = reg.pay_first_purchase_reward(
        JOINER_TG_ID, order_amount=450000, order_id="order-2", actor_id=ROOT_TG_ID)
    assert first["status"] == "credited"
    assert first["amount_toman"] == 5000 + 45000
    assert second["status"] == "already_paid"
    assert database.get_user_wallet_balance(REFERRER_TG_ID) == 50000
    with database.get_db() as conn:
        rows = conn.execute(
            "SELECT amount_toman, reward_type FROM referral_rewards "
            "WHERE referrer_user_id=?", (REFERRER_TG_ID,)).fetchall()
        ledger = conn.execute(
            "SELECT COUNT(*) AS total FROM financial_ledger WHERE user_id=?",
            (REFERRER_TG_ID,)).fetchone()
    assert len(rows) == 1
    assert int(ledger["total"]) == 1


def test_a_disabled_reward_credits_nothing(s51):
    _set_setting("referral_signup_bonus", "0")
    code = _code_of(REFERRER_TG_ID)
    with database.get_db() as conn:
        reg.register(conn, user_id=JOINER_TG_ID, bot_id=BOT_ID, invite_code=code, now=NOW)
        conn.commit()
    assert reg.pay_signup_bonus(JOINER_TG_ID)["status"] == "disabled"
    assert database.get_user_wallet_balance(REFERRER_TG_ID) == 0


def test_referral_stats_and_listing(s51):
    code = _code_of(REFERRER_TG_ID)
    with database.get_db() as conn:
        reg.register(conn, user_id=JOINER_TG_ID, bot_id=BOT_ID, invite_code=code, now=NOW)
        conn.commit()
        stats = reg.referral_stats(conn, REFERRER_TG_ID)
        listed = reg.list_referrals(conn, REFERRER_TG_ID)
    assert stats["invited"] == 1
    assert stats["approved"] == 1
    assert len(listed) == 1
    assert int(listed[0]["telegram_id"]) == JOINER_TG_ID


# --------------------------------------------------------------------------- #
# BUG-REG-001 — the live surfaces
# --------------------------------------------------------------------------- #

def test_start_holds_a_user_back_under_approval_required(s51):
    _set_setting("registration_mode", "approval_required")
    update, _query, message = make_update(text="/start", user_id=JOINER_TG_ID)
    context = make_context()
    asyncio.run(rep_bot.start_cmd(update, context))

    assert _status_of(JOINER_TG_ID) == reg.STATUS_PENDING
    text, kwargs = message.replies[-1]
    assert "در انتظار تایید مدیریت" in text
    callbacks = _callbacks(kwargs["reply_markup"])
    assert "user:reg_status" in callbacks
    assert "user:referral_code" in callbacks
    # The owner is notified with an actionable card.
    assert context.bot.sent, "the tenant owner must receive the approval card"


def test_start_with_a_referral_deeplink_admits_the_user(s51):
    _set_setting("registration_mode", "referral_only")
    _set_setting("referral_requires_approval", "0")
    code = _code_of(REFERRER_TG_ID)
    update, _query, message = make_update(text="/start", user_id=JOINER_TG_ID)
    asyncio.run(rep_bot.start_cmd(update, make_context(args=[f"ref_{code}"])))

    assert _status_of(JOINER_TG_ID) == reg.STATUS_APPROVED
    assert message.replies, "an approved user must reach the portal"


def test_start_without_a_code_under_referral_only_shows_the_gate(s51):
    _set_setting("registration_mode", "referral_only")
    update, _query, message = make_update(text="/start", user_id=JOINER_TG_ID)
    asyncio.run(rep_bot.start_cmd(update, make_context()))

    assert _status_of(JOINER_TG_ID) == reg.STATUS_AWAITING_REFERRAL
    text, _kwargs = message.replies[-1]
    assert "کد معرف" in text


def test_typed_referral_code_is_captured_before_any_other_handler(s51):
    _set_setting("registration_mode", "referral_only")
    _set_setting("referral_requires_approval", "0")
    code = _code_of(REFERRER_TG_ID)
    update, _query, message = make_update(text="/start", user_id=JOINER_TG_ID)
    context = make_context()
    asyncio.run(rep_bot.start_cmd(update, context))
    assert _status_of(JOINER_TG_ID) == reg.STATUS_AWAITING_REFERRAL

    context.user_data["referral_pending"] = True
    update, _query, message = make_update(text=code, user_id=JOINER_TG_ID)
    asyncio.run(rep_bot.handle_message(update, context))
    assert _status_of(JOINER_TG_ID) == reg.STATUS_APPROVED


def test_admin_approval_admits_the_user_and_pays_the_bonus(s51):
    _set_setting("registration_mode", "approval_required")
    _set_setting("referral_signup_bonus", "15000")
    code = _code_of(REFERRER_TG_ID)
    with database.get_db() as conn:
        reg.register(conn, user_id=JOINER_TG_ID, bot_id=BOT_ID, invite_code=code, now=NOW)
        conn.commit()

    update, query, _message = make_update(data=f"admin:reg_approve:{JOINER_TG_ID}")
    context = make_context()
    asyncio.run(admin_panel.handle_admin_callbacks(update, context))

    assert _status_of(JOINER_TG_ID) == reg.STATUS_APPROVED
    assert database.get_user_wallet_balance(REFERRER_TG_ID) == 15000
    assert any(int(item["chat_id"]) == JOINER_TG_ID for item in context.bot.sent)


def test_rep_scope_approval_never_touches_the_root_queue(s51):
    _set_setting("registration_mode", "approval_required")
    with database.get_db() as conn:
        reg.register(conn, user_id=JOINER_TG_ID, bot_id=REP_BOT_ID,
                     rep_id=s51["rep_id"], now=NOW)
        conn.commit()
    import rep_panel
    update, query, _message = make_update(data=f"rep:reg_approve:{JOINER_TG_ID}")
    context = make_context()
    rep_row = database.get_rep_by_id(s51["rep_id"])
    asyncio.run(rep_panel.handle_rep_callbacks(update, context, rep_row))
    assert _status_of(JOINER_TG_ID) == reg.STATUS_APPROVED


def test_registration_settings_page_lists_all_three_modes(s51):
    update, query, _message = make_update(data="admin:reg_settings")
    asyncio.run(admin_panel.handle_admin_callbacks(update, make_context()))
    callbacks = _callbacks(query.edits[-1][1]["reply_markup"])
    for mode in reg.REGISTRATION_MODES:
        assert f"admin:reg_mode:{mode}" in callbacks
    assert "admin:reg_toggle_approval" in callbacks
    assert "admin:pending_reg" in callbacks


def test_switching_the_mode_from_the_admin_page_persists_it(s51):
    update, query, _message = make_update(data="admin:reg_mode:approval_required")
    asyncio.run(admin_panel.handle_admin_callbacks(update, make_context()))
    with database.get_db() as conn:
        assert reg.resolve_mode(conn, bot_id=BOT_ID) == "approval_required"


def test_user_referral_page_shows_code_link_and_stats(s51):
    update, query, _message = make_update(data="user:referral", user_id=REFERRER_TG_ID)
    asyncio.run(user_portal.handle_user_callbacks(update, make_context()))
    text, kwargs = query.edits[-1]
    assert "کد معرف" in text
    assert "https://t.me/" in text
    callbacks = _callbacks(kwargs["reply_markup"])
    assert "user:referral_list" in callbacks


def test_registration_status_page_never_dead_ends(s51):
    update, query, _message = make_update(data="user:reg_status", user_id=JOINER_TG_ID)
    asyncio.run(user_portal.handle_user_callbacks(update, make_context()))
    _text, kwargs = query.edits[-1]
    assert _callbacks(kwargs["reply_markup"])


# --------------------------------------------------------------------------- #
# MISSION-2 — real client creation on a 3X-UI panel
# --------------------------------------------------------------------------- #

class FakePanelAdapter:
    """Minimal adapter surface: inbound enumeration plus a scripted create."""

    def __init__(self, *, state="success", inbounds=None):
        self.state = state
        self.specs = []
        self._inbounds = inbounds if inbounds is not None else [
            {"id": 7, "remark": "main", "protocol": "vless", "port": 443, "enable": True},
            {"id": 9, "remark": "disabled", "protocol": "vmess", "port": 8080, "enable": False},
        ]

    async def list_inbounds(self):
        return list(self._inbounds)

    async def create_client(self, spec):
        self.specs.append(spec)
        return SimpleNamespace(state=self.state, evidence={"stage": "scripted"})


def test_select_panel_picks_the_active_published_panel(s51):
    with database.get_db() as conn:
        panel = provisioning.select_panel(conn, for_role="end_user")
    assert int(panel["id"]) == 1


def test_select_panel_refuses_when_none_is_published(s51):
    with database.get_db() as conn:
        conn.execute("UPDATE panels SET lifecycle='maintenance' WHERE id=1")
        conn.commit()
        with pytest.raises(provisioning.NoEligiblePanel):
            provisioning.select_panel(conn, for_role="end_user")


def test_resolve_inbound_ids_parses_the_panel_setting():
    assert provisioning.resolve_inbound_ids({"default_inbound_ids_json": "[7, 9]"}) == (7, 9)
    assert provisioning.resolve_inbound_ids({"default_inbound_ids_json": "[]"}) == ()
    assert provisioning.resolve_inbound_ids({"default_inbound_ids_json": "not-json"}) == ()
    assert provisioning.resolve_inbound_ids({}) == ()


def test_discover_inbound_ids_takes_the_first_enabled_inbound(s51):
    adapter = FakePanelAdapter()
    assert asyncio.run(provisioning.discover_inbound_ids(adapter)) == (7,)


def test_discover_inbound_ids_fails_closed_without_an_enabled_inbound(s51):
    adapter = FakePanelAdapter(inbounds=[{"id": 9, "enable": False}])
    with pytest.raises(provisioning.InboundUnavailable):
        asyncio.run(provisioning.discover_inbound_ids(adapter))


def test_build_client_email_is_unique_and_prefixed(s51):
    panel = {"panel_code": "xui-1", "custom_prefix": ""}
    first = provisioning.build_client_email(panel)
    second = provisioning.build_client_email(panel)
    assert first != second
    assert first.startswith("xui-1-")


def test_prepare_provisioning_persists_an_account_and_a_prepared_operation(s51):
    with database.get_db() as conn:
        panel = provisioning.select_panel(conn, for_role="end_user")
        ticket = provisioning.prepare_provisioning(
            conn, panel=panel, email="xui-1-abc123", gb=20, days=30,
            owner_tg_id=JOINER_TG_ID, actor_tg_id=JOINER_TG_ID, bot_id=BOT_ID,
            inbound_ids=(7,), now=NOW,
        )
        conn.commit()
        account = conn.execute(
            "SELECT id, panel_id, email, owner_tg_id FROM accounts WHERE id=?",
            (ticket["account_id"],)).fetchone()
        operation = conn.execute(
            "SELECT kind, state, account_id, panel_id FROM panel_operations "
            "WHERE operation_id=?", (ticket["operation_id"],)).fetchone()
    assert account is not None
    assert int(account["panel_id"]) == 1
    assert operation["kind"] == provisioning.CREATE_KIND
    assert operation["state"] == "prepared"
    assert int(operation["account_id"]) == ticket["account_id"]


def test_build_create_spec_matches_the_adapter_contract(s51):
    ticket = {
        "operation_id": "op-1", "email": "xui-1-abc", "gb": 20, "days": 30,
        "inbound_ids": (7,), "order_id": "order-1",
    }
    spec = provisioning.build_create_spec(ticket, credentials={"id": "uuid-1"})
    assert spec["operation_id"] == "op-1"
    assert spec["quota_mode"] == "finite"
    assert spec["quota_bytes"] == 20 * provisioning.GIB
    assert spec["duration_seconds"] == 30 * 86400
    assert spec["on_first_connect"] is False
    assert spec["inbound_ids"] == (7,)
    assert spec["candidate_credentials"] == {"id": "uuid-1"}


def test_provision_account_creates_the_client_and_finalizes_success(s51):
    adapter = FakePanelAdapter(state="success")
    with database.get_db() as conn:
        panel = provisioning.select_panel(conn, for_role="end_user")
        outcome = asyncio.run(provisioning.provision_account(
            adapter, conn, panel=panel, email="xui-1-live01", gb=20, days=30,
            owner_tg_id=JOINER_TG_ID, actor_tg_id=JOINER_TG_ID, bot_id=BOT_ID,
            inbound_ids=(7,), now=NOW,
        ))
        row = conn.execute(
            "SELECT state FROM panel_operations WHERE operation_id=?",
            (outcome["operation_id"],)).fetchone()
        account = conn.execute(
            "SELECT 1 FROM accounts WHERE id=?", (outcome["account_id"],)).fetchone()
    assert outcome["state"] == "success"
    assert row["state"] == "succeeded"
    assert account is not None
    assert adapter.specs and adapter.specs[0]["inbound_ids"] == (7,)


def test_provision_account_removes_the_provisional_row_on_definite_failure(s51):
    adapter = FakePanelAdapter(state="definite_failure")
    with database.get_db() as conn:
        panel = provisioning.select_panel(conn, for_role="end_user")
        outcome = asyncio.run(provisioning.provision_account(
            adapter, conn, panel=panel, email="xui-1-fail01", gb=20, days=30,
            owner_tg_id=JOINER_TG_ID, actor_tg_id=JOINER_TG_ID, bot_id=BOT_ID,
            inbound_ids=(7,), now=NOW,
        ))
        account = conn.execute(
            "SELECT deleted_at FROM accounts WHERE id=?", (outcome["account_id"],)).fetchone()
        row = conn.execute(
            "SELECT state FROM panel_operations WHERE operation_id=?",
            (outcome["operation_id"],)).fetchone()
    assert outcome["state"] == "definite_failure"
    assert account is not None and account["deleted_at"] is not None, \
        "an orphan account must be soft-deleted, never left usable"
    assert row["state"] == "definite_failure"


def test_provision_account_keeps_the_lock_when_the_outcome_is_unknown(s51):
    adapter = FakePanelAdapter(state="unknown")
    with database.get_db() as conn:
        panel = provisioning.select_panel(conn, for_role="end_user")
        outcome = asyncio.run(provisioning.provision_account(
            adapter, conn, panel=panel, email="xui-1-unk01", gb=20, days=30,
            owner_tg_id=JOINER_TG_ID, actor_tg_id=JOINER_TG_ID, bot_id=BOT_ID,
            inbound_ids=(7,), now=NOW,
        ))
        row = conn.execute(
            "SELECT state FROM panel_operations WHERE operation_id=?",
            (outcome["operation_id"],)).fetchone()
        account = conn.execute(
            "SELECT 1 FROM accounts WHERE id=?", (outcome["account_id"],)).fetchone()
    assert outcome["state"] == "unknown"
    assert row["state"] == "needs_reconciliation"
    assert account is not None, "an ambiguous creation keeps its row for reconciliation"


def test_duplicate_identifier_is_refused(s51):
    with database.get_db() as conn:
        panel = provisioning.select_panel(conn, for_role="end_user")
        provisioning.prepare_provisioning(
            conn, panel=panel, email="xui-1-dup", gb=20, days=30,
            owner_tg_id=JOINER_TG_ID, actor_tg_id=JOINER_TG_ID, bot_id=BOT_ID,
            inbound_ids=(7,), now=NOW)
        conn.commit()
        with pytest.raises(provisioning.ProvisioningError):
            provisioning.prepare_provisioning(
                conn, panel=panel, email="xui-1-dup", gb=20, days=30,
                owner_tg_id=JOINER_TG_ID, actor_tg_id=JOINER_TG_ID, bot_id=BOT_ID,
                inbound_ids=(7,), now=NOW)


def test_purchase_falls_back_to_real_panel_creation_when_the_pool_cannot_serve(s51, monkeypatch):
    """The end-to-end path: pool rejection -> real create -> delivered service."""
    database.record_user_payment(JOINER_TG_ID, 500000, "test credit", ROOT_TG_ID, "Root")
    intent_id = database.create_user_action_intent(
        user_id=JOINER_TG_ID, action_type="buy", plan_code="P20-30",
        plan_name="۲۰ گیگ ۱ ماهه", gb=20, days=30, amount=450000,
    )
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO pricing_plans(plan_code,name,gb,days,user_price,rep_price,is_active) "
            "VALUES('P20-30','۲۰ گیگ ۱ ماهه',20,30,450000,400000,1) "
            "ON CONFLICT(plan_code) DO UPDATE SET is_active=1")
        conn.execute("UPDATE panels SET default_inbound_ids_json='[7]' WHERE id=1")
        conn.commit()

    adapter = FakePanelAdapter(state="success")
    monkeypatch.setattr(
        user_portal.xui_manager, "allocate_pool_client",
        lambda *a, **k: {"status": "unknown", "state": "unknown", "dispatched": False,
                         "reason": "account_binding_missing_or_ambiguous"},
    )
    monkeypatch.setattr(user_portal.xui_manager, "get_panel_adapter", lambda _pid: adapter)

    update, query, _message = make_update(data=f"user:do_buy:{intent_id}", user_id=JOINER_TG_ID)
    asyncio.run(user_portal.handle_user_callbacks(update, make_context()))

    assert adapter.specs, "the panel must actually be asked to create the client"
    assert adapter.specs[0]["inbound_ids"] == (7,)
    with database.get_db() as conn:
        created = conn.execute(
            "SELECT COUNT(*) AS total FROM accounts WHERE panel_id=1 "
            "AND deleted_at IS NULL").fetchone()
        succeeded = conn.execute(
            "SELECT COUNT(*) AS total FROM panel_operations WHERE state='succeeded'").fetchone()
    assert int(created["total"]) == 1
    assert int(succeeded["total"]) == 1
    assert database.get_user_locked_amount(JOINER_TG_ID) == 0


def test_adapter_list_inbounds_normalizes_the_panel_response():
    """The adapter must expose a read-only inbound listing for the picker."""
    import inspect
    from panel_adapters.xui_openapi import XUIOpenAPIAdapter
    method = getattr(XUIOpenAPIAdapter, "list_inbounds", None)
    assert callable(method), "the adapter must expose list_inbounds"
    assert inspect.iscoroutinefunction(method)


def test_inbound_picker_callbacks_are_registered():
    for callback in ("v52:panelinbound:1", "v52:panelsetinbound:1:7"):
        assert isinstance(v52_router.route_callback(callback), v52_router.Route)
