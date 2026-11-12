"""W18.9: onboarding is idempotent and the registered applications share one scheduler."""
from __future__ import annotations

import asyncio
import base64
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace

from crypto_store import encrypt_secret
from migrations_runner import apply_migrations
import multi_bot_runtime
import rep_bot


def _load(path, name, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


class _Updater:
    def __init__(self):
        self.drop_pending_updates = None

    async def start_polling(self, *, drop_pending_updates):
        self.drop_pending_updates = drop_pending_updates

    async def stop(self):
        return None


class _Application:
    def __init__(self, token):
        self.token = token
        self.bot_data = {}
        self.user_data = {}
        self.handlers = []
        self.updater = _Updater()

    def add_handler(self, handler):
        self.handlers.append(handler)

    async def initialize(self):
        return None

    async def start(self):
        return None

    async def stop(self):
        return None

    async def shutdown(self):
        return None


def test_onboarding_and_single_scheduler(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helper = _load(Path(__file__).with_name("v52_helpers.py"),
                   "_w18_start_helpers", monkeypatch)
    helper.guard_network(monkeypatch)
    database, sentinel = helper.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    config = sys.modules["config"]
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 7001)
    monkeypatch.setattr(rep_bot, "ADMIN_TELEGRAM_ID", 7001)
    monkeypatch.setattr(rep_bot, "database", database)
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    identity = _load(root / "identity_service.py", "_w18_identity_service", monkeypatch)
    subscription = _load(root / "subscription_service.py", "_w18_subscription_service", monkeypatch)
    monkeypatch.setitem(sys.modules, "identity_service", identity)
    monkeypatch.setitem(sys.modules, "subscription_service", subscription)

    with helper.make_db(database, monkeypatch, tmp_path, filename="w18-start.sqlite3"):
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=1_794_500_000)) == 18
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision) "
                "VALUES(9001,NULL,1,?,'w18-primary','active',1,1)",
                (encrypt_secret("W18_PRIMARY_TOKEN", "hstagent/columns/v1"),),
            )
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,is_active) "
                "VALUES(202,'W18-REP','W18 representative',7202,'all',1)"
            )
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision) "
                "VALUES(9002,202,0,?,'w18-rep','active',1,1)",
                (encrypt_secret("W18_REP_TOKEN", "hstagent/columns/v1"),),
            )

        ordinary = SimpleNamespace(id=7101, username="ordinary", full_name="Ordinary")
        assert rep_bot._onboard_v52_start(ordinary, 9001) == ("end_user", None)
        assert rep_bot._onboard_v52_start(ordinary, 9001) == ("end_user", None)
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM subscription_profiles WHERE owner_tg_id=7101 AND is_default=1"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT balance_toman FROM user_wallets WHERE user_id=7101"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM bot_customers WHERE bot_id=9001 AND user_tg_id=7101"
            ).fetchone()[0] == 1
            conn.execute(
                "UPDATE user_settings SET active_persona='admin' WHERE user_id=7101"
            )
        assert rep_bot._onboard_v52_start(ordinary, 9001) == ("end_user", None)
        assert rep_bot._onboard_v52_start(ordinary, 9002) == ("end_user", None)
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM bot_customers WHERE user_tg_id=7101"
            ).fetchone()[0] == 2

        root_user = SimpleNamespace(id=7001, username="root", full_name="Root")
        assert rep_bot._onboard_v52_start(root_user, 9001) == ("super_admin", None)
        with database.get_db() as conn:
            conn.execute(
                "UPDATE user_settings SET active_persona='rep' WHERE user_id=7001"
            )
        root_rep_role, root_rep = rep_bot._onboard_v52_start(root_user, 9001)
        assert root_rep_role == "rep"
        assert root_rep["rep_code"] == "root_admin_rep"
        with database.get_db() as conn:
            conn.execute(
                "UPDATE user_settings SET active_persona='end_user' WHERE user_id=7001"
            )
        assert rep_bot._onboard_v52_start(root_user, 9001) == ("end_user", None)

        with database.get_db() as conn:
            scheduler = object()
            runtime = asyncio.run(multi_bot_runtime.run_registered_bots(
                conn, database=database, scheduler=scheduler,
                handler_factory=lambda _app, bot_id: [("handler", bot_id)],
                application_factory=_Application,
            ))
            assert runtime["started_bot_ids"] == [9001, 9002]
            apps = runtime["applications"]
            assert apps[9001].bot_data["scheduler"] is scheduler
            assert apps[9002].bot_data["scheduler"] is scheduler
            assert apps[9001].updater.drop_pending_updates is False
            asyncio.run(multi_bot_runtime.stop_registered_bots(runtime))

        assert "run_registered_bots" in __import__("inspect").getsource(rep_bot._run_multi_bot_runtime)
        assert __import__("inspect").getsource(rep_bot._run_multi_bot_runtime).count(
            "watchdog_worker(primary_app)"
        ) == 1
        assert not Path(sentinel).exists()
