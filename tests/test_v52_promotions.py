"""Offline acceptance for issuer-budget coupons and cashback replay/refund safety."""
from __future__ import annotations

from pathlib import Path
import importlib.util
import sqlite3
import threading

import pytest

from migrations_runner import apply_migrations
from promotion_service import (
    PromotionBudgetExceeded,
    finalize_coupon,
    hold_coupon,
    post_cashback,
)
from financial_service import reverse_order


def _load_helpers():
    helper_path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w07_promotions_helpers", helper_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _order(database, order_id, *, status, amount=500):
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO quotes(quote_id,actor_tg_id,bot_id,merchant_key,purpose,amount_toman,"
            "spec_json,price_edges_json,policy_version,created_at,expires_at,fingerprint) "
            "VALUES(?,101,9001,'root','wallet_topup',?,'{}','[]','v1.0',1794237000,1794238800,?)",
            (f"quote-{order_id}", amount, f"fingerprint-{order_id}"),
        )
        conn.execute(
            "INSERT INTO payment_orders(order_id,quote_id,actor_tg_id,bot_id,merchant_key,"
            "purpose,payment_rail,amount_toman,status,received_toman,created_at,expires_at,revision) "
            "VALUES(?, ?,101,9001,'root','wallet_topup','card_to_card',?,?,?,1794237000,1794238800,1)",
            (order_id, f"quote-{order_id}", amount, status, amount if status == "completed" else 0),
        )


def test_coupon_budget_and_cashback_replay(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _load_helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w07-promotions.sqlite3") as db_path:
        helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=1794237000)) == 18
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision) "
                "VALUES(9001,NULL,1,NULL,'synthetic-primary','active',1794237000,1)"
            )
            conn.execute(
                "INSERT INTO coupons(code,issuer_key,discount_toman,budget_toman,used_toman,max_uses,expires_at,is_active) "
                "VALUES('W07-BUDGET','root',80,100,0,2,1794239000,1)"
            )
            conn.execute(
                "INSERT INTO financial_ledger(rep_id,user_id,transaction_type,amount,balance_after,description,"
                "actor_id,business_key,merchant_key,ledger_scope,policy_version) "
                "VALUES(NULL,909,'credit',200,200,'issuer cashback budget',303,'issuer-opening','root','wallet','v1.0')"
            )
            conn.execute(
                "INSERT INTO user_wallets(user_id,balance_toman) VALUES(909,200) "
                "ON CONFLICT(user_id) DO UPDATE SET balance_toman=200"
            )
        _order(database, "coupon-a", status="awaiting_payment")
        _order(database, "coupon-b", status="awaiting_payment")

        barrier = threading.Barrier(2)
        outcomes = []
        lock = threading.Lock()

        def reserve(order_id):
            local = sqlite3.connect(str(db_path), timeout=30)
            local.row_factory = sqlite3.Row
            local.execute("PRAGMA foreign_keys=ON")
            local.execute("PRAGMA busy_timeout=30000")
            barrier.wait(timeout=10)
            try:
                result = hold_coupon(
                    local, order_id=order_id, code="W07-BUDGET",
                    price_toman=500, now=1794237001,
                )
                item = ("held", result)
            except PromotionBudgetExceeded as exc:
                item = ("budget", str(exc))
            finally:
                local.close()
            with lock:
                outcomes.append(item)

        threads = [
            threading.Thread(target=reserve, args=("coupon-a",)),
            threading.Thread(target=reserve, args=("coupon-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert all(not thread.is_alive() for thread in threads)
        assert sorted(item[0] for item in outcomes) == ["budget", "held"]
        winner = next(item[1]["order_id"] for item in outcomes if item[0] == "held")
        with database.get_db() as conn:
            use = conn.execute(
                "SELECT code,amount_toman,state FROM coupon_uses WHERE code='W07-BUDGET'"
            ).fetchall()
            assert len(use) == 1
            assert (use[0][0], int(use[0][1]), use[0][2]) == ("W07-BUDGET", 80, "held")
            assert conn.execute("SELECT used_toman FROM coupons WHERE code='W07-BUDGET'").fetchone()[0] == 0
        with database.get_db() as conn:
            finalized = finalize_coupon(conn, order_id=winner, success=True)
            assert finalized["state"] == "used"
            replay = finalize_coupon(conn, order_id=winner, success=True)
            assert replay["replayed"] is True
            assert conn.execute("SELECT used_toman FROM coupons WHERE code='W07-BUDGET'").fetchone()[0] == 80
            assert conn.execute(
                "SELECT amount_toman FROM quotes WHERE quote_id=?",
                (f"quote-{winner}",),
            ).fetchone()[0] == 500

        _order(database, "cashback-order", status="completed", amount=100)
        with database.get_db() as conn:
            conn.execute(
                "INSERT INTO financial_ledger(rep_id,user_id,transaction_type,amount,balance_after,description,"
                "actor_id,business_key,merchant_key,ledger_scope,policy_version) "
                "VALUES(NULL,909,'credit',100,300,'issuer cashback opening',303,'issuer-cashback-opening','root','wallet','v1.0')"
            )
            conn.execute("UPDATE user_wallets SET balance_toman=300 WHERE user_id=909")
        with database.get_db() as conn:
            cashback = post_cashback(
                conn, order_id="cashback-order", amount_toman=50,
                issuer_source_key="wallet:root:909", actor_id=303, now=1794237002,
            )
            assert cashback["replayed"] is False
            count_after = conn.execute(
                "SELECT COUNT(*) FROM financial_ledger WHERE order_id='cashback-order'"
            ).fetchone()[0]
            replay = post_cashback(
                conn, order_id="cashback-order", amount_toman=50,
                issuer_source_key="wallet:root:909", actor_id=303, now=1794237003,
            )
            assert replay["replayed"] is True
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_ledger WHERE order_id='cashback-order'"
            ).fetchone()[0] == count_after
            assert conn.execute("SELECT balance_toman FROM user_wallets WHERE user_id=909").fetchone()[0] == 250
            assert conn.execute("SELECT balance_toman FROM user_wallets WHERE user_id=101").fetchone()[0] == 50

        # The buyer spends all cashback. A refund must not create a negative wallet.
        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT INTO financial_ledger(rep_id,user_id,transaction_type,amount,balance_after,description,"
                "actor_id,business_key,merchant_key,ledger_scope,policy_version) "
                "VALUES(NULL,101,'debit',50,0,'synthetic cashback spend',303,'cashback-spend','root','wallet','v1.0')"
            )
            conn.execute("UPDATE user_wallets SET balance_toman=0 WHERE user_id=101")
        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            refund = reverse_order(conn, order_id="cashback-order", actor_id=303, now=1794237004)
        assert refund["status"] == "operator_exception"
        with database.get_db() as conn:
            assert conn.execute("SELECT balance_toman FROM user_wallets WHERE user_id=101").fetchone()[0] == 0
            assert conn.execute("SELECT balance_toman FROM user_wallets WHERE user_id=909").fetchone()[0] == 300
            claim = conn.execute(
                "SELECT state,result_json FROM scheduled_jobs WHERE job_key LIKE "
                "'debt-claim:order:cashback-order:ledger:%'"
            ).fetchone()
            assert claim is not None and claim[0] == "operator_review"
            assert '"amount_toman":50' in claim[1]
            count_after_refund = conn.execute(
                "SELECT COUNT(*) FROM financial_ledger WHERE order_id='cashback-order'"
            ).fetchone()[0]
        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            replay_refund = reverse_order(conn, order_id="cashback-order", actor_id=303, now=1794237005)
        assert replay_refund["status"] == "operator_exception"
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_ledger WHERE order_id='cashback-order'"
            ).fetchone()[0] == count_after_refund
            assert conn.execute("SELECT balance_toman FROM user_wallets WHERE user_id=101").fetchone()[0] == 0
        assert Path(sentinel) != Path(database.DB_PATH)
    assert network_attempts == []
