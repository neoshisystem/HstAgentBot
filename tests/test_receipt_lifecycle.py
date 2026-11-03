import pytest

import database


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "receipt-lifecycle.sqlite3"))
    database.init_db()
    with database.get_db() as conn:
        rep_id = conn.execute(
            """
            INSERT INTO representatives
                (rep_code, name, telegram_id, scope_filter, is_active, balance_toman, credit_limit_toman)
            VALUES ('receipt-rep', 'Receipt Representative', 82001, 'all', 1, 800000, 5000000)
            """
        ).lastrowid
    yield rep_id


def scalar(sql, params=()):
    with database.get_db() as conn:
        return conn.execute(sql, params).fetchone()[0]


def test_end_user_receipt_approval_credits_wallet_and_is_idempotent(isolated_db):
    receipt_code = database.create_payment_receipt(
        user_id=82002,
        role="end_user",
        amount_toman=250_000,
        photo_id="telegram-photo-file-id",
        note="bank reference 12345",
    )
    receipt = database.get_payment_receipt(receipt_code)
    assert receipt["receipt_photo_id"] == "telegram-photo-file-id"
    assert receipt["status"] == "pending"

    approved = database.approve_payment_receipt(receipt_code, 9000, "Super Admin")
    assert approved["status"] == "approved"
    assert approved["reviewed_by"] == 9000
    assert database.get_user_wallet_balance(82002) == 250_000
    assert scalar(
        "SELECT COUNT(*) FROM financial_ledger WHERE user_id=? AND transaction_type='credit'",
        (82002,),
    ) == 1

    replay = database.approve_payment_receipt(receipt["id"], 9000, "Super Admin")
    assert replay["status"] == "approved"
    assert database.get_user_wallet_balance(82002) == 250_000
    assert scalar(
        "SELECT COUNT(*) FROM financial_ledger WHERE user_id=? AND transaction_type='credit'",
        (82002,),
    ) == 1


def test_representative_receipt_approval_credits_rep_ledger_once(isolated_db):
    rep_id = isolated_db
    receipt_code = database.create_payment_receipt(
        user_id=82001,
        role="rep",
        amount_toman=300_000,
        rep_id=rep_id,
        photo_id="rep-receipt-file",
        note="representative bank transfer",
    )

    approved = database.approve_payment_receipt(receipt_code, 9000, "Super Admin")
    assert approved["status"] == "approved"
    assert database.get_rep_balance(rep_id) == 500_000
    assert scalar(
        "SELECT COUNT(*) FROM financial_ledger WHERE rep_id=? AND transaction_type='credit'",
        (rep_id,),
    ) == 1

    replay = database.approve_payment_receipt(receipt_code, 9000, "Super Admin")
    assert replay["status"] == "approved"
    assert database.get_rep_balance(rep_id) == 500_000
    assert scalar(
        "SELECT COUNT(*) FROM financial_ledger WHERE rep_id=? AND transaction_type='credit'",
        (rep_id,),
    ) == 1


def test_receipt_approval_rolls_back_status_and_wallet_when_credit_fails(isolated_db, monkeypatch):
    receipt_code = database.create_payment_receipt(
        user_id=82003,
        role="end_user",
        amount_toman=175_000,
        photo_id="rollback-photo",
    )
    original_credit = database._credit_user_wallet

    def credit_then_fail(cur, *args, **kwargs):
        original_credit(cur, *args, **kwargs)
        raise RuntimeError("injected failure after wallet and ledger write")

    monkeypatch.setattr(database, "_credit_user_wallet", credit_then_fail)
    with pytest.raises(RuntimeError, match="injected failure"):
        database.approve_payment_receipt(receipt_code, 9000, "Super Admin")

    stored = database.get_payment_receipt(receipt_code)
    assert stored["status"] == "pending"
    assert stored["reviewed_by"] is None
    assert database.get_user_wallet_balance(82003) == 0
    assert scalar(
        "SELECT COUNT(*) FROM financial_ledger WHERE user_id=?",
        (82003,),
    ) == 0

    monkeypatch.setattr(database, "_credit_user_wallet", original_credit)
    approved = database.approve_payment_receipt(receipt_code, 9000, "Super Admin")
    assert approved["status"] == "approved"
    assert database.get_user_wallet_balance(82003) == 175_000


def test_rejecting_receipt_is_idempotent_and_never_credits(isolated_db):
    receipt_code = database.create_payment_receipt(
        user_id=82004,
        role="end_user",
        amount_toman=90_000,
        photo_id="rejected-photo",
    )
    rejected = database.reject_payment_receipt(receipt_code, 9000, "Invalid bank reference")
    assert rejected["status"] == "rejected"
    assert rejected["rejection_reason"] == "Invalid bank reference"
    assert database.get_user_wallet_balance(82004) == 0

    replay = database.reject_payment_receipt(receipt_code, 9001, "Second reviewer")
    assert replay["status"] == "rejected"
    assert replay["reviewed_by"] == 9000
    assert database.get_user_wallet_balance(82004) == 0
    assert scalar(
        "SELECT COUNT(*) FROM financial_ledger WHERE user_id=?",
        (82004,),
    ) == 0


def test_pending_receipt_queue_paginates_without_dropping_older_submissions(isolated_db):
    codes = []
    for index in range(12):
        codes.append(database.create_payment_receipt(
            user_id=83000 + index,
            role="end_user",
            amount_toman=100_000 + index,
            photo_id=f"photo-{index}",
        ))

    page1, total1, pages1 = database.get_receipts_paginated("pending", page=1, page_size=10)
    page2, total2, pages2 = database.get_receipts_paginated("pending", page=2, page_size=10)
    page_out_of_range, total3, pages3 = database.get_receipts_paginated(
        "pending", page=999, page_size=10
    )

    assert (total1, pages1, len(page1)) == (12, 2, 10)
    assert (total2, pages2, len(page2)) == (12, 2, 2)
    assert (total3, pages3, len(page_out_of_range)) == (12, 2, 2)
    assert page1[0]["tracking_code"] == codes[-1]
    assert page2[-1]["tracking_code"] == codes[0]
    assert {item["tracking_code"] for item in page1 + page2} == set(codes)
