"""Offline acceptance for immutable tenant-bound v5.2 quote snapshots."""
from __future__ import annotations

from pathlib import Path
import importlib.util
import json

import pytest

from migrations_runner import apply_migrations
from quote_service import QuoteError, QuoteExpired, QuoteTenantMismatch, create_quote, load_quote_for_actor


def _load_helpers():
    helper_path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w07_quote_helpers", helper_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_snapshot_ttl_and_tenant_binding(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _load_helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w07-quotes.sqlite3"):
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=1794237000)) == 18
            helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
            conn.execute(
                "INSERT INTO bot_registry("
                "bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision"
                ") VALUES(9001,NULL,1,NULL,'synthetic-primary','active',1794237000,1)"
            )
            conn.execute(
                "INSERT INTO bot_registry("
                "bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision"
                ") VALUES(9002,202,0,NULL,'synthetic-rep','active',1794237000,1)"
            )
            # The quote's panel_id is a real FK; seed a completely synthetic panel.
            conn.execute(
                "INSERT INTO panels(id,panel_code,name,panel_type) "
                "VALUES(501,'W07-PANEL-501','Synthetic panel','3x-ui')"
            )

        created_at = 1794237000
        spec_snapshot = {
            "purpose": "vpn_buy",
            "payer_tg_id": 101,
            "plan": {"code": "20gb_30d", "gb": 20, "days": 30},
            "panel": {"panel_id": 501, "revision": 4},
            "before": {"quota_bytes": 0, "expires_at": 0},
            "after": {"quota_bytes": 20 * 1024**3, "expires_at": 1796830000},
            "renewal": {"method": "smart_rollover"},
        }
        price_edges = [{
            "seller_key": "root",
            "buyer_key": "user:101",
            "source_key": "wallet:root:101",
            "price_toman": 900,
            "revision": 4,
            "discount_bps": 0,
            "markup_bps": 0,
        }]
        with database.get_db() as conn:
            quote = create_quote(
                conn,
                actor_tg_id=101,
                bot_id=9001,
                merchant_key="root",
                purpose="vpn_buy",
                amount_toman=900,
                spec=spec_snapshot,
                price_edges=price_edges,
                plan_code="20gb_30d",
                panel_id=501,
                now=created_at,
            )
            assert quote["expires_at"] == created_at + 1800
            assert quote["amount_toman"] == 900
            assert quote["snapshot"]["bot"]["merchant_key"] == "root"
            assert quote["snapshot"]["payer_tg_id"] == 101
            assert quote["price_edges"] == price_edges

        # Repricing source data changes, but the stored quote is not repriced.
        with database.get_db() as conn:
            conn.execute(
                "UPDATE pricing_plans SET rep_price=999999,user_price=999999 "
                "WHERE plan_code='20gb_30d'"
            )
            loaded = load_quote_for_actor(
                conn, quote["quote_id"], 101, 9001, now=created_at + 1799
            )
            assert loaded["amount_toman"] == 900
            assert loaded["expires_at"] == created_at + 1800
            assert loaded["spec"]["plan"]["code"] == "20gb_30d"
            assert loaded["price_edges"] == price_edges
            snapshot_text = loaded["spec_json"] + loaded["price_edges_json"]
            for marker in ("token_cipher", "FAKE_BOT_TOKEN", "password", "sub_id"):
                assert marker not in snapshot_text

            with pytest.raises(QuoteTenantMismatch):
                load_quote_for_actor(
                    conn, quote["quote_id"], 104, 9001, now=created_at + 100
                )
            with pytest.raises(QuoteTenantMismatch):
                load_quote_for_actor(
                    conn, quote["quote_id"], 101, 9002, now=created_at + 100
                )
            with pytest.raises(QuoteExpired):
                load_quote_for_actor(
                    conn, quote["quote_id"], 101, 9001, now=created_at + 1800
                )

            with pytest.raises(QuoteError):
                create_quote(
                    conn,
                    actor_tg_id=101,
                    bot_id=9001,
                    merchant_key="root",
                    purpose="vpn_buy",
                    amount_toman=100,
                    spec={"action": "execute", "payer_tg_id": 101},
                    price_edges=[],
                    now=created_at,
                )
            with pytest.raises(QuoteError):
                create_quote(
                    conn,
                    actor_tg_id=101,
                    bot_id=9001,
                    merchant_key="root",
                    purpose="vpn_buy",
                    amount_toman=100,
                    spec={"secret_marker": "V52_SYNTHETIC_SECRET"},
                    price_edges=[],
                    now=created_at,
                )
            row = conn.execute(
                "SELECT spec_json,price_edges_json,created_at,expires_at,fingerprint "
                "FROM quotes WHERE quote_id=?",
                (quote["quote_id"],),
            ).fetchone()
            assert json.loads(row[0])["payer_tg_id"] == 101
            assert row[2] == created_at and row[3] == created_at + 1800
            assert len(row[4]) == 64

        assert Path(sentinel) != Path(database.DB_PATH)

    assert network_attempts == []
