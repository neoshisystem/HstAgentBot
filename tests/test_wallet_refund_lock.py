import pytest

import database


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "wallet-refund.sqlite3"))
    database.init_db()
    yield


def ledger_count(user_id):
    with database.get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM financial_ledger WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]


def prepare_debited_user(user_id, amount=500):
    database.record_user_payment(
        user_id, 2_000, "initial test funding", actor_id=9000, actor_name="test"
    )
    ok, balance = database.deduct_user_wallet(
        user_id, amount, "test purchase debit"
    )
    assert ok is True
    assert balance == 2_000 - amount


def test_refund_and_lock_resolution_are_atomic_and_token_replay_safe(isolated_db):
    user_id = 9101
    amount = 500
    prepare_debited_user(user_id, amount)
    lock_key = f"user-buy:{user_id}"
    token = database.acquire_operation_lock(lock_key, ttl_seconds=None)
    assert token

    refunded_balance = database.refund_user_wallet_and_release_operation_lock(
        user_id=user_id,
        amount=amount,
        description="refund after definite X-UI failure",
        actor_id=user_id,
        actor_name="SystemRefund",
        lock_key=lock_key,
        token=token,
    )

    assert refunded_balance == 2_000
    assert database.get_user_wallet_balance(user_id) == 2_000
    assert ledger_count(user_id) == 3
    assert all(item["lock_key"] != lock_key for item in database.get_operation_locks())

    with pytest.raises(ValueError, match="ownership mismatch"):
        database.refund_user_wallet_and_release_operation_lock(
            user_id=user_id,
            amount=amount,
            description="duplicate refund attempt",
            actor_id=user_id,
            actor_name="SystemRefund",
            lock_key=lock_key,
            token=token,
        )

    assert database.get_user_wallet_balance(user_id) == 2_000
    assert ledger_count(user_id) == 3


def test_refund_failure_rolls_back_credit_and_preserves_operation_lock(isolated_db, monkeypatch):
    user_id = 9102
    amount = 500
    prepare_debited_user(user_id, amount)
    lock_key = f"user-renewal:{user_id}:A9102"
    token = database.acquire_operation_lock(lock_key, ttl_seconds=None)
    assert token

    original_credit = database._credit_user_wallet

    def credit_then_fail(cur, *args, **kwargs):
        original_credit(cur, *args, **kwargs)
        raise RuntimeError("simulated failure after wallet and ledger writes")

    monkeypatch.setattr(database, "_credit_user_wallet", credit_then_fail)

    with pytest.raises(RuntimeError, match="simulated failure"):
        database.refund_user_wallet_and_release_operation_lock(
            user_id=user_id,
            amount=amount,
            description="refund after definite X-UI failure",
            actor_id=user_id,
            actor_name="RenewRefund",
            lock_key=lock_key,
            token=token,
        )

    # The context manager must roll back the staged credit/ledger insert and
    # leave the original persistent lock untouched.
    assert database.get_user_wallet_balance(user_id) == 2_000 - amount
    assert ledger_count(user_id) == 2
    locks = database.get_operation_locks()
    assert any(item["lock_key"] == lock_key for item in locks)


def test_refund_rejects_wrong_lock_token_without_credit_or_release(isolated_db):
    user_id = 9103
    amount = 500
    prepare_debited_user(user_id, amount)
    lock_key = f"user-buy:{user_id}"
    token = database.acquire_operation_lock(lock_key, ttl_seconds=None)
    assert token

    with pytest.raises(ValueError, match="ownership mismatch"):
        database.refund_user_wallet_and_release_operation_lock(
            user_id=user_id,
            amount=amount,
            description="unauthorized refund attempt",
            actor_id=user_id,
            actor_name="SystemRefund",
            lock_key=lock_key,
            token="wrong-token",
        )

    assert database.get_user_wallet_balance(user_id) == 2_000 - amount
    assert ledger_count(user_id) == 2
    assert any(item["lock_key"] == lock_key for item in database.get_operation_locks())
