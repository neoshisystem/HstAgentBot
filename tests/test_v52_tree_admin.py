"""Offline acceptance for representative soft-delete and manual tier administration."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

from migrations_runner import apply_migrations
from policy_registry import seed_tier_defaults


def _load_helpers():
    path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w05_tree_admin_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_tree(root, monkeypatch):
    spec = importlib.util.spec_from_file_location("rep_tree_service", root / "rep_tree_service.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "rep_tree_service", module)
    spec.loader.exec_module(module)
    return module


def test_soft_delete_and_manual_tier(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _load_helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    config = sys.modules["config"]
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9091)
    tree = _load_tree(root, monkeypatch)
    root_actor = {"actor_tg_id": 9091, "is_blocked": False, "rep_id": None}
    non_root_actor = {"actor_tg_id": 9092, "is_blocked": False, "rep_id": 202}

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w05-tree-admin.sqlite3"):
        with database.get_db() as conn:
            assert apply_migrations(conn, root / "migrations", now=1794237000)
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,"
                "parent_rep_id,can_create_sub_reps,max_sub_reps) "
                "VALUES(201,'TREE-PARENT','Parent',9201,'',NULL,1,2)"
            )
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,"
                "parent_rep_id) VALUES(202,'TREE-CHILD','Child',9202,'',201)"
            )
            conn.execute(
                "INSERT INTO rep_edges(parent_key,child_rep_id,billing_mode,debt_toman,"
                "credit_limit_toman,active,started_at) VALUES('root',201,'credit',0,0,1,1)"
            )
            conn.execute(
                "INSERT INTO rep_edges(parent_key,child_rep_id,billing_mode,debt_toman,"
                "credit_limit_toman,active,started_at) VALUES('rep:201',202,'credit',900,1000,1,2)"
            )
            seed_tier_defaults(conn)

        with database.get_db() as conn:
            before_ledger = int(conn.execute("SELECT COUNT(*) FROM financial_ledger").fetchone()[0])
        deleted = tree.soft_delete_rep(root_actor, 201, now=2000)
        assert deleted["is_active"] is False
        assert deleted["deleted_at"] == 2000
        assert deleted["blocked_new_operations"] is True
        assert deleted["preserved_children"] == 1
        assert deleted["preserved_active_edges"] == 1
        assert deleted["unresolved_financial"] == {
            "edge_debt_toman": 900,
            "active_hold_count": 0,
            "unknown_operation_count": 0,
            "requires_reconciliation": True,
        }

        with database.get_db() as conn:
            parent = conn.execute(
                "SELECT is_active,deleted_at FROM representatives WHERE id=201"
            ).fetchone()
            child = conn.execute(
                "SELECT parent_rep_id,is_active FROM representatives WHERE id=202"
            ).fetchone()
            edge = conn.execute(
                "SELECT active,debt_toman FROM rep_edges WHERE child_rep_id=202"
            ).fetchone()
            after_ledger = int(conn.execute("SELECT COUNT(*) FROM financial_ledger").fetchone()[0])
            assert tuple(parent) == (0, 2000)
            assert tuple(child) == (201, 1)
            assert tuple(edge) == (1, 900)
            assert after_ledger == before_ledger

        with pytest.raises(tree.RootApprovalRequired):
            tree.set_actor_tier(non_root_actor, 202, "vip", now=2100)
        with pytest.raises(ValueError, match="tier_policies"):
            tree.set_actor_tier(root_actor, 202, "unknown-tier", now=2100)
        changed = tree.set_actor_tier(root_actor, 202, "vip", now=2200)
        assert changed == {
            "rep_id": 202, "previous_tier": "normal", "tier": "vip", "changed": True,
        }
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT tier FROM representatives WHERE id=202"
            ).fetchone()[0] == "vip"
            audit = conn.execute(
                "SELECT action,actor_id FROM audit_logs "
                "WHERE rep_id=202 AND action='representative_tier_changed'"
            ).fetchone()
            assert tuple(audit) == ("representative_tier_changed", 9091)

        with pytest.raises(tree.RootApprovalRequired):
            tree.soft_delete_rep(non_root_actor, 202, now=2300)
        assert network_attempts == []
        assert not Path(sentinel).exists()
