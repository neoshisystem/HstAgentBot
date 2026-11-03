import sqlite3
from types import SimpleNamespace

import pytest

import database


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "test.sqlite3"))
    database.init_db()
    yield


def add_rep(rep_code="rep-a", name="Representative A", credit_limit=5_000_000, scope="all"):
    with database.get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO representatives
                (rep_code, name, scope_filter, balance_toman, credit_limit_toman, is_active)
            VALUES (?, ?, ?, 0, ?, 1)
            """,
            (rep_code, name, scope, credit_limit),
        )
        return cur.lastrowid


def add_account(code, email=None):
    email = email or f"{code}@example.test"
    with database.get_db() as conn:
        conn.execute(
            """
            INSERT INTO accounts
                (rep_id, account_code, email, customer_name, sub_id, enable, total_gb, expiry_time)
            VALUES (NULL, ?, ?, '', '', 1, 20, 0)
            """,
            (code, email),
        )
    return email


def scalar(sql, params=()):
    with database.get_db() as conn:
        return conn.execute(sql, params).fetchone()[0]


def test_database_connection_is_context_managed_and_rolls_back(isolated_db):
    with pytest.raises(sqlite3.OperationalError):
        with database.get_db() as conn:
            conn.execute("INSERT INTO table_that_does_not_exist VALUES (1)")

    with database.get_db() as conn:
        assert conn.execute("SELECT 1").fetchone()[0] == 1


def test_wallet_debit_is_atomic_and_rejects_invalid_amounts(isolated_db):
    database.record_user_payment(1001, 1_000, "test credit", actor_id=9000, actor_name="test")
    assert database.deduct_user_wallet(1001, 400, "test debit") == (True, 600)
    assert database.deduct_user_wallet(1001, 900, "overspend") == (False, 600)

    with pytest.raises(ValueError):
        database.deduct_user_wallet(1001, -50, "negative debit")
    with pytest.raises(ValueError):
        database.deduct_user_wallet(1001, 0, "zero debit")

    assert database.get_user_wallet_balance(1001) == 600
    assert scalar(
        "SELECT COUNT(*) FROM financial_ledger WHERE user_id = ?",
        (1001,),
    ) == 2


def test_receipt_approval_credits_wallet_exactly_once(isolated_db):
    code = database.create_payment_receipt(
        user_id=2002, role="end_user", amount_toman=75_000
    )
    receipt = database.get_payment_receipt(code)
    assert receipt is not None
    assert receipt["tracking_code"] == code
    assert receipt["status"] == "pending"

    approved = database.approve_payment_receipt(receipt["id"], 9999, "Admin")
    assert approved["status"] == "approved"
    assert database.get_user_wallet_balance(2002) == 75_000

    repeated = database.approve_payment_receipt(receipt["id"], 9999, "Admin")
    assert repeated["status"] == "approved"
    assert database.get_user_wallet_balance(2002) == 75_000
    assert scalar(
        "SELECT COUNT(*) FROM financial_ledger WHERE user_id = ?",
        (2002,),
    ) == 1


def test_representative_receipt_is_credited_once(isolated_db):
    rep_id = add_rep()
    code = database.create_payment_receipt(
        user_id=3003, role="rep", rep_id=rep_id, amount_toman=25_000
    )
    database.approve_payment_receipt(code, 9999, "Admin")

    assert database.get_rep_balance(rep_id) == -25_000
    database.approve_payment_receipt(code, 9999, "Admin")
    assert database.get_rep_balance(rep_id) == -25_000


def test_reservation_and_representative_debit_are_atomic(isolated_db):
    rep_id = add_rep(credit_limit=10_000)
    args = dict(
        rep_id=rep_id,
        account_code="A100",
        email="A100@example.test",
        plan_code="20gb_30d",
        plan_name="20 GB / 30 days",
        gb=20,
        days=30,
        on_first_connect=0,
        charge_amount=4_000,
        actor_id=9000,
        actor_name="test",
    )
    reservation_id = database.create_reservation(**args)
    assert database.get_rep_balance(rep_id) == 4_000

    with pytest.raises(ValueError):
        database.create_reservation(**args)
    assert database.get_rep_balance(rep_id) == 4_000
    assert scalar("SELECT COUNT(*) FROM reserved_packages WHERE status='pending'") == 1

    cancelled = database.cancel_reservation(reservation_id, actor_id=9000, actor_name="test")
    assert cancelled is not None
    assert cancelled[1] == 0
    assert database.get_rep_balance(rep_id) == 0
    assert database.cancel_reservation(reservation_id, actor_id=9000, actor_name="test") is None


def test_reservation_credit_limit_is_checked_inside_transaction(isolated_db):
    rep_id = add_rep(credit_limit=1_000)
    with pytest.raises(ValueError, match="credit limit"):
        database.create_reservation(
            rep_id=rep_id, account_code="A200", email="A200@example.test",
            plan_code="20gb_30d", plan_name="20 GB", gb=20, days=30,
            on_first_connect=0, charge_amount=1_001,
            actor_id=9000, actor_name="test",
        )
    assert database.get_rep_balance(rep_id) == 0
    assert scalar("SELECT COUNT(*) FROM reserved_packages") == 0


def test_reservation_activation_is_single_shot(isolated_db):
    reservation_id = database.create_reservation(
        rep_id=None, account_code="A300", email="A300@example.test",
        plan_code="20gb_30d", plan_name="20 GB", gb=20, days=30,
        on_first_connect=0, charge_amount=0, actor_id=9000, actor_name="test",
    )
    activated = database.activate_reservation(reservation_id, actor_id=9000, actor_name="test")
    assert activated and activated["status"] == "activated"
    assert database.activate_reservation(reservation_id, actor_id=9000, actor_name="test") is None


def test_account_linking_uses_exact_identifier_and_prevents_takeover(isolated_db):
    add_account("ABCD", "account-abcd@example.test")
    linked = database.link_user_to_account("ABCD", 4004, "first", "First User")
    assert linked["status"] == "ok"
    assert database.get_user_owned_account(4004, "ABCD") is not None
    assert database.get_user_owned_account(4004, "account-abcd@example.test") is not None

    takeover = database.link_user_to_account("ABCD", 5005, "second", "Second User")
    assert takeover["status"] == "error"
    assert database.get_user_owned_account(5005, "ABCD") is None
    assert database.unlink_account_owner("ABCD", 5005) is False
    assert database.unlink_account_owner("ABCD", 4004) is True
    assert database.get_user_owned_account(4004, "ABCD") is None


def test_manual_wallet_adjustment_cannot_create_negative_balance(isolated_db):
    database.record_user_payment(6006, 500, "initial", actor_id=9000, actor_name="test")
    with pytest.raises(ValueError, match="Insufficient"):
        database.update_user_wallet_manual(
            6006, -501, "overspend", actor_id=9000, actor_name="test"
        )
    assert database.get_user_wallet_balance(6006) == 500


def test_representative_sync_does_not_assign_ambiguous_accounts(isolated_db, monkeypatch):
    rep_a = add_rep("a", "Rep A", scope="all")
    rep_b = add_rep("b", "Rep B", scope="all")
    add_account("EXISTING", "existing@example.test")
    with database.get_db() as conn:
        conn.execute(
            "UPDATE accounts SET rep_id = ? WHERE email = ?",
            (rep_b, "existing@example.test"),
        )

    clients = [
        {"email": "existing@example.test", "comment": "", "subId": "sub-1", "enable": True,
         "totalGB": 20 * 1024**3, "expiryTime": 0},
        {"email": "new@example.test", "comment": "", "subId": "sub-2", "enable": True,
         "totalGB": 10 * 1024**3, "expiryTime": 0},
    ]

    class FakeResponse:
        ok = True
        status_code = 200

        def json(self):
            return {"obj": clients}

    class FakeSession:
        def get(self, url, timeout=None):
            return FakeResponse()

    monkeypatch.setattr(
        database, "xui_manager",
        SimpleNamespace(
            BASE_URL="https://panel.invalid",
            get_session=lambda: FakeSession(),
            extract_account_identifier=lambda email: email.split("@")[0],
        ),
    )

    result = database.sync_all_active_reps_detailed()
    assert result["status"] == "success"
    assert result["count"] == 1
    assert result["ambiguous"] == 1

    with database.get_db() as conn:
        existing_owner = conn.execute(
            "SELECT rep_id FROM accounts WHERE email = ?",
            ("existing@example.test",),
        ).fetchone()[0]
        new_account = conn.execute(
            "SELECT id FROM accounts WHERE email = ?",
            ("new@example.test",),
        ).fetchone()
    assert existing_owner == rep_b
    assert new_account is None
