"""Offline acceptance for one-photo receipt submission, cooldown and expiry race."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
from pathlib import Path
import importlib.util
import time

import pytest

from crypto_store import encrypt_secret
from migrations_runner import apply_migrations
from order_service import create_order, expire_unpaid_orders
from payment_service import (
    AttemptExpired, PaymentError, ReceiptCooldown, create_card_attempt, submit_receipt,
)
from quote_service import create_quote


def _helpers():
    p = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w08_receipt_helpers", p)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _quote(conn, *, actor, bot, merchant, now):
    return create_quote(
        conn, actor_tg_id=actor, bot_id=bot, merchant_key=merchant,
        purpose="wallet_topup", amount_toman=200,
        spec={"purpose":"wallet_topup","payer_tg_id":actor},
        price_edges=[], now=now,
    )


def _add_card(conn, card_id, merchant, now):
    cipher=encrypt_secret(f"4000-0000-0000-{card_id:04d}","hstagent/columns/v1")
    conn.execute(
        "INSERT INTO bank_cards(id,merchant_key,card_number_cipher,card_holder,bank_name,"
        "target_scope,priority,daily_limit_toman,is_active) VALUES(?,?,?,'W08 Holder','W08 Bank','all',1,0,1)",
        (card_id,merchant,cipher),
    )


def _new_attempt(database, *, actor, bot, merchant, card_id, now):
    with database.get_db() as conn:
        quote=_quote(conn,actor=actor,bot=bot,merchant=merchant,now=now)
        order=create_order(
            conn,quote_id=quote["quote_id"],actor_tg_id=actor,bot_id=bot,
            payment_rail="card_to_card",now=now,
        )
        attempt=create_card_attempt(
            conn,order_id=order["order_id"],actor_tg_id=actor,bot_id=bot,now=now,
        )
        assert attempt["bank_card_id"]==card_id
        return order,attempt


def test_receipt_cooldown_album_and_expiry_race(tmp_path, monkeypatch):
    root=Path(__file__).resolve().parents[1]
    helpers=_helpers()
    network_attempts=helpers.guard_network(monkeypatch)
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    database,sentinel=helpers.load_database_module(root,monkeypatch,tmp_path)
    now=int(time.time())

    with helpers.make_db(database,monkeypatch,tmp_path,filename="w08-receipts.sqlite3"):
        with database.get_db() as conn:
            assert len(apply_migrations(conn,root/"migrations",now=now))==18
            helpers.seed_actor(database,actor_tg_id=101,rep_id=202,rep_tg_id=303)
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision) "
                "VALUES(9001,NULL,1,NULL,'w08-root','active',?,1)",
                (now,),
            )
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision) "
                "VALUES(9002,202,0,NULL,'w08-rep','active',?,1)",
                (now,),
            )
            _add_card(conn,1,"root",now)
            _add_card(conn,2,"rep:202",now)

        first_order,first_attempt=_new_attempt(
            database,actor=101,bot=9001,merchant="root",card_id=1,now=now,
        )
        with database.get_db() as conn:
            with pytest.raises(PaymentError,match="albums"):
                submit_receipt(
                    conn,attempt_id=first_attempt["attempt_id"],actor_tg_id=101,
                    bot_id=9001,amount_toman=200,receipt_photo_id="photo-album",
                    media_group_id="album-1",now=now,
                )
            assert conn.execute("SELECT COUNT(*) FROM payment_receipts").fetchone()[0]==0
            assert conn.execute(
                "SELECT state FROM payment_attempts WHERE attempt_id=?",
                (first_attempt["attempt_id"],),
            ).fetchone()[0]=="pending"

            submitted=submit_receipt(
                conn,attempt_id=first_attempt["attempt_id"],actor_tg_id=101,
                bot_id=9001,amount_toman=200,receipt_photo_id="photo-one",
                now=now,
            )
            assert submitted["status"]=="pending"
            assert submitted["replayed"] is False
            duplicate=submit_receipt(
                conn,attempt_id=first_attempt["attempt_id"],actor_tg_id=101,
                bot_id=9001,amount_toman=200,receipt_photo_id="photo-one",
                now=now+1,
            )
            assert duplicate["replayed"] is True
            assert duplicate["receipt_id"]==submitted["receipt_id"]
            assert conn.execute("SELECT COUNT(*) FROM payment_receipts").fetchone()[0]==1
            assert conn.execute(
                "SELECT status FROM payment_orders WHERE order_id=?",
                (first_order["order_id"],),
            ).fetchone()[0]=="pending_review"
            # Force deterministic SQLite CURRENT_TIMESTAMP to the injected epoch.
            created=datetime.fromtimestamp(now,timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute(
                "UPDATE payment_receipts SET created_at=? WHERE id=?",
                (created,submitted["receipt_id"]),
            )

        # Same Telegram identity, different bot: 119 seconds is rejected; exactly
        # 120 seconds is accepted. The cooldown is global across all bots.
        second_order,second_attempt=_new_attempt(
            database,actor=101,bot=9002,merchant="rep:202",card_id=2,now=now+119,
        )
        with database.get_db() as conn:
            with pytest.raises(ReceiptCooldown):
                submit_receipt(
                    conn,attempt_id=second_attempt["attempt_id"],actor_tg_id=101,
                    bot_id=9002,amount_toman=200,receipt_photo_id="photo-119",
                    now=now+119,
                )
            accepted=submit_receipt(
                conn,attempt_id=second_attempt["attempt_id"],actor_tg_id=101,
                bot_id=9002,amount_toman=200,receipt_photo_id="photo-120",
                now=now+120,
            )
            assert accepted["replayed"] is False
            assert conn.execute(
                "SELECT COUNT(*) FROM payment_receipts WHERE user_id=101"
            ).fetchone()[0]==2
            assert conn.execute(
                "SELECT status FROM payment_orders WHERE order_id=?",
                (second_order["order_id"],),
            ).fetchone()[0]=="pending_review"

        # Expiry and receipt submission are serialized. If expiry wins first, a
        # still-live UUID attempt may submit a late receipt for wallet-credit review.
        with database.get_db() as conn:
            late_quote=_quote(conn,actor=101,bot=9001,merchant="root",now=now)
            late_order=create_order(
                conn,quote_id=late_quote["quote_id"],actor_tg_id=101,
                bot_id=9001,payment_rail="card_to_card",now=now,
            )
            late_attempt=create_card_attempt(
                conn,order_id=late_order["order_id"],actor_tg_id=101,
                bot_id=9001,now=now+1500,
            )
            expired=expire_unpaid_orders(conn,now=now+1801)
            assert late_order["order_id"] in expired["expired_order_ids"]
            assert first_order["order_id"] in expired["retained_pending_review_ids"]
            assert conn.execute(
                "SELECT status FROM payment_orders WHERE order_id=?",
                (late_order["order_id"],),
            ).fetchone()[0]=="expired"
            late=submit_receipt(
                conn,attempt_id=late_attempt["attempt_id"],actor_tg_id=101,
                bot_id=9001,amount_toman=200,receipt_photo_id="photo-late",
                now=now+1900,
            )
            assert late["status"]=="pending"
            assert late["order_status"]=="expired"
            assert conn.execute(
                "SELECT status FROM payment_orders WHERE order_id=?",
                (late_order["order_id"],),
            ).fetchone()[0]=="expired"
            assert conn.execute(
                "SELECT COUNT(*) FROM payment_receipts WHERE order_id=?",
                (late_order["order_id"],),
            ).fetchone()[0]==1

        with database.get_db() as conn:
            expired_attempt_order,expired_attempt=_new_attempt(
                database,actor=101,bot=9001,merchant="root",card_id=1,now=now+2000,
            )
        with database.get_db() as conn:
            with pytest.raises(AttemptExpired):
                submit_receipt(
                    conn,attempt_id=expired_attempt["attempt_id"],actor_tg_id=101,
                    bot_id=9001,amount_toman=200,receipt_photo_id="expired-photo",
                    now=now+3800,
                )
            assert conn.execute(
                "SELECT COUNT(*) FROM payment_receipts WHERE order_id=?",
                (expired_attempt_order["order_id"],),
            ).fetchone()[0]==0

        assert Path(sentinel)!=Path(database.DB_PATH)
    assert network_attempts==[]
