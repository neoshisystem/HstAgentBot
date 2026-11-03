import asyncio
from types import SimpleNamespace

import pytest

import database
import rep_bot
import rep_panel
import user_portal
from xui_result import XUIResult, classify_xui_result


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"status": "success"}, XUIResult.SUCCESS),
        ({"status": "ok"}, XUIResult.SUCCESS),
        ({"success": True}, XUIResult.SUCCESS),
        ({"status": "failed"}, XUIResult.FAILURE),
        ({"status": "error"}, XUIResult.FAILURE),
        ({"success": False}, XUIResult.FAILURE),
        ({"status": "failed", "success": True}, XUIResult.UNKNOWN),
        ({"status": "success", "success": False}, XUIResult.UNKNOWN),
        ({"status": "ok", "success": False}, XUIResult.UNKNOWN),
        ({"status": "failure", "success": True}, XUIResult.UNKNOWN),
        ({"status": "pending"}, XUIResult.UNKNOWN),
        ({"success": "true"}, XUIResult.UNKNOWN),
        ({}, XUIResult.UNKNOWN),
        (None, XUIResult.UNKNOWN),
        ("success", XUIResult.UNKNOWN),
    ],
)
def test_xui_result_classifier_requires_consistent_explicit_signals(payload, expected):
    assert classify_xui_result(payload) == expected


class FakeQuery:
    def __init__(self, data=""):
        self.data = data
        self.answers = []
        self.edits = []
        self.message = FakeMessage()

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_reply_markup(self, **kwargs):
        self.edits.append(kwargs)

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeMessage:
    def __init__(self, text=None):
        self.text = text
        self.message_thread_id = None
        self.reply_to_message = None
        self.replies = []
        self.photos = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))

    async def reply_photo(self, photo=None, **kwargs):
        self.photos.append((photo, kwargs))


def callback_update(data, user_id=71001, chat_id=71001, role="end_user"):
    query = FakeQuery(data)
    user = SimpleNamespace(
        id=user_id, username="test-user", full_name="Test User", first_name="Test"
    )
    chat = SimpleNamespace(id=chat_id, type="private")
    update = SimpleNamespace(
        callback_query=query,
        effective_user=user,
        effective_chat=chat,
        effective_message=SimpleNamespace(message_thread_id=None),
        message=query.message,
    )
    return update, query


def test_user_purchase_conflicting_xui_signals_keep_wallet_and_lock_for_reconciliation(monkeypatch):
    update, query = callback_update("user:do_buy:buy-intent")
    monkeypatch.setattr(user_portal.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(user_portal.database, "get_user_role", lambda *a, **k: ("end_user", None))
    monkeypatch.setattr(
        user_portal.database, "begin_user_action_intent",
        lambda *a, **k: {
            "status": "started",
            "intent": {"amount": 250000, "gb": 20, "days": 30, "plan_name": "20G"},
            "lock_key": "user-intent:buy-intent",
            "lock_token": "lock-token",
            "new_balance": 750000,
        },
    )
    monkeypatch.setattr(
        user_portal, "xui_manager",
        SimpleNamespace(allocate_pool_client=lambda *a, **k: {"status": "failed", "success": True}),
    )
    reconciled = []
    refunds = []
    monkeypatch.setattr(
        user_portal.database, "mark_user_action_intent_for_reconciliation",
        lambda *a, **k: reconciled.append((a, k)) or True,
    )
    monkeypatch.setattr(
        user_portal.database, "refund_user_wallet_and_release_operation_lock",
        lambda *a, **k: refunds.append((a, k)),
    )
    monkeypatch.setattr(
        user_portal.database, "complete_user_action_intent",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("contradictory result must not finalize")),
    )

    asyncio.run(user_portal.handle_user_callbacks(update, SimpleNamespace(user_data={})))

    assert reconciled
    assert not refunds
    assert query.answers and "مبلغ محفوظ" in query.answers[-1][0]


def test_user_purchase_xui_success_with_local_upsert_failure_keeps_reconciliation_hold(monkeypatch):
    update, query = callback_update("user:do_buy:buy-intent")
    monkeypatch.setattr(user_portal.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(user_portal.database, "get_user_role", lambda *a, **k: ("end_user", None))
    monkeypatch.setattr(
        user_portal.database, "begin_user_action_intent",
        lambda *a, **k: {
            "status": "started",
            "intent": {"amount": 250000, "gb": 20, "days": 30, "plan_name": "20G"},
            "lock_key": "user-intent:buy-intent",
            "lock_token": "lock-token",
            "new_balance": 750000,
        },
    )
    monkeypatch.setattr(
        user_portal, "xui_manager",
        SimpleNamespace(
            allocate_pool_client=lambda *a, **k: {
                "status": "success", "email": "new@example.test",
                "sub_id": "new-sub", "data": {},
            },
            extract_account_identifier=lambda _email: "NEW-001",
        ),
    )
    monkeypatch.setattr(
        user_portal.database, "upsert_account_record",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("injected local persistence failure")),
    )
    reconciled = []
    refunds = []
    monkeypatch.setattr(
        user_portal.database, "mark_user_action_intent_for_reconciliation",
        lambda *a, **k: reconciled.append((a, k)) or True,
    )
    monkeypatch.setattr(
        user_portal.database, "refund_user_wallet_and_release_operation_lock",
        lambda *a, **k: refunds.append((a, k)),
    )

    asyncio.run(user_portal.handle_user_callbacks(update, SimpleNamespace(user_data={})))

    assert reconciled
    assert not refunds
    assert query.answers and "ثبت محلی کامل نشد" in query.answers[-1][0]


def test_representative_nlp_issuance_creates_durable_intent_before_financial_claim(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "nlp-rep-issue.sqlite3"))
    database.init_db()
    with database.get_db() as conn:
        rep_id = conn.execute(
            """
            INSERT INTO representatives
                (rep_code, name, telegram_id, scope_filter, is_active, balance_toman, credit_limit_toman)
            VALUES ('nlp-rep', 'NLP Rep', 81001, 'all', 1, 0, 3000000)
            """
        ).lastrowid
    database.add_or_update_plan("20gb_30d", "20G 30D", 20, 30, 300000, 500000, is_quick=0)

    message = FakeMessage(text="یک اکانت ۲۰ گیگ ۳۰ روزه بساز")
    user = SimpleNamespace(id=81001, username="rep", full_name="NLP Rep", first_name="Rep")
    chat = SimpleNamespace(id=81001, type="private")
    update = SimpleNamespace(
        message=message, effective_user=user, effective_chat=chat
    )
    context = SimpleNamespace(user_data={})
    rep = {"id": rep_id, "name": "NLP Rep", "scope_filter": "all"}
    monkeypatch.setattr(rep_bot.database, "get_user_role", lambda *a, **k: ("rep", rep))
    monkeypatch.setattr(rep_bot.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(
        rep_bot.nlp_matcher, "detect_intent",
        lambda _text: ("new_account", {"gb": 20, "days": 30, "comment": "Customer A"}),
    )

    asyncio.run(rep_bot.handle_message(update, context))

    with database.get_db() as conn:
        row = conn.execute("SELECT * FROM rep_action_intents").fetchone()
        debit_count = conn.execute(
            "SELECT COUNT(*) FROM financial_ledger WHERE rep_id = ? AND transaction_type='debit'",
            (rep_id,),
        ).fetchone()[0]
        lock_count = conn.execute("SELECT COUNT(*) FROM operation_locks").fetchone()[0]
    assert row is not None
    assert row["rep_id"] == rep_id
    assert row["actor_tg_id"] == 81001
    assert row["status"] == "pending"
    assert row["customer_name"] == "Customer A"
    assert debit_count == 0
    assert lock_count == 0
    assert message.replies
    keyboard = message.replies[0][1]["reply_markup"]
    assert keyboard.inline_keyboard[0][0].callback_data == f"rep:issue_acc_direct:{row['intent_id']}:noname"


def test_rep_renewal_conflicting_xui_signals_are_ambiguous_not_refunded(renewal_env, monkeypatch):
    rep_id, user, rep, acc, client = renewal_env
    monkeypatch.setattr(
        rep_bot, "xui_manager",
        SimpleNamespace(renew_client=lambda *a, **k: {"status": "failed", "success": True}),
    )

    ok, message, _keyboard = asyncio.run(rep_bot.process_account_renewal(
        user=user, role="rep", rep=rep, acc_record=acc, client_info=client,
        gb=20, days=30, plan_code="pipeline_20gb_30d",
        effective_first=False, curr_comment="Pipeline Customer",
    ))

    rows = database.get_rep_renewal_intents()
    locks = [x for x in database.get_operation_locks() if x["lock_key"].startswith("account-renewal:")]
    assert ok is False
    assert "مبهم" in message
    assert rows[-1]["status"] == "needs_reconciliation"
    assert locks
