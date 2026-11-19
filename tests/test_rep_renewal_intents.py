import pytest

import database


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "rep-renewal-intents.sqlite3"))
    database.init_db()
    with database.get_db() as conn:
        rep_id = conn.execute(
            """
            INSERT INTO representatives
                (rep_code, name, telegram_id, scope_filter, is_active, balance_toman, credit_limit_toman)
            VALUES ('rep-renew', 'Renewal Representative', 87001, 'all', 1, 0, 600000)
            """
        ).lastrowid
    yield rep_id


def balance(rep_id):
    with database.get_db() as conn:
        return conn.execute(
            "SELECT balance_toman FROM representatives WHERE id=?", (rep_id,)
        ).fetchone()[0]


def ledger_count(rep_id):
    with database.get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM financial_ledger WHERE rep_id=?", (rep_id,)
        ).fetchone()[0]


def start(rep_id, code="RN-001", email="rn-001@example.test", amount=400_000, actor=87001):
    return database.begin_rep_renewal_intent(
        rep_id=rep_id,
        actor_tg_id=actor,
        account_code=code,
        account_email=email,
        plan_code="renew_20gb_30d",
        plan_name="Renew 20G",
        gb=20,
        days=30,
        amount=amount,
    )


def test_rep_renewal_intent_precharge_and_success_are_linked_and_atomic(isolated_db):
    rep_id = isolated_db
    started = start(rep_id)
    assert started["status"] == "started"
    assert started["new_balance"] == 400_000
    assert balance(rep_id) == 400_000
    assert ledger_count(rep_id) == 1

    locks = database.get_operation_locks()
    linked = next(item for item in locks if item["lock_key"] == started["lock_key"])
    assert linked["intent_id"] == started["intent_id"]
    assert linked["action_type"] == "renew"
    assert linked["intent_owner_type"] == "representative"
    assert linked["user_id"] == 87001
    assert linked["account_code"] == "RN-001"
    assert "token" not in linked

    with database.get_db() as conn:
        intent = conn.execute(
            "SELECT * FROM rep_renewal_intents WHERE intent_id=?",
            (started["intent_id"],),
        ).fetchone()
    assert intent["status"] == "processing"

    assert database.complete_rep_renewal_intent(
        started["intent_id"], rep_id, 87001, "Explicit X-UI success"
    )
    assert balance(rep_id) == 400_000
    assert ledger_count(rep_id) == 1
    assert database.get_operation_locks() == []
    with database.get_db() as conn:
        status = conn.execute(
            "SELECT status FROM rep_renewal_intents WHERE intent_id=?",
            (started["intent_id"],),
        ).fetchone()[0]
    assert status == "succeeded"


def test_rep_renewal_intent_rejects_same_account_overlap_and_credit_limit(isolated_db):
    rep_id = isolated_db
    first = start(rep_id)
    assert first["status"] == "started"

    overlap = start(rep_id)
    assert overlap == {"status": "busy"}

    # A different account is not blocked by the account lock, but the
    # transactional ledger guard still enforces the shared representative limit.
    with pytest.raises(ValueError, match="credit limit exceeded"):
        start(rep_id, code="RN-002", email="rn-002@example.test", amount=250_000)

    assert balance(rep_id) == 400_000
    assert ledger_count(rep_id) == 1
    assert len(database.get_operation_locks()) == 1


def test_rep_renewal_refund_is_atomic_and_cannot_be_applied_twice(isolated_db):
    rep_id = isolated_db
    started = start(rep_id)
    result = database.refund_rep_renewal_intent(
        started["intent_id"], rep_id, 87001, "Explicit X-UI rejection"
    )
    assert result == {"status": "refunded", "new_balance": 0, "amount": 400_000}
    assert balance(rep_id) == 0
    assert ledger_count(rep_id) == 2
    assert database.get_operation_locks() == []

    with pytest.raises(ValueError, match="not unresolved"):
        database.refund_rep_renewal_intent(
            started["intent_id"], rep_id, 87001, "replay"
        )
    assert balance(rep_id) == 0
    assert ledger_count(rep_id) == 2


def test_rep_renewal_begin_rolls_back_precharge_and_lock_on_injected_failure(isolated_db, monkeypatch):
    rep_id = isolated_db
    original = database._record_rep_ledger_entry

    def write_then_fail(cur, *args, **kwargs):
        original(cur, *args, **kwargs)
        raise RuntimeError("injected failure after debit")

    monkeypatch.setattr(database, "_record_rep_ledger_entry", write_then_fail)
    with pytest.raises(RuntimeError, match="injected failure"):
        start(rep_id)

    assert balance(rep_id) == 0
    assert ledger_count(rep_id) == 0
    assert database.get_operation_locks() == []
    with database.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM rep_renewal_intents").fetchone()[0] == 0


def test_rep_renewal_ambiguous_result_requires_evidence_gated_resolution(isolated_db):
    rep_id = isolated_db
    started = start(rep_id)
    assert database.mark_rep_renewal_for_reconciliation(
        started["intent_id"], rep_id, 87001, "X-UI timeout; external state is unknown"
    )

    lock = next(item for item in database.get_operation_locks() if item["lock_key"] == started["lock_key"])
    assert lock["intent_status"] == "needs_reconciliation"
    with pytest.raises(ValueError, match="resolvereprenewal"):
        database.resolve_operation_lock(
            started["lock_key"], 9000, "Super Admin",
            "RECONCILED: inspected account state",
        )

    result = database.resolve_rep_renewal_intent(
        started["intent_id"], 9000, "Super Admin", "REFUND_NOT_APPLIED",
        "REFUND_NOT_APPLIED: inspected exact X-UI account and no renewal was applied",
    )
    assert result["status"] == "refunded"
    assert result["new_balance"] == 0
    assert balance(rep_id) == 0
    assert ledger_count(rep_id) == 2
    assert database.get_operation_locks() == []

    with pytest.raises(ValueError, match="No unresolved"):
        database.resolve_rep_renewal_intent(
            started["intent_id"], 9000, "Super Admin", "REFUND_NOT_APPLIED",
            "REFUND_NOT_APPLIED: repeated resolution attempt",
        )


def test_rep_renewal_applied_resolution_preserves_precharge(isolated_db):
    rep_id = isolated_db
    started = start(rep_id)
    database.mark_rep_renewal_for_reconciliation(
        started["intent_id"], rep_id, 87001, "X-UI response lost after request"
    )

    result = database.resolve_rep_renewal_intent(
        started["intent_id"], 9000, "Super Admin", "APPLIED",
        "APPLIED: inspected exact account and confirmed target expiry and quota",
    )
    assert result["status"] == "succeeded"
    assert balance(rep_id) == 400_000
    assert ledger_count(rep_id) == 1
    assert database.get_operation_locks() == []
