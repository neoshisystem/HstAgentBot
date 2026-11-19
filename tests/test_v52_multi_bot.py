"""Offline independent-Application lifecycle test for W10.3."""
from __future__ import annotations
import asyncio
import base64
import sqlite3
from crypto_store import encrypt_secret
from multi_bot_runtime import run_registered_bots, stop_registered_bots


class FakeUpdater:
    def __init__(self, fail=False):
        self.fail = fail
        self.polling = False
        self.drop_pending_updates = None
        self.stop_calls = 0

    async def start_polling(self, *, drop_pending_updates):
        self.drop_pending_updates = drop_pending_updates
        if self.fail:
            raise RuntimeError("synthetic polling failure")
        self.polling = True

    async def stop(self):
        self.stop_calls += 1
        self.polling = False


class FakeApplication:
    def __init__(self, token):
        self.token = token
        self.bot_data = {}
        self.user_data = {}
        self.handlers = []
        self.updater = FakeUpdater(fail=(token == "V52_TOKEN_FAIL"))
        self.initialize_calls = self.start_calls = 0
        self.stop_calls = self.shutdown_calls = 0

    async def initialize(self):
        self.initialize_calls += 1

    async def start(self):
        self.start_calls += 1

    def add_handler(self, handler):
        self.handlers.append(handler)

    async def stop(self):
        self.stop_calls += 1

    async def shutdown(self):
        self.shutdown_calls += 1


def test_independent_apps_single_scheduler(monkeypatch):
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="))
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE bot_registry(bot_id INTEGER PRIMARY KEY,token_cipher TEXT,status TEXT NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO bot_registry(bot_id,token_cipher,status) VALUES(?,?, 'active')",
        [(9001,encrypt_secret("V52_TOKEN_OK","hstagent/columns/v1")),
         (9002,encrypt_secret("V52_TOKEN_FAIL","hstagent/columns/v1"))],
    )
    db_singleton, scheduler_singleton = object(), object()
    created = []

    def app_factory(token):
        app = FakeApplication(token)
        created.append(app)
        return app

    def handlers(app, bot_id):
        assert app.bot_data["bot_id"] == bot_id
        return [("start",bot_id),("callback",bot_id)]

    runtime = asyncio.run(run_registered_bots(
        conn, database=db_singleton, scheduler=scheduler_singleton,
        handler_factory=handlers, application_factory=app_factory))
    assert runtime["started_bot_ids"] == [9001]
    assert runtime["failed_bot_ids"] == [9002]
    assert runtime["failures"] == {9002:"RuntimeError"}
    assert len(created) == 2
    good, failed = created
    for app in (good,failed):
        assert app.bot_data["database"] is db_singleton
        assert app.bot_data["scheduler"] is scheduler_singleton
        assert len(app.handlers) == 2
        assert app.updater.drop_pending_updates is False
    assert good.user_data is not failed.user_data
    assert good.initialize_calls == good.start_calls == 1
    assert failed.initialize_calls == failed.start_calls == 1
    assert failed.stop_calls == failed.shutdown_calls == 1

    replay = asyncio.run(run_registered_bots(
        conn, database=db_singleton, scheduler=scheduler_singleton,
        handler_factory=handlers, application_factory=app_factory))
    assert replay["applications"][9001] is good
    assert good.initialize_calls == good.start_calls == 1

    first_stop = asyncio.run(stop_registered_bots(replay))
    second_stop = asyncio.run(stop_registered_bots(replay))
    assert first_stop["stopped_bot_ids"] == second_stop["stopped_bot_ids"] == [9001]
    assert first_stop["replayed"] is False and second_stop["replayed"] is True
    assert good.updater.stop_calls == good.stop_calls == good.shutdown_calls == 1
    assert replay["stopped"] is True
    conn.close()
