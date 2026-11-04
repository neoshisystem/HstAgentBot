import asyncio
import json
from types import SimpleNamespace

import pytest

import admin_panel
import database


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "transfer.sqlite3"))
    database.init_db()


def add_rep(code, name, active=1):
    with database.get_db() as conn:
        cur = conn.execute(
            "INSERT INTO representatives (rep_code, name, scope_filter, is_active) VALUES (?, ?, 'all', ?)",
            (code, name, active),
        )
        return cur.lastrowid


def add_account(code="A100", rep_id=None, owner_tg_id=None):
    email = f"{code.lower()}@example.test"
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO accounts (rep_id, account_code, email, customer_name, sub_id, enable, total_gb, expiry_time, owner_tg_id) "
            "VALUES (?, ?, ?, 'Test Customer', 'sub-1', 1, 25, 0, ?)",
            (rep_id, code, email, owner_tg_id),
        )
    return email


def scalar(sql, params=()):
    with database.get_db() as conn:
        return conn.execute(sql, params).fetchone()[0]


class FakeQuery:
    def __init__(self, data):
        self.data = data
        self.answers = []
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeMessage:
    def __init__(self, text=""):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


def make_callback(data, user_id=9000):
    query = FakeQuery(data)
    update = SimpleNamespace(
        callback_query=query,
        message=FakeMessage(),
        effective_message=SimpleNamespace(message_thread_id=None),
        effective_user=SimpleNamespace(id=user_id, full_name="Super Admin", username="admin"),
        effective_chat=SimpleNamespace(id=123456, type="private"),
    )
    return update, query


def make_text(text, user_id=9000):
    message = FakeMessage(text)
    update = SimpleNamespace(
        callback_query=None,
        message=message,
        effective_message=SimpleNamespace(message_thread_id=None),
        effective_user=SimpleNamespace(id=user_id, full_name="Super Admin", username="admin"),
        effective_chat=SimpleNamespace(id=123456, type="private"),
    )
    return update, message


def callbacks(markup):
    return [button.callback_data for row in markup.inline_keyboard for button in row]


def authorize_admin(monkeypatch):
    monkeypatch.setattr(
        admin_panel.database, "get_user_role",
        lambda *args, **kwargs: ("super_admin", None),
    )


def sample_account():
    return {
        "id": 31,
        "account_code": "ABCD",
        "email": "abcd@example.test",
        "customer_name": "Wizard Customer",
        "rep_id": 10,
        "rep_name": "Current Rep",
        "owner_tg_id": None,
        "enable": 1,
        "total_gb": 25,
        "expiry_time": 0,
        "updated_at": "2026-11-04 12:00:00",
    }


def test_account_lookup_resolves_code_and_email_with_rep_name(isolated_db):
    rep_id = add_rep("source", "Source Representative")
    email = add_account("LOOK1", rep_id=rep_id)

    by_code = database.get_account_by_code_or_email("look1")
    by_email = database.get_account_by_code_or_email(email)

    assert by_code["account_code"] == "LOOK1"
    assert by_code["rep_name"] == "Source Representative"
    assert by_email["email"] == email
    assert database.get_account_by_code_or_email("missing") is None


def test_database_reassigns_to_rep_user_and_pool_with_audit(isolated_db):
    source_rep = add_rep("source", "Source Representative")
    target_rep = add_rep("target", "Target Representative")
    add_account("MOVE1", rep_id=source_rep)
    database.ensure_bot_user(7777, username="target-user", full_name="Target User")

    moved_to_rep = database.reassign_account(
        "MOVE1", new_rep_id=target_rep, actor_id=9000, actor_name="Admin"
    )
    assert moved_to_rep["status"] == "ok"
    assert database.get_account_by_code_or_email("MOVE1")["rep_id"] == target_rep

    moved_to_user = database.reassign_account(
        "MOVE1", new_owner_tg_id=7777, actor_id=9000, actor_name="Admin"
    )
    assert moved_to_user["status"] == "ok"
    account = database.get_account_by_code_or_email("MOVE1")
    assert account["rep_id"] is None
    assert account["owner_tg_id"] == 7777

    moved_to_pool = database.reassign_account(
        "MOVE1", to_pool=True, actor_id=9000, actor_name="Admin"
    )
    assert moved_to_pool["status"] == "ok"
    account = database.get_account_by_code_or_email("MOVE1")
    assert account["rep_id"] is None and account["owner_tg_id"] is None
    assert scalar(
        "SELECT COUNT(*) FROM audit_logs WHERE account_code='MOVE1' AND action='reassign_account' AND result_status='success'"
    ) == 3
    with database.get_db() as conn:
        audit = conn.execute(
            "SELECT details_json FROM audit_logs WHERE account_code='MOVE1' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    assert json.loads(audit["details_json"])["to_pool"] is True


def test_pool_destination_rejects_other_destination_arguments(isolated_db):
    add_account("BADPOOL")
    result = database.reassign_account("BADPOOL", new_rep_id=4, to_pool=True)
    assert result["status"] == "error"


def test_single_identifier_enters_step_two_and_shows_destination_options(monkeypatch):
    account = sample_account()
    monkeypatch.setattr(admin_panel.database, "get_account_by_code_or_email", lambda _value: account)
    update, message = make_text("ABCD")
    context = SimpleNamespace(user_data={
        "admin_state": {"action": "transfer", "ts": admin_panel.time.time(), "return_callback": "admin:scope"}
    })

    assert asyncio.run(admin_panel.process_admin_text_input(update, context)) is True
    assert context.user_data["admin_state"]["account_code"] == "ABCD"
    assert "گام ۲ از ۴" in message.replies[0][0]
    values = callbacks(message.replies[0][1]["reply_markup"])
    assert "admin:tr_select_rep:ABCD" in values
    assert "admin:tr_prompt_user:ABCD" in values
    assert "admin:tr_confirm:ABCD:pool:0" in values
    assert "admin:scope" in values


def test_rep_destination_picker_lists_active_representatives(monkeypatch):
    authorize_admin(monkeypatch)
    monkeypatch.setattr(admin_panel.database, "get_account_by_code_or_email", lambda _code: sample_account())
    monkeypatch.setattr(admin_panel.database, "get_all_active_reps", lambda: [
        {"id": 21, "name": "Target One", "rep_code": "one", "is_active": 1},
        {"id": 22, "name": "Target Two", "rep_code": "two", "is_active": 1},
    ])
    update, query = make_callback("admin:tr_select_rep:ABCD")

    asyncio.run(admin_panel.handle_admin_callbacks(update, SimpleNamespace(user_data={})))

    values = callbacks(query.edits[0][1]["reply_markup"])
    assert "admin:tr_confirm:ABCD:rep:21" in values
    assert "admin:tr_confirm:ABCD:rep:22" in values


@pytest.mark.parametrize(
    ("dest_type", "dest_id", "expected"),
    [
        ("rep", "21", {"new_rep_id": 21, "to_pool": False}),
        ("user", "7777", {"new_owner_tg_id": 7777, "to_pool": False}),
        ("pool", "0", {"to_pool": True}),
    ],
)
def test_confirm_and_execute_each_destination(monkeypatch, dest_type, dest_id, expected):
    authorize_admin(monkeypatch)
    account = sample_account()
    monkeypatch.setattr(admin_panel.database, "get_account_by_code_or_email", lambda _code: dict(account))
    monkeypatch.setattr(
        admin_panel.database, "get_rep_by_id",
        lambda _rep_id: {"id": 21, "name": "Destination Rep", "rep_code": "dest", "is_active": 1},
    )
    calls = []
    monkeypatch.setattr(
        admin_panel.database, "reassign_account",
        lambda code, **kwargs: calls.append((code, kwargs)) or {"status": "ok", "account_code": code},
    )
    context = SimpleNamespace(user_data={})
    confirm_update, confirm_query = make_callback(f"admin:tr_confirm:ABCD:{dest_type}:{dest_id}")
    asyncio.run(admin_panel.handle_admin_callbacks(confirm_update, context))

    execute_callback = f"admin:tr_execute:ABCD:{dest_type}:{dest_id}"
    assert execute_callback in callbacks(confirm_query.edits[0][1]["reply_markup"])
    assert "گام ۴ از ۴" in confirm_query.edits[0][0]

    execute_update, execute_query = make_callback(execute_callback)
    asyncio.run(admin_panel.handle_admin_callbacks(execute_update, context))

    assert calls and calls[0][0] == "ABCD"
    for key, value in expected.items():
        assert calls[0][1][key] == value
    assert calls[0][1]["actor_id"] == 9000
    assert "با موفقیت انجام شد" in execute_query.edits[0][0]
    assert "transfer_confirm" not in context.user_data


def test_legacy_one_line_transfer_remains_supported(monkeypatch):
    rep = {"id": 41, "name": "Legacy Rep", "rep_code": "legacy", "is_active": 1}
    monkeypatch.setattr(admin_panel.database, "get_rep_by_code", lambda code: rep if code == "legacy" else None)
    calls = []
    monkeypatch.setattr(
        admin_panel.database, "reassign_account",
        lambda code, **kwargs: calls.append((code, kwargs)) or {"status": "ok", "account_code": code},
    )
    update, message = make_text("ABCD legacy")
    context = SimpleNamespace(user_data={
        "admin_state": {"action": "transfer", "ts": admin_panel.time.time()}
    })

    assert asyncio.run(admin_panel.process_admin_text_input(update, context)) is True
    assert calls[0][0] == "ABCD"
    assert calls[0][1]["new_rep_id"] == 41
    assert calls[0][1]["actor_id"] == 9000
    assert "سازگاری با فرمت قدیمی" in message.replies[0][0]
    assert "admin_state" not in context.user_data


def test_user_target_input_creates_confirmation_callback(monkeypatch):
    account = sample_account()
    monkeypatch.setattr(admin_panel.database, "get_account_by_code_or_email", lambda _code: account)
    update, message = make_text("7777")
    context = SimpleNamespace(user_data={
        "admin_state": {
            "action": "transfer_user_target",
            "account_code": "ABCD",
            "ts": admin_panel.time.time(),
            "return_callback": "admin:tr_account:ABCD",
        }
    })

    assert asyncio.run(admin_panel.process_admin_text_input(update, context)) is True
    assert "admin_state" not in context.user_data
    assert "admin:tr_confirm:ABCD:user:7777" in callbacks(message.replies[0][1]["reply_markup"])


def test_confirmation_is_rejected_if_source_ownership_changed(monkeypatch):
    authorize_admin(monkeypatch)
    original = sample_account()
    changed = dict(original, rep_id=99, rep_name="Different Rep")
    lookups = [0]

    def lookup(_code):
        lookups[0] += 1
        return dict(original) if lookups[0] == 1 else dict(changed)

    monkeypatch.setattr(admin_panel.database, "get_account_by_code_or_email", lookup)
    context = SimpleNamespace(user_data={})
    confirm_update, _ = make_callback("admin:tr_confirm:ABCD:pool:0")
    asyncio.run(admin_panel.handle_admin_callbacks(confirm_update, context))

    execute_update, execute_query = make_callback("admin:tr_execute:ABCD:pool:0")
    asyncio.run(admin_panel.handle_admin_callbacks(execute_update, context))

    assert execute_query.answers and execute_query.answers[-1][1] is True
    assert "تغییر کرده" in execute_query.answers[-1][0]
