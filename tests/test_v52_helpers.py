"""Acceptance test for W01.1 isolated helpers and C06 invariants."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import sqlite3


def _load_helpers():
    helper_path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_hstagentbot_v52_helpers_w01_1", helper_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_never_uses_production_db(tmp_path, monkeypatch):
    helpers = _load_helpers()
    network_attempts = helpers.guard_network(monkeypatch)

    product_root = Path(__file__).resolve().parents[1]
    database, original_db_path = helpers.load_database_module(
        product_root, monkeypatch, tmp_path
    )
    original_time_module = database.time
    assert database.DB_PATH == original_db_path
    production_path = Path(original_db_path).resolve()
    assert not production_path.exists()

    real_connect = sqlite3.connect
    production_open_attempts = []

    def guarded_connect(database_path, *args, **kwargs):
        candidate = Path(str(database_path)).expanduser().resolve()
        if candidate == production_path:
            production_open_attempts.append(str(candidate))
            raise AssertionError("production DB must never be opened")
        return real_connect(database_path, *args, **kwargs)

    monkeypatch.setattr(database.sqlite3, "connect", guarded_connect)

    def row_counts():
        with database.get_db() as conn:
            return {
                "representatives": conn.execute(
                    "SELECT COUNT(*) FROM representatives"
                ).fetchone()[0],
                "bot_users": conn.execute(
                    "SELECT COUNT(*) FROM bot_users"
                ).fetchone()[0],
                "user_wallets": conn.execute(
                    "SELECT COUNT(*) FROM user_wallets"
                ).fetchone()[0],
                "accounts": conn.execute(
                    "SELECT COUNT(*) FROM accounts"
                ).fetchone()[0],
                "financial_ledger": conn.execute(
                    "SELECT COUNT(*) FROM financial_ledger"
                ).fetchone()[0],
            }

    clock = helpers.FakeClock(start=1_700_000_000.0)
    with helpers.make_db(database, monkeypatch, tmp_path, clock=clock) as isolated_db:
        assert Path(database.DB_PATH).resolve() == isolated_db.resolve()
        assert isolated_db.is_file()
        assert database.time.time() == 1_700_000_000.0
        assert clock.advance(30) == 1_700_000_030.0
        assert database.time.time() == 1_700_000_030.0

        before = row_counts()
        assert before == {
            "representatives": 0,
            "bot_users": 0,
            "user_wallets": 0,
            "accounts": 0,
            "financial_ledger": 0,
        }

        panel = helpers.seed_panel(panel_id=101)
        actor = helpers.seed_actor(
            database, actor_tg_id=101, rep_id=202, rep_tg_id=303,
            wallet_balance=0,
        )
        bot = helpers.seed_bot(bot_id=303)
        account = helpers.seed_account(
            database, account_id=303, rep_id=202, owner_tg_id=101,
        )

        assert panel.panel_id == 101
        assert panel.base_url.endswith(".invalid")
        assert panel.api_secret == "V52_FAKE_PANEL_SECRET_101"
        assert actor["actor_tg_id"] == 101 and actor["rep_id"] == 202
        assert actor["rep_tg_id"] == 303 and actor["wallet_user_id"] == 101
        assert bot["bot_id"] == 303
        assert bot["telegram"].calls == []
        assert account["account_id"] == 303
        assert account["sub_id"] == "V52_FAKE_SUBSCRIPTION_MARKER"

        after_seeding = row_counts()
        assert after_seeding == {
            "representatives": 1,
            "bot_users": 1,
            "user_wallets": 1,
            "accounts": 1,
            "financial_ledger": 0,
        }
        assert after_seeding["financial_ledger"] == before["financial_ledger"]

        with database.get_db() as conn:
            assert conn.execute(
                "SELECT balance_toman FROM user_wallets WHERE user_id = ?", (101,)
            ).fetchone()["balance_toman"] == 0
            assert conn.execute(
                "SELECT rep_id FROM accounts WHERE id = ?", (303,)
            ).fetchone()["rep_id"] == 202

        injected_result = {"state": "success", "evidence": "V52_FAKE_RESULT_MARKER"}
        adapter = helpers.FakeAdapter(result=injected_result)
        mutation_count_before = len(adapter.mutation_calls)
        ledger_before_adapter = row_counts()["financial_ledger"]
        assert adapter.result is injected_result
        assert mutation_count_before == 0
        assert adapter.mutation_calls == []

        assert adapter.create_client(
            {"email": "fixture@example.invalid"}
        ) is injected_result
        assert len(adapter.mutation_calls) == mutation_count_before + 1
        assert adapter.mutation_calls[0]["method"] == "create_client"
        assert row_counts()["financial_ledger"] == ledger_before_adapter == 0

        telegram = bot["telegram"]
        telegram.result = {"message_id": 1, "marker": "V52_FAKE_TELEGRAM_RESULT"}
        assert telegram.send_message(101, "synthetic") == telegram.result
        assert len(telegram.calls) == 1
        assert telegram.calls[0]["method"] == "send_message"
        assert network_attempts == []

    assert database.DB_PATH == original_db_path
    assert database.time is original_time_module
    assert not production_path.exists()
    assert production_open_attempts == []
