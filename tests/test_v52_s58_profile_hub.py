"""S58 regression tests — the 360-degree profile as a command hub.

The mission had three axes, and each one is pinned here against the real product
modules and a real temporary SQLite database:

1. ``profile_360.build_profile_page`` takes a ``viewer_rep_id``. An ACTIVE
   representative looking at a person who is genuinely downstream of them — a
   customer whose ``bot_users.assigned_rep_id`` is theirs, or a sub-representative
   whose ``representatives.parent_rep_id`` is theirs — gets a 'Scoped Rep Controls'
   section with exactly three buttons, and nothing else. A root viewer keeps the
   full existing control set; a peer, an upstream viewer and an inactive
   representative get neither.
2. The two cross-persona management actions — issuing a service for this person and
   moving an account's ownership — are on the card for the root *and* for a scoped
   representative, and they are ``v52:`` routes because ``admin_panel`` refuses
   every ``admin:`` callback for a non-root actor. Every callback the card emits
   resolves through ``v52_router.route_callback`` to a handler the bridge in
   ``rep_bot._dispatch_v52_callback`` actually implements.
3. Deep drill-down back navigation: an account opened from the service list and a
   ticket opened from the ticket list both remember the profile they came from, so
   Back returns to that card with its origin and ref instead of a generic list.

Offline only: no panel, no Telegram, no network. CI has no ``.env``, so the
configured root is ``0`` there; the fixture pins a synthetic root on ``config``,
``database`` and ``rep_bot`` the way the S57 fixture does.
"""
import asyncio
import importlib.util
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from migrations_runner import apply_migrations
from panel_adapters.base import LookupResult

import access_service
import account_card_service
import admin_panel
import config
import database
import profile_360
import rep_bot
import v52_router

ROOT_TG_ID = 101258148
BOT_ID = 9103
NOW = 1794700000

ROOT_REP_ID = 1
REP_ID = 41
REP_TG_ID = 303700001
PEER_REP_ID = 42
PEER_TG_ID = 303700002
CUSTOMER_TG_ID = 303700101
CHILD_REP_ID = 43
CHILD_REP_TG_ID = 303700102
PEER_CUSTOMER_TG_ID = 303700103
ACCOUNT_ID = 77
TICKET_ID = "10041"


def _load_helpers():
    helper_path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_v52_s58_helpers", helper_path)
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
        self.chat_id = ROOT_TG_ID
        self.message_thread_id = None
        self.message_id = 900
        self.replies = []
        self.edits = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))
        return SimpleNamespace(message_id=len(self.replies))

    async def edit_message_text(self, text=None, **kwargs):
        self.edits.append((text, kwargs))
        self.text = text

    async def edit_message_caption(self, caption=None, **kwargs):
        self.edits.append((caption, kwargs))
        self.caption = caption


class FakeQuery:
    def __init__(self, data="", message=None):
        self.data = data
        self.message = message or FakeMessage()
        self.answers = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text=None, **kwargs):
        await self.message.edit_message_text(text, **kwargs)

    async def edit_message_caption(self, caption=None, **kwargs):
        await self.message.edit_message_caption(caption, **kwargs)

    async def edit_message_reply_markup(self, reply_markup=None):
        self.message.edits.append(("", {"reply_markup": reply_markup}))


class FakeBot:
    def __init__(self, bot_id=BOT_ID):
        self.id = bot_id
        self.username = "s58_bot"
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
    return SimpleNamespace(
        callback_query=query,
        effective_user=SimpleNamespace(id=uid, full_name="Actor", first_name="Actor",
                                       username=f"u{uid}"),
        effective_chat=SimpleNamespace(id=uid, type=chat_type, title=None),
        effective_message=message,
        message=message,
    ), query


class FakeAdapter:
    def __init__(self, lookup):
        self.lookup = lookup
        self.calls = []

    async def get_client(self, identifier):
        self.calls.append(identifier)
        return self.lookup


class FakeAdapterFactory:
    """A local adapter factory; it never opens a socket."""

    def __init__(self):
        self.client = SimpleNamespace(
            enabled=True, quota_mode="limited", quota_bytes=10_000, used_bytes=2_500,
            time_mode="limited", expires_at=1_900_000_000, observed_at=1_800_000_123,
            subscription_id="synthetic-panel-sub", adapter_revision="panel-r1",
        )
        self.adapter = FakeAdapter(LookupResult("found", self.client))

    def resolve_account_adapter(self, account_id):
        assert int(account_id) == ACCOUNT_ID
        return self.adapter


@contextmanager
def _profile_db(monkeypatch, tmp_path, *, filename):
    """A migrated database with a root, two representatives and their downstream people."""
    # CI has no ``.env``, so the configured root is 0 there and every root-only page
    # would refuse a synthetic actor. Pin the identity instead of inheriting the
    # deployment's — the same value the fixture's context uses.
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", ROOT_TG_ID, raising=False)
    monkeypatch.setattr(database, "ADMIN_TELEGRAM_ID", ROOT_TG_ID, raising=False)
    monkeypatch.setattr(rep_bot, "ADMIN_TELEGRAM_ID", ROOT_TG_ID, raising=False)
    assert helpers.guard_network(monkeypatch) == []
    with helpers.make_db(database, monkeypatch, tmp_path, filename=filename):
        with database.get_db() as conn:
            apply_migrations(conn, Path(__file__).resolve().parent.parent / "migrations",
                             now=NOW)
            _seed_people(conn)
        yield


def _seed_people(conn):
    """One root, one active representative, one peer, and their downstream people."""
    conn.execute(
        "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,is_active,"
        "parent_rep_id,tier,can_create_sub_reps,max_sub_reps) "
        "VALUES(?, 'root_admin_rep', 'ریشه', ?, '', 1, NULL, 'normal', 0, 0)",
        (ROOT_REP_ID, ROOT_TG_ID))
    conn.execute(
        "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,is_active,"
        "parent_rep_id,tier,can_create_sub_reps,max_sub_reps) "
        "VALUES(?, 'S58-REP', 'نمایندهٔ من', ?, '', 1, ?, 'normal', 1, 5)",
        (REP_ID, REP_TG_ID, ROOT_REP_ID))
    conn.execute(
        "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,is_active,"
        "parent_rep_id,tier,can_create_sub_reps,max_sub_reps) "
        "VALUES(?, 'S58-PEER', 'نمایندهٔ همتا', ?, '', 1, ?, 'normal', 1, 5)",
        (PEER_REP_ID, PEER_TG_ID, ROOT_REP_ID))
    conn.execute(
        "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,is_active,"
        "parent_rep_id,tier) VALUES(?, 'S58-CHILD', 'زیرنماینده', ?, '', 1, ?, 'normal')",
        (CHILD_REP_ID, CHILD_REP_TG_ID, REP_ID))
    conn.execute(
        "INSERT INTO bot_users(telegram_id,role,full_name,username,assigned_rep_id) "
        "VALUES(?,'end_user','مشتری من','mine',?)", (CUSTOMER_TG_ID, REP_ID))
    conn.execute(
        "INSERT INTO bot_users(telegram_id,role,full_name,username,assigned_rep_id) "
        "VALUES(?,'end_user','مشتری همتا','peer',?)", (PEER_CUSTOMER_TG_ID, PEER_REP_ID))
    conn.execute(
        "INSERT INTO bot_users(telegram_id,role,full_name,username) "
        "VALUES(?,'rep','نمایندهٔ من','mine_rep')", (REP_TG_ID,))
    conn.execute(
        "INSERT INTO bot_users(telegram_id,role,full_name,username) "
        "VALUES(?,'rep','نمایندهٔ همتا','peer_rep')", (PEER_TG_ID,))
    # ``init_db`` already seeds the default tier ladder; keep the fixture idempotent
    # so it does not depend on whether that seed ran.
    conn.execute(
        "INSERT INTO tier_policies(actor_type,tier_code,title_fa,rank,discount_bps) "
        "VALUES('rep','normal','نماینده عادی',0,0) "
        "ON CONFLICT(actor_type,tier_code) DO NOTHING")
    conn.execute(
        "INSERT INTO tier_policies(actor_type,tier_code,title_fa,rank,discount_bps) "
        "VALUES('rep','gold','نماینده طلایی',2,2000) "
        "ON CONFLICT(actor_type,tier_code) DO NOTHING")
    conn.execute(
        "INSERT INTO accounts(id,rep_id,account_code,email,customer_name,sub_id,enable,"
        "owner_tg_id,seller_key,revision,acl_revision) "
        "VALUES(?,?,'ACC-77','acc-77@example.invalid','مشتری من','sub-77',1,?,'root',1,1)",
        (ACCOUNT_ID, REP_ID, CUSTOMER_TG_ID))
    conn.execute(
        "INSERT INTO bot_registry(bot_id,rep_id,is_primary,username,status,revision) "
        "VALUES(?,NULL,1,'s58_bot','active',1)", (BOT_ID,))
    conn.execute(
        "INSERT INTO support_tickets(ticket_id,sender_tg_id,bot_id,account_id,rep_id,"
        "ticket_type,assigned_key,status,created_at,updated_at) "
        "VALUES(?,?,?,?,?,'general',?,'open',?,?)",
        (TICKET_ID, CUSTOMER_TG_ID, BOT_ID, ACCOUNT_ID, REP_ID,
         f"rep:{REP_ID}", NOW, NOW))
    conn.commit()


def _callbacks(rows):
    return [button.callback_data for row in rows for button in row]


def _labels(rows):
    return [button.text for row in rows for button in row]


def _page_callbacks(query):
    _text, kwargs = query.message.edits[-1]
    return _callbacks(kwargs["reply_markup"].inline_keyboard)


def _page_text(query):
    return query.message.edits[-1][0]


def _profile_callbacks(monkeypatch, user_id, *, viewer_rep_id, allow_management=False,
                       origin="", ref=""):
    monkeypatch.setattr(profile_360, "_count", lambda *_a, **_k: 0)
    _text, rows = profile_360.build_profile_page(
        user_id, origin=origin, ref=ref, allow_management=allow_management,
        viewer_rep_id=viewer_rep_id)
    return _callbacks(rows), _labels(rows)


def _is_bridged(handler: str) -> bool:
    """Whether the v5.2 bridge names this registry handler at all.

    A registry entry with no bridge renders the "not connected to the Bridge" error
    page, so the registry and the bridge must agree for every callback emitted.
    """
    return f'"{handler}"' in Path(rep_bot.__file__).read_text(encoding="utf-8")


def _assert_every_v52_callback_resolves(callbacks):
    for callback in callbacks:
        if not callback.startswith("v52:"):
            continue
        route = v52_router.route_callback(callback)
        assert isinstance(route, v52_router.Route), callback
        assert _is_bridged(route.handler), callback


# --------------------------------------------------------------------------
# 1. The scope rule
# --------------------------------------------------------------------------

def test_the_scope_rule_admits_only_a_genuine_downstream_target(monkeypatch, tmp_path):
    with _profile_db(monkeypatch, tmp_path, filename="s58-scope.sqlite3"):
        # (a) a customer whose assigned_rep_id is mine, (b) my own sub-representative.
        assert profile_360.scoped_rep_scope_holds(CUSTOMER_TG_ID, REP_ID) is True
        assert profile_360.scoped_rep_scope_holds(CHILD_REP_TG_ID, REP_ID) is True
        # A peer's customer, a peer, my own parent, the root and my own card are not.
        assert profile_360.scoped_rep_scope_holds(PEER_CUSTOMER_TG_ID, REP_ID) is False
        assert profile_360.scoped_rep_scope_holds(PEER_TG_ID, REP_ID) is False
        assert profile_360.scoped_rep_scope_holds(ROOT_TG_ID, REP_ID) is False
        assert profile_360.scoped_rep_scope_holds(REP_TG_ID, REP_ID) is False
        # No viewer at all is the root's own card, which never gets scoped controls.
        assert profile_360.scoped_rep_scope_holds(CUSTOMER_TG_ID, None) is False
        assert profile_360.scoped_rep_scope_holds(CUSTOMER_TG_ID, 0) is False
        assert profile_360.scoped_rep_scope_holds(0, REP_ID) is False
        assert profile_360.scoped_rep_scope_holds(CUSTOMER_TG_ID, "not-an-id") is False


def test_an_inactive_representative_loses_the_scoped_controls(monkeypatch, tmp_path):
    with _profile_db(monkeypatch, tmp_path, filename="s58-inactive.sqlite3"):
        assert profile_360.scoped_rep_scope_holds(CUSTOMER_TG_ID, REP_ID) is True
        with database.get_db() as conn:
            conn.execute("UPDATE representatives SET is_active=0 WHERE id=?", (REP_ID,))
            conn.commit()
        assert profile_360.scoped_rep_scope_holds(CUSTOMER_TG_ID, REP_ID) is False
        with database.get_db() as conn:
            conn.execute("UPDATE representatives SET is_active=1, deleted_at=? WHERE id=?",
                         (NOW, REP_ID))
            conn.commit()
        assert profile_360.scoped_rep_scope_holds(CUSTOMER_TG_ID, REP_ID) is False


# --------------------------------------------------------------------------
# 2. The card: scoped controls, and only when the scope rule holds
# --------------------------------------------------------------------------

def test_a_scoped_rep_gets_the_three_controls_and_no_root_control(monkeypatch, tmp_path):
    with _profile_db(monkeypatch, tmp_path, filename="s58-card-scoped.sqlite3"):
        monkeypatch.setattr(profile_360, "_count", lambda *_a, **_k: 0)
        text, rows = profile_360.build_profile_page(
            CUSTOMER_TG_ID, origin="repnet", ref=str(REP_ID), allow_management=False,
            viewer_rep_id=REP_ID)
        callbacks, labels = _callbacks(rows), _labels(rows)
        # The section is announced on the card, not only drawn in the keyboard.
        assert profile_360.SCOPED_SECTION_TITLE in text
        assert profile_360.SCOPED_PROMOTE_LABEL in labels
        assert profile_360.SCOPED_TIER_LABEL in labels
        assert profile_360.SCOPED_NOTE_LABEL in labels
        for section in ("sc_promote", "sc_tier", "sc_note"):
            assert f"p360:{section}:{CUSTOMER_TG_ID}:repnet:41" in callbacks
        # The two cross-persona management actions are there for a scoped rep too.
        assert profile_360.ISSUE_SERVICE_LABEL in labels
        assert profile_360.TRANSFER_OWNERSHIP_LABEL in labels
        assert f"v52:p360new:{CUSTOMER_TG_ID}:repnet:41" in callbacks
        assert f"v52:p360own:{CUSTOMER_TG_ID}:repnet:41" in callbacks
        # Not one root-only control leaks onto the card.
        for prefix in ("admin:prompt_charge_user", "admin:set_block_user",
                       "admin:prompt_edit_user_id", "admin:prompt_link_user_id",
                       "admin:prompt_unlink_user_acc", "admin:confirm_del_user"):
            assert not any(cb.startswith(prefix) for cb in callbacks), prefix
        assert not any(cb.startswith("p360:tier:") for cb in callbacks)
        assert not any(cb.startswith("p360:promote:") for cb in callbacks)
        assert not any(cb.startswith("p360:notenew:") for cb in callbacks)
        # The read-only drill-downs and the way back stay reachable.
        assert f"p360:services:{CUSTOMER_TG_ID}:repnet:41" in callbacks
        assert "rep:my_network" in callbacks
        _assert_every_v52_callback_resolves(callbacks)


def test_a_root_viewer_keeps_the_full_control_set_and_no_scoped_section(
        monkeypatch, tmp_path):
    with _profile_db(monkeypatch, tmp_path, filename="s58-card-root.sqlite3"):
        callbacks, labels = _profile_callbacks(
            monkeypatch, CUSTOMER_TG_ID, viewer_rep_id=None, allow_management=True,
            origin="repnet", ref="41")
        for prefix in ("admin:prompt_charge_user", "admin:set_block_user",
                       "admin:prompt_edit_user_id", "admin:prompt_link_user_id",
                       "admin:prompt_unlink_user_acc", "admin:confirm_del_user"):
            assert any(cb.startswith(prefix) for cb in callbacks), prefix
        assert "p360:tier:" in "".join(callbacks)
        assert any(cb.startswith("p360:promote:") for cb in callbacks)
        # The note *form* lives on the notes page; the card links to that page.
        assert any(cb.startswith("p360:notes:") for cb in callbacks)
        assert not any(cb.startswith("p360:notenew:") for cb in callbacks)
        # The root is never treated as a scoped representative.
        assert profile_360.SCOPED_PROMOTE_LABEL not in labels
        assert profile_360.SCOPED_TIER_LABEL not in labels
        assert profile_360.SCOPED_NOTE_LABEL not in labels
        assert not any(cb.startswith("p360:sc_") for cb in callbacks)
        # Both management actions are on the card for the root as well.
        assert f"v52:p360new:{CUSTOMER_TG_ID}:repnet:41" in callbacks
        assert f"v52:p360own:{CUSTOMER_TG_ID}:repnet:41" in callbacks
        _assert_every_v52_callback_resolves(callbacks)


def test_a_viewer_without_the_scope_rule_gets_no_scoped_control(monkeypatch, tmp_path):
    with _profile_db(monkeypatch, tmp_path, filename="s58-card-outside.sqlite3"):
        # A peer's customer: read-only, and not even the cross-persona pair.
        callbacks, labels = _profile_callbacks(
            monkeypatch, PEER_CUSTOMER_TG_ID, viewer_rep_id=REP_ID)
        assert not any(cb.startswith("p360:sc_") for cb in callbacks)
        assert not any(cb.startswith("v52:p360new:") for cb in callbacks)
        assert not any(cb.startswith("v52:p360own:") for cb in callbacks)
        assert profile_360.SCOPED_PROMOTE_LABEL not in labels
        assert profile_360.ISSUE_SERVICE_LABEL not in labels
        # My own sub-representative is downstream, so the same viewer does get them.
        callbacks, _labels_ = _profile_callbacks(
            monkeypatch, CHILD_REP_TG_ID, viewer_rep_id=REP_ID)
        assert any(cb.startswith("p360:sc_promote:") for cb in callbacks)
        assert any(cb.startswith("v52:p360new:") for cb in callbacks)


# --------------------------------------------------------------------------
# 3. The scoped promote: two steps, one canonical writer
# --------------------------------------------------------------------------

def _press(monkeypatch, data, *, uid=REP_TG_ID, role="rep", rep=None):
    update, query = _update(uid, data=data)
    context = FakeContext()
    asyncio.run(profile_360.handle_profile_callback(
        update, context, role=role, rep=rep))
    return query, context


def test_the_scoped_promote_is_a_two_step_confirmation(monkeypatch, tmp_path):
    with _profile_db(monkeypatch, tmp_path, filename="s58-promote.sqlite3"):
        query, _context = _press(
            monkeypatch, f"p360:sc_promote:{CUSTOMER_TG_ID}:repnet:{REP_ID}")
        # Step one only asks; nothing has been written yet.
        assert "تایید نهایی" in _page_text(query)
        assert f"p360:sc_promote_do:{CUSTOMER_TG_ID}:repnet:{REP_ID}" in _page_callbacks(query)
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT 1 FROM representatives WHERE telegram_id=?",
                (CUSTOMER_TG_ID,)).fetchone() is None

        # Step two applies it, and the card comes back with the scoped controls.
        query, _context = _press(
            monkeypatch, f"p360:sc_promote_do:{CUSTOMER_TG_ID}:repnet:{REP_ID}")
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT id,parent_rep_id,tier FROM representatives WHERE telegram_id=?",
                (CUSTOMER_TG_ID,)).fetchone()
            edge = conn.execute(
                "SELECT parent_key,child_rep_id,active FROM rep_edges "
                "WHERE child_rep_id=? AND active=1", (int(row["id"]),)).fetchone()
            role = conn.execute(
                "SELECT role FROM bot_users WHERE telegram_id=?",
                (CUSTOMER_TG_ID,)).fetchone()["role"]
        assert int(row["parent_rep_id"]) == REP_ID
        assert edge is not None and edge["parent_key"] == f"rep:{REP_ID}"
        assert int(edge["child_rep_id"]) == int(row["id"])
        assert role == "rep"
        assert any(cb.startswith("p360:sc_promote:") for cb in _page_callbacks(query))


def test_a_representative_cannot_promote_somebody_elses_customer(monkeypatch, tmp_path):
    """A peer's customer is not even openable, so the scoped action is unreachable."""
    with _profile_db(monkeypatch, tmp_path, filename="s58-promote-denied.sqlite3"):
        query, _context = _press(
            monkeypatch, f"p360:sc_promote_do:{PEER_CUSTOMER_TG_ID}:repnet:{REP_ID}")
        assert query.answers and "مجاز نیست" in query.answers[-1][0]
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT 1 FROM representatives WHERE telegram_id=?",
                (PEER_CUSTOMER_TG_ID,)).fetchone() is None
        assert query.message.edits == []


def test_a_scoped_action_is_refused_once_the_scope_rule_stops_holding(
        monkeypatch, tmp_path):
    """The rule is re-checked on the press, not only when the button was drawn.

    A soft-deleted sub-representative is still *openable* — ``_rep_may_open`` asks
    only whether the target hangs under the viewer — but is no longer downstream for
    the purposes of the scoped controls, so a card delivered before the deletion
    cannot be used to write afterwards.
    """
    with _profile_db(monkeypatch, tmp_path, filename="s58-promote-stale.sqlite3"):
        assert profile_360.scoped_rep_scope_holds(CHILD_REP_TG_ID, REP_ID) is True
        with database.get_db() as conn:
            conn.execute("UPDATE representatives SET deleted_at=? WHERE id=?",
                         (NOW, CHILD_REP_ID))
            conn.commit()
        assert profile_360.scoped_rep_scope_holds(CHILD_REP_TG_ID, REP_ID) is False
        for section in ("sc_promote", "sc_promote_do", "sc_tier", "sc_tier_do_gold",
                        "sc_note"):
            query, _context = _press(
                monkeypatch, f"p360:{section}:{CHILD_REP_TG_ID}:repnet:{REP_ID}")
            assert query.answers and "فقط برای نمایندهٔ بالاسری فعال" in query.answers[-1][0], \
                section
            assert query.message.edits == [], section
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT tier, deleted_at FROM representatives WHERE id=?",
                (CHILD_REP_ID,)).fetchone()
            assert row["tier"] == "normal"
            assert int(row["deleted_at"]) == NOW
            assert conn.execute(
                "SELECT COUNT(*) AS n FROM profile_admin_notes").fetchone()["n"] == 0


def test_a_root_actor_never_reaches_a_scoped_action(monkeypatch, tmp_path):
    with _profile_db(monkeypatch, tmp_path, filename="s58-promote-root.sqlite3"):
        query, _context = _press(
            monkeypatch, f"p360:sc_promote_do:{CUSTOMER_TG_ID}:repnet:{REP_ID}",
            uid=ROOT_TG_ID, role="super_admin")
        assert query.answers and "مجاز است" in query.answers[-1][0]
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT 1 FROM representatives WHERE telegram_id=?",
                (CUSTOMER_TG_ID,)).fetchone() is None


def test_the_scoped_promote_refuses_a_peer_sub_representative(monkeypatch, tmp_path):
    """A person who already hangs under another parent is not a scoped promotion."""
    with _profile_db(monkeypatch, tmp_path, filename="s58-promote-peer-child.sqlite3"):
        with database.get_db() as conn:
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,"
                "is_active,parent_rep_id,tier) VALUES(44,'S58-PEERCHILD','فرزند همتا',?,"
                "'',1,?,'normal')", (303700104, PEER_REP_ID))
            # They are also a customer of the viewer, which is the only reason the
            # scope rule admits them at all.
            conn.execute(
                "INSERT INTO bot_users(telegram_id,role,full_name,username,assigned_rep_id) "
                "VALUES(?,'rep','فرزند همتا','peerchild',?)", (303700104, REP_ID))
            conn.commit()
        assert profile_360.scoped_rep_scope_holds(303700104, REP_ID) is True
        query, _context = _press(
            monkeypatch, f"p360:sc_promote_do:303700104:repnet:{REP_ID}")
        assert query.answers and "نمایندهٔ دیگری" in query.answers[-1][0]
        with database.get_db() as conn:
            parent = conn.execute(
                "SELECT parent_rep_id FROM representatives WHERE id=44").fetchone()
            edge = conn.execute(
                "SELECT COUNT(*) AS n FROM rep_edges WHERE child_rep_id=44").fetchone()
        assert int(parent["parent_rep_id"]) == PEER_REP_ID
        assert int(edge["n"]) == 0


# --------------------------------------------------------------------------
# 4. The scoped tariff tier
# --------------------------------------------------------------------------

def test_the_scoped_tier_picker_sets_the_sub_representative_tier(monkeypatch, tmp_path):
    with _profile_db(monkeypatch, tmp_path, filename="s58-tier.sqlite3"):
        query, _context = _press(
            monkeypatch, f"p360:sc_tier:{CHILD_REP_TG_ID}:repnet:{REP_ID}")
        callbacks = _page_callbacks(query)
        assert f"p360:sc_tier_do_gold:{CHILD_REP_TG_ID}:repnet:{REP_ID}" in callbacks
        assert f"p360:sc_tier_do_normal:{CHILD_REP_TG_ID}:repnet:{REP_ID}" in callbacks

        query, _context = _press(
            monkeypatch, f"p360:sc_tier_do_gold:{CHILD_REP_TG_ID}:repnet:{REP_ID}")
        with database.get_db() as conn:
            tier = conn.execute(
                "SELECT tier FROM representatives WHERE id=?", (CHILD_REP_ID,)).fetchone()
        assert tier["tier"] == "gold"
        assert any(cb.startswith("p360:sc_tier:") for cb in _page_callbacks(query))


def test_the_scoped_tier_refuses_an_unknown_code_and_a_non_representative(
        monkeypatch, tmp_path):
    with _profile_db(monkeypatch, tmp_path, filename="s58-tier-denied.sqlite3"):
        query, _context = _press(
            monkeypatch, f"p360:sc_tier_do_platinum:{CHILD_REP_TG_ID}:repnet:{REP_ID}")
        assert query.answers and "تعریف نشده" in query.answers[-1][0]
        with database.get_db() as conn:
            tier = conn.execute(
                "SELECT tier FROM representatives WHERE id=?", (CHILD_REP_ID,)).fetchone()
        assert tier["tier"] == "normal"

        # A customer who is not a representative yet has no tier to set.
        query, _context = _press(
            monkeypatch, f"p360:sc_tier_do_gold:{CUSTOMER_TG_ID}:repnet:{REP_ID}")
        assert query.answers and "هنوز نماینده نیست" in query.answers[-1][0]


# --------------------------------------------------------------------------
# 5. The scoped management note reuses the one profile_note writer
# --------------------------------------------------------------------------

def test_the_scoped_note_form_is_the_admin_profile_note_flow(monkeypatch, tmp_path):
    with _profile_db(monkeypatch, tmp_path, filename="s58-note.sqlite3"):
        query, context = _press(
            monkeypatch, f"p360:sc_note:{CUSTOMER_TG_ID}:repnet:{REP_ID}")
        state = context.user_data["admin_state"]
        assert state["action"] == "profile_note"
        assert state["target_user_id"] == CUSTOMER_TG_ID
        assert state["scoped_rep_id"] == REP_ID
        assert "یادداشت" in _page_text(query)

        # The text router hands this one admin form action to the admin writer.
        update, _query = _update(REP_TG_ID, text="مشتری وفادار است")
        context.effective_chat = update.effective_chat
        assert rep_bot._p360_scoped_note_pending(context) is True
        assert asyncio.run(admin_panel.process_admin_text_input(update, context)) is True
        notes = database.get_profile_notes(CUSTOMER_TG_ID)
        assert notes and notes[0]["body"] == "مشتری وفادار است"
        assert int(notes[0]["author_tg_id"]) == REP_TG_ID
        # The page it lands on still offers the scoped form, not the root-only one.
        body, kwargs = update.message.replies[-1]
        assert "یادداشت داخلی ثبت شد" in body
        callbacks = _callbacks(kwargs["reply_markup"].inline_keyboard)
        assert f"p360:sc_note:{CUSTOMER_TG_ID}:repnet:{REP_ID}" in callbacks
        assert not any(cb.startswith("p360:notenew:") for cb in callbacks)
        assert "admin_state" not in context.user_data


def test_the_note_marker_is_only_written_for_a_scoped_representative(monkeypatch, tmp_path):
    with _profile_db(monkeypatch, tmp_path, filename="s58-note-marker.sqlite3"):
        # The root's own note form carries no scoped marker, so nothing changes for it.
        _query, context = _press(
            monkeypatch, f"p360:notenew:{CUSTOMER_TG_ID}:repnet:{REP_ID}",
            uid=ROOT_TG_ID, role="super_admin")
        assert "scoped_rep_id" not in context.user_data["admin_state"]
        assert rep_bot._p360_scoped_note_pending(context) is False
        # And a bare admin_state of another action is never routed for a rep.
        context.user_data["admin_state"] = {"action": "charge_user", "ts": 0}
        assert rep_bot._p360_scoped_note_pending(context) is False
        context.user_data.pop("admin_state")


# --------------------------------------------------------------------------
# 6. The two cross-persona management actions
# --------------------------------------------------------------------------

def _dispatch(monkeypatch, data, *, uid=ROOT_TG_ID, role="super_admin", rep=None):
    update, query = _update(uid, data=data)
    context = FakeContext()
    context.bot_data["panel_adapter_factory"] = FakeAdapterFactory()
    monkeypatch.setattr(access_service, "authorize_account", lambda *_a, **_k: True)
    asyncio.run(rep_bot._dispatch_v52_callback(update, context, role=role, rep=rep))
    return query, context


def test_the_issue_service_route_is_registered_and_bridged_for_both_viewers(
        monkeypatch, tmp_path):
    with _profile_db(monkeypatch, tmp_path, filename="s58-issue.sqlite3"):
        route = v52_router.route_callback(f"v52:p360new:{CUSTOMER_TG_ID}:repnet:{REP_ID}")
        assert isinstance(route, v52_router.Route)
        assert route.handler == "p360_issue_service"
        assert route.params == {"user_id": CUSTOMER_TG_ID, "origin": "repnet",
                               "ref": str(REP_ID)}
        assert _is_bridged(route.handler)

        # Root: the free pool and the account link, plus the way back to the card.
        query, _context = _dispatch(
            monkeypatch, f"v52:p360new:{CUSTOMER_TG_ID}:repnet:{REP_ID}")
        callbacks = _page_callbacks(query)
        assert "admin:pool" in callbacks
        assert any(cb.startswith("admin:prompt_link_user_id") for cb in callbacks)
        assert f"p360:view:{CUSTOMER_TG_ID}:repnet:{REP_ID}" in callbacks
        assert "صدور سرویس" in _page_text(query)

        # A scoped representative: the representative issuance surface instead.
        query, _context = _dispatch(
            monkeypatch, f"v52:p360new:{CUSTOMER_TG_ID}:repnet:{REP_ID}",
            uid=REP_TG_ID, role="rep",
            rep={"id": REP_ID, "name": "نمایندهٔ من"})
        callbacks = _page_callbacks(query)
        assert "rep:new_acc_plans" in callbacks
        assert "admin:pool" not in callbacks
        assert f"p360:view:{CUSTOMER_TG_ID}:repnet:{REP_ID}" in callbacks

        # A representative who is not the customer's representative is refused.
        query, _context = _dispatch(
            monkeypatch, f"v52:p360new:{PEER_CUSTOMER_TG_ID}:repnet:{REP_ID}",
            uid=REP_TG_ID, role="rep", rep={"id": REP_ID, "name": "نمایندهٔ من"})
        assert "مجاز نیست" in _page_text(query)


def test_the_transfer_ownership_route_is_registered_and_bridged(monkeypatch, tmp_path):
    with _profile_db(monkeypatch, tmp_path, filename="s58-transfer.sqlite3"):
        route = v52_router.route_callback(f"v52:p360own:{CUSTOMER_TG_ID}")
        assert isinstance(route, v52_router.Route)
        assert route.handler == "p360_transfer_ownership"
        assert route.params == {"user_id": CUSTOMER_TG_ID, "origin": "", "ref": ""}
        assert _is_bridged(route.handler)

        # Root: one transfer button per account, routed to the existing v5.2 flow.
        query, _context = _dispatch(monkeypatch, f"v52:p360own:{CUSTOMER_TG_ID}")
        callbacks = _page_callbacks(query)
        assert f"v52:transfer:{ACCOUNT_ID}" in callbacks
        assert f"p360:view:{CUSTOMER_TG_ID}::" in callbacks
        _assert_every_v52_callback_resolves(callbacks)

        # A scoped representative cannot move ownership; the page says so and offers
        # the management note instead of a button that only ever refuses.
        query, _context = _dispatch(
            monkeypatch, f"v52:p360own:{CUSTOMER_TG_ID}:repnet:{REP_ID}",
            uid=REP_TG_ID, role="rep", rep={"id": REP_ID, "name": "نمایندهٔ من"})
        callbacks = _page_callbacks(query)
        assert not any(cb.startswith("v52:transfer:") for cb in callbacks)
        assert f"p360:sc_note:{CUSTOMER_TG_ID}:repnet:{REP_ID}" in callbacks
        assert "مدیر کل" in _page_text(query)


# --------------------------------------------------------------------------
# 7. Deep drill-down back navigation
# --------------------------------------------------------------------------

def test_an_account_opened_from_the_service_list_returns_to_the_profile(
        monkeypatch, tmp_path):
    with _profile_db(monkeypatch, tmp_path, filename="s58-drill-account.sqlite3"):
        # The service list carries the profile it was opened from.
        text, rows = profile_360._services_page(
            CUSTOMER_TG_ID, "repnet", str(REP_ID))
        callbacks = _callbacks(rows)
        contextual = f"v52:p360acc:{ACCOUNT_ID}:{CUSTOMER_TG_ID}:repnet:{REP_ID}"
        assert contextual in callbacks
        assert f"p360:view:{CUSTOMER_TG_ID}:repnet:{REP_ID}" in callbacks
        assert "ACC-77" in text

        route = v52_router.route_callback(contextual)
        assert isinstance(route, v52_router.Route)
        assert route.handler == "p360_account_card"
        assert route.params["account_id"] == ACCOUNT_ID
        assert _is_bridged(route.handler)

        # The card itself comes back with the profile as its Back destination.
        query, _context = _dispatch(monkeypatch, contextual)
        card_callbacks = _page_callbacks(query)
        assert f"p360:view:{CUSTOMER_TG_ID}:repnet:{REP_ID}" in card_callbacks
        assert "ACC-77" in _page_text(query)


def test_a_ticket_opened_from_the_ticket_list_returns_to_the_profile(
        monkeypatch, tmp_path):
    with _profile_db(monkeypatch, tmp_path, filename="s58-drill-ticket.sqlite3"):
        text, rows = profile_360._tickets_page(
            CUSTOMER_TG_ID, "users", "")
        callbacks = _callbacks(rows)
        contextual = f"v52:p360tkt:{TICKET_ID}:{CUSTOMER_TG_ID}:users:"
        assert contextual not in callbacks, "an empty ref must not be carried"
        assert f"v52:ticket:{TICKET_ID}" in callbacks
        assert TICKET_ID in text

        # With a real originating surface the context is carried instead.
        _text, rows = profile_360._tickets_page(CUSTOMER_TG_ID, "ticket", "10001")
        contextual = f"v52:p360tkt:{TICKET_ID}:{CUSTOMER_TG_ID}:ticket:10001"
        assert contextual in _callbacks(rows)
        route = v52_router.route_callback(contextual)
        assert isinstance(route, v52_router.Route)
        assert route.handler == "p360_ticket"
        assert route.params == {"ticket_id": TICKET_ID, "user_id": CUSTOMER_TG_ID,
                               "origin": "ticket", "ref": "10001"}
        assert _is_bridged(route.handler)

        query, _context = _dispatch(monkeypatch, contextual)
        page_callbacks = _page_callbacks(query)
        # The ticket's own generic exit is retargeted at the profile: the v5.2 footer
        # never adds a second back control to a page that already ships one.
        assert f"p360:view:{CUSTOMER_TG_ID}:ticket:10001" in page_callbacks
        assert "v52:tickets" not in page_callbacks
        assert "🔙 بازگشت به پروفایل ۳۶۰ درجه" in [
            button.text for row in query.message.edits[-1][1]["reply_markup"].inline_keyboard
            for button in row]


def test_the_plain_routes_still_work_without_a_profile_context(monkeypatch, tmp_path):
    with _profile_db(monkeypatch, tmp_path, filename="s58-drill-plain.sqlite3"):
        # No origin/ref means no context to carry: the plain, registered route stays.
        assert profile_360.account_card_callback(ACCOUNT_ID, CUSTOMER_TG_ID) == \
            f"v52:sub:{ACCOUNT_ID}"
        assert profile_360.ticket_callback(TICKET_ID, CUSTOMER_TG_ID) == \
            f"v52:ticket:{TICKET_ID}"
        # An unknown surface is not a context either.
        assert profile_360.account_card_callback(ACCOUNT_ID, CUSTOMER_TG_ID, "nope", "1") == \
            f"v52:sub:{ACCOUNT_ID}"

        query, _context = _dispatch(monkeypatch, f"v52:sub:{ACCOUNT_ID}")
        assert "ACC-77" in _page_text(query)
        # The plain card's Back button is a registered destination, not the
        # long-dead ``v52:accounts:<persona>`` placeholder.
        assert "v52:home:admin" in _page_callbacks(query)

        query, _context = _dispatch(monkeypatch, f"v52:ticket:{TICKET_ID}")
        assert f"v52:ticket:{TICKET_ID}" in _page_callbacks(query) or \
            "v52:home:admin" in _page_callbacks(query)


def test_an_oversized_reference_degrades_instead_of_breaking_the_message(
        monkeypatch, tmp_path):
    with _profile_db(monkeypatch, tmp_path, filename="s58-drill-oversize.sqlite3"):
        long_ref = "r" * 40
        oversized = f"v52:p360acc:{ACCOUNT_ID}:{CUSTOMER_TG_ID}:ticket:{long_ref}"
        assert len(oversized.encode("utf-8")) > 64
        assert profile_360.account_card_callback(
            ACCOUNT_ID, CUSTOMER_TG_ID, "ticket", long_ref) == f"v52:sub:{ACCOUNT_ID}"
        assert profile_360.ticket_callback(
            TICKET_ID, CUSTOMER_TG_ID, "ticket", long_ref) == f"v52:ticket:{TICKET_ID}"
        assert profile_360.management_callback(
            "p360new", CUSTOMER_TG_ID, "ticket", long_ref) == \
            f"v52:p360new:{CUSTOMER_TG_ID}"

        # Every callback the card emits still fits Telegram's 64-byte budget.
        _text, rows = profile_360.build_profile_page(
            CUSTOMER_TG_ID, origin="ticket", ref=long_ref, allow_management=True)
        for callback in _callbacks(rows):
            assert len(callback.encode("utf-8")) <= 64, callback


def test_the_registry_and_the_bridge_agree_on_every_new_route(monkeypatch, tmp_path):
    """The four new routes resolve, and each one has a bridge branch."""
    with _profile_db(monkeypatch, tmp_path, filename="s58-registry.sqlite3"):
        expected = {
            f"v52:p360new:{CUSTOMER_TG_ID}": "p360_issue_service",
            f"v52:p360new:{CUSTOMER_TG_ID}:ticket:10001": "p360_issue_service",
            f"v52:p360own:{CUSTOMER_TG_ID}": "p360_transfer_ownership",
            f"v52:p360acc:{ACCOUNT_ID}:{CUSTOMER_TG_ID}:ticket:10001": "p360_account_card",
            f"v52:p360tkt:{TICKET_ID}:{CUSTOMER_TG_ID}:ticket:10001": "p360_ticket",
        }
        for callback, handler in expected.items():
            route = v52_router.route_callback(callback)
            assert isinstance(route, v52_router.Route), callback
            assert route.handler == handler, callback
            assert _is_bridged(handler), callback
        # A malformed or widened callback is still refused by the registry.
        for forged in (
            f"v52:p360new:0",
            f"v52:p360acc:{ACCOUNT_ID}:{CUSTOMER_TG_ID}:bogus:1",
            f"v52:p360acc:{ACCOUNT_ID}:{CUSTOMER_TG_ID}",
            f"v52:p360tkt:{TICKET_ID}:{CUSTOMER_TG_ID}:ticket:",
            f"v52:p360tkt:{TICKET_ID}:0:ticket:1",
        ):
            assert not isinstance(v52_router.route_callback(forged), v52_router.Route), forged


def test_the_bridge_never_renders_its_unimplemented_handler_page(monkeypatch, tmp_path):
    """Every new route answers with a page, never with the bridge error."""
    with _profile_db(monkeypatch, tmp_path, filename="s58-bridge-pages.sqlite3"):
        for callback in (
            f"v52:p360new:{CUSTOMER_TG_ID}",
            f"v52:p360own:{CUSTOMER_TG_ID}",
            f"v52:p360acc:{ACCOUNT_ID}:{CUSTOMER_TG_ID}:ticket:10001",
            f"v52:p360tkt:{TICKET_ID}:{CUSTOMER_TG_ID}:ticket:10001",
        ):
            query, _context = _dispatch(monkeypatch, callback)
            assert query.message.edits, callback
            body = _page_text(query)
            # The two failure pages a registry entry without a bridge can produce.
            assert "به Bridge متصل نیست" not in body, callback
            assert "در registry ثبت نشده" not in body, callback
            assert "مسیر" not in body.split("\n")[0], callback
