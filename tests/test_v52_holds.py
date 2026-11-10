"""Offline acceptance for ledger-derived available funds and atomic holds."""
from __future__ import annotations

from pathlib import Path
import importlib.util
import sqlite3
import threading

import pytest

from financial_service import (
    FinancialError,
    FinancialSourceError,
    HoldConflict,
    InsufficientFunds,
    available_balance,
    reserve_holds,
)
from migrations_runner import apply_migrations


def _load_helpers():
    helper_path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w07_holds_helpers", helper_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert_order(conn, order_id: str, quote_id: str, now: int = 1794237000):
    conn.execute(
        "INSERT INTO quotes("
        "quote_id,actor_tg_id,bot_id,merchant_key,purpose,account_id,plan_code,"
        "product_id,panel_id,amount_toman,spec_json,price_edges_json,policy_version,"
        "created_at,expires_at,fingerprint"
        ") VALUES(?,101,9001,'root','wallet_topup',NULL,NULL,NULL,NULL,80,'{}','[]','v1.0',?,?,?)",
        (quote_id, now, now + 1800, "fingerprint-" + quote_id),
    )
    conn.execute(
        "INSERT INTO payment_orders("
        "order_id,quote_id,actor_tg_id,bot_id,merchant_key,purpose,payment_rail,"
        "amount_toman,status,received_toman,created_at,expires_at,revision"
        ") VALUES(?,?,101,9001,'root','wallet_topup','wallet',80,'awaiting_payment',0,?,?,1)",
        (order_id, quote_id, now, now + 1800),
    )


def test_atomic_chain_and_double_spend(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _load_helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w07-holds.sqlite3") as db_path:
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=1794237000)) == 18
            helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
            conn.execute(
                "INSERT INTO bot_registry("
                "bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision"
                ") VALUES(9001,NULL,1,NULL,'synthetic-primary','active',1794237000,1)"
            )
            conn.execute("UPDATE user_wallets SET balance_toman=999999 WHERE user_id=101")
            conn.execute(
                "INSERT INTO financial_ledger("
                "rep_id,user_id,account_code,email,plan_code,transaction_type,amount,"
                "balance_after,description,actor_id,actor_name,business_key,merchant_key,"
                "ledger_scope,edge_id,order_id,operation_id,reversal_of,policy_version"
                ") VALUES(NULL,101,NULL,NULL,NULL,'credit',100,100,'synthetic wallet opening',101,"
                "'fixture','w07-wallet-opening','root','wallet',NULL,NULL,NULL,NULL,'v1.0')"
            )
            conn.execute(
                "UPDATE representatives SET parent_rep_id=NULL,billing_mode='prepaid_wallet' "
                "WHERE id=202"
            )
            conn.execute(
                "INSERT INTO representatives("
                "id,rep_code,name,telegram_id,group_id,topic_id,scope_filter,parent_rep_id,"
                "tier,billing_mode,markup_bps,is_active,deleted_at"
                ") VALUES(204,'V52-REP-204','W07 child',404,0,0,'w07:child',202,"
                "'normal','credit',0,1,NULL)"
            )
            conn.execute(
                "INSERT INTO representatives("
                "id,rep_code,name,telegram_id,group_id,topic_id,scope_filter,parent_rep_id,"
                "tier,billing_mode,markup_bps,is_active,deleted_at"
                ") VALUES(205,'V52-REP-205','W07 unlimited',405,0,0,'w07:unlimited',NULL,"
                "'normal','credit',0,1,NULL)"
            )
            conn.execute(
                "INSERT INTO rep_edges("
                "id,parent_key,child_rep_id,billing_mode,debt_toman,credit_limit_toman,"
                "active,started_at,ended_at"
                ") VALUES(601,'root',202,'prepaid_wallet',0,0,1,1794237000,NULL)"
            )
            conn.execute(
                "INSERT INTO rep_edges("
                "id,parent_key,child_rep_id,billing_mode,debt_toman,credit_limit_toman,"
                "active,started_at,ended_at"
                ") VALUES(602,'rep:202',204,'credit',0,50,1,1794237000,NULL)"
            )
            conn.execute(
                "INSERT INTO rep_edges("
                "id,parent_key,child_rep_id,billing_mode,debt_toman,credit_limit_toman,"
                "active,started_at,ended_at"
                ") VALUES(603,'root',205,'credit',0,0,1,1794237000,NULL)"
            )
            conn.execute(
                "INSERT INTO financial_ledger("
                "rep_id,user_id,transaction_type,amount,balance_after,description,actor_id,"
                "actor_name,business_key,merchant_key,ledger_scope,edge_id,policy_version"
                ") VALUES(202,NULL,'credit',100,-100,'prepaid edge funding',303,"
                "'fixture','w07-edge-601','root','edge',601,'v1.0')"
            )
            conn.execute(
                "INSERT INTO financial_ledger("
                "rep_id,user_id,transaction_type,amount,balance_after,description,actor_id,"
                "actor_name,business_key,merchant_key,ledger_scope,edge_id,policy_version"
                ") VALUES(204,NULL,'debit',60,60,'child edge debt',303,"
                "'fixture','w07-edge-602','rep:202','edge',602,'v1.0')"
            )
            conn.execute(
                "INSERT INTO financial_ledger("
                "rep_id,user_id,transaction_type,amount,balance_after,description,actor_id,"
                "actor_name,business_key,merchant_key,ledger_scope,edge_id,policy_version"
                ") VALUES(205,NULL,'debit',5000,5000,'unbounded credit edge',303,"
                "'fixture','w07-edge-603','root','edge',603,'v1.0')"
            )
            _insert_order(conn, "W07-wallet-order-a", "W07-wallet-quote-a")
            _insert_order(conn, "W07-wallet-order-b", "W07-wallet-quote-b")
            _insert_order(conn, "W07-edge-order", "W07-edge-quote")
            _insert_order(conn, "W07-unlimited-order", "W07-unlimited-quote")

        # A fake cache value is intentionally huge; the authoritative ledger is 100.
        with database.get_db() as conn:
            assert available_balance(conn, "wallet:root:101") == 100
            assert available_balance(conn, "edge:601") == 100
            assert available_balance(conn, "edge:602") == 0
            assert available_balance(conn, "edge:603") is None

        barrier = threading.Barrier(2)
        outcomes = []
        outcome_lock = threading.Lock()

        def competing_hold(order_id: str):
            local = sqlite3.connect(str(db_path), timeout=30)
            try:
                local.execute("PRAGMA foreign_keys=ON")
                local.execute("PRAGMA busy_timeout=30000")
                barrier.wait(timeout=10)
                try:
                    result = reserve_holds(
                        local,
                        order_id=order_id,
                        holds={"wallet:root:101": 80},
                        now=1794237001,
                    )
                    outcome = ("held", result)
                except InsufficientFunds as exc:
                    outcome = ("insufficient", str(exc))
                with outcome_lock:
                    outcomes.append(outcome)
            finally:
                local.close()

        threads = [
            threading.Thread(target=competing_hold, args=("W07-wallet-order-a",)),
            threading.Thread(target=competing_hold, args=("W07-wallet-order-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert all(not thread.is_alive() for thread in threads)
        assert sorted(item[0] for item in outcomes) == ["held", "insufficient"]
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_holds WHERE source_key='wallet:root:101' "
                "AND state='active'"
            ).fetchone()[0] == 1
            winning = conn.execute(
                "SELECT order_id FROM financial_holds WHERE source_key='wallet:root:101' "
                "AND state='active'"
            ).fetchone()[0]
            assert winning in {"W07-wallet-order-a", "W07-wallet-order-b"}
            assert available_balance(conn, "wallet:root:101") == 20
            # Identical replay is a no-op; a changed amount/source set conflicts.
            replay = reserve_holds(
                conn, order_id=winning, holds={"wallet:root:101": 80}, now=1794237002
            )
            assert replay["replayed"] is True
            with pytest.raises(HoldConflict):
                reserve_holds(
                    conn, order_id=winning, holds={"wallet:root:101": 70}, now=1794237002
                )

        # Release the first order hold to isolate the refund-hold calculation.
        with database.get_db() as conn:
            conn.execute(
                "UPDATE financial_holds SET state='released' WHERE order_id=? AND state='active'",
                (winning,),
            )
            conn.execute(
                "INSERT INTO wallet_refund_holds(hold_key,merchant_key,user_tg_id,"
                "amount_toman,state,created_at) VALUES('W07-refund-hold','root',101,30,'active',1794237003)"
            )
            assert available_balance(conn, "wallet:root:101") == 70

        # A late failing leg rolls back an earlier hold: no partial source reservation.
        with database.get_db() as conn:
            with pytest.raises(InsufficientFunds):
                reserve_holds(
                    conn,
                    order_id="W07-edge-order",
                    holds={"edge:601": 80, "edge:602": 10},
                    now=1794237004,
                )
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_holds WHERE order_id='W07-edge-order'"
            ).fetchone()[0] == 0
            assert available_balance(conn, "edge:601") == 100
            with pytest.raises(FinancialSourceError):
                available_balance(conn, "edge:999999")
            with pytest.raises(FinancialError):
                reserve_holds(
                    conn, order_id="W07-unlimited-order",
                    holds={"wallet:root": 10}, now=1794237005,
                )

        assert Path(sentinel) != Path(db_path)

    assert network_attempts == []
