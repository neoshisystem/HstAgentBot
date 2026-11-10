from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
import sys

from crypto_store import encrypt_secret
from migrations_runner import apply_migrations
from payment_service import handle_additional_received_payment


def _helpers():
    path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w087_extra_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _nonce(conn, value, *, order_id, now, revision=1):
    conn.execute(
        "INSERT INTO action_confirmations(nonce,actor_tg_id,bot_id,action,target_type,target_id,"
        "expected_revision,payload_json,expires_at,consumed_at,operation_id) "
        "VALUES(?,901,9001,'payment.additional_received','payment_order',?,?, '{}',?,NULL,NULL)",
        (value, order_id, revision, now + 300),
    )


def test_second_real_payment_credits_without_reissue(tmp_path, monkeypatch):
    helpers = _helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    database, sentinel = helpers.load_database_module(Path(__file__).resolve().parents[1], monkeypatch, tmp_path)
    config = sys.modules["config"]
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 901)
    now = 1794237000
    order_id = "completed-vpn-order"

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w087-extra.sqlite3"):
        with database.get_db() as conn:
            assert len(apply_migrations(conn, Path(__file__).resolve().parents[1] / "migrations", now=now)) == 18
            helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision) "
                "VALUES(9001,NULL,1,NULL,'w087-root','active',?,1)", (now,),
            )
            conn.execute(
                "INSERT INTO bank_cards(id,merchant_key,card_number_cipher,sheba_cipher,card_holder,bank_name,"
                "target_scope,priority,daily_limit_toman,is_active) "
                "VALUES(1,'root',?,?,'Synthetic Owner','Synthetic Bank','all',1,0,1)",
                (
                    encrypt_secret("4111111111111111", "hstagent/columns/v1"),
                    encrypt_secret("IR000000000000000000000000", "hstagent/columns/v1"),
                ),
            )
            conn.execute(
                "INSERT INTO quotes(quote_id,actor_tg_id,bot_id,merchant_key,purpose,amount_toman,"
                "spec_json,price_edges_json,policy_version,created_at,expires_at,fingerprint) "
                "VALUES('quote-w087',101,9001,'root','vpn_buy',500,'{}','[]','v1.0',?,?, 'fp-w087')",
                (now, now + 1800),
            )
            conn.execute(
                "INSERT INTO payment_orders(order_id,quote_id,actor_tg_id,bot_id,merchant_key,purpose,"
                "payment_rail,amount_toman,status,received_toman,created_at,expires_at,revision) "
                "VALUES(?,'quote-w087',101,9001,'root','vpn_buy','card_to_card',500,'completed',500,?,?,1)",
                (order_id, now, now + 1800),
            )
            card_snapshot = {
                "bank_card_id": 1, "merchant_key": "root", "card_number": "4111111111111111",
                "sheba": "IR000000000000000000000000", "card_holder": "Synthetic Owner",
                "bank_name": "Synthetic Bank", "target_scope": "all",
            }
            conn.execute(
                "INSERT INTO payment_attempts(attempt_id,order_id,rail,merchant_key,assigned_card_id,"
                "amount_toman,state,external_payment_key,evidence_cipher,created_at,verified_at) "
                "VALUES('00000000-0000-4000-8000-000000000087',?,'card_to_card','root',1,500,"
                "'succeeded','BANK-FIRST-087',?,?,?)",
                (
                    order_id,
                    encrypt_secret(json.dumps(card_snapshot, sort_keys=True, separators=(",", ":")),
                                   "hstagent/columns/v1"),
                    now, now,
                ),
            )
            _nonce(conn, "A" * 24, order_id=order_id, now=now)

        with database.get_db() as conn:
            credited = handle_additional_received_payment(
                conn, order_id=order_id, reviewer_tg_id=901, bot_id=9001, nonce="A" * 24,
                external_payment_key="BANK-EXTRA-087", destination="4111111111111111",
                amount_toman=250, evidence_note="Root checked bank reference, destination and amount.",
                now=now + 1,
            )
            assert credited["status"] == "wallet_credited"
            assert credited["wallet_credit_toman"] == 250
            assert credited["dispatch_created"] is False
            assert conn.execute(
                "SELECT status,received_toman,revision FROM payment_orders WHERE order_id=?",
                (order_id,),
            ).fetchone()
            assert tuple(conn.execute(
                "SELECT status,received_toman,revision FROM payment_orders WHERE order_id=?",
                (order_id,),
            ).fetchone()) == ("completed", 750, 2)
            assert conn.execute(
                "SELECT balance_toman FROM user_wallets WHERE user_id=101"
            ).fetchone()[0] == 250
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_ledger WHERE business_key='payment:BANK-EXTRA-087:additional-wallet-credit'"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM scheduled_jobs WHERE job_key LIKE 'fulfill:%'"
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT consumed_at,operation_id FROM action_confirmations WHERE nonce=?",
                ("A" * 24,),
            ).fetchone()[0] == now + 1
            audit = conn.execute(
                "SELECT actor_id,details_cipher FROM audit_logs WHERE action='payment.additional_received'"
            ).fetchone()
            assert audit is not None and int(audit[0]) == 901 and audit[1]

            duplicate = handle_additional_received_payment(
                conn, order_id=order_id, reviewer_tg_id=901, bot_id=9001, nonce="A" * 24,
                external_payment_key="BANK-EXTRA-087", destination="4111111111111111",
                amount_toman=250, evidence_note="Duplicate replay must not create a second credit.",
                now=now + 2,
            )
            assert duplicate["status"] == "duplicate" and duplicate["credited"] is False
            assert conn.execute("SELECT balance_toman FROM user_wallets WHERE user_id=101").fetchone()[0] == 250
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_ledger WHERE business_key='payment:BANK-EXTRA-087:additional-wallet-credit'"
            ).fetchone()[0] == 1

    assert sentinel != str((tmp_path / "w087-extra.sqlite3").resolve())
    assert network_attempts == []
