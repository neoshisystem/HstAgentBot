"""Offline acceptance for the three-level representative tree."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from threading import Barrier, Thread

import pytest

from migrations_runner import apply_migrations


def _load_helpers():
    path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w05_tree_helpers", path)
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


def test_cycle_depth_quota_atomic(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _load_helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    config = sys.modules["config"]
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9001)
    tree = _load_tree(root, monkeypatch)
    root_actor = {"actor_tg_id": 9001, "is_blocked": False, "rep_id": None}

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w05-tree.sqlite3"):
        with database.get_db() as conn:
            assert apply_migrations(conn, root / "migrations", now=1794237000)
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter) "
                "VALUES(1,'root_admin_rep','Root identity',9001,'')"
            )

        first = tree.create_sub_rep(
            root_actor, None, rep_code="TREE-A", name="A", telegram_id=101,
            can_create_sub_reps=True, max_sub_reps=2, now=1000,
        )
        second = tree.create_sub_rep(
            root_actor, first["rep_id"], rep_code="TREE-B", name="B", telegram_id=102,
            can_create_sub_reps=True, max_sub_reps=1, now=1001,
        )
        third = tree.create_sub_rep(
            root_actor, second["rep_id"], rep_code="TREE-C", name="C", telegram_id=103,
            now=1002,
        )
        assert (first["depth"], second["depth"], third["depth"]) == (1, 2, 3)
        assert first["parent_key"] == "root"
        assert second["parent_key"] == f"rep:{first['rep_id']}"
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT active,parent_key FROM rep_edges WHERE child_rep_id=?",
                (third["rep_id"],),
            ).fetchone()[:] == (1, f"rep:{second['rep_id']}")
            with pytest.raises(tree.TreeValidationError):
                tree.validate_parent_change(conn, third["rep_id"])
            with pytest.raises(tree.TreeValidationError, match="own parent"):
                tree.validate_parent_change(conn, first["rep_id"], first["rep_id"])
            with pytest.raises(tree.TreeValidationError, match="cycle"):
                tree.validate_parent_change(conn, third["rep_id"], first["rep_id"])

        with pytest.raises(tree.RepQuotaExceeded):
            tree.create_sub_rep(
                root_actor, second["rep_id"], rep_code="TREE-C2", name="C2",
                telegram_id=104, now=1003,
            )
        with pytest.raises(tree.TreeValidationError, match="parent_shared"):
            tree.create_sub_rep(
                root_actor, first["rep_id"], rep_code="TREE-SHARED", name="Shared",
                telegram_id=105, billing_mode="parent_shared", now=1004,
            )

        quota_parent = tree.create_sub_rep(
            root_actor, None, rep_code="TREE-Q", name="Quota", telegram_id=200,
            can_create_sub_reps=True, max_sub_reps=1, now=1010,
        )
        barrier = Barrier(2)
        outcomes = []

        def worker(index):
            barrier.wait()
            try:
                result = tree.create_sub_rep(
                    root_actor, quota_parent["rep_id"],
                    rep_code=f"TREE-Q{index}", name=f"Quota child {index}",
                    telegram_id=200 + index, now=1020 + index,
                )
                outcomes.append(("created", result["rep_id"]))
            except tree.RepQuotaExceeded:
                outcomes.append(("quota", None))

        workers = [Thread(target=worker, args=(i,)) for i in (1, 2)]
        for worker_thread in workers:
            worker_thread.start()
        for worker_thread in workers:
            worker_thread.join(timeout=5)
        assert all(not worker_thread.is_alive() for worker_thread in workers)
        assert sorted(item[0] for item in outcomes) == ["created", "quota"]
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM representatives WHERE parent_rep_id=? "
                "AND is_active=1 AND deleted_at IS NULL",
                (quota_parent["rep_id"],),
            ).fetchone()[0] == 1

        zero_parent = tree.create_sub_rep(
            root_actor, None, rep_code="TREE-ZERO", name="Zero quota",
            telegram_id=300, can_create_sub_reps=True, max_sub_reps=0, now=1030,
        )
        with pytest.raises(tree.RepQuotaExceeded):
            tree.create_sub_rep(
                root_actor, zero_parent["rep_id"], rep_code="TREE-ZERO-CHILD",
                name="Must be rejected", telegram_id=301, now=1031,
            )

        assert network_attempts == []
        assert not Path(sentinel).exists()
