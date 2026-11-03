import pytest

import database


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "reservation-activation-resolution.sqlite3"))
    database.init_db()
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
