"""W18.12: sync reads through each panel adapter before its short DB transaction."""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys

from migrations_runner import apply_migrations


def _load(path, name, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


class _FakeAdapter:
    def __init__(self, clients, active_connections):
        self.clients = clients
        self.active_connections = active_connections
        self.calls = 0

    def list_clients(self):
        self.calls += 1
        assert not any(conn.in_transaction for conn in self.active_connections)
        return self.clients


def test_database_no_panel_url_or_network_in_tx(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _load(Path(__file__).with_name("v52_helpers.py"), "_w18_db_helpers", monkeypatch)
    helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w18-db-sync.sqlite3"):
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=1_794_500_000)) == 18
            conn.execute(
                "INSERT INTO panels(id,panel_code,name,panel_type) VALUES"
                "(1,'W18-PANEL-1','Panel one','3x-ui'),"
                "(2,'W18-PANEL-2','Panel two','3x-ui')"
            )
        helpers.seed_actor(database, actor_tg_id=7101, rep_id=202, rep_tg_id=7202)
        with database.get_db() as conn:
            conn.execute("UPDATE representatives SET scope_filter='all' WHERE id=202")
        with database.get_db() as conn:
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,is_active) "
                "VALUES(203,'W18-REP-203','Second representative',7203,'all',1)"
            )
        helpers.seed_account(
            database, account_id=301, rep_id=202, owner_tg_id=7101,
            account_code="W18-PANEL-1", email="shared@example.invalid",
        )
        helpers.seed_account(
            database, account_id=302, rep_id=203, owner_tg_id=7101,
            account_code="W18-PANEL-2", email="shared@example.invalid",
        )
        with database.get_db() as conn:
            conn.execute("UPDATE accounts SET panel_id=1 WHERE id=301")
            conn.execute("UPDATE accounts SET panel_id=2 WHERE id=302")

        active_connections = []
        real_get_db = database.get_db

        @contextmanager
        def tracked_get_db():
            with real_get_db() as conn:
                active_connections.append(conn)
                try:
                    yield conn
                finally:
                    active_connections.remove(conn)

        monkeypatch.setattr(database, "get_db", tracked_get_db)
        adapters = {
            1: _FakeAdapter([{
                "ref": (301, 1, "shared@example.invalid"), "enabled": True,
                "quota_mode": "finite", "quota_bytes": 5 * 1024**3, "used_bytes": 0,
                "time_mode": "dated", "expires_at": 1_795_000_000,
                "subscription_id": "panel-one-sub",
            }], active_connections),
            2: _FakeAdapter([{
                "ref": (302, 2, "shared@example.invalid"), "enabled": True,
                "quota_mode": "finite", "quota_bytes": 20 * 1024**3, "used_bytes": 0,
                "time_mode": "dated", "expires_at": 1_796_000_000,
                "subscription_id": "panel-two-sub",
            }], active_connections),
        }
        monkeypatch.setattr(database, "_get_panel_adapter", lambda panel_id: adapters[int(panel_id)])

        result = database.sync_all_active_reps_detailed()
        assert result["status"] == "success"
        assert result["count"] == 2
        assert adapters[1].calls == adapters[2].calls == 1

        with real_get_db() as conn:
            rows = conn.execute(
                "SELECT id,panel_id,email,total_gb,sub_id FROM accounts "
                "WHERE email='shared@example.invalid' ORDER BY panel_id"
            ).fetchall()
            assert [(int(row["id"]), int(row["panel_id"])) for row in rows] == [(301, 1), (302, 2)]
            assert [float(row["total_gb"]) for row in rows] == [5.0, 20.0]
            assert [row["sub_id"] for row in rows] == ["panel-one-sub", "panel-two-sub"]
            assert conn.execute(
                "SELECT rep_id FROM accounts WHERE id=301"
            ).fetchone()[0] == 202
            assert conn.execute(
                "SELECT rep_id FROM accounts WHERE id=302"
            ).fetchone()[0] == 203

        # The representative-scoped wrapper uses the same adapter-before-transaction
        # bridge and retains separate panel data for duplicate legacy email values.
        assert database.sync_rep_accounts("V52-REP-202") == 2
        assert adapters[1].calls == adapters[2].calls == 2
        with real_get_db() as conn:
            rows = conn.execute(
                "SELECT panel_id,total_gb,sub_id FROM accounts "
                "WHERE email='shared@example.invalid' ORDER BY panel_id"
            ).fetchall()
            assert [(int(row["panel_id"]), float(row["total_gb"]), row["sub_id"]) for row in rows] == [
                (1, 5.0, "panel-one-sub"), (2, 20.0, "panel-two-sub")
            ]

        source = (root / "database.py").read_text(encoding="utf-8")
        assert "xui_manager.BASE_URL" not in source
        assert "xui_manager.get_session" not in source
        assert not Path(sentinel).exists()
