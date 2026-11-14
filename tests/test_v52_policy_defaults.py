"""Offline acceptance for non-destructive tier and feature defaults."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from migrations_runner import apply_migrations
from policy_registry import seed_feature_defaults, seed_tier_defaults


def _load_helpers():
    path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w05_policy_defaults_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_non_destructive_seed(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _load_helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w05-policy-defaults.sqlite3"):
        with database.get_db() as conn:
            assert apply_migrations(conn, root / "migrations", now=1794237000)
            # init_db has already seeded the approved 2.0/2.0 rollover caps.
            assert conn.execute(
                "SELECT value FROM system_settings WHERE key='rollover_max_gb'"
            ).fetchone()[0] == "2.0"
            assert conn.execute(
                "SELECT value FROM system_settings WHERE key='rollover_max_days'"
            ).fetchone()[0] == "2.0"
            conn.execute(
                "INSERT INTO tier_policies(actor_type,tier_code,title_fa,rank,discount_bps,"
                "cashback_bps,daily_test_quota,default_credit_limit,min_monthly_sales_toman) "
                "VALUES('rep','vip','Custom VIP',1,375,125,4,7654321,990000)"
            )
            conn.execute(
                "INSERT INTO system_settings(key,value) VALUES('sub_profile_limit','9')"
            )
            conn.execute("INSERT INTO feature_flags(name,enabled) VALUES('custom_gate',1)")

        with database.get_db() as conn:
            seed_tier_defaults(conn)
            seed_feature_defaults(conn)
            seed_tier_defaults(conn)
            seed_feature_defaults(conn)
            tiers = conn.execute(
                "SELECT actor_type,tier_code,title_fa,rank,discount_bps,cashback_bps,"
                "daily_test_quota,default_credit_limit,min_monthly_sales_toman "
                "FROM tier_policies ORDER BY actor_type,rank"
            ).fetchall()
            assert len(tiers) == 7
            expected_ranks = {
                ("rep", "normal"): 0, ("rep", "vip"): 1, ("rep", "vvip"): 2,
                ("end_user", "normal"): 0, ("end_user", "silver"): 1,
                ("end_user", "gold"): 2, ("end_user", "vip"): 3,
            }
            for row in tiers:
                key = (row["actor_type"], row["tier_code"])
                assert row["rank"] == expected_ranks[key]
                if key == ("rep", "vip"):
                    assert (row["title_fa"], row["discount_bps"], row["cashback_bps"],
                            row["daily_test_quota"], row["default_credit_limit"],
                            row["min_monthly_sales_toman"]) == (
                        "Custom VIP", 375, 125, 4, 7654321, 990000
                    )
                else:
                    assert row["discount_bps"] == 0
                    assert row["cashback_bps"] == 0
                    assert row["daily_test_quota"] == 1
            flags = dict(conn.execute("SELECT name,enabled FROM feature_flags").fetchall())
            assert flags["crypto"] == 0
            assert flags["gateway"] == 0
            assert flags["custom_gate"] == 1
            assert "parent_shared" not in flags
            settings = dict(conn.execute("SELECT key,value FROM system_settings").fetchall())
            assert settings["rollover_max_gb"] == "2.0"
            assert settings["rollover_max_days"] == "2.0"
            assert settings["sub_profile_limit"] == "9"
            assert conn.execute("SELECT COUNT(*) FROM tier_policies").fetchone()[0] == 7
            assert network_attempts == []
            assert not Path(sentinel).exists()
