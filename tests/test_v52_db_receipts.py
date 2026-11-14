"""Offline acceptance for legacy receipt approval and order-backed delegation."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
import importlib.util
from pathlib import Path

from migrations_runner import apply_migrations


def _helpers():
    spec = importlib.util.spec_from_file_location(
        "_w18_db_receipt_helpers", Path(__file__).with_name("v52_helpers.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _seed_root_bot(database, now):
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision) "
            "VALUES(9001,NULL,1,NULL,'w18-root','active',?,1)",
            (now,),
        )
        conn.execute(
            "UPDATE bot_users SET role='super_admin',is_blocked=0 WHERE telegram_id=101"
        )


def _new_order(conn, *, order_id, quote_id, now):
    conn.execute(
        "INSERT INTO quotes(quote_id,actor_tg_id,bot_id,merchant_key,purpose,amount_toman,"
        "spec_json,price_edges_json,policy_version,created_at,expires_at,fingerprint) "
        "VALUES(?,101,9001,'root','vpn_buy',650,'{}','[]','v1.0',?,?,?)",
        (quote_id, now, now + 1800, "fingerprint-" + order_id),
    )
    conn.execute(
        "INSERT INTO payment_orders(order_id,quote_id,actor_tg_id,bot_id,merchant_key,purpose,"
        "payment_rail,amount_toman,status,received_toman,created_at,expires_at,revision) "
        "VALUES(?,?,101,9001,'root','vpn_buy','card_to_card',650,'pending_review',0,?,?,1)",
        (order_id, quote_id, now, now + 1800),
    )


def test_legacy_topup_and_new_invoice_separate(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    helpers = _helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    root = Path(__file__).resolve().parents[1]
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    now = 1_800_000_000

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w18-receipts.sqlite3"):
        helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=now)) == 18
        _seed_root_bot(database, now)

        legacy = database.submit_payment_receipt(
            user_id=101, role="end_user", amount_toman=500,
            photo_id="legacy-photo", sender_name="Legacy payer",
        )
        legacy_id = int(legacy["id"])
        outbox_before = database.get_receipt_notification_outbox(legacy_id)
        assert outbox_before and outbox_before["status"] == "pending"

        approved = database.approve_payment_receipt(legacy_id, 101, "W18 reviewer")
        assert approved["status"] == "approved"
        assert database.get_user_wallet_balance(101) == 500
        replay = database.approve_payment_receipt(legacy_id, 101, "W18 reviewer")
        assert replay["status"] == "approved"
        assert database.get_user_wallet_balance(101) == 500
        outbox_after = database.get_receipt_notification_outbox(legacy_id)
        assert outbox_after and outbox_after["status"] == outbox_before["status"]

        with database.get_db() as conn:
            _new_order(conn, order_id="w18-vpn-order", quote_id="w18-vpn-quote", now=now)
        invoice = database.submit_payment_receipt(
            user_id=101, role="end_user", amount_toman=650,
            photo_id="invoice-photo", sender_name="Invoice payer",
        )
        invoice_id = int(invoice["id"])
        nonce = "W18OneUseNonce00000000001"
        with database.get_db() as conn:
            conn.execute(
                "UPDATE payment_receipts SET order_id=?,bot_id=9001,merchant_key='root' WHERE id=?",
                ("w18-vpn-order", invoice_id),
            )
            conn.execute(
                "INSERT INTO action_confirmations(nonce,actor_tg_id,bot_id,action,target_type,"
                "target_id,expected_revision,payload_json,expires_at,consumed_at,operation_id) "
                "VALUES(?,101,9001,'receipt.approve','receipt',?,1,'{}',?,NULL,NULL)",
                (nonce, str(invoice_id), now + 600),
            )
        balance_before = database.get_user_wallet_balance(101)

        # The invoice is delegated with no transaction active at the service boundary.
        import payment_service
        original = payment_service.approve_order_receipt
        boundary = []

        def inspect_boundary(conn, **kwargs):
            boundary.append(conn.in_transaction)
            return original(conn, **kwargs)

        monkeypatch.setattr(payment_service, "approve_order_receipt", inspect_boundary)
        result = database.approve_payment_receipt(
            invoice_id, 101, "W18 reviewer", nonce=nonce, now=now,
        )
        assert boundary == [False]
        assert result["status"] == "accepted"
        assert result["payment_result"]["wallet_credit"] == 0
        assert result["payment_result"]["panel_job"] is True
        assert database.get_user_wallet_balance(101) == balance_before
        with database.get_db() as conn:
            job = conn.execute(
                "SELECT kind,state FROM scheduled_jobs WHERE job_key=?",
                ("fulfill:order:w18-vpn-order",),
            ).fetchone()
            assert job and job["kind"] == "fulfill_order"
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_ledger WHERE order_id='w18-vpn-order'"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM receipt_notification_outbox WHERE receipt_id=?",
                (invoice_id,),
            ).fetchone()[0] == 1

        # A linked invoice can never be approved by the old button without a nonce.
        another = database.submit_payment_receipt(
            user_id=101, role="end_user", amount_toman=650,
            photo_id="invoice-photo-no-nonce",
        )
        with database.get_db() as conn:
            _new_order(conn, order_id="w18-vpn-order-no-nonce",
                       quote_id="w18-vpn-quote-no-nonce", now=now)
            conn.execute(
                "UPDATE payment_receipts SET order_id=?,bot_id=9001,merchant_key='root' WHERE id=?",
                ("w18-vpn-order-no-nonce", int(another["id"])),
            )
        import pytest
        with pytest.raises(ValueError, match="one-use"):
            database.approve_payment_receipt(int(another["id"]), 101, "W18 reviewer")

        assert database.get_user_wallet_balance(101) == balance_before

        # Two reviewers racing the same linked receipt produce exactly one winner.
        with database.get_db() as conn:
            _new_order(conn, order_id="w18-vpn-order-race", quote_id="w18-vpn-quote-race", now=now)
        raced = database.submit_payment_receipt(
            user_id=101, role="end_user", amount_toman=650,
            photo_id="invoice-photo-race",
        )
        raced_id = int(raced["id"])
        raced_nonce = "W18RaceNonce000000000001"
        with database.get_db() as conn:
            conn.execute(
                "UPDATE payment_receipts SET order_id=?,bot_id=9001,merchant_key='root' WHERE id=?",
                ("w18-vpn-order-race", raced_id),
            )
            conn.execute(
                "INSERT INTO action_confirmations(nonce,actor_tg_id,bot_id,action,target_type,"
                "target_id,expected_revision,payload_json,expires_at,consumed_at,operation_id) "
                "VALUES(?,101,9001,'receipt.approve','receipt',?,1,'{}',?,NULL,NULL)",
                (raced_nonce, str(raced_id), now + 600),
            )

        import payment_service

        def approve_race():
            try:
                return database.approve_payment_receipt(
                    raced_id, 101, "W18 reviewer", nonce=raced_nonce, now=now,
                )
            except Exception as exc:
                return exc

        with ThreadPoolExecutor(max_workers=2) as pool:
            raced_results = list(pool.map(lambda _index: approve_race(), range(2)))
        winners = [
            value for value in raced_results
            if isinstance(value, dict) and value.get("status") == "accepted"
        ]
        conflicts = [
            value for value in raced_results
            if isinstance(value, payment_service.ReviewConflict)
        ]
        assert len(winners) == 1 and len(conflicts) == 1
        assert database.get_user_wallet_balance(101) == balance_before
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM scheduled_jobs WHERE job_key='fulfill:order:w18-vpn-order-race'"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM receipt_notification_outbox WHERE receipt_id=?",
                (raced_id,),
            ).fetchone()[0] == 1
        assert boundary == [False, False, False]

        assert Path(sentinel) != Path(database.DB_PATH)
    assert network_attempts == []
