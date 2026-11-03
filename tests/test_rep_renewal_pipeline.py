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
    assert database.get_operation_locks() == []


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
    assert database.get_operation_locks() == []


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
    assert len(database.get_operation_locks()) == 1


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
    assert len(database.get_operation_locks()) == 1


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
    assert len(database.get_operation_locks()) == 1
