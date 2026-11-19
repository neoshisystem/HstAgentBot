"""Offline acceptance for immutable order capture and projection cache safety."""
from __future__ import annotations

from pathlib import Path
import importlib.util
import sqlite3

import pytest

import financial_service
from financial_service import FinancialError, InsufficientFunds, capture_holds
from migrations_runner import apply_migrations


def _load_helpers():
    helper_path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w07_ledger_helpers", helper_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prepare(database, root, order_id, *, status="accepted"):
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO quotes(quote_id,actor_tg_id,bot_id,merchant_key,purpose,amount_toman,"
            "spec_json,price_edges_json,policy_version,created_at,expires_at,fingerprint) "
            "VALUES(?,101,9001,'root','vpn_buy',990,'{}','[]','v1.0',1794237000,1794238800,?)",
            (f"quote-{order_id}", f"fingerprint-{order_id}"),
        )
        conn.execute(
            "INSERT INTO payment_orders(order_id,quote_id,actor_tg_id,bot_id,merchant_key,"
            "purpose,payment_rail,amount_toman,status,received_toman,created_at,expires_at,revision) "
            "VALUES(?, ?,101,9001,'root','vpn_buy','wallet',990,?,0,1794237000,1794238800,1)",
            (order_id, f"quote-{order_id}", status),
        )


def _hold(database, order_id, source_key, amount):
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO financial_holds(hold_id,order_id,source_key,amount_toman,state,created_at) "
            "VALUES(?,?,?,?,'active',1794237000)",
            (f"hold-{order_id}-{source_key}", order_id, source_key, amount),
        )


def test_capture_once_preserves_signs(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _load_helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w07-ledger.sqlite3"):
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
                "INSERT INTO rep_edges(id,parent_key,child_rep_id,billing_mode,debt_toman,credit_limit_toman,"
                "active,started_at,ended_at) VALUES(601,'root',202,'credit',0,0,1,1794237000,NULL)"
            )
            conn.execute(
                "INSERT INTO rep_edges(id,parent_key,child_rep_id,billing_mode,debt_toman,credit_limit_toman,"
                "active,started_at,ended_at) VALUES(602,'rep:202',204,'credit',0,0,1,1794237000,NULL)"
            )
            conn.execute(
                "INSERT INTO financial_ledger(rep_id,user_id,transaction_type,amount,balance_after,description,"
                "actor_id,business_key,merchant_key,ledger_scope,policy_version) "
                "VALUES(NULL,101,'credit',2000,2000,'synthetic wallet opening',101,'w07-opening','root','wallet','v1.0')"
            )
            conn.execute("UPDATE user_wallets SET balance_toman=2000 WHERE user_id=101")

        _prepare(database, root, "capture-success")
        for source, amount in [("edge:601", 900), ("edge:602", 990), ("wallet:root:101", 990)]:
            _hold(database, "capture-success", source, amount)

        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            first = capture_holds(conn, order_id="capture-success", actor_id=303, now=1794237001)
        assert first["status"] if "status" in first else first["order_id"] == "capture-success"
        with database.get_db() as conn:
            rows = conn.execute(
                "SELECT transaction_type,amount,ledger_scope,edge_id,user_id,balance_after "
                "FROM financial_ledger WHERE order_id='capture-success' ORDER BY id"
            ).fetchall()
            assert [(r[0], int(r[1]), r[2]) for r in rows] == [
                ("debit", 900, "edge"), ("debit", 990, "edge"), ("debit", 990, "wallet")
            ]
            assert int(rows[0][5]) == 900
            assert int(rows[1][5]) == 990
            assert int(rows[2][5]) == 1010
            assert conn.execute("SELECT balance_toman FROM representatives WHERE id=202").fetchone()[0] == 900
            assert conn.execute("SELECT debt_toman FROM rep_edges WHERE id=602").fetchone()[0] == 990
            assert conn.execute("SELECT balance_toman FROM user_wallets WHERE user_id=101").fetchone()[0] == 1010
            before = conn.execute(
                "SELECT COUNT(*) FROM financial_ledger WHERE order_id='capture-success'"
            ).fetchone()[0]

        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            replay = capture_holds(conn, order_id="capture-success", actor_id=303, now=1794237002)
        assert replay["replayed"] is True
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_ledger WHERE order_id='capture-success'"
            ).fetchone()[0] == before

        _prepare(database, root, "capture-rollback")
        for source, amount in [("edge:601", 100), ("wallet:root:101", 100)]:
            _hold(database, "capture-rollback", source, amount)
        original_sync = financial_service._sync_projection_cache
        calls = {"n": 0}

        def fail_between_post_and_projection(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("injected projection-cache failure")
            return original_sync(*args, **kwargs)

        monkeypatch.setattr(financial_service, "_sync_projection_cache", fail_between_post_and_projection)
        with pytest.raises(RuntimeError, match="injected projection-cache failure"):
            with database.get_db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                capture_holds(conn, order_id="capture-rollback", actor_id=303, now=1794237003)
        monkeypatch.setattr(financial_service, "_sync_projection_cache", original_sync)
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_ledger WHERE order_id='capture-rollback'"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_holds WHERE order_id='capture-rollback' AND state='active'"
            ).fetchone()[0] == 2

        _prepare(database, root, "capture-source-failure")
        for source, amount in [("edge:601", 50), ("edge:99999", 50)]:
            _hold(database, "capture-source-failure", source, amount)
        with pytest.raises(Exception):
            with database.get_db() as conn:
                conn.execute("BEGIN IMMEDIATE")
                capture_holds(conn, order_id="capture-source-failure", actor_id=303, now=1794237004)
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_ledger WHERE order_id='capture-source-failure'"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_holds WHERE order_id='capture-source-failure' AND state='active'"
            ).fetchone()[0] == 2

        assert Path(sentinel) != Path(database.DB_PATH)
    assert network_attempts == []
