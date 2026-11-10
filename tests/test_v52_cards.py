"""Offline acceptance for tenant-scoped bank-card choice, daily limits and snapshots."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import base64
import importlib.util
import json
import time
from zoneinfo import ZoneInfo

import pytest

from crypto_store import encrypt_secret, decrypt_secret
from migrations_runner import apply_migrations
from order_service import create_order
from payment_service import create_card_attempt, select_card
from quote_service import create_quote


def _helpers():
    p = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w08_card_helpers", p)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _quote(conn, *, actor, bot, merchant, purpose, amount, now):
    return create_quote(
        conn, actor_tg_id=actor, bot_id=bot, merchant_key=merchant,
        purpose=purpose, amount_toman=amount,
        spec={"purpose": purpose, "payer_tg_id": actor},
        price_edges=[], now=now,
    )


def _add_card(conn, *, card_id, merchant, number, scope, priority, limit=0, active=1):
    cipher = encrypt_secret(number, "hstagent/columns/v1")
    sheba = encrypt_secret("IR000000000000000000000000", "hstagent/columns/v1")
    conn.execute(
        "INSERT INTO bank_cards(id,merchant_key,card_number_cipher,sheba_cipher,card_holder,"
        "bank_name,target_scope,priority,daily_limit_toman,is_active) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (card_id,merchant,cipher,sheba,f"Holder {card_id}",f"Bank {card_id}",
         scope,priority,limit,active),
    )


def test_card_scope_snapshot_and_limit(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    key = bytes(range(32))
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(key).decode("ascii").rstrip("="),
    )
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    now = int(time.time())

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w08-cards.sqlite3"):
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=now)) == 18
            helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,group_id,topic_id,scope_filter,"
                "parent_rep_id,tier,billing_mode,markup_bps,is_active,deleted_at) "
                "VALUES(204,'W08-CHILD','W08 child',404,0,0,'w08:child',202,'normal','credit',0,1,NULL)"
            )
            conn.execute(
                "INSERT INTO rep_edges(id,parent_key,child_rep_id,billing_mode,debt_toman,credit_limit_toman,"
                "active,started_at,ended_at) VALUES(601,'root',202,'credit',0,0,1,?,NULL)",
                (now,),
            )
            conn.execute(
                "INSERT INTO rep_edges(id,parent_key,child_rep_id,billing_mode,debt_toman,credit_limit_toman,"
                "active,started_at,ended_at) VALUES(602,'rep:202',204,'credit',0,0,1,?,NULL)",
                (now,),
            )
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
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision) "
                "VALUES(9003,204,0,NULL,'w08-child','active',?,1)",
                (now,),
            )
            conn.execute(
                "INSERT INTO bot_users(telegram_id,role,rep_id,username,full_name,is_blocked) "
                "VALUES(404,'rep',204,'w08_child','W08 child',0)"
            )
            _add_card(conn,card_id=1,merchant="root",number="1111-1111-1111-1111",
                      scope="root",priority=1,limit=500)
            _add_card(conn,card_id=2,merchant="rep:202",number="2222-2222-2222-2222",
                      scope="rep",priority=0)
            _add_card(conn,card_id=3,merchant="root",number="3333-3333-3333-3333",
                      scope="all",priority=5,limit=0)
            _add_card(conn,card_id=4,merchant="rep:202",number="4444-4444-4444-4444",
                      scope="rep_settlement",priority=1)
            _add_card(conn,card_id=5,merchant="root",number="5555-5555-5555-5555",
                      scope="all",priority=0,active=0)

        # Root customers may not be routed to a representative-owned card even
        # when that card has a better priority. The selected card is snapshotted.
        with database.get_db() as conn:
            root_quote = _quote(
                conn,actor=101,bot=9001,merchant="root",
                purpose="wallet_topup",amount=100,now=now,
            )
            root_order = create_order(
                conn,quote_id=root_quote["quote_id"],actor_tg_id=101,
                bot_id=9001,payment_rail="card_to_card",now=now,
            )
            selected = select_card(
                conn,order_id=root_order["order_id"],actor_tg_id=101,
                bot_id=9001,now=now,
            )
            assert selected["bank_card_id"] == 1
            attempt = create_card_attempt(
                conn,order_id=root_order["order_id"],actor_tg_id=101,
                bot_id=9001,now=now,
            )
            assert attempt["bank_card_id"] == 1
            assert attempt["card"]["card_number"] == "1111-1111-1111-1111"
            assert attempt["attempt_id"] != ""
            # A terminal failed attempt is never recycled; the replacement UUID is new.
            conn.execute(
                "UPDATE payment_attempts SET state='failed' WHERE attempt_id=?",
                (attempt["attempt_id"],),
            )
            replacement = create_card_attempt(
                conn,order_id=root_order["order_id"],actor_tg_id=101,
                bot_id=9001,now=now+1,
            )
            assert replacement["attempt_id"] != attempt["attempt_id"]
            stored = conn.execute(
                "SELECT evidence_cipher FROM payment_attempts WHERE attempt_id=?",
                (attempt["attempt_id"],),
            ).fetchone()[0]
            snapshot = json.loads(decrypt_secret(stored,"hstagent/columns/v1"))
            assert snapshot["card_number"] == "1111-1111-1111-1111"
            assert "1111-1111-1111-1111" not in stored

            # Card edits do not mutate the encrypted evidence of an old attempt.
            conn.execute(
                "UPDATE bank_cards SET card_number_cipher=?,card_holder='Changed holder' WHERE id=1",
                (encrypt_secret("9999-9999-9999-9999","hstagent/columns/v1"),),
            )
            after_edit = json.loads(decrypt_secret(stored,"hstagent/columns/v1"))
            assert after_edit["card_number"] == "1111-1111-1111-1111"
            assert after_edit["card_holder"] == "Holder 1"

        # A dedicated representative bot selects its own card.
        with database.get_db() as conn:
            rep_quote = _quote(
                conn,actor=101,bot=9002,merchant="rep:202",
                purpose="wallet_topup",amount=100,now=now+2,
            )
            rep_order = create_order(
                conn,quote_id=rep_quote["quote_id"],actor_tg_id=101,
                bot_id=9002,payment_rail="card_to_card",now=now+2,
            )
            rep_attempt = create_card_attempt(
                conn,order_id=rep_order["order_id"],actor_tg_id=101,
                bot_id=9002,now=now+2,
            )
            assert rep_attempt["bank_card_id"] == 2

        # A child representative's settlement may use its parent's explicitly
        # settlement-scoped card, but never an unrelated root/rep card.
        with database.get_db() as conn:
            child_quote = _quote(
                conn,actor=404,bot=9003,merchant="rep:204",
                purpose="rep_settlement",amount=100,now=now+3,
            )
            child_order = create_order(
                conn,quote_id=child_quote["quote_id"],actor_tg_id=404,
                bot_id=9003,payment_rail="card_to_card",now=now+3,
            )
            child_attempt = create_card_attempt(
                conn,order_id=child_order["order_id"],actor_tg_id=404,
                bot_id=9003,now=now+3,
            )
            assert child_attempt["bank_card_id"] == 4

        # Pending/approved receipts consume the card's Tehran-day limit. Rejected
        # receipts do not count; inactive cards are never selected.
        with database.get_db() as conn:
            date = datetime.fromtimestamp(now+4,timezone.utc).astimezone(
                ZoneInfo("Asia/Tehran")
            ).date().isoformat()
            conn.execute(
                "INSERT INTO payment_receipts(tracking_code,user_id,role,amount_toman,status,"
                "order_id,bot_id,bank_card_id,merchant_key,submitted_tehran_date) "
                "VALUES('W08-PENDING',101,'end_user',450,'pending',?,9001,1,'root',?)",
                (root_order["order_id"],date),
            )
            conn.execute(
                "INSERT INTO payment_receipts(tracking_code,user_id,role,amount_toman,status,"
                "order_id,bot_id,bank_card_id,merchant_key,submitted_tehran_date) "
                "VALUES('W08-REJECTED',101,'end_user',900,'rejected',?,9001,1,'root',?)",
                (root_order["order_id"],date),
            )
            next_quote = _quote(
                conn,actor=101,bot=9001,merchant="root",
                purpose="wallet_topup",amount=100,now=now+4,
            )
            next_order = create_order(
                conn,quote_id=next_quote["quote_id"],actor_tg_id=101,
                bot_id=9001,payment_rail="card_to_card",now=now+4,
            )
            next_card = select_card(
                conn,order_id=next_order["order_id"],actor_tg_id=101,
                bot_id=9001,now=now+4,
            )
            assert next_card["bank_card_id"] == 3
            assert next_card["bank_card_id"] != 5

        assert Path(sentinel) != Path(database.DB_PATH)
    assert network_attempts == []
