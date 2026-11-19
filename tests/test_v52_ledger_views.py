"""Offline acceptance for merchant/edge-scoped financial ledger pagination."""
from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import pytest
from migrations_runner import apply_migrations


def _helpers():
    spec = importlib.util.spec_from_file_location(
        "_w18_ledger_helpers", Path(__file__).with_name("v52_helpers.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _add_rep(conn, rep_id, code):
    conn.execute(
        "INSERT INTO representatives(id,rep_code,name,telegram_id,group_id,topic_id,scope_filter,"
        "parent_rep_id,tier,billing_mode,markup_bps,is_active,deleted_at) "
        "VALUES(?,?,?, ?,0,0,'all',NULL,'normal','credit',0,1,NULL)",
        (rep_id, code, code, rep_id + 10000),
    )


def _ledger(conn, *, rep_id, merchant, scope, amount, tx="credit",
            edge_id=None, code="", email="", business_key=None):
    conn.execute(
        "INSERT INTO financial_ledger(rep_id,user_id,account_code,email,transaction_type,amount,"
        "balance_after,description,actor_id,actor_name,business_key,merchant_key,ledger_scope,edge_id,policy_version) "
        "VALUES(?,NULL,?,?,?, ?,?, 'W18 synthetic ledger row',101,'W18',?,?,?,?,'v1.0')",
        (rep_id, code, email, tx, amount, amount, business_key, merchant, scope, edge_id),
    )


def test_scoped_pagination_and_opening(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(b"w18-ledger-key-material-1234567").decode("ascii").rstrip("="),
    )
    helpers = _helpers()
    attempts = helpers.guard_network(monkeypatch)
    root = Path(__file__).resolve().parents[1]
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w18-ledger.sqlite3"):
        helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=1_800_000_000)) == 18
            _add_rep(conn, 204, "W18-CHILD-A")
            _add_rep(conn, 205, "W18-REP-B")
            _add_rep(conn, 206, "W18-CHILD-B")
            conn.execute(
                "INSERT INTO rep_edges(id,parent_key,child_rep_id,billing_mode,debt_toman,credit_limit_toman,"
                "active,started_at,ended_at) VALUES(602,'rep:202',204,'credit',0,0,1,1800000000,NULL)"
            )
            conn.execute(
                "INSERT INTO rep_edges(id,parent_key,child_rep_id,billing_mode,debt_toman,credit_limit_toman,"
                "active,started_at,ended_at) VALUES(605,'rep:205',206,'credit',0,0,1,1800000000,NULL)"
            )
            _ledger(conn, rep_id=None, merchant="root", scope="legacy", amount=100,
                    code="ROOT-LEGACY", email="root@safe.invalid")
            _ledger(conn, rep_id=202, merchant="rep:202", scope="merchant", amount=200,
                    code="REP-A", email="a@safe.invalid")
            _ledger(conn, rep_id=204, merchant="rep:202", scope="edge", edge_id=602, amount=300,
                    code="CHILD-A", email="child-a@safe.invalid")
            _ledger(conn, rep_id=202, merchant="root", scope="legacy", amount=50,
                    code="A-LEGACY", email="a-legacy@safe.invalid")
            _ledger(conn, rep_id=205, merchant="rep:205", scope="merchant", amount=900,
                    code="B-SECRET-ACCOUNT", email="b-secret@unsafe.invalid")
            _ledger(conn, rep_id=202, merchant="rep:202", scope="merchant", amount=250,
                    tx="opening", code="OPEN-A", email="opening@safe.invalid",
                    business_key="opening:rep:202")

        # Root-wide access is explicit; count and page count use one predicate.
        root_page, root_total, root_pages = database.get_financial_ledger_paginated(
            scope="root", page=1, page_size=2,
        )
        assert root_total == 6
        assert root_pages == 3
        assert any(row["account_code"] == "ROOT-LEGACY" for row in
                   database.get_financial_ledger_paginated(scope="root")[0])

        rep_page, rep_total, rep_pages = database.get_financial_ledger_paginated(
            rep_id=202, scope="rep", page=1, page_size=1,
        )
        assert rep_total == 4
        assert rep_pages == 4
        rows, total, pages = database.get_financial_ledger_paginated(
            rep_id=202, scope="rep", page=1, page_size=100,
        )
        serialized = json.dumps(rows, default=str)
        assert "B-SECRET-ACCOUNT" not in serialized
        assert "b-secret@unsafe.invalid" not in serialized
        assert {row["account_code"] for row in rows} == {"REP-A", "CHILD-A", "A-LEGACY", "OPEN-A"}
        opening = next(row for row in rows if row["account_code"] == "OPEN-A")
        assert opening["is_opening_anchor"] == 1
        assert opening["sales_amount"] == 0
        assert total == rep_total and pages == 1

        edge_rows, edge_total, edge_pages = database.get_financial_ledger_paginated(
            scope="edge", merchant_key="rep:202", edge_id=602,
        )
        assert edge_total == 1 and edge_pages == 1
        assert [row["account_code"] for row in edge_rows] == ["CHILD-A"]

        # The historical no-selector call remains root compatibility; new
        # child-edge readers still require a merchant owner and fail closed.
        legacy_root, legacy_total, _legacy_pages = database.get_financial_ledger_paginated()
        assert legacy_total == root_total
        with pytest.raises(ValueError, match="owning merchant_key"):
            database.get_financial_ledger_paginated(scope="edge", edge_id=602)
        with pytest.raises(PermissionError, match="another merchant"):
            database.get_financial_ledger_paginated(
                rep_id=202, scope="rep", merchant_key="rep:205",
            )
        assert Path(sentinel) != Path(database.DB_PATH)
    assert attempts == []
