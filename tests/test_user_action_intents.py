import pytest

import database


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "user-action-intents.sqlite3"))
    database.init_db()
    database.add_or_update_plan(
        "20gb_30d", "20 GB / 30 days", 20, 30, 300_000, 500_000, is_quick=0
    )
    yield


def fund_user(user_id, amount=5_000_000):
    database.record_user_payment(
        user_id, amount, "test funding", actor_id=9000, actor_name="test"
    )


def make_intent(user_id, action="buy", account_code="", account_email=""):
    plan = database.get_plan("20gb_30d")
    return database.create_user_action_intent(
        user_id=user_id,
        action_type=action,
        plan_code=plan["plan_code"],
        plan_name=plan["name"],
        gb=plan["gb"],
        days=plan["days"],
        amount=plan["user_price"],
        account_code=account_code,
        account_email=account_email,
    )


def prepare_owned_account(user_id, code="ACC-123"):
    email = f"{code.lower()}@example.test"
    database.upsert_account_record(
        rep_id=None,
        email=email,
        account_code=code,
        customer_name="Intent Test",
        owner_tg_id=user_id,
    )
    return code, email


def ledger_count(user_id):
    with database.get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM financial_ledger WHERE user_id = ?",
            (user_id,),
        ).fetchone()[0]


def has_lock(lock_key):
    return any(item["lock_key"] == lock_key for item in database.get_operation_locks())


def test_consumed_buy_token_cannot_debit_twice_but_new_intent_can(isolated_db):
    user_id = 7001
    fund_user(user_id)

    first = make_intent(user_id)
    started = database.begin_user_action_intent(first, user_id, "buy")
    assert started["status"] == "started"
    assert started["new_balance"] == 4_500_000
    assert database.get_user_wallet_balance(user_id) == 4_500_000
    assert has_lock(f"user-buy:{user_id}")

    duplicate_while_processing = database.begin_user_action_intent(first, user_id, "buy")
    assert duplicate_while_processing == {
        "status": "already_processed",
        "intent_status": "processing",
    }
    assert database.get_user_wallet_balance(user_id) == 4_500_000
    assert ledger_count(user_id) == 2

    assert database.complete_user_action_intent(
        first, user_id, started["lock_key"], started["lock_token"], "account=NEW-1"
    )
    duplicate_after_success = database.begin_user_action_intent(first, user_id, "buy")
    assert duplicate_after_success == {
        "status": "already_processed",
        "intent_status": "succeeded",
    }
    assert not has_lock(f"user-buy:{user_id}")
    assert database.get_user_wallet_balance(user_id) == 4_500_000

    # Reopening the menu creates a different intent, so a deliberate second
    # purchase of the same plan remains possible.
    second = make_intent(user_id)
    assert second != first
    second_started = database.begin_user_action_intent(second, user_id, "buy")
    assert second_started["status"] == "started"
    assert database.get_user_wallet_balance(user_id) == 4_000_000
    assert ledger_count(user_id) == 3
    assert database.complete_user_action_intent(
        second, user_id, second_started["lock_key"], second_started["lock_token"]
    )


def test_renewal_intent_is_bound_to_owner_and_account_lock(isolated_db):
    user_id = 7002
    other_user = 7003
    fund_user(user_id)
    fund_user(other_user)
    code, email = prepare_owned_account(user_id)

    first = make_intent(user_id, "renew", code, email)
    foreign = database.get_user_action_intent(first, other_user)
    assert foreign is None
    assert database.begin_user_action_intent(first, other_user, "renew") == {
        "status": "not_found"
    }

    second = make_intent(user_id, "renew", code, email)
    started = database.begin_user_action_intent(first, user_id, "renew")
    assert started["status"] == "started"
    assert started["lock_key"] == f"account-renewal:{code.lower()}"
    assert database.get_user_wallet_balance(user_id) == 4_500_000

    competing = database.begin_user_action_intent(second, user_id, "renew")
    assert competing["status"] == "busy"
    assert database.get_user_action_intent(second, user_id)["status"] == "cancelled"
    assert database.get_user_wallet_balance(user_id) == 4_500_000

    assert database.complete_user_action_intent(
        first, user_id, started["lock_key"], started["lock_token"]
    )

    # A fresh menu after completion may intentionally renew the same account.
    third = make_intent(user_id, "renew", code, email)
    next_started = database.begin_user_action_intent(third, user_id, "renew")
    assert next_started["status"] == "started"
    assert database.get_user_wallet_balance(user_id) == 4_000_000
    assert database.complete_user_action_intent(
        third, user_id, next_started["lock_key"], next_started["lock_token"]
    )


def test_insufficient_funds_keeps_pending_intent_for_retry_after_top_up(isolated_db):
    user_id = 7004
    fund_user(user_id, 100)
    intent = make_intent(user_id)

    result = database.begin_user_action_intent(intent, user_id, "buy")
    assert result == {"status": "insufficient_funds", "balance": 100, "required": 500_000}
    assert database.get_user_action_intent(intent, user_id)["status"] == "pending"
    assert not has_lock(f"user-buy:{user_id}")
    assert database.get_user_wallet_balance(user_id) == 100

    database.record_user_payment(
        user_id, 1_000_000, "top up", actor_id=user_id, actor_name="test"
    )
    started = database.begin_user_action_intent(intent, user_id, "buy")
    assert started["status"] == "started"
    assert started["new_balance"] == 500_100
    assert database.complete_user_action_intent(
        intent, user_id, started["lock_key"], started["lock_token"]
    )


def test_intent_is_invalidated_when_plan_changes_after_menu_creation(isolated_db):
    user_id = 7005
    fund_user(user_id)
    intent = make_intent(user_id)

    database.update_plan_price("20gb_30d", user_price=600_000)
    result = database.begin_user_action_intent(intent, user_id, "buy")

    assert result == {"status": "plan_changed"}
    assert database.get_user_action_intent(intent, user_id)["status"] == "cancelled"
    assert database.get_user_wallet_balance(user_id) == 5_000_000
    assert not has_lock(f"user-buy:{user_id}")
    assert ledger_count(user_id) == 1


def test_ambiguous_intent_is_marked_for_reconciliation_and_never_replayed(isolated_db):
    user_id = 7006
    fund_user(user_id)
    intent = make_intent(user_id)
    started = database.begin_user_action_intent(intent, user_id, "buy")
    assert started["status"] == "started"

    assert database.mark_user_action_intent_for_reconciliation(
        intent,
        user_id,
        started["lock_key"],
        started["lock_token"],
        "X-UI call timed out; inspect account before any retry",
    )
    stored = database.get_user_action_intent(intent, user_id)
    assert stored["status"] == "needs_reconciliation"
    assert has_lock(started["lock_key"])

    replay = database.begin_user_action_intent(intent, user_id, "buy")
    assert replay == {
        "status": "already_processed",
        "intent_status": "needs_reconciliation",
    }
    assert database.get_user_wallet_balance(user_id) == 4_500_000
    assert ledger_count(user_id) == 2


def test_definite_failure_refund_atomically_consumes_intent_and_lock(isolated_db):
    user_id = 7007
    fund_user(user_id)
    intent = make_intent(user_id)
    started = database.begin_user_action_intent(intent, user_id, "buy")
    assert started["status"] == "started"
    assert database.get_user_wallet_balance(user_id) == 4_500_000

    refunded = database.refund_user_wallet_and_release_operation_lock(
        user_id=user_id,
        amount=500_000,
        description="refund after definite X-UI failure",
        actor_id=user_id,
        actor_name="SystemRefund",
        lock_key=started["lock_key"],
        token=started["lock_token"],
        intent_id=intent,
    )
    assert refunded == 5_000_000
    assert database.get_user_wallet_balance(user_id) == 5_000_000
    assert database.get_user_action_intent(intent, user_id)["status"] == "refunded"
    assert not has_lock(started["lock_key"])
    assert ledger_count(user_id) == 3

    replay = database.begin_user_action_intent(intent, user_id, "buy")
    assert replay == {"status": "already_processed", "intent_status": "refunded"}
    assert database.get_user_wallet_balance(user_id) == 5_000_000


def test_failed_intent_refund_rolls_back_and_preserves_lock_and_intent(isolated_db, monkeypatch):
    user_id = 7008
    fund_user(user_id)
    intent = make_intent(user_id)
    started = database.begin_user_action_intent(intent, user_id, "buy")
    original_credit = database._credit_user_wallet

    def credit_then_fail(cur, *args, **kwargs):
        original_credit(cur, *args, **kwargs)
        raise RuntimeError("injected failure after staged credit")

    monkeypatch.setattr(database, "_credit_user_wallet", credit_then_fail)
    with pytest.raises(RuntimeError, match="injected failure"):
        database.refund_user_wallet_and_release_operation_lock(
            user_id=user_id,
            amount=500_000,
            description="refund after definite X-UI failure",
            actor_id=user_id,
            actor_name="SystemRefund",
            lock_key=started["lock_key"],
            token=started["lock_token"],
            intent_id=intent,
        )

    assert database.get_user_wallet_balance(user_id) == 4_500_000
    assert database.get_user_action_intent(intent, user_id)["status"] == "processing"
    assert has_lock(started["lock_key"])
    assert ledger_count(user_id) == 2


def test_unresolved_intent_lock_cannot_use_generic_unlock_and_applied_resolution_is_atomic(isolated_db):
    user_id = 7009
    fund_user(user_id)
    intent_id = make_intent(user_id)
    started = database.begin_user_action_intent(intent_id, user_id, "buy")
    assert started["status"] == "started"
    assert database.mark_user_action_intent_for_reconciliation(
        intent_id,
        user_id,
        started["lock_key"],
        started["lock_token"],
        "X-UI response was ambiguous; inspect exact client before resolving",
    )

    operation = next(
        item for item in database.get_operation_locks()
        if item["lock_key"] == started["lock_key"]
    )
    assert operation["intent_id"] == intent_id
    assert operation["intent_status"] == "needs_reconciliation"
    assert operation["user_id"] == user_id

    with pytest.raises(ValueError, match="resolveintent"):
        database.resolve_operation_lock(
            started["lock_key"], 9000, "Admin",
            "RECONCILED: checked panel state",
        )
    assert has_lock(started["lock_key"])

    result = database.resolve_user_action_intent(
        intent_id,
        actor_id=9000,
        actor_name="Super Admin",
        resolution="APPLIED",
        evidence_note="APPLIED: exact client was inspected in X-UI and renewal is present",
    )
    assert result["status"] == "succeeded"
    assert result["new_balance"] is None
    assert database.get_user_action_intent(intent_id, user_id)["status"] == "succeeded"
    assert database.get_user_wallet_balance(user_id) == 4_500_000
    assert ledger_count(user_id) == 2
    assert not has_lock(started["lock_key"])
    assert scalar(
        "SELECT COUNT(*) FROM audit_logs WHERE action='resolve_user_action_intent_applied'",
    ) == 1


def test_unresolved_intent_refund_resolution_credits_once_and_consumes_lock(isolated_db):
    user_id = 7010
    fund_user(user_id)
    intent_id = make_intent(user_id)
    started = database.begin_user_action_intent(intent_id, user_id, "buy")
    assert started["status"] == "started"
    assert database.mark_user_action_intent_for_reconciliation(
        intent_id,
        user_id,
        started["lock_key"],
        started["lock_token"],
        "X-UI response was ambiguous; inspect exact client before resolving",
    )

    result = database.resolve_user_action_intent(
        intent_id,
        actor_id=9000,
        actor_name="Super Admin",
        resolution="REFUND_NOT_APPLIED",
        evidence_note="REFUND_NOT_APPLIED: exact client was inspected in X-UI and no new client exists",
    )
    assert result["status"] == "refunded"
    assert result["new_balance"] == 5_000_000
    assert database.get_user_wallet_balance(user_id) == 5_000_000
    assert database.get_user_action_intent(intent_id, user_id)["status"] == "refunded"
    assert ledger_count(user_id) == 3
    assert not has_lock(started["lock_key"])

    with pytest.raises(ValueError, match="unresolved"):
        database.resolve_user_action_intent(
            intent_id,
            actor_id=9000,
            actor_name="Super Admin",
            resolution="REFUND_NOT_APPLIED",
            evidence_note="REFUND_NOT_APPLIED: duplicate admin attempt",
        )
    assert database.get_user_wallet_balance(user_id) == 5_000_000
    assert ledger_count(user_id) == 3
    assert scalar(
        "SELECT COUNT(*) FROM audit_logs WHERE action='resolve_user_action_intent_refund'",
    ) == 1
