import asyncio
from types import SimpleNamespace

import pytest

import database
import nlp_matcher
import rep_bot


@pytest.fixture
def renewal_env(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "rep-renewal-pipeline.sqlite3"))
    database.init_db()
    with database.get_db() as conn:
        rep_id = conn.execute(
            """
            INSERT INTO representatives
                (rep_code, name, telegram_id, scope_filter, is_active, balance_toman, credit_limit_toman)
            VALUES ('pipeline-rep', 'Pipeline Representative', 88001, 'all', 1, 0, 1000000)
            """
        ).lastrowid
    database.add_or_update_plan(
        "pipeline_20gb_30d", "Pipeline 20G", 20, 30, 400000, 600000, is_quick=1
    )
    database.upsert_account_record(
        rep_id=rep_id,
        email="pipe-001@example.test",
        account_code="PIPE-001",
        customer_name="Pipeline Customer",
        sub_id="pipe-sub",
        enable=1,
        total_gb=20,
        expiry_time=0,
    )

    user = SimpleNamespace(id=88001, full_name="Pipeline Rep", username="pipeline")
    rep = {"id": rep_id, "name": "Pipeline Representative", "scope_filter": "all"}
    acc = {
        "rep_id": rep_id,
        "account_code": "PIPE-001",
        "email": "pipe-001@example.test",
    }
    client = {
        "remaining_gb": 0,
        "days_left": 0,
        "is_expired": True,
        "is_depleted": False,
        "enabled": True,
    }
    monkeypatch.setattr(
        rep_bot, "nlp_matcher",
        SimpleNamespace(build_account_keyboard=lambda *args, **kwargs: "keyboard"),
    )
    monkeypatch.setattr(rep_bot, "send_admin_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(rep_bot, "xui_manager", SimpleNamespace(renew_client=None))
    return rep_id, user, rep, acc, client


def renewal_locks():
    return [
        item for item in database.get_operation_locks()
        if item["lock_key"].startswith("account-renewal:")
    ]


def renewal_rows():
    with database.get_db() as conn:
        return [dict(row) for row in conn.execute(
            "SELECT * FROM rep_renewal_intents ORDER BY created_at"
        ).fetchall()]


def run_renewal(env):
    rep_id, user, rep, acc, client = env
    return asyncio.run(rep_bot.process_account_renewal(
        user=user,
        role="rep",
        rep=rep,
        acc_record=acc,
        client_info=client,
        gb=20,
        days=30,
        plan_code="pipeline_20gb_30d",
        effective_first=False,
        curr_comment="Pipeline Customer",
    ))


def invoke_renewal(env, plan_code, gb=20, days=30):
    _rep_id, user, rep, acc, client = env
    return asyncio.run(rep_bot.process_account_renewal(
        user=user,
        role="rep",
        rep=rep,
        acc_record=acc,
        client_info=client,
        gb=gb,
        days=days,
        plan_code=plan_code,
        effective_first=False,
        curr_comment="Pipeline Customer",
    ))


@pytest.mark.parametrize(
    ("plan_code", "gb", "days", "disable_plan"),
    [
        ("missing_99gb_90d", 99, 90, False),
        ("pipeline_20gb_30d", 20, 30, True),
        ("pipeline_20gb_30d", 50, 30, False),
        ("pipeline_20gb_30d", 20, 45, False),
    ],
)
def test_renewal_rejects_unapproved_inactive_or_mismatched_plan_before_any_mutation(
    renewal_env, monkeypatch, plan_code, gb, days, disable_plan
):
    rep_id = renewal_env[0]
    if disable_plan:
        with database.get_db() as conn:
            conn.execute(
                "UPDATE pricing_plans SET is_active=0 WHERE plan_code=?",
                (plan_code,),
            )

    def forbidden_xui(*_args, **_kwargs):
        raise AssertionError("X-UI must not be called for an invalid renewal plan")

    monkeypatch.setattr(rep_bot, "xui_manager", SimpleNamespace(renew_client=forbidden_xui))
    ok, message, _keyboard = invoke_renewal(renewal_env, plan_code, gb, days)

    assert ok is False
    assert "پلن" in message or "مطابقت" in message
    assert database.get_rep_balance(rep_id) == 0
    assert renewal_rows() == []
    assert renewal_locks() == []
    with database.get_db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM financial_ledger WHERE rep_id=?", (rep_id,)
        ).fetchone()[0] == 0


def test_rep_renewal_pipeline_finalizes_intent_on_explicit_xui_success(renewal_env, monkeypatch):
    monkeypatch.setattr(
        rep_bot, "xui_manager",
        SimpleNamespace(renew_client=lambda *args, **kwargs: {"status": "success", "report": "renewed"}),
    )
    ok, message, keyboard = run_renewal(renewal_env)
    rep_id = renewal_env[0]

    assert ok is True
    assert keyboard == "keyboard"
    assert "renewed" in message
    assert database.get_rep_balance(rep_id) == 400000
    assert len(renewal_rows()) == 1
    assert renewal_rows()[0]["status"] == "succeeded"
    assert renewal_locks() == []


def test_rep_renewal_pipeline_refunds_once_on_explicit_xui_failure(renewal_env, monkeypatch):
    monkeypatch.setattr(
        rep_bot, "xui_manager",
        SimpleNamespace(renew_client=lambda *args, **kwargs: {"status": "failed", "message": "rejected"}),
    )
    ok, message, _ = run_renewal(renewal_env)
    rep_id = renewal_env[0]

    assert ok is False
    assert "مسترد شد" in message
    assert database.get_rep_balance(rep_id) == 0
    assert len(renewal_rows()) == 1
    assert renewal_rows()[0]["status"] == "refunded"
    with database.get_db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM financial_ledger WHERE rep_id=?", (rep_id,)
        ).fetchone()[0] == 2
    assert renewal_locks() == []


@pytest.mark.parametrize(
    "response",
    [
        {"status": "pending", "message": "processing"},
        {"unexpected": "response"},
    ],
)
def test_rep_renewal_pipeline_retains_precharge_and_lock_for_ambiguous_response(
    renewal_env, monkeypatch, response
):
    monkeypatch.setattr(
        rep_bot, "xui_manager",
        SimpleNamespace(renew_client=lambda *args, **kwargs: response),
    )
    ok, message, _ = run_renewal(renewal_env)
    rep_id = renewal_env[0]

    assert ok is False
    assert "نامشخص" in message or "قطعی" in message
    assert database.get_rep_balance(rep_id) == 400000
    assert renewal_rows()[0]["status"] == "needs_reconciliation"
    assert len(renewal_locks()) == 1


def test_rep_renewal_pipeline_marks_exception_for_reconciliation(renewal_env, monkeypatch):
    def timeout(*args, **kwargs):
        raise TimeoutError("X-UI request timed out")

    monkeypatch.setattr(rep_bot, "xui_manager", SimpleNamespace(renew_client=timeout))
    ok, message, _ = run_renewal(renewal_env)
    rep_id = renewal_env[0]

    assert ok is False
    assert "نامشخص" in message
    assert database.get_rep_balance(rep_id) == 400000
    assert renewal_rows()[0]["status"] == "needs_reconciliation"
    assert len(renewal_locks()) == 1


def test_rep_renewal_pipeline_keeps_lock_if_local_success_finalization_fails(
    renewal_env, monkeypatch
):
    monkeypatch.setattr(
        rep_bot, "xui_manager",
        SimpleNamespace(renew_client=lambda *args, **kwargs: {"status": "success", "report": "renewed"}),
    )

    def fail_finalize(*args, **kwargs):
        raise RuntimeError("injected local finalization failure")

    monkeypatch.setattr(database, "complete_rep_renewal_intent", fail_finalize)
    ok, message, _ = run_renewal(renewal_env)
    rep_id = renewal_env[0]

    assert ok is False
    assert "ثبت نهایی محلی" in message
    assert database.get_rep_balance(rep_id) == 400000
    assert renewal_rows()[0]["status"] == "needs_reconciliation"
    assert len(renewal_locks()) == 1

def make_rep_confirmation(rep_id):
    plan = database.get_plan("pipeline_20gb_30d")
    return database.create_account_renewal_confirmation(
        actor_id=88001,
        actor_role="rep",
        rep_id=rep_id,
        account_code="PIPE-001",
        account_email="pipe-001@example.test",
        plan_code=plan["plan_code"],
        plan_name=plan["name"],
        gb=plan["gb"],
        days=plan["days"],
        amount=plan["rep_price"],
    )


def test_quick_confirmation_claims_rep_immediate_renewal_before_xui(renewal_env, monkeypatch):
    rep_id = renewal_env[0]
    token = make_rep_confirmation(rep_id)
    monkeypatch.setattr(
        rep_bot, "xui_manager",
        SimpleNamespace(renew_client=lambda *args, **kwargs: {"status": "success", "report": "renewed"}),
    )
    ok, _, _ = asyncio.run(rep_bot.process_account_renewal(
        user=renewal_env[1], role="rep", rep=renewal_env[2],
        acc_record=renewal_env[3], client_info=renewal_env[4],
        gb=20, days=30, plan_code="pipeline_20gb_30d",
        effective_first=False, curr_comment="Pipeline Customer",
        confirmation_id=token,
    ))
    confirmation = database.get_account_renewal_confirmation(token, 88001)
    assert ok is True
    assert confirmation["status"] == "succeeded"
    assert confirmation["operation_ref_type"] == "rep_renewal"
    assert database.get_rep_balance(rep_id) == 400000


def test_quick_confirmation_claims_rep_reservation_atomically(renewal_env, monkeypatch):
    rep_id, user, rep, acc, client = renewal_env
    token = make_rep_confirmation(rep_id)
    client.update({
        "remaining_gb": 10,
        "days_left": 10,
        "is_expired": False,
        "is_depleted": False,
        "enabled": True,
    })
    monkeypatch.setattr(
        rep_bot, "xui_manager",
        SimpleNamespace(renew_client=lambda *args, **kwargs: {"status": "success", "report": "not-called"}),
    )
    ok, message, _ = asyncio.run(rep_bot.process_account_renewal(
        user=user, role="rep", rep=rep, acc_record=acc, client_info=client,
        gb=20, days=30, plan_code="pipeline_20gb_30d",
        effective_first=False, curr_comment="Pipeline Customer",
        confirmation_id=token,
    ))
    confirmation = database.get_account_renewal_confirmation(token, 88001)
    assert ok is True
    assert "بسته رزرو" in message
    assert confirmation["status"] == "reserved"
    assert confirmation["operation_ref_type"] == "reservation"
    assert database.get_rep_balance(rep_id) == 400000


def test_super_admin_quick_confirmation_claims_direct_renewal_without_rep_debit(
    renewal_env, monkeypatch
):
    database.upsert_account_record(
        rep_id=None,
        email="admin-pipe@example.test",
        account_code="ADMIN-PIPE",
        customer_name="Admin Customer",
        sub_id="admin-pipe-sub",
        enable=1,
        total_gb=20,
        expiry_time=0,
    )
    plan = database.get_plan("pipeline_20gb_30d")
    token = database.create_account_renewal_confirmation(
        actor_id=9000, actor_role="super_admin", rep_id=None,
        account_code="ADMIN-PIPE", account_email="admin-pipe@example.test",
        plan_code=plan["plan_code"], plan_name=plan["name"],
        gb=plan["gb"], days=plan["days"], amount=plan["rep_price"],
    )
    admin_user = SimpleNamespace(id=9000, full_name="Super Admin", username="admin")
    admin_acc = {
        "rep_id": None, "account_code": "ADMIN-PIPE", "email": "admin-pipe@example.test",
    }
    admin_client = {
        "remaining_gb": 0, "days_left": 0, "is_expired": True,
        "is_depleted": False, "enabled": True,
    }
    monkeypatch.setattr(
        rep_bot, "xui_manager",
        SimpleNamespace(renew_client=lambda *args, **kwargs: {"status": "success", "report": "renewed"}),
    )
    ok, _, _ = asyncio.run(rep_bot.process_account_renewal(
        user=admin_user, role="super_admin", rep=None, acc_record=admin_acc,
        client_info=admin_client, gb=plan["gb"], days=plan["days"],
        plan_code=plan["plan_code"], effective_first=False,
        curr_comment="Admin Customer", confirmation_id=token,
    ))
    confirmation = database.get_account_renewal_confirmation(token, 9000)
    assert ok is True
    assert confirmation["status"] == "succeeded"
    assert confirmation["operation_ref_type"] == "admin_renewal"
    assert database.get_rep_balance(renewal_env[0]) == 0
    assert not any(item["lock_key"] == "account-renewal:admin-pipe" for item in database.get_operation_locks())

def make_admin_confirmation():
    database.upsert_account_record(
        rep_id=None,
        email="admin-pipe@example.test",
        account_code="ADMIN-PIPE",
        customer_name="Admin Customer",
        sub_id="admin-pipe-sub",
        enable=1,
        total_gb=20,
        expiry_time=0,
    )
    plan = database.get_plan("pipeline_20gb_30d")
    return database.create_account_renewal_confirmation(
        actor_id=9000, actor_role="super_admin", rep_id=None,
        account_code="ADMIN-PIPE", account_email="admin-pipe@example.test",
        plan_code=plan["plan_code"], plan_name=plan["name"],
        gb=plan["gb"], days=plan["days"], amount=plan["rep_price"],
    )


def run_admin_confirmation(env, token, monkeypatch, response):
    plan = database.get_plan("pipeline_20gb_30d")
    admin_user = SimpleNamespace(id=9000, full_name="Super Admin", username="admin")
    admin_acc = {
        "rep_id": None, "account_code": "ADMIN-PIPE", "email": "admin-pipe@example.test",
    }
    admin_client = {
        "remaining_gb": 0, "days_left": 0, "is_expired": True,
        "is_depleted": False, "enabled": True,
    }
    monkeypatch.setattr(
        rep_bot, "xui_manager",
        SimpleNamespace(renew_client=lambda *args, **kwargs: response),
    )
    return asyncio.run(rep_bot.process_account_renewal(
        user=admin_user, role="super_admin", rep=None, acc_record=admin_acc,
        client_info=admin_client, gb=plan["gb"], days=plan["days"],
        plan_code=plan["plan_code"], effective_first=False,
        curr_comment="Admin Customer", confirmation_id=token,
    ))


def test_super_admin_quick_confirmation_is_failed_without_financial_mutation_on_xui_rejection(
    renewal_env, monkeypatch
):
    rep_id = renewal_env[0]
    token = make_admin_confirmation()
    ok, message, _ = run_admin_confirmation(
        renewal_env, token, monkeypatch, {"status": "failed", "message": "rejected"}
    )
    assert ok is False
    assert "رد کرد" in message
    confirmation = database.get_account_renewal_confirmation(token, 9000)
    assert confirmation["status"] == "failed"
    assert database.get_rep_balance(rep_id) == 0
    assert not any(item["lock_key"] == "account-renewal:admin-pipe" for item in database.get_operation_locks())


def test_super_admin_quick_confirmation_ambiguous_result_retains_lock_for_resolution(
    renewal_env, monkeypatch
):
    rep_id = renewal_env[0]
    token = make_admin_confirmation()
    ok, message, _ = run_admin_confirmation(
        renewal_env, token, monkeypatch, {"status": "pending", "message": "unknown"}
    )
    assert ok is False
    assert "محفوظ مانده" in message or "مشخص نمی‌کند" in message
    confirmation = database.get_account_renewal_confirmation(token, 9000)
    assert confirmation["status"] == "needs_reconciliation"
    lock = next(item for item in database.get_operation_locks() if item["lock_key"] == "account-renewal:admin-pipe")
    assert lock["action_type"] == "admin_renewal"
    assert lock["intent_id"] == token
    with pytest.raises(ValueError, match="resolveadminrenewal"):
        database.resolve_operation_lock(
            lock["lock_key"], 9000, "Super Admin",
            "RECONCILED: verified exact account state",
        )
    result = database.resolve_admin_renewal_confirmation(
        token, 9000, "Super Admin", "NOT_APPLIED",
        "NOT_APPLIED: checked exact X-UI account and renewal is absent",
    )
    assert result["status"] == "failed"
    assert database.get_rep_balance(rep_id) == 0
    assert not any(item["lock_key"] == "account-renewal:admin-pipe" for item in database.get_operation_locks())



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

    rows = renewal_rows()
    locks = renewal_locks()
    assert ok is False
    assert "موفقیت یا شکست قطعی" in message
    assert rows[-1]["status"] == "needs_reconciliation"
    assert locks



def test_rep_renewal_pipeline_uses_per_account_rollover_policy(renewal_env, monkeypatch):
    database.set_account_rollover_mode("PIPE-001", False)
    observed = {}

    def renew_client(*args, **kwargs):
        observed.update(kwargs)
        return {"status": "success", "report": "renewed"}

    monkeypatch.setattr(rep_bot, "xui_manager", SimpleNamespace(renew_client=renew_client))
    ok, _message, _keyboard = run_renewal(renewal_env)

    assert ok is True
    assert observed["rollover"] is False
