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
