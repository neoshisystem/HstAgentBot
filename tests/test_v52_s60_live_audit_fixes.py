"""S60 regression tests for the live-test audit findings.

One test per fixed defect, pinned to the exact behaviour that was wrong:

1. the settings centre no longer crashes on Markdown, and the internal bootstrap
   marker no longer leaks into «سایر پارامترها»;
2. the registration pages escape a Telegram name, and re-render through
   ``message_edit`` so a photo message or a repeat press is not a crash;
3. the bot lifecycle has its missing half — a disabled bot can be enabled again,
   reassigned, or deleted — and suspending a representative disables its bot.

Offline only: no Telegram, no network, no live database.
"""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

import admin_panel
import bot_admin_ui
import bot_registry_service
import config
import database
import registration_ui
import service_errors_fa

REPO_ROOT = Path(__file__).resolve().parent.parent
ROOT_ID = 777


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _db():
    """The migration-006 schema plus the tables the confirmation flow needs."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE representatives(id INTEGER PRIMARY KEY,is_active INTEGER,deleted_at INTEGER);
        CREATE TABLE bot_registry(
            bot_id INTEGER PRIMARY KEY,rep_id INTEGER REFERENCES representatives(id),
            is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN (0,1)),
            token_cipher TEXT,username TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL CHECK(status IN ('pending','active','disabled')),
            verified_at INTEGER,revision INTEGER NOT NULL DEFAULT 1
        );
        CREATE UNIQUE INDEX ux_primary_bot ON bot_registry(is_primary) WHERE is_primary=1;
        CREATE UNIQUE INDEX ux_active_rep_bot ON bot_registry(rep_id)
            WHERE rep_id IS NOT NULL AND status='active';
        CREATE TABLE action_confirmations(
            nonce TEXT PRIMARY KEY,actor_tg_id INTEGER NOT NULL,bot_id INTEGER NOT NULL,
            action TEXT NOT NULL,target_type TEXT NOT NULL,target_id TEXT NOT NULL,
            expected_revision INTEGER NOT NULL,payload_json TEXT NOT NULL,
            expires_at INTEGER NOT NULL,consumed_at INTEGER,operation_id TEXT
        );
        CREATE TABLE payment_orders(order_id TEXT PRIMARY KEY,bot_id INTEGER,status TEXT);
        CREATE TABLE bot_customers(
            bot_id INTEGER NOT NULL REFERENCES bot_registry(bot_id),
            user_tg_id INTEGER NOT NULL,started_at INTEGER NOT NULL,
            PRIMARY KEY(bot_id,user_tg_id));
    """)
    conn.execute("INSERT INTO representatives VALUES(202,1,NULL)")
    conn.execute("INSERT INTO representatives VALUES(203,1,NULL)")
    conn.commit()
    return conn


@pytest.fixture()
def conn(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", ROOT_ID, raising=False)
    connection = _db()
    yield connection
    connection.close()


def _seed(conn, bot_id, *, status, rep_id=None, is_primary=0, revision=1):
    conn.execute(
        "INSERT INTO bot_registry(bot_id,rep_id,is_primary,username,status,revision) "
        "VALUES(?,?,?,?,?,?)",
        (bot_id, rep_id, is_primary, f"bot_{bot_id}", status, revision),
    )
    conn.commit()


def _confirm(conn, prepared, actor=ROOT_ID):
    return prepared["confirmation"]["nonce"]


# --------------------------------------------------------------------------- #
# Task 1 — the settings centre
# --------------------------------------------------------------------------- #

def test_the_bootstrap_cutoff_marker_is_hidden_from_the_settings_centre(monkeypatch):
    """``bootstrap_v52`` writes a JSON marker whose ``_`` broke Markdown v1."""
    rows = [
        {"key": "v52.bootstrap.financial_cutoff",
         "value": '{"actor_id":1,"anchor_count":0,"policy_version":"v1.0"}',
         "label": "v5.2 financial opening cutoff", "description": "Immutable marker."},
        {"key": "rollover_max_gb", "value": "50", "label": "",
         "description": ""},
    ]
    monkeypatch.setattr(database, "get_all_settings", lambda: rows)

    assert admin_panel.is_hidden_setting("v52.bootstrap.financial_cutoff") is True
    assert admin_panel.is_hidden_setting("v52.bootstrap.anything_else") is True
    assert admin_panel.is_hidden_setting("ticket_code_seq") is True
    assert admin_panel.is_hidden_setting("rollover_max_gb") is False

    text, _rows = admin_panel.build_settings_category_page("other")
    assert "anchor_count" not in text
    assert "bootstrap" not in text


def test_a_settings_value_with_an_underscore_is_escaped_for_markdown(monkeypatch):
    """A raw ``_`` in any interpolated field made Telegram reject the whole page."""
    rows = [
        {"key": "some_other_key", "value": "value_with_underscores",
         "label": "برچسب_با_زیرخط", "description": "توضیح_با_زیرخط"},
    ]
    monkeypatch.setattr(database, "get_all_settings", lambda: rows)

    text, _rows = admin_panel.build_settings_category_page("other")
    # Outside a code span every underscore must be backslash-escaped, because
    # Markdown v1 reads a bare "_" as the start of an italic run.
    for index, part in enumerate(text.split("`")):
        if index % 2:
            continue  # inside a code span: not parsed, so the raw form is correct
        for position, char in enumerate(part):
            if char == "_":
                assert position and part[position - 1] == "\\", (part, position)
    # A value inside a code span keeps its raw form.
    assert "`value_with_underscores`" in text


def test_the_wizard_value_display_is_bare_for_the_html_pages():
    """The wizard renders HTML and escapes its own text, so it must not get backticks."""
    assert admin_panel.setting_value_display("x", "a_b") == "`a_b`"
    assert admin_panel.setting_value_display("x", "a_b", markdown=False) == "a_b"


def test_the_group_id_wizard_provisions_topics_after_commit():
    """Saving ``main_group_id`` must sync the report topics, as the legacy path did."""
    source = (REPO_ROOT / "admin_panel.py").read_text(encoding="utf-8")
    section = source[source.index("def _register_setting_wizard"):]
    # The function defines nested ``commit``/``after_commit`` helpers, so the section
    # ends at the next TOP-LEVEL def rather than at the next "def ".
    section = section[:section.index("\ndef ", 10)]
    assert "after_commit=after_commit" in section
    assert "_provision_topics_after_group_change" in section
    # The engine must actually await the hook it is given.
    engine = (REPO_ROOT / "ui_v52.py").read_text(encoding="utf-8")
    assert "after_commit" in engine
    assert "await _hook(update, context, _values, notice)" in engine


# --------------------------------------------------------------------------- #
# Task 1 — the registration surfaces
# --------------------------------------------------------------------------- #

def test_the_registration_pages_escape_a_telegram_name_with_an_underscore():
    """A Telegram username very often contains ``_``; these pages are Markdown v1."""
    pending = [{"telegram_id": 555, "full_name": "Ali_Reza", "username": "ali_reza",
                "registration_status": "pending"}]
    text, _markup = registration_ui.build_pending_registrations_page(pending)
    assert "Ali\\_Reza" in text and "ali\\_reza" in text

    card, _markup = registration_ui.build_approval_card(
        {"telegram_id": 555, "full_name": "Ali_Reza", "username": "ali_reza",
         "referred_by_code": "AB_12"})
    assert "Ali\\_Reza" in card

    referrals = [{"telegram_id": 555, "full_name": "Ali_Reza", "username": "ali_reza",
                  "registration_status": "approved", "created_at": "2026-11-18 10:00"}]
    listed, _markup = registration_ui.build_referral_list_page(referrals)
    assert "Ali\\_Reza" in listed

    # A code span keeps the raw value; only the backtick matters inside one.
    assert registration_ui._md_code("a`b") == "a\\`b"


def test_the_registration_admin_pages_edit_through_message_edit():
    """A raw ``edit_message_text`` raised on a photo message and on a repeat press."""
    source = (REPO_ROOT / "rep_bot.py").read_text(encoding="utf-8")
    section = source[source.index("async def _handle_registration_admin"):]
    section = section[:section.index("if action == \"reg_approve_all\"")]
    assert section.count("message_edit.render_page(") == 2
    assert "query.edit_message_text(" not in section


# --------------------------------------------------------------------------- #
# Task 2 — the bot lifecycle
# --------------------------------------------------------------------------- #

def test_a_disabled_bot_can_be_enabled_again(conn):
    """``disable_bot`` had no counterpart, so a disabled bot was bricked forever."""
    _seed(conn, 9001, status="disabled", rep_id=202)
    prepared = bot_registry_service.prepare_bot_enable(
        conn, actor_tg_id=ROOT_ID, bot_id=9001, now=int(time.time()))
    assert prepared["status"] == "disabled"

    result = bot_registry_service.enable_bot(
        conn, actor_tg_id=ROOT_ID, bot_id=9001, nonce=_confirm(conn, prepared),
        now=int(time.time()))
    assert result["status"] == "active" and result["replayed"] is False
    assert conn.execute("SELECT status FROM bot_registry WHERE bot_id=9001"
                        ).fetchone()["status"] == "active"


def test_only_a_disabled_bot_can_be_enabled(conn):
    _seed(conn, 9001, status="active", rep_id=202)
    with pytest.raises(bot_registry_service.BotRegistryConflict):
        bot_registry_service.prepare_bot_enable(
            conn, actor_tg_id=ROOT_ID, bot_id=9001, now=int(time.time()))


def test_enable_refuses_when_the_representative_slot_is_taken(conn):
    """``ux_active_rep_bot`` would reject it, so the refusal comes before the nonce."""
    _seed(conn, 9001, status="disabled", rep_id=202)
    _seed(conn, 9002, status="active", rep_id=202)
    with pytest.raises(bot_registry_service.BotRegistryConflict) as excinfo:
        bot_registry_service.prepare_bot_enable(
            conn, actor_tg_id=ROOT_ID, bot_id=9001, now=int(time.time()))
    assert "already has an active bot" in str(excinfo.value)


def test_a_violating_representatives_bot_can_be_confiscated_to_root(conn):
    _seed(conn, 9001, status="active", rep_id=202)
    prepared = bot_registry_service.prepare_bot_reassign(
        conn, actor_tg_id=ROOT_ID, bot_id=9001, new_rep_id=None, now=int(time.time()))
    assert prepared["new_rep_id"] is None

    result = bot_registry_service.reassign_bot(
        conn, actor_tg_id=ROOT_ID, bot_id=9001, new_rep_id=None,
        nonce=_confirm(conn, prepared), now=int(time.time()))
    assert result["rep_id"] is None
    row = conn.execute("SELECT rep_id,is_primary FROM bot_registry WHERE bot_id=9001"
                       ).fetchone()
    assert row["rep_id"] is None and row["is_primary"] == 0


def test_a_bot_can_be_transferred_to_another_representative(conn):
    _seed(conn, 9001, status="active", rep_id=202)
    prepared = bot_registry_service.prepare_bot_reassign(
        conn, actor_tg_id=ROOT_ID, bot_id=9001, new_rep_id=203, now=int(time.time()))
    bot_registry_service.reassign_bot(
        conn, actor_tg_id=ROOT_ID, bot_id=9001, new_rep_id=203,
        nonce=_confirm(conn, prepared), now=int(time.time()))
    assert conn.execute("SELECT rep_id FROM bot_registry WHERE bot_id=9001"
                        ).fetchone()["rep_id"] == 203


def test_the_primary_bot_cannot_be_reassigned_or_deleted(conn):
    _seed(conn, 9001, status="active", is_primary=1)
    with pytest.raises(bot_registry_service.BotRegistryConflict):
        bot_registry_service.prepare_bot_reassign(
            conn, actor_tg_id=ROOT_ID, bot_id=9001, new_rep_id=202, now=int(time.time()))
    with pytest.raises(bot_registry_service.BotRegistryConflict):
        bot_registry_service.delete_bot(
            conn, actor_tg_id=ROOT_ID, bot_id=9001, now=int(time.time()))


def test_delete_refuses_an_active_bot_and_a_bot_with_history(conn):
    _seed(conn, 9001, status="active", rep_id=202)
    with pytest.raises(bot_registry_service.BotRegistryConflict):
        bot_registry_service.delete_bot(
            conn, actor_tg_id=ROOT_ID, bot_id=9001, now=int(time.time()))

    # Disabled, but a customer row still points at it: a hard delete would orphan it.
    _seed(conn, 9002, status="disabled", rep_id=202)
    conn.execute("INSERT INTO bot_customers VALUES(9002, 4242, 1)")
    conn.commit()
    conn.execute("PRAGMA foreign_keys = ON")
    with pytest.raises(bot_registry_service.BotRegistryConflict) as excinfo:
        bot_registry_service.delete_bot(
            conn, actor_tg_id=ROOT_ID, bot_id=9002, now=int(time.time()))
    assert "operational history" in str(excinfo.value)


def test_a_clean_disabled_bot_is_deleted(conn):
    _seed(conn, 9001, status="disabled", rep_id=202)
    result = bot_registry_service.delete_bot(
        conn, actor_tg_id=ROOT_ID, bot_id=9001, now=int(time.time()))
    assert result["status"] == "deleted"
    assert conn.execute("SELECT 1 FROM bot_registry WHERE bot_id=9001").fetchone() is None


def test_only_root_may_manage_bots(conn):
    _seed(conn, 9001, status="disabled", rep_id=202)
    with pytest.raises(bot_registry_service.BotRegistryPermissionError):
        bot_registry_service.prepare_bot_enable(
            conn, actor_tg_id=4242, bot_id=9001, now=int(time.time()))


def test_the_kill_switch_disables_every_active_bot_of_a_representative(conn):
    """The statement ``database`` runs inside the suspension transaction."""
    _seed(conn, 9001, status="active", rep_id=202)
    _seed(conn, 9002, status="disabled", rep_id=202)
    _seed(conn, 9003, status="active", rep_id=203)

    cur = conn.cursor()
    disabled = database._disable_rep_bots(cur, 202)
    conn.commit()

    assert disabled == 1
    rows = {r["bot_id"]: r["status"] for r in
            conn.execute("SELECT bot_id,status FROM bot_registry").fetchall()}
    assert rows[9001] == "disabled"      # the active one was cut off
    assert rows[9002] == "disabled"      # untouched, already disabled
    assert rows[9003] == "active"        # another representative is unaffected
    # The revision bump is what makes the change visible to the CAS transitions.
    assert conn.execute("SELECT revision FROM bot_registry WHERE bot_id=9001"
                        ).fetchone()["revision"] == 2


def test_suspending_and_archiving_a_rep_both_run_the_kill_switch():
    """Both entry points must cut the bot off, not just one of them."""
    source = (REPO_ROOT / "database.py").read_text(encoding="utf-8")
    suspend = source[source.index("def set_rep_active_status("):]
    suspend = suspend[:suspend.index("def delete_rep(")]
    assert "_disable_rep_bots(cur, int(rep_id))" in suspend
    assert "if not value:" in suspend

    archive = source[source.index("def delete_rep(rep_id: int) -> bool:"):]
    archive = archive[:archive.index("def update_rep_full(")]
    assert "_disable_rep_bots(cur, int(rep_id))" in archive


def test_the_bot_detail_page_offers_the_actions_each_status_allows(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", ROOT_ID, raising=False)
    root = {"actor_tg_id": ROOT_ID}

    disabled = {"bot_id": 9001, "status": "disabled", "is_primary": 0,
                "username": "rep_bot", "rep_id": 202, "rep_name": "Rep", "revision": 3}
    page = bot_admin_ui.build_bot_detail_page(root, disabled)
    callbacks = [b.callback_data for row in page.reply_markup.inline_keyboard for b in row]
    assert "v52:bot:enable:9001" in callbacks
    assert "v52:bot:reassign:9001" in callbacks
    assert "v52:bot:delete:9001" in callbacks
    assert "🔴 غیرفعال" in page.text

    active = dict(disabled, status="active")
    page = bot_admin_ui.build_bot_detail_page(root, active)
    callbacks = [b.callback_data for row in page.reply_markup.inline_keyboard for b in row]
    assert "v52:bot:disable:9001" in callbacks
    # An active bot is not deletable, so the button must not be offered.
    assert "v52:bot:delete:9001" not in callbacks

    primary = dict(disabled, is_primary=1)
    page = bot_admin_ui.build_bot_detail_page(root, primary)
    callbacks = [b.callback_data for row in page.reply_markup.inline_keyboard for b in row]
    assert "v52:bot:reassign:9001" not in callbacks
    assert "v52:bot:delete:9001" not in callbacks


def test_the_bot_lifecycle_refusals_are_translated():
    """Every new refusal reaches the operator in Persian, not as the fallback."""
    for message in (
        "bot status is not recognised",
        "only a disabled bot can be enabled",
        "enable preview could not be created",
        "enable confirmation is invalid, stale or expired",
        "another primary bot is already registered",
        "this representative already has an active bot; disable or reassign it first",
        "the primary bot cannot be reassigned",
        "reassign preview could not be created",
        "reassign confirmation is invalid, stale or expired",
        "this representative already has an active bot; disable it first",
        "an active bot must be disabled before deletion",
        "the primary bot cannot be deleted",
        "this bot has operational history; disable it instead of deleting it",
    ):
        rendered = service_errors_fa.translate(message)
        assert rendered != service_errors_fa.DEFAULT_FALLBACK, message
        assert service_errors_fa.is_persian(rendered), message


# --------------------------------------------------------------------------- #
# Task 6 — the test environment
# --------------------------------------------------------------------------- #

def test_conftest_pins_the_env_flags_a_local_dotenv_would_leak():
    """``config.load_dotenv`` must not decide what the suite tests."""
    import os
    conftest = (REPO_ROOT / "conftest.py").read_text(encoding="utf-8")
    for flag in ("V52_MIGRATIONS_ENABLED", "V52_UI_ENABLED", "BACKUP_ENCRYPTION_KEY"):
        assert flag in conftest, flag
        assert os.environ.get(flag, "") == "", flag
    # ``config`` parses this one with int(), so it is pinned to "0" rather than "".
    assert os.environ.get("ADMIN_TELEGRAM_ID") == "0"


def test_the_legacy_reset_tests_pin_the_v52_gate_off():
    """``init_db`` seeds ``v52_ui_enabled='1'``, which intercepted the legacy path."""
    source = (REPO_ROOT / "tests/test_subscription_callback_routing.py").read_text(
        encoding="utf-8")
    assert 'monkeypatch.setattr(user_portal, "_v52_ui_enabled", lambda: False)' in source
    assert '"0" if key == "v52_ui_enabled"' in source
    # The one test that wants the gate on must still be able to turn it on.
    assert 'monkeypatch.setattr(user_portal, "_v52_ui_enabled", lambda: True)' in source


# --------------------------------------------------------------------------- #
# Task 2.2 — the wiring that was missing
# --------------------------------------------------------------------------- #

class _FakeDB:
    """Stand-in for ``database.get_db``: same context-manager contract, my connection."""

    def __init__(self, connection):
        self._conn = connection

    def __enter__(self):
        return self._conn

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self._conn.commit()
        return False


def _identity():
    return {"bot_id": 9001, "actor_tg_id": ROOT_ID, "chat_id": ROOT_ID}


def test_every_bot_management_route_resolves():
    """A registered callback that resolves to no handler dead-ends in the bridge error."""
    import v52_router
    expected = {
        "v52:bots": "list_bots",
        "v52:bot:view:9001": "bot_view",
        "v52:bot:enable:9001": "bot_enable",
        "v52:bot:disable:9001": "bot_disable",
        "v52:bot:reassign:9001": "bot_reassign_pick",
        "v52:bot:reassign:9001:root": "bot_reassign",
        "v52:bot:reassign:9001:202": "bot_reassign",
        "v52:bot:del:9001": "bot_delete",
    }
    for data, handler in expected.items():
        # ``Route.name`` is the second callback segment; ``Route.handler`` is the
        # dispatcher branch the bridge must find.
        route = v52_router.route_callback(data)
        assert route is not False, data
        assert route.handler == handler, (data, route.handler)
    # The new routes must not shadow the pre-existing rep-registration route.
    route = v52_router.route_callback("v52:bot:202")
    assert route.handler == "begin_register_bot" and route.params["rep_id"] == 202


def test_every_bot_route_name_has_a_dispatcher_branch():
    """The route table and the ``elif name == ...`` chain must agree."""
    source = (REPO_ROOT / "rep_bot.py").read_text(encoding="utf-8")
    for name in ("list_bots", "bot_view", "bot_enable", "bot_disable",
                 "bot_reassign_pick", "bot_reassign", "bot_delete"):
        assert f'name == "{name}"' in source or f'"{name}"' in source, name


def test_the_confirmation_consumer_actually_enables_a_disabled_bot(conn, monkeypatch):
    """The core Task 2.2 defect: the nonce was consumed but nothing ever happened."""
    import rep_bot
    monkeypatch.setattr(database, "get_db", lambda: _FakeDB(conn))
    _seed(conn, 9001, status="disabled", rep_id=202)
    prepared = bot_registry_service.prepare_bot_enable(
        conn, actor_tg_id=ROOT_ID, bot_id=9001, now=int(time.time()))
    nonce = prepared["confirmation"]["nonce"]
    conn.commit()

    page = rep_bot._v52_consume_confirmation({"actor_tg_id": ROOT_ID}, "admin", nonce)

    assert conn.execute("SELECT status FROM bot_registry WHERE bot_id=9001"
                        ).fetchone()["status"] == "active"
    assert "انجام شد" in page.text
    # The nonce is spent: a second press must not transition again.
    assert conn.execute("SELECT consumed_at FROM action_confirmations WHERE nonce=?",
                        (nonce,)).fetchone()["consumed_at"] is not None


def test_the_confirmation_consumer_reassigns_from_the_stored_payload(conn, monkeypatch):
    """``new_rep_id`` lives in ``payload_json``, so the consumer must read it."""
    import rep_bot
    monkeypatch.setattr(database, "get_db", lambda: _FakeDB(conn))
    _seed(conn, 9001, status="active", rep_id=202)
    prepared = bot_registry_service.prepare_bot_reassign(
        conn, actor_tg_id=ROOT_ID, bot_id=9001, new_rep_id=None, now=int(time.time()))
    nonce = prepared["confirmation"]["nonce"]
    conn.commit()

    rep_bot._v52_consume_confirmation({"actor_tg_id": ROOT_ID}, "admin", nonce)

    assert conn.execute("SELECT rep_id FROM bot_registry WHERE bot_id=9001"
                        ).fetchone()["rep_id"] is None


def test_the_confirmation_consumer_still_consumes_a_non_bot_action(conn, monkeypatch):
    """Actions with no transition of their own keep the previous behaviour."""
    import rep_bot
    monkeypatch.setattr(database, "get_db", lambda: _FakeDB(conn))
    from confirmation_service import create_confirmation
    # ``create_confirmation`` requires a caller-owned transaction.
    conn.execute("BEGIN IMMEDIATE")
    confirmation = create_confirmation(
        conn, actor_tg_id=ROOT_ID, bot_id=9001, action="account.rotate",
        target_type="accounts", target_id="5", expected_revision=1,
        payload={"account_id": 5}, now=int(time.time()), ttl_seconds=300)
    nonce = confirmation["nonce"]
    conn.commit()

    page = rep_bot._v52_consume_confirmation({"actor_tg_id": ROOT_ID}, "admin", nonce)

    assert "تأیید ثبت شد" in page.text
    assert conn.execute("SELECT consumed_at FROM action_confirmations WHERE nonce=?",
                        (nonce,)).fetchone()["consumed_at"] is not None


def test_the_token_form_arms_the_private_dm_state(conn, monkeypatch):
    """Without the armed state the token answer fell through to the text handlers."""
    import rep_bot
    import v52_router
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", ROOT_ID, raising=False)
    v52_router.DEFAULT_FSM.clear({"bot_id": 9001, "actor_tg_id": ROOT_ID,
                                  "chat_id": ROOT_ID})
    rep_bot._v52_arm_bot_token_state(
        {"bot_id": 9001, "actor_tg_id": ROOT_ID, "chat_id": ROOT_ID},
        rep_id=202, is_primary=False)

    entry = v52_router.DEFAULT_FSM.consume(
        {"bot_id": 9001, "actor_tg_id": ROOT_ID, "chat_id": ROOT_ID},
        event="text", now=int(time.time()))
    assert entry is not None and entry.state == "bot_register_token"
    assert entry.payload == {"rep_id": 202, "is_primary": False}


def test_the_dm_intake_handler_is_wired_to_the_service():
    """``begin_registration_from_text`` had no caller, so the flow could not finish."""
    source = (REPO_ROOT / "rep_bot.py").read_text(encoding="utf-8")
    assert 'route.name == "bot_register_token"' in source
    assert "begin_registration_from_text(" in source
    assert "verify_bot_registration(" in source
    # The token message is removed rather than left in the chat history.
    assert "await message.delete()" in source
    assert '"bot_register_token"' in (REPO_ROOT / "v52_router.py").read_text(
        encoding="utf-8")
