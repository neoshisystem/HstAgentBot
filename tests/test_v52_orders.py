"""Offline acceptance for atomic quote-to-order holds, capacity and expiry."""
from __future__ import annotations

from pathlib import Path
import importlib.util
import sqlite3
import time

import pytest

from capacity_service import CapacityFull
from financial_service import available_balance
from migrations_runner import apply_migrations
from order_service import OrderError, create_order, expire_unpaid_orders
from quote_service import create_quote


def _helpers():
    p = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w08_order_helpers", p)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _quote(conn, *, actor, bot, merchant, purpose, amount, now, panel=None, edges=None, coupon=None):
    spec = {"purpose": purpose, "payer_tg_id": actor}
    if panel is not None:
        spec["panel"] = panel
    if coupon is not None:
        spec["coupon"] = {"code": coupon}
    return create_quote(
        conn, actor_tg_id=actor, bot_id=bot, merchant_key=merchant,
        purpose=purpose, amount_toman=amount, spec=spec,
        price_edges=edges or [], panel_id=None if panel is None else panel["panel_id"],
        now=now,
    )


def test_order_creation_rollback_and_expiry(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    now = int(time.time())

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w08-orders.sqlite3"):
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=now)) == 18
            helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision) "
                "VALUES(9001,NULL,1,NULL,'w08-root','active',?,1)",
                (now,),
            )
            conn.execute(
                "INSERT INTO panels(id,panel_code,name,panel_type,lifecycle,max_clients) "
                "VALUES(501,'W08-PANEL','W08 synthetic panel','3x-ui','active',1)"
            )
            conn.execute(
                "INSERT INTO financial_ledger(rep_id,user_id,transaction_type,amount,balance_after,"
                "description,actor_id,business_key,merchant_key,ledger_scope,policy_version) "
                "VALUES(NULL,101,'credit',2000,2000,'synthetic wallet opening',101,'w08-wallet-open','root','wallet','v1.0')"
            )
            conn.execute("UPDATE user_wallets SET balance_toman=2000 WHERE user_id=101")
            conn.execute(
                "INSERT INTO coupons(code,issuer_key,discount_toman,budget_toman,used_toman,max_uses,expires_at,is_active) "
                "VALUES('W08-COUPON','root',50,50,0,1,?,1)",
                (now + 5000,),
            )

        panel = {"panel_id": 501, "active_count": 0, "snapshot_at": now}
        with database.get_db() as conn:
            vpn_quote = _quote(
                conn, actor=101, bot=9001, merchant="root", purpose="vpn_buy",
                amount=500, now=now, panel=panel,
            )
            first = create_order(
                conn, quote_id=vpn_quote["quote_id"], actor_tg_id=101,
                bot_id=9001, payment_rail="card_to_card", now=now,
            )
            replay = create_order(
                conn, quote_id=vpn_quote["quote_id"], actor_tg_id=101,
                bot_id=9001, payment_rail="card_to_card", now=now + 1,
            )
            assert first["status"] == "awaiting_payment"
            assert first["replayed"] is False
            assert replay["replayed"] is True
            assert replay["order_id"] == first["order_id"]
            assert conn.execute(
                "SELECT COUNT(*) FROM capacity_reservations WHERE order_id=? AND state='active'",
                (first["order_id"],),
            ).fetchone()[0] == 1

        # A second quote races for the last sell slot. The order, holds and coupon
        # reservation must all roll back when capacity refuses the reservation.
        with database.get_db() as conn:
            blocked_quote = _quote(
                conn, actor=101, bot=9001, merchant="root", purpose="vpn_buy",
                amount=450, now=now + 2, panel=panel, coupon="W08-COUPON",
            )
            with pytest.raises(CapacityFull):
                create_order(
                    conn, quote_id=blocked_quote["quote_id"], actor_tg_id=101,
                    bot_id=9001, payment_rail="card_to_card", now=now + 2,
                )
            assert conn.execute(
                "SELECT COUNT(*) FROM payment_orders WHERE quote_id=?",
                (blocked_quote["quote_id"],),
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM capacity_reservations WHERE order_id IN "
                "(SELECT order_id FROM payment_orders WHERE quote_id=?)",
                (blocked_quote["quote_id"],),
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM coupon_uses WHERE code='W08-COUPON'"
            ).fetchone()[0] == 0

        with database.get_db() as conn:
            wallet_quote = _quote(
                conn, actor=101, bot=9001, merchant="root", purpose="wallet_topup",
                amount=300, now=now + 3,
                edges=[{"seller_key":"root","buyer_key":"user:101",
                        "source_key":"wallet:root:101","price_toman":300}],
            )
            wallet_order = create_order(
                conn, quote_id=wallet_quote["quote_id"], actor_tg_id=101,
                bot_id=9001, payment_rail="wallet", now=now + 3,
            )
            assert wallet_order["status"] == "awaiting_payment"
            assert available_balance(conn, "wallet:root:101") == 1700
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_holds WHERE order_id=? AND state='active'",
                (wallet_order["order_id"],),
            ).fetchone()[0] == 1
            with pytest.raises(OrderError):
                create_order(
                    conn, quote_id=wallet_quote["quote_id"], actor_tg_id=101,
                    bot_id=9001, payment_rail="crypto_tron", now=now + 4,
                )

            pending_quote = _quote(
                conn, actor=101, bot=9001, merchant="root", purpose="wallet_topup",
                amount=200, now=now + 5,
                edges=[{"seller_key":"root","buyer_key":"user:101",
                        "source_key":"wallet:root:101","price_toman":200}],
            )
            pending_order = create_order(
                conn, quote_id=pending_quote["quote_id"], actor_tg_id=101,
                bot_id=9001, payment_rail="wallet", now=now + 5,
            )
            conn.execute(
                "UPDATE payment_orders SET expires_at=? WHERE order_id=?",
                (now + 4, first["order_id"]),
            )
            conn.execute(
                "UPDATE payment_orders SET expires_at=? WHERE order_id=?",
                (now + 4, wallet_order["order_id"]),
            )
            conn.execute(
                "UPDATE payment_orders SET status='pending_review',expires_at=? WHERE order_id=?",
                (now + 4, pending_order["order_id"]),
            )

        with database.get_db() as conn:
            expired = expire_unpaid_orders(conn, now=now + 10)
            assert wallet_order["order_id"] in expired["expired_order_ids"]
            assert pending_order["order_id"] in expired["retained_pending_review_ids"]
            assert conn.execute(
                "SELECT status FROM payment_orders WHERE order_id=?",
                (wallet_order["order_id"],),
            ).fetchone()[0] == "expired"
            assert conn.execute(
                "SELECT state FROM financial_holds WHERE order_id=?",
                (wallet_order["order_id"],),
            ).fetchone()[0] == "released"
            assert conn.execute(
                "SELECT status FROM payment_orders WHERE order_id=?",
                (pending_order["order_id"],),
            ).fetchone()[0] == "pending_review"
            assert conn.execute(
                "SELECT COUNT(*) FROM scheduled_jobs WHERE job_key LIKE ?",
                (f"%{pending_order['order_id']}%",),
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT state FROM capacity_reservations WHERE order_id=?",
                (first["order_id"],),
            ).fetchone()[0] == "released"

        assert Path(sentinel) != Path(database.DB_PATH)
    assert network_attempts == []
