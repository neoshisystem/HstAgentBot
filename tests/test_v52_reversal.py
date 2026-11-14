"""Offline acceptance for append-only, idempotent all-edge reversals."""
from __future__ import annotations

from pathlib import Path
import importlib.util

import pytest

from financial_service import capture_holds, reverse_order
from migrations_runner import apply_migrations


def _load_helpers():
    helper_path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w07_reversal_helpers", helper_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _new_order(database, order_id, *, amount=1000, status="completed"):
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
            "VALUES(?, ?,101,9001,'root','wallet_topup','card_to_card',?, ?,?,1794237000,1794238800,1)",
            (order_id, f"quote-{order_id}", amount, status, amount),
        )


def _ledger_hold(database, order_id, source_key, amount):
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO financial_holds(hold_id,order_id,source_key,amount_toman,state,created_at) "
            "VALUES(?,?,?,?,'active',1794237000)",
            (f"hold-{order_id}-{source_key}", order_id, source_key, amount),
        )


def test_all_edges_refund_once(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _load_helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w07-reversal.sqlite3"):
        helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=1794237000)) == 18
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision) "
                "VALUES(9001,NULL,1,NULL,'synthetic-primary','active',1794237000,1)"
            )
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,group_id,topic_id,scope_filter,"
                "parent_rep_id,tier,billing_mode,markup_bps,is_active,deleted_at) "
                "VALUES(204,'W07-CHILD','W07 child',404,0,0,'w07:child',202,'normal','credit',0,1,NULL)"
            )
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,group_id,topic_id,scope_filter,"
                "parent_rep_id,tier,billing_mode,markup_bps,is_active,deleted_at) "
                "VALUES(205,'W07-REP-205','W07 other',405,0,0,'w07:other',NULL,'normal','credit',0,1,NULL)"
            )
            for edge_id, parent_key, child_id in [
                (601, "root", 202), (602, "rep:202", 204), (603, "root", 205)
            ]:
                conn.execute(
                    "INSERT INTO rep_edges(id,parent_key,child_rep_id,billing_mode,debt_toman,credit_limit_toman,"
                    "active,started_at,ended_at) VALUES(?,?,?,'credit',0,0,1,1794237000,NULL)",
                    (edge_id, parent_key, child_id),
                )
            conn.execute(
                "INSERT INTO financial_ledger(rep_id,user_id,transaction_type,amount,balance_after,description,"
                "actor_id,business_key,merchant_key,ledger_scope,policy_version) "
                "VALUES(NULL,101,'credit',3000,3000,'synthetic wallet opening',101,'w07-opening','root','wallet','v1.0')"
            )
            conn.execute("UPDATE user_wallets SET balance_toman=3000 WHERE user_id=101")

        _new_order(database, "edge-refund", amount=990, status="accepted")
        for source, amount in [("edge:601", 900), ("edge:602", 990), ("wallet:root:101", 990)]:
            _ledger_hold(database, "edge-refund", source, amount)
        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            capture_holds(conn, order_id="edge-refund", actor_id=303, now=1794237001)
        _ledger_hold(database, "edge-refund", "edge:603", 10)

        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            refunded = reverse_order(conn, order_id="edge-refund", actor_id=303, now=1794237002)
        assert refunded["status"] == "refunded"
        with database.get_db() as conn:
            originals = conn.execute(
                "SELECT ledger_scope,amount,transaction_type FROM financial_ledger "
                "WHERE order_id='edge-refund' AND business_key LIKE 'order:edge-refund:capture:%' "
                "ORDER BY id"
            ).fetchall()
            reversals = conn.execute(
                "SELECT ledger_scope,amount,transaction_type,reversal_of,business_key "
                "FROM financial_ledger WHERE order_id='edge-refund' AND business_key LIKE "
                "'order:edge-refund:reverse:%' ORDER BY id"
            ).fetchall()
            assert [(r[0], int(r[1]), r[2]) for r in originals] == [
                ("edge", 900, "debit"), ("edge", 990, "debit"), ("wallet", 990, "debit")
            ]
            assert [(r[0], int(r[1]), r[2]) for r in reversals] == [
                ("edge", 900, "credit"), ("edge", 990, "credit"), ("wallet", 990, "credit")
            ]
            assert all(r[3] is not None and str(r[4]).startswith("order:edge-refund:reverse:") for r in reversals)
            assert conn.execute(
                "SELECT state FROM financial_holds WHERE order_id='edge-refund' AND source_key='edge:603'"
            ).fetchone()[0] == "released"
            assert conn.execute("SELECT debt_toman FROM rep_edges WHERE id=601").fetchone()[0] == 0
            assert conn.execute("SELECT debt_toman FROM rep_edges WHERE id=602").fetchone()[0] == 0
            assert conn.execute("SELECT balance_toman FROM representatives WHERE id=202").fetchone()[0] == 0
            assert conn.execute("SELECT balance_toman FROM user_wallets WHERE user_id=101").fetchone()[0] == 3000
            count_after_first = conn.execute(
                "SELECT COUNT(*) FROM financial_ledger WHERE order_id='edge-refund'"
            ).fetchone()[0]

        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            replay = reverse_order(conn, order_id="edge-refund", actor_id=303, now=1794237003)
        assert replay["status"] == "replayed"
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_ledger WHERE order_id='edge-refund'"
            ).fetchone()[0] == count_after_first

        _new_order(database, "direct-refund", amount=1000)
        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            before = 3000
            ledger_id = financial_post(
                conn, user_id=101, amount=1000, balance_after=before + 1000,
                actor_id=303, order_id="direct-refund",
            )
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_ledger WHERE order_id='direct-refund' AND ledger_scope<>'wallet'"
            ).fetchone()[0] == 0
            conn.execute("BEGIN IMMEDIATE")
            result = reverse_order(conn, order_id="direct-refund", actor_id=303, now=1794237004)
        assert result["status"] == "refunded"
        with database.get_db() as conn:
            direct = conn.execute(
                "SELECT transaction_type,amount,ledger_scope,reversal_of FROM financial_ledger "
                "WHERE order_id='direct-refund' ORDER BY id"
            ).fetchall()
            assert [(r[0], int(r[1]), r[2]) for r in direct] == [
                ("credit", 1000, "wallet"), ("debit", 1000, "wallet")
            ]
            assert direct[1][3] == ledger_id
            assert conn.execute("SELECT balance_toman FROM user_wallets WHERE user_id=101").fetchone()[0] == 3000

        _new_order(database, "unknown-refund", amount=200, status="exception")
        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = 3000
            financial_post(conn, user_id=101, amount=200, balance_after=current + 200,
                           actor_id=303, order_id="unknown-refund")
        with database.get_db() as conn:
            before = conn.execute("SELECT COUNT(*) FROM financial_ledger").fetchone()[0]
            conn.execute("BEGIN IMMEDIATE")
            blocked = reverse_order(conn, order_id="unknown-refund", actor_id=303, now=1794237005)
        assert blocked["status"] == "blocked"
        with database.get_db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM financial_ledger").fetchone()[0] == before
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_ledger WHERE order_id='unknown-refund' "
                "AND business_key LIKE 'order:unknown-refund:reverse:%'"
            ).fetchone()[0] == 0

        assert Path(sentinel) != Path(database.DB_PATH)
    assert network_attempts == []


def financial_post(conn, *, user_id, amount, balance_after, actor_id, order_id):
    from financial_service import post_entry
    return post_entry(
        conn, rep_id=None, user_id=user_id, transaction_type="credit",
        amount=amount, balance_after=balance_after, description="Direct payment received",
        actor_id=actor_id, business_key=f"order:{order_id}:capture:direct-payment",
        merchant_key="root", ledger_scope="wallet", edge_id=None, order_id=order_id,
        operation_id=f"capture:{order_id}", policy_version="v1.0",
    )
