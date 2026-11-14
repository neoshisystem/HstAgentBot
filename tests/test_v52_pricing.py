"""Offline acceptance for v5.2 per-edge pricing."""
from __future__ import annotations

from pathlib import Path
import importlib.util

import pytest

from migrations_runner import apply_migrations
from policy_registry import seed_tier_defaults
from pricing_service import PricingError, price_chain, round_toman


def _load_helpers():
    helper_path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w07_pricing_helpers", helper_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_edge_price_table(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _load_helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w07-pricing.sqlite3"):
        # Seed baseline identities before applying versioned financial migrations.
        helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=1794237000)) == 18
            seed_tier_defaults(conn)
            conn.execute(
                "UPDATE pricing_plans SET rep_price=100000,user_price=120000,"
                "revision=3,is_active=1,sale_status='active' WHERE plan_code='20gb_30d'"
            )
            conn.execute(
                "UPDATE tier_policies SET discount_bps=1000 "
                "WHERE actor_type='rep' AND tier_code='vip'"
            )
            conn.execute(
                "UPDATE tier_policies SET discount_bps=500 "
                "WHERE actor_type='end_user' AND tier_code='gold'"
            )
            conn.execute(
                "UPDATE representatives SET parent_rep_id=NULL,tier='vip',markup_bps=1000 "
                "WHERE id=202"
            )
            conn.execute(
                "INSERT INTO representatives("
                "id,rep_code,name,telegram_id,group_id,topic_id,scope_filter,"
                "parent_rep_id,tier,billing_mode,markup_bps,is_active,deleted_at"
                ") VALUES(204,'V52-REP-204','W07 child',404,0,0,'w07:child',202,"
                "'normal','credit',0,1,NULL)"
            )
            conn.execute(
                "INSERT INTO rep_edges("
                "id,parent_key,child_rep_id,billing_mode,debt_toman,credit_limit_toman,"
                "active,started_at,ended_at"
                ") VALUES(601,'root',202,'credit',0,0,1,1794237000,NULL)"
            )
            conn.execute(
                "INSERT INTO rep_edges("
                "id,parent_key,child_rep_id,billing_mode,debt_toman,credit_limit_toman,"
                "active,started_at,ended_at"
                ") VALUES(602,'rep:202',204,'credit',0,0,1,1794237000,NULL)"
            )

            root_to_rep = price_chain(
                conn, plan_code="20gb_30d", seller_key="root", buyer_rep_id=202
            )
            assert root_to_rep["amount_toman"] == 90000
            assert root_to_rep["price_edges"][0]["discount_bps"] == 1000

            parent_to_child = price_chain(
                conn, plan_code="20gb_30d", seller_key="rep:202", buyer_rep_id=204
            )
            assert parent_to_child["amount_toman"] == 99000
            assert len(parent_to_child["price_edges"]) == 2
            assert parent_to_child["price_edges"][-1]["markup_bps"] == 1000

            conn.execute(
                "INSERT INTO rep_custom_prices("
                "seller_key,buyer_rep_id,plan_code,price_toman,revision"
                ") VALUES('rep:202',204,'20gb_30d',95000,7)"
            )
            custom = price_chain(
                conn, plan_code="20gb_30d", seller_key="rep:202", buyer_rep_id=204
            )
            assert custom["amount_toman"] == 95000
            assert custom["price_edges"][-1]["custom"] is True
            assert custom["price_edges"][-1]["revision"] == 7

            rep_to_user = price_chain(
                conn, plan_code="20gb_30d", seller_key="rep:202", buyer_tg_id=101
            )
            assert rep_to_user["amount_toman"] == 99000

            conn.execute("UPDATE bot_users SET tier='gold' WHERE telegram_id=101")
            root_to_user = price_chain(
                conn, plan_code="20gb_30d", seller_key="root", buyer_tg_id=101
            )
            assert root_to_user["amount_toman"] == 114000

            root_personal = price_chain(
                conn, plan_code="20gb_30d", seller_key="root",
                buyer_rep_id=202, purpose="personal_use",
            )
            assert root_personal["amount_toman"] == 0
            rep_personal = price_chain(
                conn, plan_code="20gb_30d", seller_key="rep:202",
                buyer_tg_id=101, purpose="personal_use",
            )
            assert rep_personal["amount_toman"] > 0

            conn.execute(
                "INSERT INTO coupons(code,issuer_key,discount_toman,budget_toman,"
                "used_toman,max_uses,expires_at,is_active) "
                "VALUES('W07-REP-COUPON','rep:202',4000,4000,0,1,1794238000,1)"
            )
            discounted = price_chain(
                conn, plan_code="20gb_30d", seller_key="rep:202",
                buyer_tg_id=101, coupon_code="W07-REP-COUPON", now=1794237000,
            )
            assert discounted["amount_toman"] == 90050
            with pytest.raises(PricingError):
                price_chain(
                    conn, plan_code="20gb_30d", seller_key="root",
                    buyer_rep_id=202, coupon_code="W07-REP-COUPON", now=1794237000,
                )
            conn.execute(
                "UPDATE coupons SET discount_toman=5000,budget_toman=5000 "
                "WHERE code='W07-REP-COUPON'"
            )
            with pytest.raises(PricingError):
                price_chain(
                    conn, plan_code="20gb_30d", seller_key="rep:202",
                    buyer_tg_id=101, coupon_code="W07-REP-COUPON", now=1794237000,
                )

            fixtures = [
                ("0", 0), ("1", 1), ("1.4", 1), ("1.5", 2), ("2.5", 3),
                ("10.5", 11), ("99.49", 99), ("99.50", 100), ("100.50", 101),
                ("999.4", 999), ("999.5", 1000), ("100000.5", 100001),
                ("100000.49", 100000), ("0.49", 0), ("0.50", 1),
            ]
            assert len(fixtures) == 15
            for raw, expected in fixtures:
                assert round_toman(raw) == expected
            for invalid in (-1, "-0.01", float("inf"), True):
                with pytest.raises(PricingError):
                    round_toman(invalid)

            with pytest.raises(PricingError):
                price_chain(
                    conn, plan_code="20gb_30d", seller_key="root",
                    buyer_rep_id=204,
                )
            assert Path(sentinel) != Path(database.DB_PATH)

    assert network_attempts == []
