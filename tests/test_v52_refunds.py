from __future__ import annotations

import base64
import importlib.util
from pathlib import Path
import sys

import pytest

from financial_service import (
    InsufficientFunds, _sync_projection_cache, available_balance, post_entry, reserve_holds,
)
from migrations_runner import apply_migrations
from refund_service import (
    RefundConflict, RefundFundsUnavailable, finalize_bank_refund, request_bank_refund,
)


def _helpers():
    path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w085_refund_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _refund_nonce(conn, nonce, refund_id, now):
    conn.execute(
        "INSERT INTO action_confirmations(nonce,actor_tg_id,bot_id,action,target_type,target_id,"
        "expected_revision,payload_json,expires_at,consumed_at,operation_id) "
        "VALUES(?,901,9001,'refund.finalize','refund_request',?,1,'{}',?,NULL,NULL)",
        (nonce, refund_id, now + 300),
    )


def _seed_order(conn, order_id, *, user_id, status, amount, now):
    quote_id = f"quote-{order_id}"
    conn.execute(
        "INSERT INTO quotes(quote_id,actor_tg_id,bot_id,merchant_key,purpose,amount_toman,"
        "spec_json,price_edges_json,policy_version,created_at,expires_at,fingerprint) "
        "VALUES(?, ?,9001,'root','wallet_topup',?,'{}','[]','v1.0',?,?,?)",
        (quote_id, user_id, amount, now, now + 1800, f"fingerprint-{order_id}"),
    )
    conn.execute(
        "INSERT INTO payment_orders(order_id,quote_id,actor_tg_id,bot_id,merchant_key,purpose,"
        "payment_rail,amount_toman,status,received_toman,created_at,expires_at,revision) "
        "VALUES(?,?,?,9001,'root','wallet_topup','card_to_card',?,?,?,?,?,1)",
        (order_id, quote_id, user_id, amount, status, amount if status == "completed" else 0, now, now + 1800),
    )


def _credit_wallet(conn, user_id, amount, actor_id):
    # The caller owns the database.get_db() transaction.
    post_entry(
        conn, rep_id=None, user_id=user_id, transaction_type="credit", amount=amount,
        balance_after=amount, description="synthetic opening wallet", actor_id=actor_id,
        business_key=f"w085-opening:{user_id}", merchant_key="root", ledger_scope="wallet",
    )
    _sync_projection_cache(conn, ledger_scope="wallet", merchant_key="root", user_id=user_id)


def test_wallet_credit_not_spent_and_refunded_twice(tmp_path, monkeypatch):
    helpers = _helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    database, sentinel = helpers.load_database_module(Path(__file__).resolve().parents[1], monkeypatch, tmp_path)
    config = sys.modules["config"]
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 901)
    now = 1794237000

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w085-refunds.sqlite3"):
        with database.get_db() as conn:
            assert len(apply_migrations(conn, Path(__file__).resolve().parents[1] / "migrations", now=now)) == 18
            helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision) "
                "VALUES(9001,NULL,1,NULL,'w085-root','active',?,1)", (now,),
            )
            for uid in (101, 102, 103):
                if uid != 101:
                    conn.execute(
                        "INSERT INTO bot_users(telegram_id,role,rep_id,username,full_name,is_blocked) "
                        "VALUES(?,'end_user',202,?,?,0)", (uid, f"w085-{uid}", f"W085 user {uid}"),
                    )
                    conn.execute(
                        "INSERT INTO user_wallets(user_id,balance_toman) VALUES(?,0)", (uid,),
                    )
                _credit_wallet(conn, uid, {101: 1000, 102: 500, 103: 300}[uid], 901)
            _seed_order(conn, "paid-refund-order", user_id=101, status="completed", amount=700, now=now)
            _seed_order(conn, "competing-buy", user_id=101, status="awaiting_payment", amount=500, now=now)

        with database.get_db() as conn:
            requested = request_bank_refund(
                conn, refund_id="refund-101", order_id="paid-refund-order",
                merchant_key="root", user_tg_id=101, requester_tg_id=101, amount_toman=700,
                bank_details={"destination": "BANK-DEST-101", "account_name": "Synthetic Owner"},
                now=now,
            )
        assert requested["state"] == "processing"
        with database.get_db() as conn:
            assert available_balance(conn, "wallet:root:101") == 300
            with pytest.raises(InsufficientFunds):
                reserve_holds(
                    conn, order_id="competing-buy", holds={"wallet:root:101": 500}, now=now + 1,
                )

        with database.get_db() as conn:
            _refund_nonce(conn, "R" * 24, "refund-101", now)
            settled = finalize_bank_refund(
                conn, refund_id="refund-101", reviewer_tg_id=901, bot_id=9001,
                nonce="R" * 24, decision="settled",
                review_note="Bank statement confirms the manual transfer.", now=now + 2,
                bank_reference="BANK-REF-101", destination="BANK-DEST-101", amount_toman=700,
            )
            replay = finalize_bank_refund(
                conn, refund_id="refund-101", reviewer_tg_id=901, bot_id=9001,
                nonce="R" * 24, decision="settled",
                review_note="Bank statement confirms the manual transfer.", now=now + 3,
                bank_reference="BANK-REF-101", destination="BANK-DEST-101", amount_toman=700,
            )
            assert settled["state"] == "settled" and settled["ledger_id"] is not None
            assert replay["replayed"] is True
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_ledger WHERE business_key='refund:refund-101:wallet-debit'"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT state FROM wallet_refund_holds WHERE hold_key='refund:refund-101:wallet'"
            ).fetchone()[0] == "captured"
            assert conn.execute(
                "SELECT balance_toman FROM user_wallets WHERE user_id=101"
            ).fetchone()[0] == 300
            assert available_balance(conn, "wallet:root:101") == 300
            audit = conn.execute(
                "SELECT actor_id,action,details_cipher FROM audit_logs WHERE action='bank_refund.settled'"
            ).fetchone()
            assert audit is not None and int(audit[0]) == 901 and audit[2]
            assert "BANK-REF-101" not in str(audit[2])

        with database.get_db() as conn:
            unknown = request_bank_refund(
                conn, refund_id="refund-102", merchant_key="root",
                user_tg_id=102, requester_tg_id=102, amount_toman=200,
                bank_details={"destination": "BANK-DEST-102"}, now=now + 4,
            )
        assert unknown["state"] == "processing"
        with database.get_db() as conn:
            _refund_nonce(conn, "U" * 24, "refund-102", now + 4)
            result = finalize_bank_refund(
                conn, refund_id="refund-102", reviewer_tg_id=901, bot_id=9001,
                nonce="U" * 24, decision="unknown",
                review_note="Bank transfer result is still ambiguous.", now=now + 5,
            )
            assert result["state"] == "unknown" and result["hold_state"] == "active"
            assert available_balance(conn, "wallet:root:102") == 300
            assert conn.execute(
                "SELECT state FROM wallet_refund_holds WHERE hold_key='refund:refund-102:wallet'"
            ).fetchone()[0] == "active"
            with pytest.raises(RefundConflict):
                finalize_bank_refund(
                    conn, refund_id="refund-102", reviewer_tg_id=901, bot_id=9001,
                    nonce="S" * 24, decision="settled",
                    review_note="Do not automatically retry an unknown transfer.", now=now + 6,
                    bank_reference="BANK-REF-102", destination="BANK-DEST-102", amount_toman=200,
                )

        with database.get_db() as conn:
            request_bank_refund(
                conn, refund_id="refund-103", merchant_key="root",
                user_tg_id=103, requester_tg_id=103, amount_toman=100,
                bank_details={"destination": "BANK-DEST-103"}, now=now + 7,
            )
        with database.get_db() as conn:
            _refund_nonce(conn, "D" * 24, "refund-103", now + 7)
            rejected = finalize_bank_refund(
                conn, refund_id="refund-103", reviewer_tg_id=901, bot_id=9001,
                nonce="D" * 24, decision="rejected",
                review_note="Transfer was not approved after root review.", now=now + 8,
            )
            assert rejected["state"] == "rejected" and rejected["hold_state"] == "released"
            assert available_balance(conn, "wallet:root:103") == 300
            assert conn.execute(
                "SELECT state FROM wallet_refund_holds WHERE hold_key='refund:refund-103:wallet'"
            ).fetchone()[0] == "released"

    assert sentinel != str((tmp_path / "w085-refunds.sqlite3").resolve())
    assert network_attempts == []
