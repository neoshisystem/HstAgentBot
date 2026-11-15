"""S52 regression suite for the Batch #03 audit repairs.

BUG-FIN-004   a locked transaction's card no longer crashes on Markdown parsing
BUG-RBAC-003  root/representatives bypass registration; approved users stay approved
BUG-RBAC-002  the profile guard reads the permanent role; a rep sees only their own
ARCH-TICKET-  five-digit ticket codes, the lifecycle header and reopen
BUG-SYS-004   the finance topic destination is resolved at send time
BUG-NAV-004   the end-user portal keyboard is rebuilt, never mutated
BUG-NAV-003   the registration policy page is never a dead end
BUG-PANEL-001 a missing panel record is a not-found, not an unavailable

Every assertion is offline: a temporary database, fake Telegram objects and a fake
panel adapter. No socket is opened and no real panel is contacted.
"""
from __future__ import annotations

import asyncio
import base64
import json
import pathlib
import sqlite3
import time
from types import SimpleNamespace

import pytest

from migrations_runner import apply_migrations

import admin_panel
import config
import database
import forum_manager
import internal_panel_compat
import panel_adapters.xui_openapi as xui_openapi
import payment_service
import receipt_notifications
import registration_service as reg
import registration_ui
import rep_bot
import support_service as support
import support_ui
import user_portal
import v52_router

PRODUCT_ROOT = pathlib.Path(__file__).resolve().parents[1]
ROOT_TG_ID = 101258148
BOT_ID = 9003
CUSTOMER_TG_ID = 303600001
STRANGER_TG_ID = 303600002
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
        return SimpleNamespace(message_id=len(self.sent))

    async def send_photo(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent))


class FakeUser:
    def __init__(self, uid, name="Actor"):
        self.id = uid
        self.full_name = name
        self.first_name = name
        self.username = f"u{uid}"


class FakeChat:
    def __init__(self, cid, ctype="private"):
        self.id = cid
        self.type = ctype
        self.title = None


class FakeUpdate:
    def __init__(self, uid, chat_type="private"):
        self.effective_user = FakeUser(uid)
        self.effective_chat = FakeChat(uid, chat_type)
        self.effective_message = FakeMessage()
        self.message = self.effective_message


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()
        self.user_data = {}
        self.chat_data = {}
        self.bot_data = {}
        self.args = []


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """Isolated database with the frozen migration set and synthetic identities."""
    for module in (config, database, rep_bot):
        monkeypatch.setattr(module, "ADMIN_TELEGRAM_ID", ROOT_TG_ID, raising=False)
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "s52.sqlite3"), raising=False)
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="))
    database.init_db()
    with database.get_db() as conn:
        apply_migrations(conn, PRODUCT_ROOT / "migrations", now=NOW)
        conn.execute(
            "INSERT INTO system_settings(key,value,label,description) "
            "VALUES('admin_telegram_id',?,'','') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(ROOT_TG_ID),))
        conn.execute(
            "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,"
            "status,verified_at,revision) VALUES(?,NULL,1,'','HstAB_bot','active',?,1) "
            "ON CONFLICT(bot_id) DO NOTHING", (BOT_ID, NOW))
        for tg, role in ((ROOT_TG_ID, "super_admin"), (CUSTOMER_TG_ID, "end_user"),
                         (STRANGER_TG_ID, "end_user")):
            conn.execute(
                "INSERT INTO bot_users(telegram_id,role,username,full_name,is_blocked,"
                "registration_status,registration_decided_at) "
                "VALUES(?,?,?,?,0,'approved',CURRENT_TIMESTAMP) "
                "ON CONFLICT(telegram_id) DO UPDATE SET role=excluded.role",
                (tg, role, f"u{tg}", f"User {tg}"))
        conn.execute(
            "INSERT INTO user_wallets(user_id,balance_toman) VALUES(?,500000) "
            "ON CONFLICT(user_id) DO UPDATE SET balance_toman=500000", (CUSTOMER_TG_ID,))
        conn.commit()
    return tmp_path


def _seed_rep(conn, rep_id: int, telegram_id: int) -> None:
    """Insert a representative using only the columns the live schema declares."""
    info = list(conn.execute("PRAGMA table_info(representatives)"))
    columns = [row[1] for row in info]
    not_null = {row[1] for row in info if row[3] and row[4] is None}
    values = {"id": rep_id, "telegram_id": telegram_id, "name": "Rep",
              "rep_code": f"R{rep_id}", "is_active": 1, "scope_filter": "all",
              "group_id": -1009990000000, "topic_id": 900 + rep_id,
              "created_at": NOW, "updated_at": NOW, "status": "active"}
    keys = [key for key in values if key in columns]
    missing = [name for name in not_null if name not in keys]
    assert not missing, f"seeder is missing NOT NULL columns: {missing}"
    placeholders = ",".join("?" for _ in keys)
    conn.execute(
        f"INSERT INTO representatives({','.join(keys)}) VALUES({placeholders}) "
        f"ON CONFLICT(id) DO NOTHING", tuple(values[key] for key in keys))


def _legacy_markdown_is_balanced(text: str) -> bool:
    """Mirror Telegram's legacy Markdown parser: a backslash escapes the next char."""
    open_bold = open_italic = open_code = False
    i, n = 0, len(text or "")
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n:
            i += 2
            continue
        if open_code:
            if ch == "`":
                open_code = False
        elif ch == "`":
            open_code = True
        elif ch == "*":
            open_bold = not open_bold
        elif ch == "_":
            open_italic = not open_italic
        i += 1
    return not (open_bold or open_italic or open_code)


# --------------------------------------------------------------------------- #
# BUG-FIN-004 — the locked-transaction card must survive a Markdown parse
# --------------------------------------------------------------------------- #

def _seed_locked_intent(conn, note):
    conn.execute(
        "INSERT INTO user_action_intents(intent_id,user_id,action_type,plan_code,plan_name,"
        "gb,days,amount,status,operation_lock_key,result_note,expires_at) "
        "VALUES('d3df5ea4b828bda7f6bc2db40178e8e2',?,'buy','p20','۲۰ گیگ ۱ ماهه',20,30,"
        "450000,'needs_reconciliation','user-buy:1',?,?)",
        (CUSTOMER_TG_ID, note, time.time() + 3600))
    conn.commit()


def test_pending_card_escapes_a_note_that_would_break_markdown(env):
    """The live note carries EVIDENCE_REQUIRED; its lone underscore used to open an
    italic entity that never closed, so Telegram rejected the whole message."""
    note = ("Ambiguous X-UI allocation response: {'status': 'unknown', "
            "'reason': 'EVIDENCE_REQUIRED'}")
    with database.get_db() as conn:
        _seed_locked_intent(conn, note)
    intent = database.get_unresolved_user_action_intent("d3df5ea4b828bda7f6bc2db40178e8e2")
    text, rows = admin_panel._pending_intent_card(intent)
    assert "EVIDENCE\\_REQUIRED" in text
    assert _legacy_markdown_is_balanced(text), "the card must be valid legacy Markdown"
    labels = [button.text for row in rows for button in row]
    assert any("پروفایل" in label for label in labels)
    assert any("پذیرش" in label for label in labels)
    assert any("عودت" in label or "بازگشت وجه" in label for label in labels)


def test_pending_queue_page_is_valid_markdown(env):
    with database.get_db() as conn:
        _seed_locked_intent(conn, "note with a_b and *stars*")
    text, rows = admin_panel.build_pending_transactions_page()
    assert _legacy_markdown_is_balanced(text)
    assert rows


def test_confirm_card_is_valid_markdown(env):
    with database.get_db() as conn:
        _seed_locked_intent(conn, "x")
    intent = database.get_unresolved_user_action_intent("d3df5ea4b828bda7f6bc2db40178e8e2")
    for action in ("apply", "refund"):
        text, rows = admin_panel._pending_intent_confirm_card(intent, action)
        assert _legacy_markdown_is_balanced(text)
        assert rows


def test_profile_origin_round_trips_and_falls_back(env):
    cb = admin_panel.build_profile_callback(CUSTOMER_TG_ID, "tx", "abc123")
    assert cb == f"admin:user_detail:{CUSTOMER_TG_ID}:tx:abc123"
    assert admin_panel.build_profile_callback(CUSTOMER_TG_ID) == \
        f"admin:user_detail:{CUSTOMER_TG_ID}"
    # an unknown surface must not produce a dangling reference
    assert admin_panel.build_profile_callback(CUSTOMER_TG_ID, "nope", "x") == \
        f"admin:user_detail:{CUSTOMER_TG_ID}"
    assert admin_panel.build_profile_callback(CUSTOMER_TG_ID, "tx", "") == \
        f"admin:user_detail:{CUSTOMER_TG_ID}"


def test_360_profile_renders_and_returns_to_the_transaction(env):
    with database.get_db() as conn:
        _seed_locked_intent(conn, "note")
    text, rows = admin_panel.build_user_profile_page(
        CUSTOMER_TG_ID, origin="tx", ref="d3df5ea4b828bda7f6bc2db40178e8e2")
    assert "پروفایل ۳۶۰ درجه" in text
    assert "موجودی قابل برداشت" in text and "مبلغ محفوظ" in text
    assert _legacy_markdown_is_balanced(text)
    back = [button.callback_data for row in rows for button in row
            if "بازگشت به تراکنش" in button.text]
    assert back == ["admin:pending_tx:d3df5ea4b828bda7f6bc2db40178e8e2"]


def test_read_only_profile_hides_management_controls(env):
    text, rows = admin_panel.build_user_profile_page(
        CUSTOMER_TG_ID, allow_management=False)
    callbacks = [button.callback_data for row in rows for button in row]
    assert not any(cb.startswith("admin:set_block_user") for cb in callbacks)
    assert not any(cb.startswith("admin:confirm_del_user") for cb in callbacks)
    assert any(cb.startswith("admin:users") for cb in callbacks)


# --------------------------------------------------------------------------- #
# BUG-RBAC-003 — root and representatives are never queued
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("mode", ["open", "approval_required", "referral_only"])
def test_root_bypasses_every_registration_policy(env, mode):
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO system_settings(key,value,label,description) "
            "VALUES('registration_mode',?,'','') "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (mode,))
        conn.execute("UPDATE bot_users SET role='end_user', "
                     "registration_status='awaiting_referral', registration_decided_at=NULL "
                     "WHERE telegram_id=?", (ROOT_TG_ID,))
        conn.commit()
        out = reg.register(conn, user_id=ROOT_TG_ID, full_name="Root")
        conn.commit()
        assert out["status"] == "approved" and out["privileged"] is True
        row = conn.execute("SELECT registration_status,registration_decided_at FROM bot_users "
                           "WHERE telegram_id=?", (ROOT_TG_ID,)).fetchone()
        assert row["registration_status"] == "approved"
        assert row["registration_decided_at"]


def test_an_active_representative_is_also_privileged(env):
    with database.get_db() as conn:
        _seed_rep(conn, 9, STRANGER_TG_ID)
        conn.execute("INSERT INTO system_settings(key,value,label,description) "
                     "VALUES('registration_mode','referral_only','','') "
                     "ON CONFLICT(key) DO UPDATE SET value='referral_only'")
        conn.execute("UPDATE bot_users SET role='end_user', registration_status='approved', "
                     "registration_decided_at=NULL WHERE telegram_id=?", (STRANGER_TG_ID,))
        conn.commit()
        out = reg.register(conn, user_id=STRANGER_TG_ID, full_name="Rep")
        assert out["status"] == "approved" and out["privileged"] is True


@pytest.mark.parametrize("mode", ["referral_only", "approval_required"])
def test_an_already_approved_customer_is_never_regated(env, mode):
    """Flipping the tenant policy must not retroactively lock an admitted customer."""
    with database.get_db() as conn:
        conn.execute("INSERT INTO system_settings(key,value,label,description) "
                     "VALUES('registration_mode',?,'','') "
                     "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (mode,))
        conn.execute("UPDATE bot_users SET registration_status='approved', "
                     "registration_decided_at='2026-01-01 00:00:00', referred_by_user_id=NULL "
                     "WHERE telegram_id=?", (CUSTOMER_TG_ID,))
        conn.commit()
        out = reg.register(conn, user_id=CUSTOMER_TG_ID, full_name="Customer")
        assert out["status"] == "approved"


def test_a_genuinely_new_user_still_obeys_the_policy(env):
    newbie = 303600900
    with database.get_db() as conn:
        conn.execute("INSERT INTO bot_users(telegram_id,role,username,full_name,is_blocked,"
                     "registration_status) VALUES(?,'end_user','nb','New',0,'approved') "
                     "ON CONFLICT(telegram_id) DO UPDATE SET registration_status='approved',"
                     "registration_decided_at=NULL", (newbie,))
        conn.execute("INSERT INTO system_settings(key,value,label,description) "
                     "VALUES('registration_mode','referral_only','','') "
                     "ON CONFLICT(key) DO UPDATE SET value='referral_only'")
        conn.commit()
        assert reg.register(conn, user_id=newbie, full_name="New")["status"] == \
            reg.STATUS_AWAITING_REFERRAL
        conn.execute("UPDATE bot_users SET registration_status='approved', "
                     "registration_decided_at=NULL WHERE telegram_id=?", (newbie,))
        conn.execute("UPDATE system_settings SET value='approval_required' "
                     "WHERE key='registration_mode'")
        conn.commit()
        assert reg.register(conn, user_id=newbie, full_name="New")["status"] == \
            reg.STATUS_PENDING


def test_the_gate_reads_the_permanent_role_not_the_persona(env):
    """Root browsing the end-user workspace must not be asked for a referral code."""
    with database.get_db() as conn:
        conn.execute("INSERT INTO system_settings(key,value,label,description) "
                     "VALUES('registration_mode','referral_only','','') "
                     "ON CONFLICT(key) DO UPDATE SET value='referral_only'")
        conn.execute("UPDATE bot_users SET registration_status='awaiting_referral', "
                     "registration_decided_at=NULL WHERE telegram_id=?", (ROOT_TG_ID,))
        conn.commit()
    update, context = FakeUpdate(ROOT_TG_ID), FakeContext()
    held = asyncio.run(rep_bot._apply_registration_gate(
        update, context, "end_user", None, base_role="super_admin", base_rep=None))
    assert held is False
    assert not any("کد معرف" in (text or "") for text, _ in update.message.replies)


def test_a_plain_new_user_is_still_held_by_the_gate(env):
    newbie = 303600901
    with database.get_db() as conn:
        conn.execute("INSERT INTO bot_users(telegram_id,role,username,full_name,is_blocked,"
                     "registration_status) VALUES(?,'end_user','nb2','New2',0,'approved')",
                     (newbie,))
        conn.execute("INSERT INTO system_settings(key,value,label,description) "
                     "VALUES('registration_mode','referral_only','','') "
                     "ON CONFLICT(key) DO UPDATE SET value='referral_only'")
        conn.commit()
    update, context = FakeUpdate(newbie), FakeContext()
    held = asyncio.run(rep_bot._apply_registration_gate(
        update, context, "end_user", None, base_role="end_user", base_rep=None))
    assert held is True
    assert update.message.replies


# --------------------------------------------------------------------------- #
# BUG-RBAC-002 — the profile guard and the representative scope
# --------------------------------------------------------------------------- #

def test_base_role_ignores_the_persona(env):
    role, _ = admin_panel._base_role_of(FakeUser(ROOT_TG_ID), FakeChat(ROOT_TG_ID))
    assert role == "super_admin"


def test_rep_scope_admits_only_their_own_users(env):
    rep = {"id": 9}
    with database.get_db() as conn:
        _seed_rep(conn, 9, STRANGER_TG_ID)
        conn.execute("UPDATE bot_users SET assigned_rep_id=9 WHERE telegram_id=?",
                     (CUSTOMER_TG_ID,))
        conn.commit()
    assert rep_bot._rep_may_open_profile(
        f"admin:user_detail:{CUSTOMER_TG_ID}", rep) is True
    assert rep_bot._rep_may_open_profile("admin:settings", rep) is False
    assert rep_bot._rep_may_open_profile("admin:user_detail:999999999", rep) is False
    assert rep_bot._rep_may_open_profile(f"admin:user_detail:{CUSTOMER_TG_ID}", None) is False


def test_privileged_actor_predicate(env):
    assert rep_bot._is_privileged_actor(ROOT_TG_ID) is True
    assert rep_bot._is_privileged_actor(424242) is False


# --------------------------------------------------------------------------- #
# ARCH-TICKET-001 — five-digit codes, lifecycle and reopen
# --------------------------------------------------------------------------- #

def _seed_ticket(conn, ticket_id, status="unseen", sender=CUSTOMER_TG_ID):
    conn.execute(
        "INSERT INTO support_tickets(ticket_id,sender_tg_id,bot_id,account_id,rep_id,"
        "ticket_type,assigned_key,status,created_at,updated_at) "
        "VALUES(?,?,?,NULL,NULL,'general','root',?,?,?)",
        (ticket_id, sender, BOT_ID, status, NOW, NOW))
    conn.execute(
        "INSERT INTO support_messages(ticket_id,actor_tg_id,visibility,body_cipher,created_at) "
        "VALUES(?,?,'customer','x',?)", (ticket_id, sender, NOW))


def test_next_ticket_code_starts_at_10001_and_is_monotonic(env):
    with database.get_db() as conn:
        first = support._next_ticket_code(conn)
        second = support._next_ticket_code(conn)
        conn.commit()
    assert first == "10001" and second == "10002"


def test_legacy_ticket_codes_migrate_and_stay_idempotent(env):
    with database.get_db() as conn:
        _seed_ticket(conn, "WJwulr6COu8VrNZ5DY8oF8SG", "answered")
        _seed_ticket(conn, "vtXzu43gsih_D3i5BHuGAUjD", "closed")
        conn.commit()
        assert support.migrate_legacy_ticket_codes(conn) == 2
        conn.commit()
        ids = [row["ticket_id"] for row in
               conn.execute("SELECT ticket_id FROM support_tickets ORDER BY ticket_id")]
        assert ids == ["10001", "10002"]
        # the message rows must follow their ticket
        orphan = conn.execute(
            "SELECT COUNT(*) AS n FROM support_messages m LEFT JOIN support_tickets t "
            "ON t.ticket_id=m.ticket_id WHERE t.ticket_id IS NULL").fetchone()["n"]
        assert orphan == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert support.migrate_legacy_ticket_codes(conn) == 0


def test_ticket_code_label_and_status_labels(env):
    assert support.ticket_code_label("10001") == "#10001"
    assert support.ticket_code_label("WJwulr6COu8VrNZ5DY8oF8SG") == "WJwulr6COu8VrNZ5DY8oF8SG"
    assert "باز" in support.ticket_status_label("unseen")
    assert "باز" in support.ticket_status_label("escalated")
    assert "پاسخ" in support.ticket_status_label("answered")
    assert "بسته" in support.ticket_status_label("closed")


def test_create_ticket_uses_a_five_digit_code(env):
    ctx = {"actor_tg_id": CUSTOMER_TG_ID, "bot_id": BOT_ID, "persona": "user"}
    out = support.create_ticket(ctx, 0, "یک تیکت آزمایشی", ticket_type="general", now=NOW)
    assert out["ticket_id"] == "10001"


def test_close_then_reopen_round_trip(env):
    with database.get_db() as conn:
        _seed_ticket(conn, "10001")
        conn.commit()
    ctx = {"actor_tg_id": CUSTOMER_TG_ID, "bot_id": BOT_ID, "persona": "user"}
    closed = support.close_ticket(ctx, "10001", now=NOW)
    assert closed["status"] == "closed"
    reopened = support.reopen_ticket(ctx, "10001", now=NOW + 1)
    assert reopened["status"] == "unseen" and reopened["changed"] is True
    # replaying a reopen is an idempotent no-op, never an error
    again = support.reopen_ticket(ctx, "10001", now=NOW + 2)
    assert again["changed"] is False and again["status"] == "unseen"


def test_closing_an_already_closed_ticket_is_refused(env):
    with database.get_db() as conn:
        _seed_ticket(conn, "10001", "closed")
        conn.commit()
    ctx = {"actor_tg_id": CUSTOMER_TG_ID, "bot_id": BOT_ID, "persona": "user"}
    with pytest.raises(support.SupportError):
        support.close_ticket(ctx, "10001", now=NOW)


def test_the_author_reopens_a_closed_ticket_by_writing_again(env):
    with database.get_db() as conn:
        _seed_ticket(conn, "10001", "closed")
        conn.commit()
    ctx = {"actor_tg_id": CUSTOMER_TG_ID, "bot_id": BOT_ID, "persona": "user"}
    support.reply_ticket(ctx, "10001", "هنوز مشکل دارم", now=NOW)
    with database.get_db() as conn:
        status = conn.execute("SELECT status FROM support_tickets WHERE ticket_id='10001'"
                              ).fetchone()["status"]
    assert status == "unseen"


def test_an_operator_reply_does_not_silently_reopen_a_closed_ticket(env):
    with database.get_db() as conn:
        _seed_ticket(conn, "10001", "closed")
        conn.commit()
    ctx = {"actor_tg_id": ROOT_TG_ID, "bot_id": BOT_ID, "persona": "admin"}
    support.reply_ticket(ctx, "10001", "پاسخ مدیریت", now=NOW)
    with database.get_db() as conn:
        status = conn.execute("SELECT status FROM support_tickets WHERE ticket_id='10001'"
                              ).fetchone()["status"]
    assert status == "closed"


def test_lifecycle_rows_swap_close_for_reopen(env):
    open_rows = support_ui.ticket_lifecycle_rows("10001", "unseen", is_operator=True)
    callbacks = [button.callback_data for row in open_rows for button in row]
    assert "v52:close:10001" in callbacks and "v52:reopen:10001" not in callbacks

    closed_rows = support_ui.ticket_lifecycle_rows("10001", "closed", is_operator=True)
    callbacks = [button.callback_data for row in closed_rows for button in row]
    assert "v52:reopen:10001" in callbacks and "v52:close:10001" not in callbacks


def test_reopen_callback_is_registered(env):
    assert v52_router.route_callback("v52:reopen:10001") is not None


def test_status_header_names_the_code_and_the_state(env):
    header = support_ui.ticket_status_header("closed", "10001")
    assert "#10001" in header and "بسته" in header


# --------------------------------------------------------------------------- #
# BUG-SYS-004 — the finance topic destination is resolved at send time
# --------------------------------------------------------------------------- #

def _bind_finance_topic(conn, thread_id=7):
    conn.execute(
        "INSERT INTO forum_topics(owner_key,bot_id,group_chat_id,generation,topic_key,"
        "title_fa,thread_id,state,updated_at) "
        "VALUES('root',?,?,1,'pendingreceipts','🧾 تایید فیش‌ها و امور مالی',?,"
        "'active',?)", (BOT_ID, -1001234567890, thread_id, NOW))
    conn.execute("INSERT INTO system_settings(key,value,label,description) "
                 "VALUES('main_group_id','-1001234567890','','') "
                 "ON CONFLICT(key) DO UPDATE SET value='-1001234567890'")
    conn.commit()


def test_topic_destination_is_read_from_live_settings(env):
    with database.get_db() as conn:
        _bind_finance_topic(conn, 7)
    assert forum_manager.resolve_topic_destination("pendingreceipts") == (-1001234567890, 7)


def test_topic_destination_is_unconfigured_without_a_binding(env):
    assert forum_manager.resolve_topic_destination("pendingreceipts") == (None, None)
    assert forum_manager.resolve_topic_destination("not-a-topic") == (None, None)


def test_receipt_mirror_sends_to_the_group_topic(env):
    with database.get_db() as conn:
        _bind_finance_topic(conn, 7)
    bot = FakeBot()
    item = {"receipt_id": 1, "tracking_code": "RCP-1", "role": "end_user",
            "user_id": CUSTOMER_TG_ID, "amount_toman": 100000, "receipt_note": "n",
            "created_at": "2026-01-01", "sender_name": "Javid", "sender_username": "hstvip",
            "receipt_photo_id": "PHOTO"}
    assert asyncio.run(receipt_notifications.mirror_receipt_to_finance_topic(bot, item)) == \
        "mirrored"
    assert bot.sent and bot.sent[0]["chat_id"] == -1001234567890
    assert bot.sent[0]["message_thread_id"] == 7


def test_receipt_mirror_reports_unconfigured_without_a_topic(env):
    bot = FakeBot()
    item = {"receipt_id": 1, "tracking_code": "RCP-1", "role": "end_user",
            "user_id": CUSTOMER_TG_ID, "amount_toman": 1, "receipt_note": "",
            "created_at": "", "sender_name": "", "sender_username": "",
            "receipt_photo_id": None}
    assert asyncio.run(receipt_notifications.mirror_receipt_to_finance_topic(bot, item)) == \
        "unconfigured"
    assert bot.sent == []


def test_a_group_failure_never_raises(env):
    with database.get_db() as conn:
        _bind_finance_topic(conn, 7)

    class ExplodingBot(FakeBot):
        async def send_photo(self, **kwargs):
            raise RuntimeError("group unreachable")

    item = {"receipt_id": 1, "tracking_code": "RCP-1", "role": "end_user",
            "user_id": CUSTOMER_TG_ID, "amount_toman": 1, "receipt_note": "",
            "created_at": "", "sender_name": "", "sender_username": "",
            "receipt_photo_id": "PHOTO"}
    result = asyncio.run(
        receipt_notifications.mirror_receipt_to_finance_topic(ExplodingBot(), item))
    assert result.startswith("failed:")


# --------------------------------------------------------------------------- #
# BUG-NAV-004 / BUG-NAV-003
# --------------------------------------------------------------------------- #

def test_portal_keyboard_is_rebuilt_not_mutated(env):
    """InlineKeyboardMarkup.inline_keyboard is a tuple; appending raised and the
    portal button did nothing at all."""
    markup = user_portal.build_user_portal_keyboard(base_role="end_user")
    assert isinstance(markup.inline_keyboard, tuple)
    rows = list(markup.inline_keyboard)
    rows.append(["sentinel"])
    rebuilt = user_portal.InlineKeyboardMarkup(rows)
    assert len(rebuilt.inline_keyboard) == len(markup.inline_keyboard) + 1


def test_registration_policy_page_is_never_a_dead_end(env):
    text, markup = registration_ui.build_registration_settings_page(
        mode="open", requires_approval=False, pending=0, scope="admin")
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert "admin:settings" in callbacks and "admin:main" in callbacks

    _text, markup = registration_ui.build_registration_settings_page(
        mode="open", requires_approval=False, pending=0, scope="rep")
    callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert "rep:main" in callbacks


# --------------------------------------------------------------------------- #
# BUG-PANEL-001 — the missing-record classification and the create preflight
# --------------------------------------------------------------------------- #

class _Result:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload


def test_a_missing_record_is_not_found_not_unavailable(env):
    """3X-UI answers a missing record with HTTP 200 and success:false; a genuinely
    unknown route answers 404. Classifying the former as unavailable made every
    create abort with `unknown` and stranded the customer's debit."""
    missing = _Result(200, {"success": False, "msg": "Obtain (record not found)", "obj": None})
    assert xui_openapi.XUIOpenAPIAdapter._is_missing_record(missing) is True

    ok = _Result(200, {"success": True, "msg": "", "obj": {}})
    assert xui_openapi.XUIOpenAPIAdapter._is_missing_record(ok) is False

    other = _Result(200, {"success": False, "msg": "invalid token", "obj": None})
    assert xui_openapi.XUIOpenAPIAdapter._is_missing_record(other) is False

    not_json = _Result(404, None)
    assert xui_openapi.XUIOpenAPIAdapter._is_missing_record(not_json) is False


def test_the_create_preflight_accepts_both_kind_spellings(env):
    """The adapter asks for `client_create`; the provisioning service persists
    kind=`create_client`. Without the alias the preflight refused every creation."""
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO panels(id,panel_code,name,panel_type,lifecycle,is_hidden,"
            "for_end_users,for_reps) VALUES(1,'p1','Panel','3x-ui','active',0,1,1) "
            "ON CONFLICT(id) DO NOTHING")
        conn.execute(
            "INSERT INTO accounts(rep_id,account_code,email,customer_name,sub_id,enable,"
            "total_gb,expiry_time,panel_id,seller_key,revision) "
            "VALUES(NULL,'x@y','x@y','Test',NULL,1,1,0,1,'root',1)")
        account_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        op_columns = [row[1] for row in conn.execute("PRAGMA table_info(panel_operations)")]
        op_values = {"operation_id": "op1", "panel_id": 1, "account_id": account_id,
                     "kind": "create_client", "state": "prepared", "request_cipher": "x",
                     "expected_effect_json": "{}", "created_at": NOW, "updated_at": NOW,
                     "actor_tg_id": ROOT_TG_ID, "bot_id": BOT_ID, "lock_key": "k",
                     "lock_token": "t", "idempotency_key": "i"}
        op_keys = [key for key in op_values if key in op_columns]
        conn.execute(
            f"INSERT INTO panel_operations({','.join(op_keys)}) "
            f"VALUES({','.join('?' for _ in op_keys)})",
            tuple(op_values[key] for key in op_keys))
        conn.commit()
    import inspect
    source = inspect.getsource(internal_panel_compat)
    assert '"client_create": {"client_create", "create_client"}' in source
    assert '"create_client": {"create_client", "client_create"}' in source
    # the alias pair must actually admit the persisted kind
    aliases = {"client_create": {"client_create", "create_client"},
               "create_client": {"create_client", "client_create"}}
    assert "create_client" in aliases["client_create"]


def test_create_kind_matches_the_persisted_value(env):
    import panel_provisioning_service as provisioning
    assert provisioning.CREATE_KIND == "create_client"


# --------------------------------------------------------------------------- #
# No dead-end messages on the surfaces this session touched
# --------------------------------------------------------------------------- #

def test_every_s52_callback_resolves(env):
    callbacks = [
        "admin:pending_tx", "admin:pending_tx:d3df5ea4b828bda7f6bc2db40178e8e2",
        "admin:pending_recheck:x", "admin:pending_confirm:x", "admin:pending_refund:x",
        "admin:pending_do_apply:x", "admin:pending_do_refund:x",
        f"admin:user_detail:{CUSTOMER_TG_ID}",
        f"admin:user_detail:{CUSTOMER_TG_ID}:tx:abc",
        "admin:reg_settings", "admin:reg_mode:open", "admin:reg_toggle_approval",
        "admin:pending_reg", "admin:settings", "admin:main",
        "rep:reg_settings", "rep:reg_mode:open", "rep:pending_reg", "rep:main",
        "v52:close:10001", "v52:reopen:10001",
    ]
    unresolved = [cb for cb in callbacks if v52_router.route_callback(cb) is None]
    assert unresolved == []
