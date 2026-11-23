"""S48 regression suite for the four live-test defects.

BUG-RBAC-001  role lockout after switching panel
BUG-NAV-002   unregistered back callback ("W18 bridge") on the second back press
BUG-SUP-003   incomplete ticketing: no new-ticket entry, no hierarchical routing
BUG-SUP-004   abuse reporting merged into the support ticket queue

Every assertion here is offline: temporary databases, fake services, no sockets.
"""
from __future__ import annotations

import base64
import importlib.util
import pathlib
import re
import sys

import pytest
from migrations_runner import apply_migrations

import admin_panel
import rep_panel
import support_ui
import ui_v52
import user_portal
import v52_router

PRODUCT_ROOT = pathlib.Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# BUG-NAV-002 — no navigation surface may emit an unregistered callback
# --------------------------------------------------------------------------- #

def _resolves(data: str) -> bool:
    return isinstance(v52_router.route_callback(data), v52_router.Route)


def test_navigation_rows_never_emit_an_unregistered_callback():
    """A page rendered without a parent must still offer a working exit."""
    for persona in ("admin", "rep", "user"):
        for parent in (None, "v52:home:" + persona):
            page = ui_v52.render_page("عنوان", "متن", persona=persona, parent_callback=parent)
            callbacks = [item.callback_data
                         for row in page.reply_markup.inline_keyboard for item in row]
            assert callbacks, "a non-root page must always expose at least one exit"
            for callback in callbacks:
                assert not callback.startswith("v52:back:"), callback
                assert _resolves(callback), callback


def test_legacy_back_placeholder_is_still_registered_for_delivered_messages():
    """Messages already in users' chats carry ``v52:back:*``; it must keep working."""
    for persona in ("admin", "rep", "user"):
        route = v52_router.route_callback(f"v52:back:{persona}")
        assert isinstance(route, v52_router.Route)
        assert route.handler == "route_home"
        assert route.params == {"persona": persona}


def test_every_v52_callback_literal_in_the_product_is_reachable():
    """Structural guard: no module may hard-code a callback the router rejects."""
    # payment_service stores these two strings as *action names* inside
    # action_confirmations.action, not as keyboard callback_data; they are never
    # emitted as navigation and are matched by the review flow, not the router.
    action_name_aliases = {
        ("payment_service.py", "v52:receipt:approve"),
        ("payment_service.py", "v52:receipt:reject"),
    }
    literal = re.compile(r'"(v52:[^"]*)"')
    prefixes = {pattern.pattern[len("^"):] for pattern, *_ in v52_router._CALLBACK_PATTERNS}
    offenders = []
    for path in sorted(PRODUCT_ROOT.glob("*.py")):
        for raw in literal.findall(path.read_text(encoding="utf-8")):
            if "{" in raw:
                continue  # an f-string template, only resolvable at runtime
            if (path.name, raw) in action_name_aliases:
                continue
            if raw.endswith(":"):
                # A prefix used for concatenation; some registered pattern must extend it.
                if not any(prefix.startswith(re.escape(raw)) for prefix in prefixes):
                    offenders.append((path.name, raw))
                continue
            if not _resolves(raw):
                offenders.append((path.name, raw))
    assert offenders == []


def test_second_and_third_back_press_stay_navigable():
    """Walk three hops down and back up; every emitted callback must resolve."""
    chain = ["v52:home:user", "v52:tickets", "v52:newticket", "v52:tickets:open"]
    for callback in chain:
        assert _resolves(callback), callback
    page = ui_v52.render_error("خطا", persona="user", parent_callback="v52:tickets")
    callbacks = [item.callback_data
                 for row in page.reply_markup.inline_keyboard for item in row]
    assert callbacks == ["v52:tickets", "v52:home:user"]
    assert all(_resolves(item) for item in callbacks)


# --------------------------------------------------------------------------- #
# BUG-RBAC-001 — persona switching must never lock an actor into a lower role
# --------------------------------------------------------------------------- #

def _callbacks(markup):
    return [item.callback_data for row in markup.inline_keyboard for item in row]


def test_user_portal_access_admits_higher_roles_and_rejects_foreign_ones():
    assert user_portal.user_portal_access("end_user")
    assert user_portal.user_portal_access("rep")
    assert user_portal.user_portal_access("super_admin")
    assert not user_portal.user_portal_access("unauthorized_group")
    assert not user_portal.user_portal_access("blocked")
    assert not user_portal.user_portal_access("")


def test_user_portal_keyboard_offers_the_way_back_for_each_base_role():
    assert _callbacks(user_portal.build_user_portal_keyboard(base_role="end_user")) == [
        item for item in _callbacks(user_portal.build_user_portal_keyboard(base_role=None))
    ]
    rep_rows = user_portal.persona_switch_rows("rep")
    assert [[item.callback_data for item in row] for row in rep_rows] == [["v52:persona:rep"]]
    admin_rows = user_portal.persona_switch_rows("super_admin")
    assert [[item.callback_data for item in row] for row in admin_rows] == [
        ["v52:persona:rep"], ["v52:persona:admin"],
    ]
    admin_keyboard = user_portal.build_user_portal_keyboard(base_role="super_admin")
    assert "v52:persona:rep" in _callbacks(admin_keyboard)
    assert "v52:persona:admin" in _callbacks(admin_keyboard)
    rep_keyboard = user_portal.build_user_portal_keyboard(base_role="rep")
    assert "v52:persona:rep" in _callbacks(rep_keyboard)
    assert "v52:persona:admin" not in _callbacks(rep_keyboard)


def test_rep_keyboard_offers_the_way_back_to_global_management():
    admin_keyboard = rep_panel.build_rep_main_keyboard(base_role="super_admin")
    assert "v52:persona:admin" in _callbacks(admin_keyboard)
    assert "v52:persona:user" in _callbacks(admin_keyboard)
    rep_keyboard = rep_panel.build_rep_main_keyboard(base_role="rep")
    assert "v52:persona:admin" not in _callbacks(rep_keyboard)
    assert "v52:persona:user" in _callbacks(rep_keyboard)


def test_admin_keyboard_reaches_both_lower_personas():
    callbacks = _callbacks(admin_panel.build_admin_main_keyboard())
    assert "v52:persona:rep" in callbacks
    assert "v52:persona:user" in callbacks


def test_persona_switch_targets_are_registered_and_resolvable():
    for target in ("admin", "rep", "user"):
        route = v52_router.route_callback(f"v52:persona:{target}")
        assert isinstance(route, v52_router.Route)
        assert route.handler == "switch_persona"
        assert route.params == {"persona": target}


def test_ensure_portal_customer_rows_never_demotes_an_existing_role(monkeypatch):
    """The guarantee adds the wallet row but must leave ``super_admin`` intact."""
    recorded = {}

    def fake_ensure_bot_user(user_id, username, full_name, role="end_user"):
        recorded["role"] = role
        return {"telegram_id": user_id, "role": "super_admin"}

    class _Conn:
        def execute(self, sql, params=None):
            recorded.setdefault("sql", []).append(sql)

        def commit(self):
            recorded["committed"] = True

    class _Ctx:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(user_portal.database, "ensure_bot_user", fake_ensure_bot_user)
    monkeypatch.setattr(user_portal.database, "get_db", lambda: _Ctx())

    user_portal.ensure_portal_customer_rows(
        type("U", (), {"id": 101258148, "username": "root", "full_name": "Root"})()
    )

    assert recorded["role"] == "end_user"
    assert any("user_wallets" in sql for sql in recorded["sql"])
    assert recorded["committed"] is True


def test_ensure_portal_customer_rows_survives_a_failing_database(monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(user_portal.database, "ensure_bot_user", boom)
    # Must not raise: a broken guarantee must not take the portal down with it.
    user_portal.ensure_portal_customer_rows(
        type("U", (), {"id": 101258148, "username": "root", "full_name": "Root"})()
    )


# --------------------------------------------------------------------------- #
# BUG-SUP-003 / BUG-SUP-004 — ticketing, hierarchical routing, abuse separation
# --------------------------------------------------------------------------- #

def _load(path, name, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _seed_user(conn, telegram_id, *, rep_id=None):
    conn.execute(
        "INSERT INTO bot_users(telegram_id,role,rep_id,username,is_blocked) "
        "VALUES(?,'end_user',?,?,0)",
        (telegram_id, rep_id, f"s48_{telegram_id}"),
    )
    conn.execute(
        "INSERT INTO user_settings(user_id,first_connect_mode,active_persona) "
        "VALUES(?,0,'end_user')",
        (telegram_id,),
    )
    conn.execute(
        "INSERT INTO user_wallets(user_id,balance_toman) VALUES(?,5000)",
        (telegram_id,),
    )


@pytest.fixture()
def support_stack(tmp_path, monkeypatch):
    """Real support/abuse services over a temporary database, network blocked."""
    helpers = _load(pathlib.Path(__file__).with_name("v52_helpers.py"), "_s48_helpers", monkeypatch)
    attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(PRODUCT_ROOT, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    _load(PRODUCT_ROOT / "access_service.py", "access_service", monkeypatch)
    config = sys.modules["config"]
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9090)
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    support = _load(PRODUCT_ROOT / "support_service.py", "support_service", monkeypatch)
    abuse = _load(PRODUCT_ROOT / "abuse_service.py", "abuse_service", monkeypatch)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="s48-support.sqlite3"):
        with database.get_db() as conn:
            apply_migrations(conn, PRODUCT_ROOT / "migrations", now=1794500000)
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,parent_rep_id) "
                "VALUES(651,'S48-PARENT','S48 parent',199,'',NULL)"
            )
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,parent_rep_id) "
                "VALUES(650,'S48-SELLER','S48 seller',303,'',651)"
            )
            for telegram_id, rep_id in ((101, None), (199, 651), (303, 650), (9090, None)):
                _seed_user(conn, telegram_id, rep_id=rep_id)
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,username,status,verified_at,revision) "
                "VALUES(9001,NULL,1,'s48_primary','active',1794500000,1)"
            )
        helpers.seed_account(
            database, account_id=803, rep_id=650, owner_tg_id=101,
            account_code="S48-ACC", email="s48@example.invalid",
        )
        with database.get_db() as conn:
            conn.execute("UPDATE accounts SET seller_key='rep:650' WHERE id=803")
        yield {
            "database": database, "support": support, "abuse": abuse,
            "attempts": attempts, "sentinel": sentinel,
            "customer": {"actor_tg_id": 101, "bot_id": 9001},
            "seller": {"actor_tg_id": 303, "bot_id": 9001, "rep_id": 650},
            "parent": {"actor_tg_id": 199, "bot_id": 9001, "rep_id": 651},
            "root": {"actor_tg_id": 9090, "bot_id": 9001},
        }


def test_customer_ticket_is_routed_to_the_parent_reseller(support_stack):
    support = support_stack["support"]
    ticket = support.create_ticket(
        support_stack["customer"], 803, "خدمات قطع است", now=1794500010,
    )
    assert ticket["assigned_key"] == "rep:650"
    assert ticket["status"] == "unseen"
    assert support.count_tickets(support_stack["seller"], status="open") == 1


def test_representative_ticket_goes_to_global_management(support_stack):
    support = support_stack["support"]
    ticket = support.create_ticket(
        support_stack["seller"], 803, "درخواست نمایندگی", now=1794500020,
    )
    assert ticket["assigned_key"] == "root"
    # A peer/parent representative must never inherit the rep's own ticket.
    assert support.count_tickets(support_stack["parent"], status="open") == 0
    assert support.count_tickets(support_stack["root"], status="open") == 1
    with support_stack["database"].get_db() as conn:
        row = conn.execute(
            "SELECT assigned_key,rep_id FROM support_tickets WHERE ticket_id=?",
            (ticket["ticket_id"],),
        ).fetchone()
    assert tuple(row) == ("root", None)


def test_general_ticket_without_an_account_is_accepted(support_stack):
    support = support_stack["support"]
    ticket = support.create_ticket(
        support_stack["customer"], None, "سؤال عمومی درباره تعرفه‌ها", now=1794500030,
    )
    assert ticket["ticket_type"] == "general"
    assert ticket["assigned_key"] == "root"
    assert ticket["snapshot"]["state"] == "not_applicable"
    with support_stack["database"].get_db() as conn:
        row = conn.execute(
            "SELECT account_id,snapshot_cipher FROM support_tickets WHERE ticket_id=?",
            (ticket["ticket_id"],),
        ).fetchone()
    assert row["account_id"] is None
    assert row["snapshot_cipher"] is None


def test_empty_accountless_ticket_is_rejected(support_stack):
    with pytest.raises(Exception):
        support_stack["support"].create_ticket(
            support_stack["customer"], 0, "   ", now=1794500040,
        )


def test_reply_marks_answered_then_close_freezes_the_ticket(support_stack):
    support = support_stack["support"]
    ticket = support.create_ticket(
        support_stack["customer"], 803, "قطعی", now=1794500050,
    )
    support.reply_ticket(
        support_stack["seller"], ticket["ticket_id"], "بررسی شد",
        visibility="customer", now=1794500060,
    )
    assert support.list_tickets(support_stack["seller"], status="answered")[0]["ticket_id"] == ticket["ticket_id"]
    assert support.count_tickets(support_stack["seller"], status="open") == 0

    closed = support.close_ticket(support_stack["seller"], ticket["ticket_id"], now=1794500070)
    assert closed["status"] == "closed"
    assert support.count_tickets(support_stack["seller"], status="closed") == 1

    # A later reply must not silently reopen a closed ticket.
    support.reply_ticket(
        support_stack["seller"], ticket["ticket_id"], "پیگیری",
        visibility="customer", now=1794500080,
    )
    with support_stack["database"].get_db() as conn:
        status = conn.execute(
            "SELECT status FROM support_tickets WHERE ticket_id=?", (ticket["ticket_id"],),
        ).fetchone()["status"]
    assert status == "closed"
    with pytest.raises(Exception):
        support.close_ticket(support_stack["seller"], ticket["ticket_id"], now=1794500090)


def test_escalation_hands_the_ticket_to_root(support_stack):
    support = support_stack["support"]
    ticket = support.create_ticket(
        support_stack["customer"], 803, "نیاز به بررسی مدیریت", now=1794500100,
    )
    result = support.escalate_ticket(support_stack["seller"], ticket["ticket_id"], now=1794500110)
    assert result == {"ticket_id": ticket["ticket_id"], "assigned_key": "root",
                      "status": "escalated"}
    assert support.count_tickets(support_stack["root"], status="open") == 1


def test_abuse_reports_never_appear_in_the_support_queue(support_stack):
    support, abuse = support_stack["support"], support_stack["abuse"]
    ticket = support.create_ticket(
        support_stack["customer"], 803, "تیکت عادی", now=1794500120,
    )
    report = abuse.create_abuse_report(
        support_stack["customer"], "گزارش محرمانه", now=1794500130,
    )
    # Root sees the abuse report only through the dedicated abuse surface.
    assert [item["ticket_id"] for item in support.list_tickets(support_stack["root"], status="all")] == [
        ticket["ticket_id"]
    ]
    assert support.count_tickets(support_stack["root"], status="all") == 1
    assert [item["ticket_id"] for item in abuse.list_abuse_reports(support_stack["root"])] == [
        report["ticket_id"]
    ]
    assert abuse.list_abuse_reports(support_stack["seller"]) == []
    assert support_stack["attempts"] == []
    assert not pathlib.Path(support_stack["sentinel"]).exists()


def test_ticket_queue_page_exposes_a_new_ticket_entry_and_status_filters():
    calls = []

    class FakeService:
        def list_tickets(self, ctx, *, status=None, statuses=None):
            calls.append(status)
            return [{"ticket_id": "abcdefghijklmnop", "status": "unseen"}]

    page = support_ui.tickets_page(
        {"actor_tg_id": 101, "bot_id": 9001, "persona": "user"},
        group="open", service=FakeService(), parent_callback="v52:home:user",
    )
    assert calls == ["open"]
    callbacks = _callbacks(page.reply_markup)
    assert "v52:newticket" in callbacks
    assert "v52:ticket:abcdefghijklmnop" in callbacks
    for group in ("open", "answered", "closed", "all"):
        assert f"v52:tickets:{group}" in callbacks
        assert _resolves(f"v52:tickets:{group}")
    assert _resolves("v52:newticket")
    assert _resolves("v52:close:abcdefghijklmnop")
    assert _resolves("v52:abuse:list")


def test_accountless_ticket_form_renders_and_submits():
    created = []

    class FakeService:
        def create_ticket(self, ctx, account_id, body, **kwargs):
            created.append((account_id, body, kwargs))
            return {"ticket_id": "ticket-0", "assigned_key": "root", "status": "unseen",
                    "snapshot": {"state": "not_applicable", "unavailable": False}}

    form = support_ui.begin_ticket({"actor_tg_id": 101, "bot_id": 9001, "persona": "user"}, 0,
                                   submit=False, service=FakeService())
    assert form.kind == "form"
    assert "بدون حساب" in form.text

    result = support_ui.begin_ticket({"actor_tg_id": 101, "bot_id": 9001, "persona": "user"}, 0,
                                     body="متن تیکت", submit=True, service=FakeService())
    assert result.kind == "result"
    assert created[0][0:2] == (0, "متن تیکت")


def test_close_action_is_reachable_through_the_ui_layer():
    closed = []

    class FakeService:
        def close_ticket(self, ctx, ticket_id, *, reason=None, now=None):
            closed.append(ticket_id)
            return {"ticket_id": ticket_id, "status": "closed", "assigned_key": "root"}

    page = support_ui.reply_or_escalate(
        {"actor_tg_id": 101, "bot_id": 9001, "persona": "user"}, "ticket-9",
        action="close", confirm=True, service=FakeService(),
    )
    assert page.kind == "result"
    assert closed == ["ticket-9"]


def test_abuse_page_is_private_and_separate_from_tickets():
    recorded = []

    class FakeAbuse:
        def create_abuse_report(self, ctx, body, **kwargs):
            recorded.append((ctx["actor_tg_id"], body))
            return {"ticket_id": "abuse-1", "status": "unseen"}

    private = support_ui.begin_abuse(
        {"actor_tg_id": 101, "bot_id": 9001, "chat_type": "private", "persona": "user"},
        "متن گزارش", service=FakeAbuse(),
    )
    assert private.kind == "result"
    assert "root" in private.text
    assert recorded == [(101, "متن گزارش")]

    group = support_ui.begin_abuse(
        {"actor_tg_id": 101, "bot_id": 9001, "chat_type": "group", "persona": "user"},
        "متن گزارش", service=FakeAbuse(),
    )
    assert group.kind == "redirect"
    assert recorded == [(101, "متن گزارش")]
