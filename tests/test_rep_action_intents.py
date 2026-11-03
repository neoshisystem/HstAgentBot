import pytest

import database


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "rep-action-intents.sqlite3"))
    database.init_db()
    with database.get_db() as conn:
        cur = conn.execute(
            """
            INSERT INTO representatives
                (rep_code, name, telegram_id, scope_filter, is_active, balance_toman, credit_limit_toman)
            VALUES ('rep-intent', 'Intent Representative', 81001, 'all', 1, 0, 5_000_000)
            """
        )
        rep_id = cur.lastrowid
    database.add_or_update_plan(
        "rep_20gb_30d", "Representative 20G", 20, 30, 500_000, 700_000, is_quick=0
    )
    yield rep_id


def make_intent(rep_id, actor_id=81001):
    plan = database.get_plan("rep_20gb_30d")
    return database.create_rep_action_intent(
        rep_id=rep_id,
        actor_tg_id=actor_id,
        plan_code=plan["plan_code"],
        plan_name=plan["name"],
        gb=plan["gb"],
        days=plan["days"],
        amount=plan["rep_price"],
    )


def rep_balance(rep_id):
    with database.get_db() as conn:
        return conn.execute(
            "SELECT balance_toman FROM representatives WHERE id=?", (rep_id,)
        ).fetchone()[0]


def ledger_rows(rep_id):
    with database.get_db() as conn:
        return conn.execute(
            "SELECT * FROM financial_ledger WHERE rep_id=? ORDER BY id", (rep_id,)
        ).fetchall()


def has_lock(lock_key):
    return any(item["lock_key"] == lock_key for item in database.get_operation_locks())


def audit_count(action):
    with database.get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action=?", (action,)
        ).fetchone()[0]


def test_rep_issue_intent_rejects_replay_after_success_but_new_intent_can_issue(isolated_db):
    rep_id = isolated_db
    first = make_intent(rep_id)
    started = database.begin_rep_action_intent(first, rep_id, 81001)
    assert started["status"] == "started"
    assert started["new_balance"] == 500_000
    assert rep_balance(rep_id) == 500_000
    assert has_lock(started["lock_key"])
    assert len(ledger_rows(rep_id)) == 1

    duplicate_processing = database.begin_rep_action_intent(first, rep_id, 81001)
    assert duplicate_processing == {
        "status": "already_processed",
        "intent_status": "processing",
    }
    assert rep_balance(rep_id) == 500_000
    assert len(ledger_rows(rep_id)) == 1

    assert database.complete_rep_action_intent(
        first, rep_id, 81001,
        started["lock_key"], started["lock_token"],
        "REP-001", "rep-001@example.test",
    )
    duplicate_success = database.begin_rep_action_intent(first, rep_id, 81001)
    assert duplicate_success == {
        "status": "already_processed",
        "intent_status": "succeeded",
    }
    assert database.get_rep_action_intent(first, rep_id, 81001)["account_code"] == "REP-001"
    assert not has_lock(started["lock_key"])
    assert rep_balance(rep_id) == 500_000
    assert len(ledger_rows(rep_id)) == 1

    second = make_intent(rep_id)
    assert second != first
    next_started = database.begin_rep_action_intent(second, rep_id, 81001)
    assert next_started["status"] == "started"
    assert rep_balance(rep_id) == 1_000_000
    assert database.complete_rep_action_intent(
        second, rep_id, 81001,
        next_started["lock_key"], next_started["lock_token"],
        "REP-002", "rep-002@example.test",
    )


def test_rep_intent_is_bound_to_rep_and_telegram_actor(isolated_db):
    rep_id = isolated_db
    intent = make_intent(rep_id)
    assert database.get_rep_action_intent(intent, rep_id, 99999) is None
    assert database.begin_rep_action_intent(intent, rep_id + 1, 81001) == {
        "status": "not_found"
    }
    assert database.begin_rep_action_intent(intent, rep_id, 99999) == {
        "status": "not_found"
    }
    assert rep_balance(rep_id) == 0
    assert ledger_rows(rep_id) == []


def test_rep_credit_limit_is_enforced_during_atomic_intent_claim(isolated_db):
    rep_id = isolated_db
    with database.get_db() as conn:
        conn.execute(
            "UPDATE representatives SET credit_limit_toman=400000 WHERE id=?",
            (rep_id,),
        )
    intent = make_intent(rep_id)

    with pytest.raises(ValueError, match="credit limit exceeded"):
        database.begin_rep_action_intent(intent, rep_id, 81001)

    assert database.get_rep_action_intent(intent, rep_id, 81001)["status"] == "pending"
    assert rep_balance(rep_id) == 0
    assert ledger_rows(rep_id) == []
    assert not has_lock(f"rep-issue-intent:{intent}")


def test_rep_definite_failure_refund_is_atomic_and_token_cannot_refund_twice(isolated_db):
    rep_id = isolated_db
    intent = make_intent(rep_id)
    started = database.begin_rep_action_intent(intent, rep_id, 81001)
    assert started["status"] == "started"
    assert rep_balance(rep_id) == 500_000

    refunded_balance = database.refund_rep_action_intent(
        intent, rep_id, 81001, started["lock_key"], started["lock_token"]
    )
    assert refunded_balance == 0
    assert rep_balance(rep_id) == 0
    assert database.get_rep_action_intent(intent, rep_id, 81001)["status"] == "refunded"
    assert len(ledger_rows(rep_id)) == 2
    assert [row["transaction_type"] for row in ledger_rows(rep_id)] == ["debit", "credit"]
    assert not has_lock(started["lock_key"])

    replay = database.begin_rep_action_intent(intent, rep_id, 81001)
    assert replay == {"status": "already_processed", "intent_status": "refunded"}
    with pytest.raises(ValueError, match="not processing"):
        database.refund_rep_action_intent(
            intent, rep_id, 81001, started["lock_key"], started["lock_token"]
        )
    assert rep_balance(rep_id) == 0
    assert len(ledger_rows(rep_id)) == 2


def test_rep_ambiguous_outcome_requires_admin_reconciliation_not_generic_unlock(isolated_db):
    rep_id = isolated_db
    intent = make_intent(rep_id)
    started = database.begin_rep_action_intent(intent, rep_id, 81001)
    assert started["status"] == "started"
    assert database.mark_rep_action_intent_for_reconciliation(
        intent, rep_id, 81001, started["lock_key"], started["lock_token"],
        "X-UI response ambiguous; inspect exact allocation before resolution",
    )

    lock = next(
        item for item in database.get_operation_locks()
        if item["lock_key"] == started["lock_key"]
    )
    assert lock["intent_id"] == intent
    assert lock["intent_owner_type"] == "representative"
    assert lock["intent_status"] == "needs_reconciliation"

    with pytest.raises(ValueError, match="representative action intent"):
        database.resolve_operation_lock(
            started["lock_key"], 9000, "Admin",
            "RECONCILED: checked account state in X-UI",
        )
    assert has_lock(started["lock_key"])
    assert rep_balance(rep_id) == 500_000

    result = database.resolve_rep_action_intent(
        intent,
        actor_id=9000,
        actor_name="Super Admin",
        resolution="REFUND_NOT_APPLIED",
        evidence_note="REFUND_NOT_APPLIED: exact X-UI pool inspected and no account was allocated",
    )
    assert result["status"] == "refunded"
    assert result["new_balance"] == 0
    assert rep_balance(rep_id) == 0
    assert database.get_rep_action_intent(intent, rep_id, 81001)["status"] == "refunded"
    assert not has_lock(started["lock_key"])
    assert audit_count("resolve_rep_action_intent_refund") == 1

    with pytest.raises(ValueError, match="unresolved"):
        database.resolve_rep_action_intent(
            intent,
            actor_id=9000,
            actor_name="Super Admin",
            resolution="REFUND_NOT_APPLIED",
            evidence_note="REFUND_NOT_APPLIED: repeated admin refund attempt",
        )
    assert rep_balance(rep_id) == 0
    assert len(ledger_rows(rep_id)) == 2
