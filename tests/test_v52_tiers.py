"""Offline acceptance for Tehran test quota and first-purchase history."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import importlib.util
import json

import pytest

from migrations_runner import apply_migrations
from policy_registry import seed_tier_defaults
from tier_service import (
    DailyTestQuotaExceeded,
    finish_test_quota,
    is_first_purchase,
    reserve_test_quota,
)


def _load_helpers():
    helper_path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w06_tier_helpers", helper_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _insert_order(conn, *, actor_tg_id, order_id, quote_id, merchant_key,
                  spec_json, purpose="vpn_buy", status="completed", created_at=1000):
    conn.execute(
        "INSERT INTO quotes(quote_id,actor_tg_id,bot_id,merchant_key,purpose,account_id,"
        "plan_code,product_id,panel_id,amount_toman,spec_json,price_edges_json,"
        "policy_version,created_at,expires_at,fingerprint) "
        "VALUES(?,?,?,?,?,NULL,'synthetic-plan',NULL,NULL,900,?,'[]','v1',?,?,?)",
        (quote_id, actor_tg_id, 9001, merchant_key, purpose,
         spec_json, created_at, created_at + 1800, f"fingerprint-{quote_id}"),
    )
    conn.execute(
        "INSERT INTO payment_orders(order_id,quote_id,actor_tg_id,bot_id,merchant_key,purpose,"
        "payment_rail,amount_toman,status,received_toman,created_at,expires_at) "
        "VALUES(?,?,?,?,?,?, 'exempt',900,?,0,?,?)",
        (order_id, quote_id, actor_tg_id, 9001, merchant_key, purpose,
         status, created_at, created_at + 1800),
    )


def test_tehran_boundary_and_first_purchase(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _load_helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w06-tiers.sqlite3"):
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=1794237000)) == 18
            seed_tier_defaults(conn)
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision) "
                "VALUES(9001,NULL,1,NULL,'synthetic-bot','active',1794237000,1)"
            )
        helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
        helpers.seed_actor(database, actor_tg_id=104, rep_id=204, rep_tg_id=304)

        # 23:59 Tehran belongs to the old quota date; 00:00 belongs to the next.
        at_2359 = int(datetime(2026, 11, 10, 23, 59, tzinfo=ZoneInfo("Asia/Tehran")).timestamp())
        at_midnight = at_2359 + 60
        with database.get_db() as conn:
            assert is_first_purchase(conn, 101) is True
            _insert_order(
                conn, actor_tg_id=101, order_id="test-order", quote_id="test-quote",
                merchant_key="root", spec_json=json.dumps({"is_test": True}),
                created_at=at_2359,
            )
            # A completed test order does not consume first-purchase eligibility.
            assert is_first_purchase(conn, 101) is True

            _insert_order(
                conn, actor_tg_id=101, order_id="other-merchant-order",
                quote_id="other-merchant-quote", merchant_key="rep:999",
                spec_json=json.dumps({"is_test": False}), created_at=at_2359,
            )
            # Merchant scoping is deliberately absent: another merchant's
            # completed non-test order is prior purchase evidence.
            assert is_first_purchase(conn, 101) is False

            conn.execute(
                "INSERT INTO bot_users(telegram_id,role,rep_id,tier,is_blocked) "
                "VALUES(105,'end_user',NULL,'normal',0)"
            )
            _insert_order(
                conn, actor_tg_id=105, order_id="unknown-history-order",
                quote_id="unknown-history-quote", merchant_key="root",
                spec_json="{malformed-json", created_at=at_2359,
            )
            assert is_first_purchase(conn, 105) is False

            conn.execute(
                "INSERT INTO financial_ledger(rep_id,user_id,transaction_type,amount,balance_after,"
                "description,actor_id,actor_name) "
                "VALUES(204,NULL,'debit',900,900,'legacy purchase evidence',304,'synthetic rep')"
            )
            assert is_first_purchase(conn, 304) is False

        # Every service command owns and commits its own short transaction.
        with database.get_db() as conn:
            failed_ticket = reserve_test_quota(conn, 101, now=at_2359)
        with database.get_db() as conn:
            released = finish_test_quota(conn, failed_ticket, outcome="definite_failure")
            assert released["released"] is True
            assert finish_test_quota(conn, failed_ticket, outcome="definite_failure") is False
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT used FROM daily_test_usage WHERE actor_tg_id=101 AND tehran_date='2026-11-10'"
            ).fetchone()[0] == 0

        with database.get_db() as conn:
            unknown_ticket = reserve_test_quota(conn, 101, now=at_2359 + 1)
        with database.get_db() as conn:
            with pytest.raises(DailyTestQuotaExceeded):
                reserve_test_quota(conn, 101, now=at_2359 + 2)
        with database.get_db() as conn:
            unknown = finish_test_quota(conn, unknown_ticket, outcome="unknown")
            assert unknown["quota_retained"] is True
            assert conn.execute(
                "SELECT used FROM daily_test_usage WHERE actor_tg_id=101 AND tehran_date='2026-11-10'"
            ).fetchone()[0] == 1
        with database.get_db() as conn:
            with pytest.raises(DailyTestQuotaExceeded):
                reserve_test_quota(conn, 101, now=at_2359 + 3)

        # The next Tehran calendar date starts with a fresh quota.
        with database.get_db() as conn:
            midnight_ticket = reserve_test_quota(conn, 101, now=at_midnight)
        with database.get_db() as conn:
            assert finish_test_quota(conn, midnight_ticket, outcome="success")["state"] == "completed"

        # VIP quota is read from the actor's tier policy, not a caller value.
        with database.get_db() as conn:
            conn.execute("UPDATE bot_users SET tier='vip' WHERE telegram_id=104")
            conn.execute(
                "UPDATE tier_policies SET daily_test_quota=2 "
                "WHERE actor_type='end_user' AND tier_code='vip'"
            )
        with database.get_db() as conn:
            vip_one = reserve_test_quota(conn, 104, now=at_midnight)
        with database.get_db() as conn:
            vip_two = reserve_test_quota(conn, 104, now=at_midnight + 1)
        with database.get_db() as conn:
            with pytest.raises(DailyTestQuotaExceeded):
                reserve_test_quota(conn, 104, now=at_midnight + 2)
        with database.get_db() as conn:
            assert finish_test_quota(conn, vip_one, outcome="success")["state"] == "completed"
        with database.get_db() as conn:
            assert finish_test_quota(conn, vip_two, outcome="success")["state"] == "completed"

        assert network_attempts == []
        assert not Path(sentinel).exists()
