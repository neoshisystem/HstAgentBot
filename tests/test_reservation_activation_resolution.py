import pytest

import database


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "reservation-activation-resolution.sqlite3"))
    database.init_db()
    database.add_or_update_plan(
        "reserve_20gb_30d", "Reserve 20G", 20, 30, 300000, 450000, is_quick=1
    )
    with database.get_db() as conn:
        rep_id = conn.execute(
            """
            INSERT INTO representatives
                (rep_code, name, telegram_id, scope_filter, is_active, balance_toman, credit_limit_toman)
            VALUES ('reserve-rep', 'Reservation Representative', 89001, 'all', 1, 0, 1000000)
            """
        ).lastrowid
    yield rep_id


def make_reservation(rep_id, code="RSV-001"):
    return database.create_reservation(
        rep_id=rep_id,
        account_code=code,
        email=f"{code.lower()}@example.test",
        plan_code="reserve_20gb_30d",
        plan_name="Reserve 20G",
        gb=20,
        days=30,
        on_first_connect=0,
        charge_amount=300000,
        actor_id=89001,
        actor_name="Reservation Representative",
    )


def rep_balance(rep_id):
    with database.get_db() as conn:
        return conn.execute(
            "SELECT balance_toman FROM representatives WHERE id=?", (rep_id,)
        ).fetchone()[0]


def reservation_status(res_id):
    return database.get_reservation_by_id(res_id)["status"]


def activation_lock(res_id):
    return next(
        (item for item in database.get_operation_locks()
         if item["lock_key"] == f"reservation-activation:{res_id}"),
        None,
    )


def test_reservation_activation_applied_resolution_marks_activated_and_releases_lock(isolated_db):
    rep_id = isolated_db
    res_id = make_reservation(rep_id)
    key = f"reservation-activation:{res_id}"
    token = database.acquire_operation_lock(key, ttl_seconds=None)
    assert token
    assert rep_balance(rep_id) == 300000

    lock = activation_lock(res_id)
    assert lock["reservation_id"] == res_id
    assert lock["action_type"] == "reservation_activation"
    assert lock["intent_owner_type"] == "representative"
    assert lock["intent_status"] == "pending"
    assert lock["user_id"] == 89001
    assert "token" not in lock

    with pytest.raises(ValueError, match="resolvereservation"):
        database.resolve_operation_lock(
            key, 9000, "Super Admin", "RECONCILED: checked exact X-UI account"
        )

    result = database.resolve_reservation_activation(
        res_id, 9000, "Super Admin", "APPLIED",
        "APPLIED: exact X-UI account confirms new expiry and traffic reset",
    )
    assert result["status"] == "activated"
    assert reservation_status(res_id) == "activated"
    assert activation_lock(res_id) is None
    assert rep_balance(rep_id) == 300000


def test_reservation_not_applied_resolution_keeps_pending_and_allows_later_cancel_refund(isolated_db):
    rep_id = isolated_db
    res_id = make_reservation(rep_id)
    key = f"reservation-activation:{res_id}"
    assert database.acquire_operation_lock(key, ttl_seconds=None)

    result = database.resolve_reservation_activation(
        res_id, 9000, "Super Admin", "NOT_APPLIED",
        "NOT_APPLIED: inspected exact X-UI account and target renewal is absent",
    )
    assert result["status"] == "pending"
    assert reservation_status(res_id) == "pending"
    assert activation_lock(res_id) is None
    assert rep_balance(rep_id) == 300000

    cancelled = database.cancel_reservation(res_id, actor_id=9000, actor_name="Super Admin")
    assert cancelled is not None
    assert reservation_status(res_id) == "cancelled"
    assert rep_balance(rep_id) == 0


def test_cancellation_is_blocked_while_activation_lock_exists(isolated_db):
    rep_id = isolated_db
    res_id = make_reservation(rep_id)
    key = f"reservation-activation:{res_id}"
    assert database.acquire_operation_lock(key, ttl_seconds=None)

    assert database.cancel_reservation(res_id, actor_id=9000, actor_name="Super Admin") is None
    assert reservation_status(res_id) == "pending"
    assert rep_balance(rep_id) == 300000
    assert activation_lock(res_id) is not None


def test_existing_activated_reservation_can_only_clear_residual_lock_as_applied(isolated_db):
    rep_id = isolated_db
    res_id = make_reservation(rep_id)
    key = f"reservation-activation:{res_id}"
    assert database.acquire_operation_lock(key, ttl_seconds=None)
    assert database.activate_reservation(res_id, actor_id=9000, actor_name="Super Admin")

    with pytest.raises(ValueError, match="already marked activated"):
        database.resolve_reservation_activation(
            res_id, 9000, "Super Admin", "NOT_APPLIED",
            "NOT_APPLIED: contradictory evidence should not pass",
        )

    result = database.resolve_reservation_activation(
        res_id, 9000, "Super Admin", "APPLIED",
        "APPLIED: local activation already committed and external state verified",
    )
    assert result["status"] == "activated"
    assert reservation_status(res_id) == "activated"
    assert activation_lock(res_id) is None
    assert rep_balance(rep_id) == 300000

def create_reservation_confirmation(rep_id, code="RSV-CONF"):
    email = f"{code.lower()}@example.test"
    database.upsert_account_record(
        rep_id=rep_id,
        email=email,
        account_code=code,
        customer_name="Confirmed Renewal",
        sub_id=f"sub-{code.lower()}",
        enable=1,
        total_gb=20,
        expiry_time=0,
    )
    plan = database.get_plan("reserve_20gb_30d")
    return database.create_account_renewal_confirmation(
        actor_id=89001,
        actor_role="rep",
        rep_id=rep_id,
        account_code=code,
        account_email=email,
        plan_code=plan["plan_code"],
        plan_name=plan["name"],
        gb=plan["gb"],
        days=plan["days"],
        amount=plan["rep_price"],
    )


def test_rep_confirmation_is_claimed_atomically_with_reservation_and_debit(isolated_db):
    rep_id = isolated_db
    code = "RSV-CONF"
    token = create_reservation_confirmation(rep_id, code)
    email = f"{code.lower()}@example.test"

    res_id = database.create_reservation(
        rep_id=rep_id,
        account_code=code,
        email=email,
        plan_code="reserve_20gb_30d",
        plan_name="Reserve 20G",
        gb=20,
        days=30,
        on_first_connect=0,
        charge_amount=300000,
        actor_id=89001,
        actor_name="Reservation Representative",
        confirmation_id=token,
        actor_role="rep",
        confirmation_rep_id=rep_id,
    )

    confirmation = database.get_account_renewal_confirmation(token, 89001)
    assert confirmation["status"] == "reserved"
    assert confirmation["operation_ref_type"] == "reservation"
    assert confirmation["operation_ref_id"] == str(res_id)
    assert reservation_status(res_id) == "pending"
    assert rep_balance(rep_id) == 300000

    with pytest.raises(ValueError, match="already reserved"):
        database.create_reservation(
            rep_id=rep_id,
            account_code=code,
            email=email,
            plan_code="reserve_20gb_30d",
            plan_name="Reserve 20G",
            gb=20,
            days=30,
            on_first_connect=0,
            charge_amount=300000,
            actor_id=89001,
            actor_name="Reservation Representative",
            confirmation_id=token,
            actor_role="rep",
            confirmation_rep_id=rep_id,
        )
    assert rep_balance(rep_id) == 300000


def test_reservation_confirmation_claim_rolls_back_with_debit_failure(isolated_db, monkeypatch):
    rep_id = isolated_db
    code = "RSV-ROLLBACK"
    token = create_reservation_confirmation(rep_id, code)
    email = f"{code.lower()}@example.test"
    original = database._record_rep_ledger_entry

    def write_then_fail(cur, *args, **kwargs):
        original(cur, *args, **kwargs)
        raise RuntimeError("injected reservation debit failure")

    monkeypatch.setattr(database, "_record_rep_ledger_entry", write_then_fail)
    with pytest.raises(RuntimeError, match="injected reservation debit failure"):
        database.create_reservation(
            rep_id=rep_id,
            account_code=code,
            email=email,
            plan_code="reserve_20gb_30d",
            plan_name="Reserve 20G",
            gb=20,
            days=30,
            on_first_connect=0,
            charge_amount=300000,
            actor_id=89001,
            actor_name="Reservation Representative",
            confirmation_id=token,
            actor_role="rep",
            confirmation_rep_id=rep_id,
        )

    assert database.get_account_renewal_confirmation(token, 89001)["status"] == "pending"
    assert rep_balance(rep_id) == 0
    assert database.get_pending_reservation(code) is None

