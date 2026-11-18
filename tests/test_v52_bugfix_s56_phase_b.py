"""Phase B regression tests — one 360-degree profile, and the cards that open it.

Three defects are pinned here:

1. ``ARCH-UX-003`` — ``admin_panel`` carried a *second*, poorer profile renderer.
   An operator who reached a profile from the user list, a receipt, a locked
   transaction or the registration queue saw a different page than one reached from
   the ticket surface: no lifetime totals, no drill-downs, no notes, no tier or
   promotion controls, and a back-button set that only knew four origins. The
   renderer now lives only in :mod:`profile_360`.
2. The block/unblock handler replaced the profile with a hand-rolled summary, and
   the wallet, identity and account handlers answered with the main menu — so the
   operator lost every remaining control on the card they were working from.
3. A sub-representative was reachable only through the root-only
   ``admin:rep_detail:`` button, which answered «دسترسی مجاز نیست» to the very
   representative whose own downstream tree was listing it.

Offline only: no panel, no Telegram, no network.
"""
import asyncio
import base64
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import admin_panel
import config
import database
import profile_360
import rep_panel
import support_ui
import v52_router

ROOT_TG_ID = 101258148
BOT_ID = 9003
CUSTOMER_TG_ID = 303600001
CHILD_REP_TG_ID = 303600777
PARENT_REP_TG_ID = 303600888
NOW = 1794600000


def _load_helpers():
    helper_path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_v52_phaseb_helpers", helper_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helpers = _load_helpers()


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------

class FakeMessage:
    def __init__(self, text="page body"):
        self.text = text
        self.caption = None
        self.photo = None
        self.document = None
        self.message_thread_id = None
        self.message_id = 700
        self.replies = []
        self.edits = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return SimpleNamespace(message_id=len(self.replies))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))
        self.text = text


class FakeQuery:
    def __init__(self, data="", message=None):
        self.data = data
        self.message = message or FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kwargs):
        await self.message.edit_message_text(text, **kwargs)

    async def edit_message_caption(self, caption=None, **kwargs):
        self.message.caption = caption

    async def edit_message_reply_markup(self, reply_markup=None):
        self.message.edits.append(("", {"reply_markup": reply_markup}))


class FakeBot:
    def __init__(self, bot_id=BOT_ID):
        self.id = bot_id
        self.username = "phaseb_bot"
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent))

    async def send_photo(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=len(self.sent))


class FakeContext:
    def __init__(self):
        self.bot = FakeBot()
        self.user_data = {}
        self.chat_data = {}
        self.bot_data = {}
        self.args = []


def _update(uid=ROOT_TG_ID, *, data="", chat_type="private", text=None):
    message = FakeMessage() if text is None else FakeMessage(text=text)
    query = FakeQuery(data, message)
    update = SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=uid, full_name="Root", first_name="Root",
                                       username=f"u{uid}"),
        effective_chat=SimpleNamespace(id=uid, type=chat_type, title=None),
        effective_message=message,
        message=message,
    )
    return update, query


def _migrated_db(monkeypatch, tmp_path, *, filename):
    return helpers.make_db(database, monkeypatch, tmp_path, filename=filename)


def _apply_migrations(conn):
    from migrations_runner import apply_migrations
    root_path = Path(__file__).resolve().parent.parent
    apply_migrations(conn, root_path / "migrations", now=NOW)


def _callbacks(rows):
    return [button.callback_data for row in rows for button in row]


def _seed_people(conn):
    """One customer, one independent rep and one sub-rep under that rep."""
    conn.execute(
        "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,tier,"
        "parent_rep_id,can_create_sub_reps,max_sub_reps) "
        "VALUES(3,'PARENT','نماینده والد',?,'','normal',NULL,1,5)", (PARENT_REP_TG_ID,))
    conn.execute(
        "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,tier,"
        "parent_rep_id) VALUES(4,'CHILD','زیرنماینده',?,'','normal',3)", (CHILD_REP_TG_ID,))
    conn.execute(
        "INSERT INTO bot_users(telegram_id,role,full_name,username,assigned_rep_id) "
        "VALUES(?,'end_user','مشتری','cust',3)", (CUSTOMER_TG_ID,))
    conn.execute(
        "INSERT INTO bot_users(telegram_id,role,full_name,username) "
        "VALUES(?,'rep','نماینده والد','parent')", (PARENT_REP_TG_ID,))
    conn.commit()


# --------------------------------------------------------------------------
# 1. One renderer (ARCH-UX-003)
# --------------------------------------------------------------------------

def _stub_profile_reads(monkeypatch, *, is_blocked=0):
    monkeypatch.setattr(database, "get_bot_user_by_tg_id", lambda _uid: {
        "telegram_id": CUSTOMER_TG_ID, "full_name": "مشتری", "username": "cust",
        "created_at": "2026-01-01 00:00:00", "is_blocked": is_blocked,
        "wallet_balance": 50000, "registration_status": "approved", "tier": "normal",
    })
    monkeypatch.setattr(database, "get_user_locked_amount", lambda _uid: 450000)
    monkeypatch.setattr(database, "get_profile_lifetime_totals", lambda *_a, **_k: {
        "deposits_toman": 900000, "purchases_toman": 450000, "net_toman": 450000})
    monkeypatch.setattr(database, "get_profile_notes", lambda *_a, **_k: [])
    monkeypatch.setattr(database, "count_profile_notes", lambda _uid: 0)
    monkeypatch.setattr(profile_360, "_rep_row_for", lambda _uid: None)
    monkeypatch.setattr(profile_360, "_count", lambda *_a, **_k: 0)


def test_the_admin_callback_renders_exactly_the_360_card(monkeypatch):
    _stub_profile_reads(monkeypatch)
    delegated = admin_panel.build_user_profile_page(CUSTOMER_TG_ID, origin="rcp", ref="42")
    canonical = profile_360.build_profile_page(CUSTOMER_TG_ID, origin="rcp", ref="42")
    assert delegated[0] == canonical[0]
    assert _callbacks(delegated[1]) == _callbacks(canonical[1])


def test_the_duplicate_profile_renderer_and_its_helpers_are_gone():
    """A second renderer, or its private helpers, must not come back."""
    source = Path(admin_panel.__file__).read_text(encoding="utf-8")
    assert "_profile_identity_lines" not in source
    assert "_profile_back_row" not in source
    # The four-value origin tuple that silently dropped the card's other origins.
    assert 'PROFILE_ORIGINS = ("tx", "rcp", "ticket", "reg")' not in source
    # Exactly one definition, and it delegates rather than rendering.
    assert source.count("def build_user_profile_page(") == 1
    assert "profile_360.build_profile_page(" in source


def test_every_origin_the_card_supports_survives_the_admin_callback():
    for origin in profile_360.PROFILE_ORIGINS:
        callback = admin_panel.build_profile_callback(CUSTOMER_TG_ID, origin, "7")
        assert callback == f"admin:user_detail:{CUSTOMER_TG_ID}:{origin}:7"
    # An unknown surface still degrades to the bare form instead of a dangling ref.
    assert admin_panel.build_profile_callback(CUSTOMER_TG_ID, "nope", "7") == \
        f"admin:user_detail:{CUSTOMER_TG_ID}"


def test_the_card_carries_every_documented_control(monkeypatch):
    _stub_profile_reads(monkeypatch)
    text, rows = profile_360.build_profile_page(
        CUSTOMER_TG_ID, origin="rcp", ref="42", allow_management=True)
    callbacks = _callbacks(rows)
    labels = [button.text for row in rows for button in row]

    # Identity and organisation.
    assert "پروفایل ۳۶۰ درجه" in text
    assert "شناسه عددی تلگرام" in text
    assert "وضعیت عضویت" in text
    assert "نقش فعلی" in text
    assert "سطح تعرفه" in text
    assert "معرف" in text
    # Lifetime financial summary.
    assert "موجودی قابل برداشت" in text and "مبلغ محفوظ" in text
    assert "مجموع کل واریزی‌ها" in text and "مجموع کل خریدها" in text
    # Drill-downs.
    for section in ("services", "tickets", "abuse", "tx", "subs"):
        assert any(cb.startswith(f"p360:{section}:{CUSTOMER_TG_ID}") for cb in callbacks)
    # Management controls.
    assert any(cb.startswith("admin:prompt_charge_user") for cb in callbacks)
    assert any(cb.startswith("admin:set_block_user") for cb in callbacks)
    assert any(cb.startswith("admin:prompt_edit_user_id") for cb in callbacks)
    assert any(cb.startswith("admin:prompt_link_user_id") for cb in callbacks)
    assert any(cb.startswith("admin:prompt_unlink_user_acc") for cb in callbacks)
    assert any(cb.startswith("admin:confirm_del_user") for cb in callbacks)
    assert any(cb.startswith("p360:tier:") for cb in callbacks)
    assert any(cb.startswith("p360:promote:") for cb in callbacks)
    assert any(cb.startswith("p360:notes:") for cb in callbacks)
    assert any("اتصال اکانت" in label for label in labels)
    assert any("حذف کاربر" in label for label in labels)
    # Context-aware back.
    assert "admin_detail_rcp_42" in callbacks


def test_a_scoped_viewer_gets_no_management_control_at_all(monkeypatch):
    _stub_profile_reads(monkeypatch)
    _text, rows = profile_360.build_profile_page(
        CUSTOMER_TG_ID, origin="repnet", ref="3", allow_management=False)
    callbacks = _callbacks(rows)
    for prefix in ("admin:prompt_charge_user", "admin:set_block_user",
                   "admin:prompt_edit_user_id", "admin:prompt_link_user_id",
                   "admin:prompt_unlink_user_acc", "admin:confirm_del_user"):
        assert not any(cb.startswith(prefix) for cb in callbacks), prefix
    assert not any("promote" in cb for cb in callbacks)
    assert not any(cb.startswith("p360:tier:") for cb in callbacks)
    # The read-only drill-downs and the way back stay reachable.
    assert any(cb.startswith("p360:services:") for cb in callbacks)
    assert "rep:my_network" in callbacks


def _is_bridged(handler: str) -> bool:
    """Whether the v5.2 bridge implements a registry handler name.

    A registry entry with no bridge renders the "not connected to the Bridge" error
    page, so the registry and the bridge must agree for every callback the card
    emits. ``rep_bot`` is imported lazily because it pulls in the whole bot.
    """
    import rep_bot
    return f'"{handler}"' in Path(rep_bot.__file__).read_text(encoding="utf-8")


def test_the_card_never_emits_a_callback_the_router_rejects(monkeypatch):
    _stub_profile_reads(monkeypatch)
    _text, rows = profile_360.build_profile_page(
        CUSTOMER_TG_ID, origin="rcp", ref="42", allow_management=True)
    for callback in _callbacks(rows):
        if callback.startswith("p360:"):
            assert profile_360.parse_profile_callback(callback) is not None, callback
        elif callback.startswith("v52:"):
            # The card's cross-persona management actions live in the v5.2
            # namespace, so every one of them must resolve to a registered route
            # *and* to a bridge handler — a registry entry with no bridge renders
            # the "not connected to the Bridge" error page instead of a page.
            route = v52_router.route_callback(callback)
            assert isinstance(route, v52_router.Route), callback
            assert _is_bridged(route.handler), callback


# --------------------------------------------------------------------------
# 2. A management action returns to the same card
# --------------------------------------------------------------------------

def test_blocking_a_user_re_renders_the_card_and_keeps_the_origin(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="phaseb-block.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.execute(
                "INSERT INTO bot_users(telegram_id,role,full_name,username) "
                "VALUES(?,'end_user','مشتری','cust')", (CUSTOMER_TG_ID,))
            conn.commit()
        monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", ROOT_TG_ID)
        monkeypatch.setattr(database, "ADMIN_TELEGRAM_ID", ROOT_TG_ID, raising=False)

        # origin=rcp, ref=42 — exactly what the profile card emits.
        data = f"admin:set_block_user:{CUSTOMER_TG_ID}:1:rcp:42"
        update, query = _update(data=data)
        context = FakeContext()
        asyncio.run(admin_panel._handle_admin_callbacks_impl(update, context))

        assert query.message.edits, "the handler must re-render a page"
        body, kwargs = query.message.edits[-1]
        assert "پروفایل ۳۶۰ درجه" in body
        assert "مسدود" in body
        callbacks = _callbacks(kwargs["reply_markup"].inline_keyboard)
        # The card came back with its own controls, not a hand-rolled summary.
        assert any(cb.startswith("admin:prompt_charge_user") for cb in callbacks)
        assert any(cb.startswith("admin:confirm_del_user") for cb in callbacks)
        # And the back button still returns to the receipt it was opened from.
        assert "admin_detail_rcp_42" in callbacks

        with database.get_db() as conn:
            blocked = conn.execute(
                "SELECT is_blocked FROM bot_users WHERE telegram_id=?",
                (CUSTOMER_TG_ID,)).fetchone()["is_blocked"]
        assert int(blocked) == 1


def test_a_wallet_charge_returns_to_the_profile_not_the_main_menu(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="phaseb-charge.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.execute(
                "INSERT INTO bot_users(telegram_id,role,full_name,username) "
                "VALUES(?,'end_user','مشتری','cust')", (CUSTOMER_TG_ID,))
            conn.execute(
                "INSERT INTO user_wallets(user_id,balance_toman) VALUES(?,0)",
                (CUSTOMER_TG_ID,))
            conn.commit()

        context = FakeContext()
        context.user_data["admin_state"] = {
            "action": "charge_user", "target_user_id": CUSTOMER_TG_ID,
            "origin": "rcp", "ref": "42", "ts": admin_panel.time.time(),
        }
        update, _query = _update(text="25000")
        assert asyncio.run(admin_panel.process_admin_text_input(update, context)) is True

        assert update.message.replies, "the handler must answer"
        body, kwargs = update.message.replies[-1]
        assert "پروفایل ۳۶۰ درجه" in body
        assert "کیف پول کاربر" in body
        callbacks = _callbacks(kwargs["reply_markup"].inline_keyboard)
        assert any(cb.startswith("admin:prompt_charge_user") for cb in callbacks)
        assert "admin_detail_rcp_42" in callbacks
        assert "admin_state" not in context.user_data


def test_the_delete_confirmation_cancel_returns_to_the_originating_card(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="phaseb-del.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.execute(
                "INSERT INTO bot_users(telegram_id,role,full_name,username) "
                "VALUES(?,'end_user','مشتری','cust')", (CUSTOMER_TG_ID,))
            conn.commit()
        monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", ROOT_TG_ID)
        monkeypatch.setattr(database, "ADMIN_TELEGRAM_ID", ROOT_TG_ID, raising=False)

        data = f"admin:confirm_del_user:{CUSTOMER_TG_ID}:rcp:42"
        update, query = _update(data=data)
        asyncio.run(admin_panel._handle_admin_callbacks_impl(update, FakeContext()))
        body, kwargs = query.message.edits[-1]
        assert "حذف پروفایل کاربر" in body
        callbacks = _callbacks(kwargs["reply_markup"].inline_keyboard)
        assert f"admin:user_detail:{CUSTOMER_TG_ID}:rcp:42" in callbacks


def test_the_management_prompt_back_button_returns_to_the_profile(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="phaseb-prompt.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.execute(
                "INSERT INTO bot_users(telegram_id,role,full_name,username) "
                "VALUES(?,'end_user','مشتری','cust')", (CUSTOMER_TG_ID,))
            conn.commit()
        monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", ROOT_TG_ID)
        monkeypatch.setattr(database, "ADMIN_TELEGRAM_ID", ROOT_TG_ID, raising=False)

        data = f"admin:prompt_charge_user:{CUSTOMER_TG_ID}:rcp:42"
        update, query = _update(data=data)
        context = FakeContext()
        asyncio.run(admin_panel._handle_admin_callbacks_impl(update, context))

        callbacks = _callbacks(query.message.edits[-1][1]["reply_markup"].inline_keyboard)
        assert f"admin:user_detail:{CUSTOMER_TG_ID}:rcp:42" in callbacks
        # S58: the form is a wizard now, so the originating surface travels in the
        # wizard's own state instead of ``admin_state``. The guarantee is the same —
        # the post-save page returns to the card the form was opened from.
        state = context.user_data["v52_wizard"]
        assert state["origin"] == "rcp" and state["ref"] == "42"
        assert state["return_callback"] == f"admin:user_detail:{CUSTOMER_TG_ID}:rcp:42"


# --------------------------------------------------------------------------
# 3. The inbound cards that open the profile
# --------------------------------------------------------------------------

def test_the_ticket_page_offers_the_profile_to_an_operator_only(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="phaseb-ticket.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.commit()
        operator = support_ui.ticket_page_rows(
            "10001", "unseen", is_operator=True, sender_tg_id=CUSTOMER_TG_ID)
        callbacks = _callbacks(operator)
        assert f"p360:view:{CUSTOMER_TG_ID}:ticket:10001" in callbacks

        customer = support_ui.ticket_page_rows(
            "10001", "unseen", is_operator=False, is_author=True,
            sender_tg_id=CUSTOMER_TG_ID)
        assert not any(cb.startswith("p360:") for cb in _callbacks(customer))


def test_the_abuse_list_offers_the_reporter_profile(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="phaseb-abuse.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,"
                "status,verified_at,revision) VALUES(?,NULL,1,'x','bot','active',?,1)",
                (BOT_ID, NOW))
            conn.execute(
                "INSERT INTO support_tickets(ticket_id,sender_tg_id,bot_id,account_id,rep_id,"
                "ticket_type,assigned_key,status,created_at,updated_at) "
                "VALUES('20001',?,?,NULL,NULL,'abuse_report','root','unseen',?,?)",
                (CUSTOMER_TG_ID, BOT_ID, NOW, NOW))
            conn.commit()
        monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", ROOT_TG_ID)

        import abuse_service
        reports = abuse_service.list_abuse_reports(
            {"actor_tg_id": ROOT_TG_ID, "bot_id": BOT_ID, "persona": "admin",
             "is_root": True})
        assert reports and int(reports[0]["sender_tg_id"]) == CUSTOMER_TG_ID
        # The card the abuse list builds carries the reporter's profile callback.
        callback = profile_360.profile_callback(
            "view", int(reports[0]["sender_tg_id"]), "abuse", str(reports[0]["ticket_id"]))
        assert callback == f"p360:view:{CUSTOMER_TG_ID}:abuse:20001"
        assert profile_360.parse_profile_callback(callback) == (
            "view", CUSTOMER_TG_ID, "abuse", "20001")


def test_the_receipt_and_locked_transaction_cards_keep_their_profile_origin():
    import finance_manager
    import admin_panel as panel

    pending = finance_manager.build_admin_receipt_review_keyboard(42, user_id=CUSTOMER_TG_ID)
    pending_callbacks = _callbacks(pending.inline_keyboard)
    assert f"admin:user_detail:{CUSTOMER_TG_ID}:rcp:42" in pending_callbacks

    settled = finance_manager.build_admin_reviewed_keyboard("approved", CUSTOMER_TG_ID, 42)
    settled_callbacks = _callbacks(settled.inline_keyboard)
    assert f"admin:user_detail:{CUSTOMER_TG_ID}:rcp:42" in settled_callbacks

    intent = {"intent_id": "INTENT-1", "user_id": CUSTOMER_TG_ID, "amount": 450000,
              "action_type": "buy", "status": "needs_reconciliation",
              "operation_lock_key": "user-buy:1", "created_at": NOW,
              "plan_name": "پلن آزمایشی", "result_note": "EVIDENCE_REQUIRED",
              "full_name": "مشتری"}
    _text, rows = panel._pending_intent_card(intent)
    assert f"admin:user_detail:{CUSTOMER_TG_ID}:tx:INTENT-1" in _callbacks(rows)


# --------------------------------------------------------------------------
# 4. Bottom-up: a sub-representative is opened through their own profile
# --------------------------------------------------------------------------

def test_a_sub_rep_button_never_uses_the_root_only_admin_surface(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="phaseb-subs.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_people(conn)
            rep_row = dict(conn.execute(
                "SELECT * FROM representatives WHERE id=3").fetchone())
            conn.commit()

        # A root viewer: the sub-rep opens through the child's own 360 card.
        _text, root_rows = profile_360._subs_page(
            PARENT_REP_TG_ID, "reps", "3", rep_row, allow_management=True)
        root_callbacks = _callbacks(root_rows)
        assert f"p360:view:{CHILD_REP_TG_ID}:reps:4" in root_callbacks
        assert not any(cb.startswith("admin:rep_detail:") for cb in root_callbacks)

        # A scoped representative: same card, but the back button returns to their
        # own network rather than to a root-only admin page.
        _text, rep_rows = profile_360._subs_page(
            PARENT_REP_TG_ID, "repnet", "3", rep_row, allow_management=False)
        rep_callbacks = _callbacks(rep_rows)
        assert f"p360:view:{CHILD_REP_TG_ID}:repnet:4" in rep_callbacks
        assert not any(cb.startswith("admin:rep_detail:") for cb in rep_callbacks)


def test_the_representative_network_opens_sub_reps_through_their_profile(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="phaseb-network.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_people(conn)
            conn.commit()
        rep = dict(database.get_rep_by_id(3))
        update, query = _update(data="rep:my_network")
        asyncio.run(rep_panel._show_rep_network(update, FakeContext(), rep))
        body, kwargs = query.message.edits[-1]
        callbacks = _callbacks(kwargs["reply_markup"].inline_keyboard)
        assert f"p360:view:{CHILD_REP_TG_ID}:repnet:3" in callbacks
        assert f"p360:view:{CUSTOMER_TG_ID}:repnet:3" in callbacks
        assert not any(cb.startswith("admin:rep_detail:") for cb in callbacks)


def test_a_representative_may_open_a_direct_sub_rep_profile_but_not_a_peer(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="phaseb-scope.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_people(conn)
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,tier,"
                "parent_rep_id) VALUES(5,'PEER','نماینده همتا',303600999,'','normal',NULL)")
            conn.commit()
        parent = dict(database.get_rep_by_id(3))
        assert profile_360._rep_may_open(CHILD_REP_TG_ID, parent) is True
        assert profile_360._rep_may_open(CUSTOMER_TG_ID, parent) is True
        assert profile_360._rep_may_open(303600999, parent) is False
        assert profile_360._rep_may_open(PARENT_REP_TG_ID, parent) is False


# --------------------------------------------------------------------------
# 5. The Phase A debts
# --------------------------------------------------------------------------

def test_init_db_provisions_the_late_objects_and_keeps_the_frozen_migration_count(
        monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="phaseb-init.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.commit()
            # init_db already ran inside make_db, before the migrations.
            database.init_db()
            names = {row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            assert "ticket_statuses" in names
            assert "inventory_import_staging" in names
            columns = {row[1] for row in conn.execute("PRAGMA table_info(digital_products)")}
            assert "usage_guide" in columns
            assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 18
        # A restart is idempotent: a second init_db must not raise or duplicate.
        database.init_db()
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM ticket_statuses").fetchone()[0] == 4
            assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
