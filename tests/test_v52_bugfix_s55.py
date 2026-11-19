"""S55 regression tests — caption-safe editing, receipt RBAC, 360 profile, sub-resellers.

Offline only: no panel, no Telegram, no network. Telegram is simulated with the
two rejections that actually matter — a photo message whose body is a caption
(``text is None``), and ``BadRequest: Message is not modified``.
"""
import asyncio
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import database
import finance_manager
import message_edit
import profile_360
import rep_bot

ROOT_TG_ID = 101258148


def _load_helpers():
    helper_path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_v52_s55_helpers", helper_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helpers = _load_helpers()


# --------------------------------------------------------------------------
# Telegram doubles
# --------------------------------------------------------------------------

class BadRequest(Exception):
    pass


class FakeMessage:
    """A message that can be a text message or a caption-bearing photo."""

    def __init__(self, *, photo=False, text="body", caption=None, chat_id=555,
                 thread_id=None, message_id=900):
        self.photo = ("file-id",) if photo else None
        self.document = None
        self.text = None if photo else text
        self.caption = caption if photo else None
        self.chat_id = chat_id
        self.message_thread_id = thread_id
        self.message_id = message_id
        self.sent = []

    def get_bot(self):
        return self._bot

    def _bind(self, bot):
        self._bot = bot
        return self


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=1000)


class FakeQuery:
    """Reproduces the two real Telegram rejections the handlers must survive."""

    def __init__(self, message, bot, data=""):
        self.message = message
        self.data = data
        self._bot = bot
        message._bind(bot)
        self.answers = []
        self.text_edits = []
        self.caption_edits = []
        self.markup_edits = []

    def get_bot(self):
        return self._bot

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text=None, **kwargs):
        if self.message.photo:
            raise BadRequest("There is no text in the message to edit")
        if text == self.message.text and kwargs.get("reply_markup") is None:
            raise BadRequest("Message is not modified")
        self.text_edits.append((text, kwargs))
        self.message.text = text

    async def edit_message_caption(self, caption=None, **kwargs):
        if not self.message.photo and not self.message.caption:
            raise BadRequest("There is no caption in the message to edit")
        if caption == self.message.caption and kwargs.get("reply_markup") is None:
            raise BadRequest("Message is not modified")
        self.caption_edits.append((caption, kwargs))
        self.message.caption = caption

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup_edits.append(reply_markup)


# --------------------------------------------------------------------------
# 1. message_edit — the crash class itself
# --------------------------------------------------------------------------

def test_is_media_message_distinguishes_photo_from_text():
    assert message_edit.is_media_message(FakeMessage(photo=True)) is True
    assert message_edit.is_media_message(FakeMessage(photo=False)) is False
    assert message_edit.is_media_message(None) is False


def test_render_page_from_a_photo_sends_a_new_message_instead_of_crashing():
    bot = FakeBot()
    query = FakeQuery(FakeMessage(photo=True, caption="receipt"), bot)
    outcome = asyncio.run(message_edit.render_page(
        query, text="profile page", parse_mode="Markdown"))
    assert outcome == message_edit.SENT_NEW
    assert bot.sent and bot.sent[0]["text"] == "profile page"
    assert query.text_edits == []


def test_render_page_from_a_text_message_edits_in_place():
    bot = FakeBot()
    query = FakeQuery(FakeMessage(photo=False, text="old"), bot)
    outcome = asyncio.run(message_edit.render_page(query, text="new page"))
    assert outcome == message_edit.EDITED_TEXT
    assert query.text_edits and query.text_edits[0][0] == "new page"
    assert bot.sent == []


def test_render_page_keeps_the_forum_topic_when_sending_a_new_message():
    bot = FakeBot()
    query = FakeQuery(FakeMessage(photo=True, caption="c", chat_id=-1004377897090,
                                  thread_id=7), bot)
    asyncio.run(message_edit.render_page(query, text="page"))
    assert bot.sent[0]["chat_id"] == -1004377897090
    assert bot.sent[0]["message_thread_id"] == 7


def test_update_body_writes_the_caption_of_a_photo():
    bot = FakeBot()
    query = FakeQuery(FakeMessage(photo=True, caption="old caption"), bot)
    outcome = asyncio.run(message_edit.update_body(query, text="new caption"))
    assert outcome == message_edit.EDITED_CAPTION
    assert query.caption_edits and query.caption_edits[0][0] == "new caption"
    assert query.text_edits == []


def test_update_body_writes_the_text_of_a_text_message():
    bot = FakeBot()
    query = FakeQuery(FakeMessage(photo=False, text="old text"), bot)
    outcome = asyncio.run(message_edit.update_body(query, text="new text"))
    assert outcome == message_edit.EDITED_TEXT
    assert query.text_edits and query.text_edits[0][0] == "new text"


def test_repeated_identical_edit_is_reported_unchanged_not_raised():
    bot = FakeBot()
    query = FakeQuery(FakeMessage(photo=False, text="same"), bot)
    outcome = asyncio.run(message_edit.update_body(query, text="same"))
    assert outcome == message_edit.UNCHANGED


# --------------------------------------------------------------------------
# 2. Receipt card + keyboard (BUG-FIN-005, profile button from first send)
# --------------------------------------------------------------------------

def _receipt(**overrides):
    base = {
        "id": 42, "tracking_code": "RCP-51064524", "role": "end_user",
        "user_id": 8537282592, "amount_toman": 450000, "receipt_note": "note",
        "created_at": "2026-11-15 20:00:00", "status": "pending",
    }
    base.update(overrides)
    return base


def test_pending_receipt_card_carries_the_pending_status_line():
    text = finance_manager.format_admin_receipt_card(_receipt())
    assert "وضعیت فعلی" in text
    assert "⏳ در انتظار بررسی" in text


def test_approved_receipt_card_shows_the_approved_status_and_decider():
    text = finance_manager.format_admin_receipt_card(
        _receipt(status="approved"), decided_by="Root Admin")
    assert "✅ تایید شده" in text
    assert "Root Admin" in text
    assert "⏳ در انتظار بررسی" not in text


def test_rejected_receipt_card_shows_the_rejected_status():
    text = finance_manager.format_admin_receipt_card(_receipt(status="rejected"))
    assert "❌ رد شده" in text


def test_review_keyboard_offers_the_payer_profile_before_any_decision():
    keyboard = finance_manager.build_admin_receipt_review_keyboard(42, 8537282592)
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "admin:user_detail:8537282592:rcp:42" in callbacks
    assert "admin_approve_rcp_42" in callbacks


def test_review_keyboard_without_a_user_id_stays_decision_only():
    keyboard = finance_manager.build_admin_receipt_review_keyboard(42)
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert not any(c.startswith("admin:user_detail:") for c in callbacks)


def test_settled_keyboard_keeps_the_profile_and_the_way_back():
    keyboard = finance_manager.build_admin_reviewed_keyboard("approved", 8537282592, 42)
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert callbacks[0] == "noop"
    assert "admin:user_detail:8537282592:rcp:42" in callbacks
    assert "admin:main" in callbacks


# --------------------------------------------------------------------------
# 3. BUG-RBAC-004 — the super admin must be able to settle a receipt in the topic
# --------------------------------------------------------------------------

def _patch_finance_topic(monkeypatch, *, group_id=-1004377897090, thread_id=7):
    """Pin the synthetic root and the finance-topic binding for one test.

    CI has no ``.env``, so ``config.ADMIN_TELEGRAM_ID`` is unset on the runner and
    ``rep_bot`` captured a 0 at import. Pinning the module attribute makes the
    root check deterministic on both the runner and a live deployment.
    """
    import forum_manager
    monkeypatch.setattr(rep_bot, "ADMIN_TELEGRAM_ID", ROOT_TG_ID)
    monkeypatch.setattr(
        forum_manager, "resolve_topic_destination",
        lambda *_a, **_k: (group_id, thread_id))


def test_root_may_settle_a_receipt_inside_the_finance_topic(monkeypatch):
    _patch_finance_topic(monkeypatch)
    user = SimpleNamespace(id=ROOT_TG_ID)
    chat = SimpleNamespace(id=-1004377897090, type="supergroup")
    assert rep_bot._may_use_admin_callback_in_group(
        user, chat, 7, "admin_approve_rcp_42") is True
    assert rep_bot._may_use_admin_callback_in_group(
        user, chat, 7, "admin:user_detail:8537282592:rcp:42") is True
    assert rep_bot._may_use_admin_callback_in_group(
        user, chat, 7, "admin:main") is True


def test_non_root_is_refused_even_inside_the_finance_topic(monkeypatch):
    _patch_finance_topic(monkeypatch)
    user = SimpleNamespace(id=999)
    chat = SimpleNamespace(id=-1004377897090, type="supergroup")
    assert rep_bot._may_use_admin_callback_in_group(
        user, chat, 7, "admin_approve_rcp_42") is False


def test_root_is_refused_outside_the_finance_topic(monkeypatch):
    _patch_finance_topic(monkeypatch)
    user = SimpleNamespace(id=ROOT_TG_ID)
    chat = SimpleNamespace(id=-1004377897090, type="supergroup")
    assert rep_bot._may_use_admin_callback_in_group(
        user, chat, 99, "admin_approve_rcp_42") is False
    assert rep_bot._may_use_admin_callback_in_group(
        user, SimpleNamespace(id=-100999, type="supergroup"), 7,
        "admin_approve_rcp_42") is False


def test_root_is_refused_for_a_non_receipt_admin_callback_in_the_topic(monkeypatch):
    _patch_finance_topic(monkeypatch)
    user = SimpleNamespace(id=ROOT_TG_ID)
    chat = SimpleNamespace(id=-1004377897090, type="supergroup")
    assert rep_bot._may_use_admin_callback_in_group(
        user, chat, 7, "admin:settings") is False
    assert rep_bot._may_use_admin_callback_in_group(
        user, chat, 7, "admin:users:1") is False


# --------------------------------------------------------------------------
# 4. profile_360 — callback round-trip, back button, page content
# --------------------------------------------------------------------------

def test_profile_callback_round_trips_the_originating_surface():
    cb = profile_360.profile_callback("view", 8537282592, "rcp", "42")
    assert cb == "p360:view:8537282592:rcp:42"
    assert profile_360.parse_profile_callback(cb) == ("view", 8537282592, "rcp", "42")


def test_profile_callback_drops_an_unknown_origin():
    cb = profile_360.profile_callback("view", 5, "bogus", "1")
    assert cb == "p360:view:5::"


def test_malformed_profile_callback_is_rejected_not_raised():
    assert profile_360.parse_profile_callback("p360:view:notanint") is None
    assert profile_360.parse_profile_callback("p360:view:0") is None
    assert profile_360.parse_profile_callback("v52:other") is None


def test_back_row_returns_to_the_exact_originating_surface():
    assert profile_360.back_row("rcp", "42")[0].callback_data == "admin_detail_rcp_42"
    assert profile_360.back_row("tx", "abc")[0].callback_data == "admin:pending_tx:abc"
    assert profile_360.back_row("repnet", "5")[0].callback_data == "rep:my_network"
    # ``v52:support:ticket:`` is not in the callback registry; the registered
    # form is ``v52:ticket:``. Emitting the unregistered one was a dead button.
    assert profile_360.back_row("ticket", "T1")[0].callback_data == "v52:ticket:T1"
    assert profile_360.back_row("abuse", "T2")[0].callback_data == "v52:ticket:T2"


def test_every_profile_back_callback_is_registered_or_handled():
    """A back button the dispatcher cannot resolve is a dead end."""
    import v52_router
    for origin, ref in (("rcp", "42"), ("tx", "abc"), ("ticket", "T1"),
                        ("abuse", "T2"), ("reg", "1"), ("reps", "5"), ("repnet", "5")):
        callback = profile_360.back_row(origin, ref)[0].callback_data
        if callback.startswith("v52:"):
            assert v52_router.route_callback(callback) is not None, callback


def test_profile_page_reports_lifetime_totals_role_and_tier(monkeypatch):
    monkeypatch.setattr(database, "get_bot_user_by_tg_id", lambda _uid: {
        "telegram_id": 8537282592, "full_name": "Javid", "username": "javid",
        "created_at": "2026-01-01 00:00:00", "is_blocked": 0,
        "wallet_balance": 50000, "registration_status": "approved", "tier": "normal",
    })
    monkeypatch.setattr(database, "get_user_locked_amount", lambda _uid: 450000)
    monkeypatch.setattr(database, "get_profile_lifetime_totals", lambda *_a, **_k: {
        "deposits_toman": 900000, "purchases_toman": 450000, "net_toman": 450000})
    monkeypatch.setattr(database, "get_profile_notes", lambda *_a, **_k: [])
    monkeypatch.setattr(database, "count_profile_notes", lambda _uid: 0)
    monkeypatch.setattr(profile_360, "_rep_row_for", lambda _uid: None)
    monkeypatch.setattr(profile_360, "_count", lambda *_a, **_k: 0)

    text, rows = profile_360.build_profile_page(8537282592, allow_management=True)
    assert "پروفایل ۳۶۰ درجه" in text
    assert "مجموع کل واریزی‌ها: **900,000 تومان**" in text
    assert "مجموع کل خریدها: **450,000 تومان**" in text
    assert "کاربر عادی" in text
    callbacks = [b.callback_data for row in rows for b in row]
    assert "p360:services:8537282592::" in callbacks
    assert "p360:notes:8537282592::" in callbacks
    assert "admin:prompt_charge_user:8537282592" in callbacks


def test_profile_page_hides_management_controls_for_a_scoped_viewer(monkeypatch):
    monkeypatch.setattr(database, "get_bot_user_by_tg_id", lambda _uid: {
        "telegram_id": 5, "full_name": "C", "username": "c",
        "created_at": "2026-01-01 00:00:00", "is_blocked": 0,
        "wallet_balance": 0, "registration_status": "approved", "tier": "normal",
    })
    monkeypatch.setattr(database, "get_user_locked_amount", lambda _uid: 0)
    monkeypatch.setattr(database, "get_profile_lifetime_totals", lambda *_a, **_k: {
        "deposits_toman": 0, "purchases_toman": 0, "net_toman": 0})
    monkeypatch.setattr(database, "get_profile_notes", lambda *_a, **_k: [])
    monkeypatch.setattr(database, "count_profile_notes", lambda _uid: 0)
    monkeypatch.setattr(profile_360, "_rep_row_for", lambda _uid: None)
    monkeypatch.setattr(profile_360, "_count", lambda *_a, **_k: 0)

    _text, rows = profile_360.build_profile_page(5, allow_management=False)
    callbacks = [b.callback_data for row in rows for b in row]
    assert not any(c.startswith("admin:prompt_charge_user") for c in callbacks)
    assert not any(c.startswith("admin:set_block_user") for c in callbacks)
    assert not any("promote" in c for c in callbacks)


def test_profile_page_labels_a_sub_representative(monkeypatch):
    monkeypatch.setattr(database, "get_bot_user_by_tg_id", lambda _uid: {
        "telegram_id": 5, "full_name": "S", "username": "s",
        "created_at": "2026-01-01 00:00:00", "is_blocked": 0,
        "wallet_balance": 0, "registration_status": "approved", "tier": "normal"})
    monkeypatch.setattr(database, "get_user_locked_amount", lambda _uid: 0)
    monkeypatch.setattr(database, "get_profile_lifetime_totals", lambda *_a, **_k: {
        "deposits_toman": 0, "purchases_toman": 0, "net_toman": 0})
    monkeypatch.setattr(database, "get_profile_notes", lambda *_a, **_k: [])
    monkeypatch.setattr(database, "count_profile_notes", lambda _uid: 0)
    monkeypatch.setattr(profile_360, "_rep_row_for", lambda _uid: {
        "id": 7, "parent_rep_id": 3, "balance_toman": 0, "tier": "normal"})
    monkeypatch.setattr(profile_360, "_count", lambda *_a, **_k: 0)

    text, _rows = profile_360.build_profile_page(5, allow_management=True)
    assert "زیرنماینده" in text
    assert "نماینده بالاسری" in text


# --------------------------------------------------------------------------
# 5. Lifetime totals and internal notes against a real isolated database
# --------------------------------------------------------------------------

def test_lifetime_totals_and_notes_round_trip(monkeypatch, tmp_path):
    root_path = Path(__file__).resolve().parent.parent
    with helpers.make_db(database, monkeypatch, tmp_path, filename="s55.sqlite3"):
        from migrations_runner import apply_migrations
        with database.get_db() as conn:
            apply_migrations(conn, root_path / "migrations", now=1794237000)
            conn.execute(
                "INSERT INTO financial_ledger (user_id, transaction_type, amount, "
                "balance_after, description, actor_id) VALUES (?,?,?,?,?,?)",
                (777, "credit", 900000, 900000, "deposit", 1))
            conn.execute(
                "INSERT INTO financial_ledger (user_id, transaction_type, amount, "
                "balance_after, description, actor_id) VALUES (?,?,?,?,?,?)",
                (777, "debit", 450000, 450000, "purchase", 1))
            conn.commit()

        totals = database.get_profile_lifetime_totals(777)
        assert totals == {"deposits_toman": 900000, "purchases_toman": 450000,
                          "net_toman": 450000}

        note_id = database.add_profile_note(777, ROOT_TG_ID, "Admin", "  VIP customer  ")
        assert note_id > 0
        notes = database.get_profile_notes(777)
        assert notes and notes[0]["body"] == "VIP customer"
        assert notes[0]["author_name"] == "Admin"
        assert database.count_profile_notes(777) == 1

        with pytest.raises(ValueError):
            database.add_profile_note(777, ROOT_TG_ID, "Admin", "   ")


# --------------------------------------------------------------------------
# 6. Promotion creates the representative row and links the hierarchy
# --------------------------------------------------------------------------

def test_promotion_creates_a_sub_rep_under_the_chosen_parent(monkeypatch, tmp_path):
    root_path = Path(__file__).resolve().parent.parent
    with helpers.make_db(database, monkeypatch, tmp_path, filename="s55-promo.sqlite3"):
        from migrations_runner import apply_migrations
        with database.get_db() as conn:
            apply_migrations(conn, root_path / "migrations", now=1794237000)
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter) "
                "VALUES(3,'PARENT','Parent rep',199,'')")
            conn.execute(
                "INSERT INTO bot_users(telegram_id, role, full_name, username) "
                "VALUES(555,'end_user','New Rep','newrep')")
            conn.commit()

        monkeypatch.setattr(profile_360, "_rep_row_for", lambda _uid: None)
        bot = FakeBot()
        query = FakeQuery(FakeMessage(photo=False), bot)
        update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=ROOT_TG_ID))

        handled = asyncio.run(profile_360._apply_promotion(
            update, SimpleNamespace(user_data={}), 555, "sub", 3, "", ""))
        assert handled is True

        with database.get_db() as conn:
            row = conn.execute(
                "SELECT parent_rep_id, tier FROM representatives WHERE telegram_id=555"
            ).fetchone()
            role = conn.execute(
                "SELECT role FROM bot_users WHERE telegram_id=555").fetchone()
        assert row is not None and int(row["parent_rep_id"]) == 3
        assert role["role"] == "rep"


def test_promotion_refuses_a_person_who_is_already_a_representative(monkeypatch):
    monkeypatch.setattr(profile_360, "_rep_row_for", lambda _uid: {"id": 9})
    bot = FakeBot()
    query = FakeQuery(FakeMessage(photo=False), bot)
    update = SimpleNamespace(callback_query=query, effective_user=SimpleNamespace(id=ROOT_TG_ID))
    handled = asyncio.run(profile_360._apply_promotion(
        update, SimpleNamespace(user_data={}), 555, "independent", None, "", ""))
    assert handled is True
    assert query.answers and "از قبل نماینده" in query.answers[-1][0]


# --------------------------------------------------------------------------
# 7. The receipt approve handler redraws the caption (BUG-FIN-005 end to end)
# --------------------------------------------------------------------------

def test_approve_handler_rewrites_the_photo_caption(monkeypatch):
    receipt = {
        "id": 42, "tracking_code": "RCP-51064524", "role": "end_user",
        "user_id": 8537282592, "amount_toman": 450000, "receipt_note": "n",
        "created_at": "2026-11-15 20:00:00", "status": "approved",
    }
    monkeypatch.setattr(database, "approve_payment_receipt",
                        lambda *_a, **_k: dict(receipt))
    monkeypatch.setattr(rep_bot, "_base_role_of", lambda *_a, **_k: ("super_admin", None))
    monkeypatch.setattr(rep_bot, "_may_use_admin_callback_in_group",
                        lambda *_a, **_k: True)

    bot = FakeBot()
    photo = FakeMessage(photo=True, caption=finance_manager.format_admin_receipt_card(
        {**receipt, "status": "pending"}), chat_id=-1004377897090, thread_id=7)
    query = FakeQuery(photo, bot, data="admin_approve_rcp_42")
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=ROOT_TG_ID, full_name="Root Admin"),
        effective_chat=SimpleNamespace(id=-1004377897090, type="supergroup"),
        effective_message=SimpleNamespace(message_thread_id=7),
    )
    monkeypatch.setattr(rep_bot.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(rep_bot, "_persona_aware_role",
                        lambda *_a, **_k: ("super_admin", None))
    monkeypatch.setattr(rep_bot, "build_actor_navigation_keyboard", lambda *_a, **_k: None)
    monkeypatch.setattr(rep_bot, "_reset_all_conversation_states", lambda *_a, **_k: None)
    monkeypatch.setattr(rep_bot, "_bot_id_of", lambda *_a, **_k: 1)

    asyncio.run(rep_bot.handle_callback(update, SimpleNamespace(user_data={}, bot=bot)))

    assert query.caption_edits, "the caption was never rewritten"
    assert "✅ تایید شده" in query.caption_edits[0][0]
    assert "⏳ در انتظار بررسی" not in query.caption_edits[0][0]
