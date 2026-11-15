"""S49 regression suite for the defects found in the live ``@HstAB_bot`` test.

BUG-FIN-007   the persona switcher stranded the wallet top-up at the amount step
BUG-UX-006    saving a panel credential answered with a dead-end message
ZERO-DEAD-END every interactive answer must carry navigation

Every assertion is offline: a temporary database, fake Telegram objects and
fake panel adapters. No socket is opened and no real panel is contacted.
"""
from __future__ import annotations

import ast
import asyncio
import base64
import pathlib
import time
from types import SimpleNamespace

import pytest

from migrations_runner import apply_migrations

import config
import database
import finance_manager
import panel_adapters.xui_openapi as xui_openapi
import rep_bot
import v52_router

PRODUCT_ROOT = pathlib.Path(__file__).resolve().parents[1]
# A fixed synthetic root: the CI runner has no .env, so config.ADMIN_TELEGRAM_ID
# is unset there and the suite must not depend on the deployment's value.
ROOT_TG_ID = 101258148
BOT_ID = 9001


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


class FakeBot:
    def __init__(self, bot_id=BOT_ID):
        self.id = bot_id
        self.sent = []
        self.deleted = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=1)

    async def delete_message(self, **kwargs):
        self.deleted.append(kwargs)
        return True


def make_update(*, text=None, photo=None, caption=None, data="", user_id=None):
    user_id = ROOT_TG_ID if user_id is None else int(user_id)
    query = FakeQuery(data)
    message = FakeMessage(text=text, photo=photo, caption=caption)
    query.message = message
    chat = SimpleNamespace(id=user_id, type="private")
    user = SimpleNamespace(id=user_id, full_name="Root", username="root", first_name="Root")
    update = SimpleNamespace(
        callback_query=query,
        message=message,
        effective_message=SimpleNamespace(message_thread_id=None),
        effective_user=user,
        effective_chat=chat,
    )
    return update, query, message


def make_context(user_data=None, bot=None):
    return SimpleNamespace(user_data=dict(user_data or {}), bot=bot or FakeBot(), args=[])


async def _noop(*_args, **_kwargs):
    return None


def _callbacks(markup):
    return [item.callback_data for row in markup.inline_keyboard for item in row]


@pytest.fixture()
def s49(tmp_path, monkeypatch):
    """Temporary product database with the v5.2 migrations applied."""
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    for module in (config, database, rep_bot):
        monkeypatch.setattr(module, "ADMIN_TELEGRAM_ID", ROOT_TG_ID)
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "s49.sqlite3"))
    database.init_db()
    with database.get_db() as conn:
        apply_migrations(conn, PRODUCT_ROOT / "migrations", now=1794600000)
        conn.execute(
            "INSERT INTO system_settings(key,value,label,description) "
            "VALUES('v52_persona_switcher_enabled','1','persona','persona switcher') "
            "ON CONFLICT(key) DO UPDATE SET value='1'"
        )
        conn.execute(
            "INSERT INTO bot_users(telegram_id,role,username,full_name,is_blocked) "
            "VALUES(?,'end_user','root','Root',0) ON CONFLICT(telegram_id) DO NOTHING",
            (ROOT_TG_ID,),
        )
        conn.execute(
            "INSERT INTO user_wallets(user_id,balance_toman) VALUES(?,0) "
            "ON CONFLICT(user_id) DO UPDATE SET balance_toman=0",
            (ROOT_TG_ID,),
        )
        conn.commit()
    # The v5.2 FSM is process-global; never let one test leak into the next.
    v52_router.DEFAULT_FSM.clear({"bot_id": BOT_ID, "actor_tg_id": ROOT_TG_ID, "chat_id": ROOT_TG_ID})
    yield
    v52_router.DEFAULT_FSM.clear({"bot_id": BOT_ID, "actor_tg_id": ROOT_TG_ID, "chat_id": ROOT_TG_ID})


def _set_persona(persona: str) -> None:
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO user_settings(user_id,active_persona) VALUES(?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET active_persona=excluded.active_persona",
            (ROOT_TG_ID, persona),
        )
        conn.commit()


def _seed_root_rep() -> int:
    """Create the root's personal representative row (the ``root_admin_rep``)."""
    import identity_service
    with database.get_db() as conn:
        identity_service.ensure_personal_identity(conn, ROOT_TG_ID)
        conn.commit()
        row = conn.execute(
            "SELECT id FROM representatives WHERE rep_code='root_admin_rep'"
        ).fetchone()
    return int(row["id"])


def _amount_prompt_state(role: str) -> dict:
    return {"fsm_state": finance_manager.STATE_AWAITING_RECEIPT_AMOUNT, "deposit_role": role}


# --------------------------------------------------------------------------- #
# BUG-FIN-007 — the wallet top-up must survive a persona switch
# --------------------------------------------------------------------------- #

def test_root_topup_amount_in_the_end_user_persona_creates_the_receipt(s49):
    _set_persona("end_user")
    update, _query, message = make_update(text="500000")
    context = make_context(_amount_prompt_state("end_user"))

    asyncio.run(rep_bot.handle_message(update, context))

    assert message.replies, "the amount step must answer"
    text, kwargs = message.replies[0]
    assert "کارت‌به‌کارت" in text
    assert kwargs.get("reply_markup") is not None
    assert "نقش" not in text, "the persona switch must not be refused"

    code = context.user_data["active_receipt_code"]
    receipt = database.get_payment_receipt(code)
    assert receipt["role"] == "end_user"
    assert int(receipt["user_id"]) == ROOT_TG_ID
    assert int(receipt["amount_toman"]) == 500_000
    assert context.user_data["fsm_state"] == finance_manager.STATE_AWAITING_RECEIPT_PHOTO


def test_root_topup_photo_in_the_end_user_persona_records_the_receipt(s49, monkeypatch):
    _set_persona("end_user")
    update, _query, _message = make_update(text="500000")
    context = make_context(_amount_prompt_state("end_user"))
    asyncio.run(rep_bot.handle_message(update, context))
    code = context.user_data["active_receipt_code"]

    monkeypatch.setattr(rep_bot, "process_receipt_notification_outbox", _noop)
    photo_update, _q, photo_message = make_update(
        photo=[SimpleNamespace(file_id="s49-receipt-photo")], caption="ref 42"
    )

    asyncio.run(rep_bot.handle_photo(photo_update, context))

    receipt = database.get_payment_receipt(code)
    assert receipt["receipt_photo_id"] == "s49-receipt-photo"
    assert receipt["status"] == "pending"
    assert context.user_data.get("fsm_state") is None
    assert photo_message.replies and photo_message.replies[0][1].get("reply_markup") is not None
    with database.get_db() as conn:
        queued = conn.execute(
            "SELECT COUNT(*) FROM receipt_notification_outbox WHERE receipt_id=?",
            (int(receipt["id"]),),
        ).fetchone()[0]
    assert queued >= 1, "the admin notification must be durably enqueued"


def test_root_topup_in_the_rep_persona_credits_the_rep_ledger_on_approval(s49):
    rep_id = _seed_root_rep()
    _set_persona("rep")
    update, _query, message = make_update(text="500000")
    context = make_context(_amount_prompt_state("rep"))

    asyncio.run(rep_bot.handle_message(update, context))

    assert message.replies and "نقش" not in message.replies[0][0]
    code = context.user_data["active_receipt_code"]
    receipt = database.get_payment_receipt(code)
    assert receipt["role"] == "rep"
    assert int(receipt["rep_id"]) == rep_id

    approved = database.approve_payment_receipt(code, 9090, "Root")
    assert approved["status"] == "approved"
    # ``balance_toman`` is the representative's outstanding debt, so an approved
    # deposit reduces it below zero.
    assert database.get_rep_balance(rep_id) == -500_000


def test_photo_step_follows_the_stored_role_after_a_persona_switch_back(s49, monkeypatch):
    """The root starts in the end-user workspace and answers from the admin one."""
    _set_persona("end_user")
    update, _query, _message = make_update(text="500000")
    context = make_context(_amount_prompt_state("end_user"))
    asyncio.run(rep_bot.handle_message(update, context))
    code = context.user_data["active_receipt_code"]

    _set_persona("admin")
    monkeypatch.setattr(rep_bot, "process_receipt_notification_outbox", _noop)
    photo_update, _q, photo_message = make_update(
        photo=[SimpleNamespace(file_id="s49-switched-photo")]
    )

    asyncio.run(rep_bot.handle_photo(photo_update, context))

    receipt = database.get_payment_receipt(code)
    assert receipt["receipt_photo_id"] == "s49-switched-photo"
    assert receipt["role"] == "end_user"
    assert "نقش فعلی" not in photo_message.replies[0][0]


def test_a_genuine_role_change_is_still_refused_at_the_photo_step(s49, monkeypatch):
    """An end-user must never complete a representative's stored top-up."""
    stranger_id = 55501
    context = make_context({
        "fsm_state": finance_manager.STATE_AWAITING_RECEIPT_PHOTO,
        "deposit_role": "rep",
        "active_receipt_code": "RCP-NOT-OURS",
        "deposit_amount": 500_000,
    })
    monkeypatch.setattr(rep_bot, "process_receipt_notification_outbox", _noop)
    photo_update, _q, photo_message = make_update(
        photo=[SimpleNamespace(file_id="s49-foreign-photo")], user_id=stranger_id
    )

    asyncio.run(rep_bot.handle_photo(photo_update, context))

    assert "نقش فعلی" in photo_message.replies[0][0]
    assert context.user_data["fsm_state"] == finance_manager.STATE_AWAITING_RECEIPT_PHOTO


# --------------------------------------------------------------------------- #
# BUG-UX-006 — the panel wizard must hand over the next step, not a dead end
# --------------------------------------------------------------------------- #

def _seed_panel(panel_type: str = "3x-ui", *, configured: bool = False) -> int:
    with database.get_db() as conn:
        cur = conn.execute(
            "INSERT INTO panels(panel_code,name,panel_type,lifecycle,is_default,created_at) "
            "VALUES('s49-panel','S49 panel',?,'maintenance',0,1794600000)",
            (panel_type,),
        )
        conn.commit()
        panel_id = int(cur.lastrowid)
    if configured:
        database.update_panel_connection(
            panel_id, "https://panel-s49.invalid:2053/", "S49SeededPanelToken"
        )
    return panel_id


def _start_panel_url_step(panel_id: int) -> None:
    v52_router.DEFAULT_FSM.set(
        {"bot_id": BOT_ID, "actor_tg_id": ROOT_TG_ID, "chat_id": ROOT_TG_ID},
        "panel_connection_url",
        payload={"panel_id": panel_id},
        parent_callback=f"v52:panel:{panel_id}",
        now=int(time.time()),
    )


def test_panel_credential_save_answers_with_the_next_step_buttons(s49):
    _set_persona("admin")
    panel_id = _seed_panel()
    _start_panel_url_step(panel_id)

    url_update, _q, url_message = make_update(text="https://panel-s49.invalid:2053/")
    url_context = make_context()
    assert asyncio.run(rep_bot._route_v52_fsm_text(url_update, url_context)) is True
    url_callbacks = _callbacks(url_message.replies[0][1]["reply_markup"])
    assert f"v52:panel:{panel_id}" in url_callbacks

    token_update, _q2, token_message = make_update(text="admin:S49-Secret-Pass")
    token_context = make_context()
    assert asyncio.run(rep_bot._route_v52_fsm_text(token_update, token_context)) is True

    text, kwargs = token_message.replies[0]
    assert "ذخیره شد" in text
    callbacks = _callbacks(kwargs["reply_markup"])
    assert f"v52:paneltest:{panel_id}" in callbacks
    assert f"v52:panelauto:{panel_id}" in callbacks
    assert f"v52:life:{panel_id}:active" in callbacks
    assert f"v52:panel:{panel_id}" in callbacks
    assert "v52:home:admin" in callbacks
    for callback in callbacks:
        assert isinstance(v52_router.route_callback(callback), v52_router.Route), callback

    with database.get_db() as conn:
        row = conn.execute(
            "SELECT auth_type, auth_cipher, raw_url FROM panels WHERE id=?", (panel_id,)
        ).fetchone()
    assert row["auth_type"] == "password"
    assert row["auth_cipher"] and "S49-Secret-Pass" not in str(row["auth_cipher"])
    assert row["raw_url"].startswith("https://panel-s49.invalid:2053")
    assert token_message.deleted is True, "the credential message must be removed"


def test_panel_credential_wizard_accepts_a_bare_api_token(s49):
    _set_persona("admin")
    panel_id = _seed_panel()
    _start_panel_url_step(panel_id)
    url_update, _q, _m = make_update(text="https://panel-s49.invalid:2053/")
    asyncio.run(rep_bot._route_v52_fsm_text(url_update, make_context()))

    token_update, _q2, token_message = make_update(text="S49BareApiTokenValue")
    asyncio.run(rep_bot._route_v52_fsm_text(token_update, make_context()))

    assert f"v52:paneltest:{panel_id}" in _callbacks(token_message.replies[0][1]["reply_markup"])
    with database.get_db() as conn:
        auth_type = conn.execute(
            "SELECT auth_type FROM panels WHERE id=?", (panel_id,)
        ).fetchone()["auth_type"]
    assert auth_type == "bearer_token"


class _ReachableAdapter:
    def __init__(self, *_args, **_kwargs):
        pass

    async def verify_connection(self):
        return SimpleNamespace(reachable=True)


class _UnreachableAdapter(_ReachableAdapter):
    async def verify_connection(self):
        return SimpleNamespace(reachable=False)


def test_successful_panel_test_offers_immediate_activation(s49, monkeypatch):
    panel_id = _seed_panel(configured=True)
    monkeypatch.setattr(xui_openapi, "XUIOpenAPIAdapter", _ReachableAdapter)
    update, query, _message = make_update(data=f"v52:paneltest:{panel_id}")

    asyncio.run(rep_bot._dispatch_v52_callback(update, make_context(), role="super_admin"))

    assert query.edits, "the panel card must be re-rendered"
    callbacks = _callbacks(query.edits[0][1]["reply_markup"])
    assert f"v52:life:{panel_id}:active" in callbacks
    assert f"v52:panel:{panel_id}" in callbacks
    assert "v52:home:admin" in callbacks


def test_failed_panel_test_offers_a_retry_instead_of_a_dead_end(s49, monkeypatch):
    panel_id = _seed_panel(configured=True)
    monkeypatch.setattr(xui_openapi, "XUIOpenAPIAdapter", _UnreachableAdapter)
    update, query, _message = make_update(data=f"v52:paneltest:{panel_id}")

    asyncio.run(rep_bot._dispatch_v52_callback(update, make_context(), role="super_admin"))

    callbacks = _callbacks(query.edits[0][1]["reply_markup"])
    assert f"v52:paneltest:{panel_id}" in callbacks
    assert f"v52:panelconfig:{panel_id}" in callbacks
    with database.get_db() as conn:
        lifecycle = conn.execute(
            "SELECT lifecycle FROM panels WHERE id=?", (panel_id,)
        ).fetchone()["lifecycle"]
    assert lifecycle == "maintenance", "a failed probe must not activate the panel"


def test_one_click_test_and_activate_promotes_the_panel(s49, monkeypatch):
    panel_id = _seed_panel(configured=True)
    monkeypatch.setattr(xui_openapi, "XUIOpenAPIAdapter", _ReachableAdapter)
    update, query, _message = make_update(data=f"v52:panelauto:{panel_id}")

    asyncio.run(rep_bot._dispatch_v52_callback(update, make_context(), role="super_admin"))

    with database.get_db() as conn:
        lifecycle = conn.execute(
            "SELECT lifecycle FROM panels WHERE id=?", (panel_id,)
        ).fetchone()["lifecycle"]
    assert lifecycle == "active"
    callbacks = _callbacks(query.edits[0][1]["reply_markup"])
    assert f"v52:panel:{panel_id}" in callbacks


# --------------------------------------------------------------------------- #
# Zero dead-end messages — structural guard over the interactive entry points
# --------------------------------------------------------------------------- #

# A blocked account is deliberately answered without navigation: every button it
# could press would be refused again by the same guard.
_DEAD_END_ALLOWLIST = {
    "⛔️ دسترسی شما به این بات مسدود شده است.",
    "⛔️ دسترسی شما به این بات توسط مدیریت مسدود شده است.",
    "⛔️ دسترسی حساب شما به این بات توسط مدیریت مسدود شده است.",
}

_INTERACTIVE_HANDLERS = (
    "handle_message",
    "handle_photo",
    "handle_callback",
    "_route_v52_fsm_text",
    "_route_v52_fsm_photo",
    "_submit_v52_order_receipt",
    "_load_v52_receipt_page",
    "start_cmd",
    "wallet_cmd",
    "deposit_cmd",
)

_REPLY_METHODS = frozenset({"reply_text", "reply_photo", "send_message", "send_photo"})


def _dead_end_calls(source: str) -> list[tuple[str, int]]:
    """Return ``(handler, lineno)`` for every reply that carries no navigation."""
    tree = ast.parse(source)
    offenders: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        if node.name not in _INTERACTIVE_HANDLERS:
            continue
        for call in ast.walk(node):
            if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
                continue
            if call.func.attr not in _REPLY_METHODS:
                continue
            if any(kw.arg == "reply_markup" for kw in call.keywords):
                continue
            first = call.args[0] if call.args else None
            if isinstance(first, ast.Constant) and first.value in _DEAD_END_ALLOWLIST:
                continue
            offenders.append((node.name, call.lineno))
    return offenders


def test_no_interactive_reply_in_rep_bot_is_sent_without_navigation():
    source = (PRODUCT_ROOT / "rep_bot.py").read_text(encoding="utf-8")
    assert _dead_end_calls(source) == []


def test_the_dead_end_guard_actually_detects_a_bare_reply():
    """Prove the guard is not vacuously green."""
    sample = (
        "async def handle_message(update, context):\n"
        "    await update.message.reply_text('bare')\n"
    )
    assert _dead_end_calls(sample) == [("handle_message", 2)]


def test_actor_navigation_keyboard_is_role_scoped_and_never_empty():
    for role, expected in (
        ("super_admin", "admin:main"),
        ("rep", "rep:main"),
        ("end_user", "user:main"),
    ):
        callbacks = _callbacks(rep_bot.build_actor_navigation_keyboard(role))
        assert callbacks, role
        assert expected in callbacks, role


def test_deposit_command_outside_a_payable_persona_is_not_silent(s49):
    _set_persona("admin")
    update, _query, message = make_update(text="/deposit 500000")
    context = make_context()
    asyncio.run(rep_bot.deposit_cmd(update, context))

    assert message.replies, "the refusal must be visible"
    text, kwargs = message.replies[0]
    assert "شارژ کیف پول" in text
    assert kwargs.get("reply_markup") is not None
    assert context.user_data == {}
