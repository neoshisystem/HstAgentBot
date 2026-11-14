"""Offline acceptance tests for legacy panel and financial bootstraps."""

from __future__ import annotations

import importlib.util
import shutil
import sqlite3
from pathlib import Path

import pytest

from bootstrap_v52 import (
    BootstrapError,
    bootstrap_financial_edges,
    bootstrap_legacy_panel,
)
from migrations_runner import apply_migrations


_MIGRATIONS = (
    "001_policy",
    "002_panels",
    "003_accounts",
    "004_plans",
    "005_tree",
    "006_bots",
    "007_access",
    "008_orders",
    "009_finance",
)


def _load_helpers(root, monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "_hstagentbot_v52_bootstrap_helpers",
        Path(__file__).with_name("v52_helpers.py"),
    )
    assert spec is not None and spec.loader is not None
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    return helpers, database, sentinel


def _apply_prefix(root, tmp_path, conn):
    source = root / "migrations"
    target = tmp_path / "bootstrap-migrations-001-009"
    target.mkdir(exist_ok=True)
    for name in _MIGRATIONS:
        shutil.copyfile(source / f"{name}.sql", target / f"{name}.sql")
    assert apply_migrations(conn, target, now=1794237000) == list(_MIGRATIONS)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def _open_migrated_db(database, path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _insert_legacy_account(conn, account_id, rep_id, account_code, email, owner_tg_id):
    conn.execute(
        "INSERT INTO accounts(id,rep_id,account_code,email,customer_name,sub_id,"
        "enable,total_gb,expiry_time,owner_tg_id) "
        "VALUES(?,?,?,?,?,'V52_SYNTHETIC_SUB',1,10.0,0,?)",
        (
            account_id,
            rep_id,
            account_code,
            email,
            f"V52 owner {owner_tg_id}",
            owner_tg_id,
        ),
    )


def _insert_legacy_reservation(conn, account_code, email):
    conn.execute(
        "INSERT INTO reserved_packages("
        "rep_id,account_code,email,plan_code,plan_name,gb,days,on_first_connect,"
        "charge_amount,actor_id,actor_name,status"
        ") VALUES(202,?,?, 'V52-PLAN-A','V52 plan',5,30,0,700,101,'fixture','pending')",
        (account_code, email),
    )


def test_legacy_mapping_preserves_prices_and_owners(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers, database, sentinel = _load_helpers(root, monkeypatch, tmp_path)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="legacy-map.sqlite3") as db_path:
        helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303, wallet_balance=777)
        with database.get_db() as seed:
            _insert_legacy_account(
                seed, 303, 202, "V52-LEGACY-A", "legacy-a@example.invalid", 101
            )
            _insert_legacy_account(
                seed, 304, 202, "V52-LEGACY-B", "legacy-b@example.invalid", 404
            )
            seed.execute(
                "INSERT INTO pricing_plans(plan_code,name,gb,days,rep_price,user_price,is_quick,sort_order) "
                "VALUES('V52-PLAN-A','Fixture Plan A',5,30,1200,1800,0,1)"
            )
            seed.execute(
                "INSERT INTO pricing_plans(plan_code,name,gb,days,rep_price,user_price,is_quick,sort_order) "
                "VALUES('V52-PLAN-B','Fixture Plan B',10,60,2400,3600,0,2)"
            )
            seed.execute(
                "UPDATE representatives SET balance_toman=9876 WHERE id=202"
            )
            _insert_legacy_reservation(
                seed, "V52-LEGACY-A", "legacy-a@example.invalid"
            )
            _insert_legacy_reservation(
                seed, "V52-LEGACY-B", "legacy-b@example.invalid"
            )

        conn = _open_migrated_db(database, db_path)
        try:
            _apply_prefix(root, tmp_path, conn)
            # A second panel exists before the one-time default-panel bootstrap.
            conn.execute(
                "INSERT INTO panels(id,panel_code,name,panel_type,is_default,lifecycle,created_at) "
                "VALUES(9002,'V52-SECOND','Second panel','3x-ui',0,'active',1794237000)"
            )
            before_accounts = [tuple(row) for row in conn.execute(
                "SELECT id,owner_tg_id FROM accounts ORDER BY id"
            ).fetchall()]
            before_prices = [tuple(row) for row in conn.execute(
                "SELECT plan_code,rep_price,user_price FROM pricing_plans "
                "WHERE plan_code LIKE 'V52-%' ORDER BY plan_code"
            ).fetchall()]
            before_rep_balance = conn.execute(
                "SELECT balance_toman FROM representatives WHERE id=202"
            ).fetchone()[0]
            before_wallet = conn.execute(
                "SELECT balance_toman FROM user_wallets WHERE user_id=101"
            ).fetchone()[0]

            result = bootstrap_legacy_panel(
                conn,
                {
                    "id": 9001,
                    "panel_code": "V52-LEGACY-DEFAULT",
                    "name": "Synthetic legacy default",
                    "panel_type": "3x-ui",
                },
                1794237001,
            )
            assert result == {"panel_id": 9001, "mapped_reservations": 2}
            assert [tuple(row) for row in conn.execute(
                "SELECT id,panel_id,seller_key,owner_tg_id FROM accounts ORDER BY id"
            ).fetchall()] == [
                (303, 9001, "rep:202", 101),
                (304, 9001, "rep:202", 404),
            ]
            assert [tuple(row) for row in conn.execute(
                "SELECT panel_id,next_value FROM panel_counters ORDER BY panel_id"
            ).fetchall()] == [(9001, 1)]
            assert [tuple(row) for row in conn.execute(
                "SELECT plan_code,panel_id,allowed FROM plan_panels ORDER BY plan_code"
            ).fetchall()] == [
                (str(row[0]), 9001, 1)
                for row in conn.execute(
                    "SELECT plan_code FROM pricing_plans ORDER BY plan_code"
                ).fetchall()
            ]
            assert {"V52-PLAN-A", "V52-PLAN-B"} == {
                row[0] for row in conn.execute(
                    "SELECT plan_code FROM plan_panels WHERE panel_id=9001"
                ).fetchall()
            } & {"V52-PLAN-A", "V52-PLAN-B"}
            assert [tuple(row) for row in conn.execute(
                "SELECT id,panel_id,account_id FROM reserved_packages ORDER BY id"
            ).fetchall()] == [(1, 9001, 303), (2, 9001, 304)]
            assert [tuple(row) for row in conn.execute(
                "SELECT id,is_default FROM panels ORDER BY id"
            ).fetchall()] == [(9001, 1), (9002, 0)]
            assert [tuple(row) for row in conn.execute(
                "SELECT plan_code,rep_price,user_price FROM pricing_plans "
                "WHERE plan_code LIKE 'V52-%' ORDER BY plan_code"
            ).fetchall()] == before_prices
            assert [tuple(row) for row in conn.execute(
                "SELECT id,owner_tg_id FROM accounts ORDER BY id"
            ).fetchall()] == before_accounts
            assert conn.execute(
                "SELECT balance_toman FROM representatives WHERE id=202"
            ).fetchone()[0] == before_rep_balance
            assert conn.execute(
                "SELECT balance_toman FROM user_wallets WHERE user_id=101"
            ).fetchone()[0] == before_wallet
            assert conn.execute(
                "SELECT COUNT(*) FROM plan_panels WHERE panel_id=9002"
            ).fetchone()[0] == 0
        finally:
            conn.close()

    # A code that points at one legacy account while its email points at another
    # is ambiguous. All panel/account/plan writes must roll back together.
    with helpers.make_db(database, monkeypatch, tmp_path, filename="legacy-ambiguous.sqlite3") as db_path:
        helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
        with database.get_db() as seed:
            _insert_legacy_account(
                seed, 311, 202, "V52-COLLISION-CODE", "collision-a@example.invalid", 101
            )
            _insert_legacy_account(
                seed, 312, 202, "V52-OTHER-CODE", "collision-b@example.invalid", 404
            )
            _insert_legacy_reservation(
                seed, "V52-COLLISION-CODE", "collision-b@example.invalid"
            )
        conn = _open_migrated_db(database, db_path)
        try:
            _apply_prefix(root, tmp_path, conn)
            with pytest.raises(BootstrapError, match="ambiguous"):
                bootstrap_legacy_panel(
                    conn,
                    {
                        "id": 9011,
                        "panel_code": "V52-AMBIGUOUS-DEFAULT",
                        "name": "Ambiguous synthetic default",
                        "panel_type": "3x-ui",
                    },
                    1794237010,
                )
            assert conn.execute("SELECT COUNT(*) FROM panels").fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM panel_counters"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM plan_panels"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM accounts WHERE panel_id IS NOT NULL"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM reserved_packages "
                "WHERE panel_id IS NOT NULL OR account_id IS NOT NULL"
            ).fetchone()[0] == 0
        finally:
            conn.close()


def test_opening_anchor_preserves_signed_debt(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers, database, sentinel = _load_helpers(root, monkeypatch, tmp_path)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="financial-anchors.sqlite3") as db_path:
        helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303, wallet_balance=300)
        with database.get_db() as seed:
            seed.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,group_id,topic_id,"
                "scope_filter,balance_toman,is_active) "
                "VALUES(199,'root_admin_rep','Root identity',9090,0,0,'',0,1)"
            )
            seed.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,group_id,topic_id,"
                "scope_filter,balance_toman,is_active) "
                "VALUES(203,'V52-REP-203','Credit representative',404,0,0,'',-200,1)"
            )
            seed.execute(
                "UPDATE representatives SET balance_toman=900 WHERE id=202"
            )
            seed.execute(
                "INSERT INTO financial_ledger("
                "rep_id,user_id,account_code,email,plan_code,transaction_type,amount,"
                "balance_after,description,actor_id,actor_name"
                ") VALUES(202,NULL,'legacy','legacy@example.invalid','legacy-plan',"
                "'debit',5,900,'pre-cutoff immutable history',9090,'fixture')"
            )
        conn = _open_migrated_db(database, db_path)
        try:
            _apply_prefix(root, tmp_path, conn)
            conn.execute(
                "INSERT INTO merchant_wallets(merchant_key,user_tg_id,balance_toman) "
                "VALUES('rep:202',101,450)"
            )
            pre_reps = conn.execute(
                "SELECT id,balance_toman FROM representatives ORDER BY id"
            ).fetchall()
            pre_wallets = conn.execute(
                "SELECT user_id,balance_toman FROM user_wallets ORDER BY user_id"
            ).fetchall()
            pre_merchant_wallets = conn.execute(
                "SELECT merchant_key,user_tg_id,balance_toman FROM merchant_wallets"
            ).fetchall()
            old_history = conn.execute(
                "SELECT id,rep_id,transaction_type,amount,balance_after,description "
                "FROM financial_ledger"
            ).fetchall()

            result = bootstrap_financial_edges(conn, "S12-W03-B02", 1794237100)
            assert result == {
                "cutoff_id": "S12-W03-B02",
                "already_applied": False,
                "edge_count": 2,
                "anchor_count": 4,
            }
            assert [tuple(row) for row in conn.execute(
                "SELECT child_rep_id,parent_key,debt_toman FROM rep_edges ORDER BY child_rep_id"
            ).fetchall()] == [(202, "root", 900), (203, "root", -200)]
            assert conn.execute(
                "SELECT COUNT(*) FROM rep_edges WHERE child_rep_id=199"
            ).fetchone()[0] == 0
            anchors = conn.execute(
                "SELECT rep_id,user_id,amount,balance_after,merchant_key,ledger_scope,edge_id "
                "FROM financial_ledger WHERE transaction_type='opening_anchor' "
                "ORDER BY ledger_scope,rep_id,user_id"
            ).fetchall()
            assert len(anchors) == 4
            assert {(r["rep_id"], r["user_id"], r["amount"], r["balance_after"], r["merchant_key"], r["ledger_scope"]) for r in anchors} == {
                (202, None, 900, 900, "root", "edge"),
                (203, None, 200, -200, "root", "edge"),
                (None, 101, 300, 300, "root", "wallet"),
                (202, 101, 450, 450, "rep:202", "wallet"),
            }
            assert all(
                row["edge_id"] is not None
                for row in anchors if row["ledger_scope"] == "edge"
            )
            assert all(
                row["edge_id"] is None
                for row in anchors if row["ledger_scope"] == "wallet"
            )
            assert conn.execute(
                "SELECT id,rep_id,transaction_type,amount,balance_after,description "
                "FROM financial_ledger WHERE transaction_type='debit'"
            ).fetchall() == old_history
            assert conn.execute(
                "SELECT id,balance_toman FROM representatives ORDER BY id"
            ).fetchall() == pre_reps
            assert conn.execute(
                "SELECT user_id,balance_toman FROM user_wallets ORDER BY user_id"
            ).fetchall() == pre_wallets
            assert conn.execute(
                "SELECT merchant_key,user_tg_id,balance_toman FROM merchant_wallets"
            ).fetchall() == pre_merchant_wallets

            ledger_count = conn.execute(
                "SELECT COUNT(*) FROM financial_ledger"
            ).fetchone()[0]
            edge_count = conn.execute("SELECT COUNT(*) FROM rep_edges").fetchone()[0]
            replay = bootstrap_financial_edges(conn, "S12-W03-B02", 1794237200)
            assert replay["already_applied"] is True
            assert replay["anchor_count"] == 4
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_ledger"
            ).fetchone()[0] == ledger_count
            assert conn.execute(
                "SELECT COUNT(*) FROM rep_edges"
            ).fetchone()[0] == edge_count
            assert conn.execute(
                "SELECT id,balance_toman FROM representatives ORDER BY id"
            ).fetchall() == pre_reps
            assert conn.execute(
                "SELECT user_id,balance_toman FROM user_wallets ORDER BY user_id"
            ).fetchall() == pre_wallets
            assert conn.execute(
                "SELECT merchant_key,user_tg_id,balance_toman FROM merchant_wallets"
            ).fetchall() == pre_merchant_wallets
        finally:
            conn.close()
