import time

import pytest

import database


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "renewal-confirmations.sqlite3"))
    database.init_db()
    with database.get_db() as conn:
        rep_id = conn.execute(
            """
            INSERT INTO representatives
                (rep_code, name, telegram_id, scope_filter, is_active, balance_toman, credit_limit_toman)
            VALUES ('renew-rep', 'Renew Representative', 85001, 'all', 1, 0, 5000000)
            """
        ).lastrowid
    database.add_or_update_plan(
        "renew_20gb_30d", "Renew 20G", 20, 30, 400_000, 600_000, is_quick=1
    )
    database.upsert_account_record(
        rep_id=rep_id,
        email="renew-client@example.test",
        account_code="RN-1001",
        customer_name="Renew Customer",
        sub_id="renew-sub",
        enable=1,
        total_gb=20,
        expiry_time=0,
    )
    yield rep_id


def create_rep_confirmation(rep_id, actor_id=85001):
    plan = database.get_plan("renew_20gb_30d")
    return database.create_account_renewal_confirmation(
        actor_id=actor_id,
        actor_role="rep",
        rep_id=rep_id,
        account_code="RN-1001",
        account_email="renew-client@example.test",
        plan_code=plan["plan_code"],
        plan_name=plan["name"],
        gb=plan["gb"],
        days=plan["days"],
        amount=plan["rep_price"],
    )


def test_quick_renew_confirmation_is_consumed_once(isolated_db):
    rep_id = isolated_db
    token = create_rep_confirmation(rep_id)
    stored = database.get_account_renewal_confirmation(token, 85001)
    assert stored["status"] == "pending"
    assert stored["account_email"] == "renew-client@example.test"

    first = database.consume_account_renewal_confirmation(
        token, 85001, "rep", rep_id
    )
    assert first["status"] == "consumed"
    assert first["intent"]["plan_code"] == "renew_20gb_30d"

    replay = database.consume_account_renewal_confirmation(
        token, 85001, "rep", rep_id
    )
    assert replay == {
        "status": "already_processed",
        "intent_status": "consumed",
    }


def test_quick_renew_confirmation_is_bound_to_actor_role_and_representative(isolated_db):
    rep_id = isolated_db
    token = create_rep_confirmation(rep_id)

    assert database.get_account_renewal_confirmation(token, 99999) is None
    assert database.consume_account_renewal_confirmation(
        token, 99999, "rep", rep_id
    ) == {"status": "not_found"}
    assert database.consume_account_renewal_confirmation(
        token, 85001, "super_admin", None
    ) == {"status": "wrong_role"}
    assert database.consume_account_renewal_confirmation(
        token, 85001, "rep", rep_id + 1
    ) == {"status": "wrong_representative"}

    stored = database.get_account_renewal_confirmation(token, 85001)
    assert stored["status"] == "pending"


def test_quick_renew_confirmation_is_cancelled_if_plan_changes(isolated_db):
    rep_id = isolated_db
    token = create_rep_confirmation(rep_id)
    database.update_plan_price("renew_20gb_30d", rep_price=450_000)

    result = database.consume_account_renewal_confirmation(
        token, 85001, "rep", rep_id
    )
    assert result == {"status": "plan_changed"}
    assert database.get_account_renewal_confirmation(token, 85001)["status"] == "cancelled"


def test_quick_renew_confirmation_expires_without_becoming_executable(isolated_db):
    rep_id = isolated_db
    token = create_rep_confirmation(rep_id)
    with database.get_db() as conn:
        conn.execute(
            "UPDATE account_renewal_confirmations SET expires_at=? WHERE intent_id=?",
            (time.time() - 1, token),
        )

    result = database.consume_account_renewal_confirmation(
        token, 85001, "rep", rep_id
    )
    assert result == {"status": "expired"}
    assert database.get_account_renewal_confirmation(token, 85001)["status"] == "expired"


def test_super_admin_quick_renew_confirmation_can_be_consumed_for_unassigned_account(isolated_db):
    plan = database.get_plan("renew_20gb_30d")
    database.upsert_account_record(
        rep_id=None,
        email="admin-owned@example.test",
        account_code="AD-2001",
        customer_name="Admin Account",
        sub_id="admin-sub",
        enable=1,
        total_gb=20,
        expiry_time=0,
    )
    token = database.create_account_renewal_confirmation(
        actor_id=9000,
        actor_role="super_admin",
        rep_id=None,
        account_code="AD-2001",
        account_email="admin-owned@example.test",
        plan_code=plan["plan_code"],
        plan_name=plan["name"],
        gb=plan["gb"],
        days=plan["days"],
        amount=plan["rep_price"],
    )

    result = database.consume_account_renewal_confirmation(
        token, 9000, "super_admin", None
    )
    assert result["status"] == "consumed"
    assert result["intent"]["rep_id"] is None
    assert database.consume_account_renewal_confirmation(
        token, 9000, "super_admin", None
    )["status"] == "already_processed"

def test_rep_confirmation_is_claimed_atomically_with_immediate_renewal_intent(isolated_db):
    rep_id = isolated_db
    token = create_rep_confirmation(rep_id)
    plan = database.get_plan("renew_20gb_30d")

    started = database.begin_rep_renewal_intent(
        rep_id=rep_id,
        actor_tg_id=85001,
        account_code="RN-1001",
        account_email="renew-client@example.test",
        plan_code=plan["plan_code"],
        plan_name=plan["name"],
        gb=plan["gb"],
        days=plan["days"],
        amount=plan["rep_price"],
        confirmation_id=token,
        actor_role="rep",
    )
    assert started["status"] == "started"
    confirmation = database.get_account_renewal_confirmation(token, 85001)
    assert confirmation["status"] == "processing"
    assert confirmation["operation_ref_type"] == "rep_renewal"
    assert confirmation["operation_ref_id"] == started["intent_id"]
    assert confirmation["operation_lock_key"] == started["lock_key"]

    with pytest.raises(ValueError, match="already processing"):
        database.begin_rep_renewal_intent(
            rep_id=rep_id,
            actor_tg_id=85001,
            account_code="RN-1001",
            account_email="renew-client@example.test",
            plan_code=plan["plan_code"],
            plan_name=plan["name"],
            gb=plan["gb"],
            days=plan["days"],
            amount=plan["rep_price"],
            confirmation_id=token,
            actor_role="rep",
        )
    assert database.get_rep_balance(rep_id) == 400_000

    database.complete_rep_renewal_intent(started["intent_id"], rep_id, 85001)
    assert database.get_account_renewal_confirmation(token, 85001)["status"] == "succeeded"


def test_rep_confirmation_claim_rolls_back_with_failed_precharge(isolated_db, monkeypatch):
    rep_id = isolated_db
    token = create_rep_confirmation(rep_id)
    plan = database.get_plan("renew_20gb_30d")
    original = database._record_rep_ledger_entry

    def write_then_fail(cur, *args, **kwargs):
        original(cur, *args, **kwargs)
        raise RuntimeError("injected ledger failure")

    monkeypatch.setattr(database, "_record_rep_ledger_entry", write_then_fail)
    with pytest.raises(RuntimeError, match="injected ledger failure"):
        database.begin_rep_renewal_intent(
            rep_id=rep_id,
            actor_tg_id=85001,
            account_code="RN-1001",
            account_email="renew-client@example.test",
            plan_code=plan["plan_code"],
            plan_name=plan["name"],
            gb=plan["gb"],
            days=plan["days"],
            amount=plan["rep_price"],
            confirmation_id=token,
            actor_role="rep",
        )

    assert database.get_account_renewal_confirmation(token, 85001)["status"] == "pending"
    assert database.get_rep_balance(rep_id) == 0
    assert database.get_operation_locks() == []
    with database.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rep_renewal_intents").fetchone()[0] == 0


def test_rep_confirmation_state_tracks_ambiguous_and_refunded_renewal(isolated_db):
    rep_id = isolated_db
    token = create_rep_confirmation(rep_id)
    plan = database.get_plan("renew_20gb_30d")
    started = database.begin_rep_renewal_intent(
        rep_id=rep_id, actor_tg_id=85001, account_code="RN-1001",
        account_email="renew-client@example.test", plan_code=plan["plan_code"],
        plan_name=plan["name"], gb=plan["gb"], days=plan["days"],
        amount=plan["rep_price"], confirmation_id=token, actor_role="rep",
    )
    database.mark_rep_renewal_for_reconciliation(
        started["intent_id"], rep_id, 85001, "X-UI timeout after request",
    )
    assert database.get_account_renewal_confirmation(token, 85001)["status"] == "needs_reconciliation"

    database.resolve_rep_renewal_intent(
        started["intent_id"], 9000, "Super Admin", "REFUND_NOT_APPLIED",
        "REFUND_NOT_APPLIED: verified no renewal on exact external account",
    )
    confirmation = database.get_account_renewal_confirmation(token, 85001)
    assert confirmation["status"] == "refunded"
    assert confirmation["operation_ref_id"] == started["intent_id"]


def test_rep_confirmation_is_bound_to_account_snapshot_at_atomic_claim(isolated_db):
    rep_id = isolated_db
    token = create_rep_confirmation(rep_id)
    plan = database.get_plan("renew_20gb_30d")
    with pytest.raises(ValueError, match="snapshot mismatch"):
        database.begin_rep_renewal_intent(
            rep_id=rep_id, actor_tg_id=85001, account_code="OTHER-ACCOUNT",
            account_email="renew-client@example.test", plan_code=plan["plan_code"],
            plan_name=plan["name"], gb=plan["gb"], days=plan["days"],
            amount=plan["rep_price"], confirmation_id=token, actor_role="rep",
        )
    assert database.get_account_renewal_confirmation(token, 85001)["status"] == "pending"
    assert database.get_rep_balance(rep_id) == 0

def create_admin_confirmation():
    plan = database.get_plan("renew_20gb_30d")
    database.upsert_account_record(
        rep_id=None,
        email="admin-renew@example.test",
        account_code="AD-2001",
        customer_name="Admin Account",
        sub_id="admin-renew-sub",
        enable=1,
        total_gb=20,
        expiry_time=0,
    )
    return database.create_account_renewal_confirmation(
        actor_id=9000,
        actor_role="super_admin",
        rep_id=None,
        account_code="AD-2001",
        account_email="admin-renew@example.test",
        plan_code=plan["plan_code"],
        plan_name=plan["name"],
        gb=plan["gb"],
        days=plan["days"],
        amount=plan["rep_price"],
    )


def test_super_admin_confirmation_is_atomically_claimed_with_persistent_lock(isolated_db):
    token = create_admin_confirmation()
    plan = database.get_plan("renew_20gb_30d")
    started = database.begin_admin_renewal_confirmation(
        confirmation_id=token,
        actor_id=9000,
        actor_role="super_admin",
        confirmation_rep_id=None,
        account_code="AD-2001",
        account_email="admin-renew@example.test",
        plan_code=plan["plan_code"],
        plan_name=plan["name"],
        gb=plan["gb"],
        days=plan["days"],
        amount=plan["rep_price"],
    )
    assert started["status"] == "started"
    confirmation = database.get_account_renewal_confirmation(token, 9000)
    assert confirmation["status"] == "processing"
    assert confirmation["operation_ref_type"] == "admin_renewal"
    assert confirmation["operation_ref_id"] == token
    assert confirmation["operation_lock_key"] == started["lock_key"]

    lock = next(item for item in database.get_operation_locks() if item["lock_key"] == started["lock_key"])
    assert lock["action_type"] == "admin_renewal"
    assert lock["intent_owner_type"] == "super_admin"
    assert lock["intent_id"] == token
    assert "token" not in lock

    with pytest.raises(ValueError, match="resolveadminrenewal"):
        database.resolve_operation_lock(
            started["lock_key"], 9000, "Super Admin",
            "RECONCILED: checked exact X-UI account",
        )

    database.complete_admin_renewal_confirmation(token, 9000, "Explicit X-UI success")
    assert database.get_account_renewal_confirmation(token, 9000)["status"] == "succeeded"
    assert database.get_operation_locks() == []


def test_super_admin_confirmation_ambiguous_resolution_has_no_financial_effect(isolated_db):
    rep_id = isolated_db
    token = create_admin_confirmation()
    plan = database.get_plan("renew_20gb_30d")
    started = database.begin_admin_renewal_confirmation(
        token, 9000, "super_admin", None, "AD-2001",
        "admin-renew@example.test", plan["plan_code"], plan["name"],
        plan["gb"], plan["days"], plan["rep_price"],
    )
    database.mark_admin_renewal_confirmation_for_reconciliation(
        token, 9000, "X-UI response timed out after request",
    )
    assert database.get_account_renewal_confirmation(token, 9000)["status"] == "needs_reconciliation"

    result = database.resolve_admin_renewal_confirmation(
        token, 9000, "Super Admin", "NOT_APPLIED",
        "NOT_APPLIED: inspected exact external account and renewal is absent",
    )
    assert result["status"] == "failed"
    assert database.get_account_renewal_confirmation(token, 9000)["status"] == "failed"
    assert database.get_operation_locks() == []
    assert database.get_rep_balance(rep_id) == 0


def test_super_admin_confirmation_claim_rolls_back_if_lock_creation_cannot_complete(isolated_db, monkeypatch):
    token = create_admin_confirmation()
    plan = database.get_plan("renew_20gb_30d")
    original = database._insert_audit

    def fail_audit(*args, **kwargs):
        raise RuntimeError("injected audit failure")

    monkeypatch.setattr(database, "_insert_audit", fail_audit)
    with pytest.raises(RuntimeError, match="injected audit failure"):
        database.begin_admin_renewal_confirmation(
            token, 9000, "super_admin", None, "AD-2001",
            "admin-renew@example.test", plan["plan_code"], plan["name"],
            plan["gb"], plan["days"], plan["rep_price"],
        )
    monkeypatch.setattr(database, "_insert_audit", original)

    confirmation = database.get_account_renewal_confirmation(token, 9000)
    assert confirmation["status"] == "pending"
    assert confirmation["operation_lock_key"] is None
    assert database.get_operation_locks() == []

