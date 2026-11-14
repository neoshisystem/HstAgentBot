"""W20.2 end-to-end financial acceptance through the real v5.2 services."""
from __future__ import annotations

import base64
import importlib.util
import sqlite3
import sys
import threading
import time
from pathlib import Path

import pytest

from crypto_store import encrypt_secret
from migrations_runner import apply_migrations


def _helpers():
    path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w20_finance_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load(root, name, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, root / (name + ".py"))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _nonce(conn, *, nonce, reviewer, bot, receipt, expires):
    conn.execute(
        "INSERT INTO action_confirmations("
        "nonce,actor_tg_id,bot_id,action,target_type,target_id,expected_revision,"
        "payload_json,expires_at,consumed_at,operation_id"
        ") VALUES(?,?,?,'receipt.approve','receipt',?,1,'{}',?,NULL,NULL)",
        (nonce, reviewer, bot, str(receipt), expires),
    )


def _card_receipt(
    database, quotes, orders, payments, *, actor, bot, amount, purpose, now,
    panel=None, account_id=None,
):
    spec = {"purpose": purpose, "payer_tg_id": actor}
    if panel is not None:
        spec["panel"] = panel
    with database.get_db() as conn:
        quote = quotes.create_quote(
            conn, actor_tg_id=actor, bot_id=bot, merchant_key="root",
            purpose=purpose, amount_toman=amount, spec=spec,
            price_edges=[], account_id=account_id,
            panel_id=None if panel is None else panel["panel_id"], now=now,
        )
        order = orders.create_order(
            conn, quote_id=quote["quote_id"], actor_tg_id=actor, bot_id=bot,
            payment_rail="card_to_card", now=now,
            capacity_snapshot_count=0 if panel is not None else None,
            capacity_snapshot_at=now if panel is not None else None,
        )
        attempt = payments.create_card_attempt(
            conn, order_id=order["order_id"], actor_tg_id=actor,
            bot_id=bot, now=now,
        )
        receipt = payments.submit_receipt(
            conn, attempt_id=attempt["attempt_id"], actor_tg_id=actor,
            bot_id=bot, amount_toman=amount,
            receipt_photo_id=f"w20-offline-photo-{actor}-{now}", now=now,
        )
    return order, attempt, receipt


def test_full_invoice_restart_and_cascade(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    config = sys.modules["config"]
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9090, raising=False)

    # Reload the production services against this isolated database module.
    financial = _load(root, "financial_service", monkeypatch)
    quotes = _load(root, "quote_service", monkeypatch)
    orders = _load(root, "order_service", monkeypatch)
    payments = _load(root, "payment_service", monkeypatch)
    confirmations = _load(root, "confirmation_service", monkeypatch)
    access = _load(root, "access_service", monkeypatch)
    identity = _load(root, "identity_service", monkeypatch)
    models = _load(root, "v52_models", monkeypatch)
    operations = _load(root, "operation_service", monkeypatch)
    worker = _load(root, "operation_worker", monkeypatch)
    reconciliation = _load(root, "reconciliation_service", monkeypatch)
    outbox_service = _load(root, "outbox_service", monkeypatch)
    outbox_worker = _load(root, "outbox_worker", monkeypatch)

    now = int(time.time())
    with helpers.make_db(
        database, monkeypatch, tmp_path, filename="w20-finance.sqlite3"
    ) as db_path:
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=now)) == 18
            helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
            helpers.seed_actor(database, actor_tg_id=1000, rep_id=202, rep_tg_id=303)
            helpers.seed_actor(database, actor_tg_id=102, rep_id=203, rep_tg_id=304)
            helpers.seed_actor(database, actor_tg_id=103, rep_id=205, rep_tg_id=305)
            conn.execute(
                "INSERT INTO bot_users(telegram_id,role,rep_id,username,full_name,is_blocked) "
                "VALUES(9090,'admin',NULL,'w20_root','W20 root',0)"
            )
            conn.execute(
                "INSERT INTO bot_users(telegram_id,role,rep_id,username,full_name,is_blocked) "
                "VALUES(9091,'admin',NULL,'w20_admin','W20 admin',0)"
            )
            conn.execute(
                "INSERT INTO bot_registry("
                "bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision"
                ") VALUES(9001,NULL,1,?,'w20_primary','active',?,1)",
                (encrypt_secret("W20_SYNTHETIC_BOT_TOKEN", "hstagent/columns/v1"), now),
            )
            conn.execute(
                "INSERT INTO panels(id,panel_code,name,panel_type,lifecycle,max_clients) "
                "VALUES(501,'W20-PANEL','W20 synthetic panel','3x-ui','active',100)"
            )
            conn.execute(
                "INSERT INTO bank_cards("
                "id,merchant_key,card_number_cipher,card_holder,bank_name,target_scope,"
                "priority,daily_limit_toman,is_active"
                ") VALUES(1,'root',?,'W20 Holder','W20 Bank','all',1,0,1)",
                (encrypt_secret("4111111111111111", "hstagent/columns/v1"),),
            )
            conn.execute(
                "INSERT INTO representatives("
                "id,rep_code,name,telegram_id,group_id,topic_id,scope_filter,parent_rep_id,"
                "tier,billing_mode,markup_bps,is_active,deleted_at"
                ") VALUES(204,'W20-CHILD','W20 child',404,0,0,'w20:child',202,"
                "'normal','credit',0,1,NULL)"
            )
            conn.execute(
                "INSERT INTO rep_edges("
                "id,parent_key,child_rep_id,billing_mode,debt_toman,credit_limit_toman,"
                "active,started_at,ended_at"
                ") VALUES(900,'root',202,'credit',0,5000,1,?,NULL)",
                (now,),
            )
            conn.execute(
                "INSERT INTO rep_edges("
                "id,parent_key,child_rep_id,billing_mode,debt_toman,credit_limit_toman,"
                "active,started_at,ended_at"
                ") VALUES(990,'rep:202',204,'credit',0,5000,1,?,NULL)",
                (now,),
            )

        helpers.seed_account(
            database, account_id=303, rep_id=202, owner_tg_id=101,
            account_code="W20-DIRECT-303", email="w20-direct@example.invalid",
            sub_id="W20_SYNTHETIC_ACCOUNT_SECRET",
        )
        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE accounts SET panel_id=501,seller_key='root' WHERE id=303"
            )
            financial.post_entry(
                conn, rep_id=None, user_id=1000, transaction_type="credit",
                amount=1000, balance_after=1000, description="W20 synthetic payer opening",
                actor_id=1000, business_key="w20-payer-opening",
                merchant_key="root", ledger_scope="wallet",
            )
            conn.commit()

        # The direct-card invoice is approved through the production receipt API.
        direct_order, direct_attempt, direct_receipt = _card_receipt(
            database, quotes, orders, payments,
            actor=101, bot=9001, amount=500, purpose="vpn_renew",
            now=now, panel={"panel_id": 501, "active_count": 0, "snapshot_at": now},
            account_id=303,
        )
        with database.get_db() as conn:
            _nonce(
                conn, nonce="w20-direct-approve", reviewer=9090, bot=9001,
                receipt=direct_receipt["receipt_id"], expires=now + 3600,
            )
            approved = payments.approve_order_receipt(
                conn, receipt_id=direct_receipt["receipt_id"],
                reviewer_tg_id=9090, bot_id=9001,
                nonce="w20-direct-approve", now=now + 1,
            )
            assert approved["status"] == "accepted"
            assert approved["wallet_credit"] == 0
            assert conn.execute(
                "SELECT status FROM payment_orders WHERE order_id=?",
                (direct_order["order_id"],),
            ).fetchone()[0] == "accepted"
            assert conn.execute(
                "SELECT state FROM payment_attempts WHERE attempt_id=?",
                (direct_attempt["attempt_id"],),
            ).fetchone()[0] == "succeeded"
            assert conn.execute(
                "SELECT COUNT(*) FROM scheduled_jobs WHERE job_key=?",
                (f"fulfill:order:{direct_order['order_id']}",),
            ).fetchone()[0] == 1

        # Prepare one immutable panel operation and deliberately lose the dispatch
        # response. Exactly one external mutation attempt is made; no retry occurs.
        with database.get_db() as conn:
            account_revision = int(conn.execute(
                "SELECT revision FROM accounts WHERE id=303"
            ).fetchone()[0])
            conn.execute("BEGIN IMMEDIATE")
            confirmation = confirmations.create_confirmation(
                conn, actor_tg_id=101, bot_id=9001,
                action="panel.operation.prepare", target_type="account",
                target_id=303, expected_revision=account_revision,
                payload={"account_id": 303, "panel_id": 501},
                now=now + 2, ttl_seconds=600,
            )
            conn.commit()
            expected_effect = {
                "snapshot": {
                    "account_id": 303, "panel_id": 501,
                    "account_revision": account_revision,
                    "quota_mode": "finite", "quota_bytes": 10737418240,
                    "used_bytes": 100, "time_mode": "dated",
                    "expires_at": now + 86400,
                },
                "target": {
                    "quota_bytes": 21474836480,
                    "expires_at": now + 30 * 86400,
                },
            }
            prepared = operations.prepare_operation(
                conn, confirmation_nonce=confirmation["nonce"],
                actor_tg_id=101, bot_id=9001,
                order_id=direct_order["order_id"], account_id=303,
                kind="vpn_renew",
                request={
                    "method": "renew_client",
                    "params": {"candidate_email": "w20-direct@example.invalid",
                               "credential": "W20_PRIVATE_REQUEST_MARKER"},
                },
                expected_effect=expected_effect,
                idempotency_key="w20-direct-invoice-303",
                now=now + 3,
            )
            assert prepared["state"] == "prepared"
            operation_id = prepared["operation_id"]
            claim = worker.claim_prepared(
                conn, operation_id=operation_id, now=now + 4, lease_seconds=30,
            )
            assert claim is not None and claim["state"] == "claimed"

        mutation_calls = []

        def one_panel_mutation(_request):
            mutation_calls.append("one-shot")
            raise TimeoutError("synthetic response timeout after dispatch")

        with database.get_db() as conn:
            dispatched = worker.dispatch_claimed(
                conn, claim=claim, dispatch=one_panel_mutation, now=now + 5,
            )
            assert dispatched["dispatch_outcome"] == "unknown"
            assert dispatched["operation_state"] == "needs_reconciliation"
        assert mutation_calls == ["one-shot"]

        # A process restart is represented by a fresh DB connection. The durable
        # UNKNOWN state and its lock owner survive and are not dispatched again.
        with database.get_db() as conn:
            state = conn.execute(
                "SELECT state,lock_token FROM panel_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
            assert state["state"] == "needs_reconciliation"
            assert state["lock_token"]
            assert worker.claim_prepared(
                conn, operation_id=operation_id, now=now + 10
            )["state"] == "needs_reconciliation"

        readback_calls = []

        def applied_readback(panel_id, candidate_id):
            readback_calls.append((panel_id, candidate_id))
            return models.ReconcileResult(
                "applied", {"expected_effect": expected_effect["target"]}
            )

        with database.get_db() as conn:
            reconciled = reconciliation.reconcile_operation(
                conn, operation_id=operation_id, lookup=applied_readback,
                now=now + 11,
            )
            assert reconciled["state"] == "succeeded"
            assert conn.execute(
                "SELECT status FROM payment_orders WHERE order_id=?",
                (direct_order["order_id"],),
            ).fetchone()[0] == "completed"
            assert conn.execute(
                "SELECT state FROM panel_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()[0] == "succeeded"
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_ledger "
                "WHERE order_id=? AND business_key LIKE '%:reverse:%'",
                (direct_order["order_id"],),
            ).fetchone()[0] == 0
            delivery = conn.execute(
                "SELECT id FROM delivery_outbox WHERE event_key=?",
                (f"panel-operation:{operation_id}:succeeded",),
            ).fetchone()
            assert delivery is not None
            delivery_id = int(delivery["id"])
        assert len(readback_calls) == 1

        # A second, explicit wallet order exercises the full immutable edge chain:
        # root edge 900, child edge 990, payer wallet 1000. It is captured once.
        wallet_quote = None
        with database.get_db() as conn:
            wallet_quote = quotes.create_quote(
                conn, actor_tg_id=1000, bot_id=9001, merchant_key="root",
                purpose="wallet_topup", amount_toman=1000,
                spec={"purpose": "wallet_topup", "payer_tg_id": 1000},
                price_edges=[
                    {"seller_key": "root", "buyer_key": "rep:202",
                     "source_key": "edge:900", "price_toman": 900},
                    {"seller_key": "rep:202", "buyer_key": "rep:204",
                     "source_key": "edge:990", "price_toman": 990},
                    {"seller_key": "rep:204", "buyer_key": "user:1000",
                     "source_key": "wallet:root:1000", "price_toman": 1000},
                ],
                now=now + 12,
            )
            wallet_order = orders.create_order(
                conn, quote_id=wallet_quote["quote_id"], actor_tg_id=1000,
                bot_id=9001, payment_rail="wallet", now=now + 12,
            )
            assert len(wallet_order["hold_ids"]) == 3
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE payment_orders SET status='accepted',received_toman=1000,"
                "revision=revision+1 WHERE order_id=? AND status='awaiting_payment'",
                (wallet_order["order_id"],),
            )
            captured = financial.capture_holds(
                conn, order_id=wallet_order["order_id"], actor_id=1000, now=now + 13,
            )
            conn.commit()
            assert captured["replayed"] is False
            assert len(captured["ledger_ids"]) == 3
            ledger_rows = conn.execute(
                "SELECT edge_id,user_id,amount,transaction_type FROM financial_ledger "
                "WHERE order_id=? AND business_key LIKE 'order:%:capture:%' ORDER BY amount",
                (wallet_order["order_id"],),
            ).fetchall()
            assert [(row["edge_id"], row["user_id"], row["amount"], row["transaction_type"])
                    for row in ledger_rows] == [
                (900, None, 900, "debit"),
                (990, None, 990, "debit"),
                (None, 1000, 1000, "debit"),
            ]
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_ledger WHERE order_id=? "
                "AND business_key LIKE 'order:%:capture:%'",
                (wallet_order["order_id"],),
            ).fetchone()[0] == 3
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_ledger WHERE order_id=? "
                "AND business_key LIKE '%:reverse:%'",
                (wallet_order["order_id"],),
            ).fetchone()[0] == 0

        # The outbox worker treats an explicit Telegram retry-after timeout as a
        # safe retry signal. Ambiguous timeout without that signal remains UNKNOWN.
        class TelegramRetryTimeout(TimeoutError):
            retry_after = 1

        sends = []
        successful_secret_deliveries = []

        def send_telegram(bot_id, token, chat_id, thread_id, resource):
            sends.append((bot_id, chat_id, thread_id))
            if len(sends) == 1:
                raise TelegramRetryTimeout("synthetic Telegram timeout with retry-after")
            successful_secret_deliveries.append(resource["secret"])
            return {"message_id": 771}

        def resolve_private_resource(_conn, _claim):
            return {"secret": "W20_SINGLE_DELIVERY_SECRET"}

        with database.get_db() as conn:
            first_delivery = outbox_worker.deliver_one(
                conn, delivery_id=delivery_id, now=now + 20,
                send=send_telegram, resolve_resource=resolve_private_resource,
                authorize_delivery=lambda _conn, _claim: True,
                rate_limiter=lambda _bot, _chat: 0,
            )
            assert first_delivery["state"] == "pending"
            second_delivery = outbox_worker.deliver_one(
                conn, delivery_id=delivery_id, now=now + 30,
                send=send_telegram, resolve_resource=resolve_private_resource,
                authorize_delivery=lambda _conn, _claim: True,
                rate_limiter=lambda _bot, _chat: 0,
            )
            assert second_delivery["state"] == "sent"
            assert len(sends) == 2
            assert successful_secret_deliveries == ["W20_SINGLE_DELIVERY_SECRET"]
            stored = conn.execute(
                "SELECT state,attempts,message_id FROM delivery_outbox WHERE id=?",
                (delivery_id,),
            ).fetchone()
            assert tuple(stored) == ("sent", 2, 771)

        # Two administrators race one pending receipt: a single CAS winner posts.
        race_order, race_attempt, race_receipt = _card_receipt(
            database, quotes, orders, payments,
            actor=102, bot=9001, amount=150, purpose="wallet_topup", now=now + 140,
        )
        with database.get_db() as conn:
            _nonce(
                conn, nonce="w20-race-admin-a", reviewer=9090, bot=9001,
                receipt=race_receipt["receipt_id"], expires=now + 1000,
            )
            _nonce(
                conn, nonce="w20-race-admin-b", reviewer=9091, bot=9001,
                receipt=race_receipt["receipt_id"], expires=now + 1000,
            )
        barrier = threading.Barrier(2)
        outcomes = []
        result_lock = threading.Lock()

        def approve_race(reviewer, nonce):
            local = sqlite3.connect(str(db_path), timeout=30)
            local.row_factory = sqlite3.Row
            local.execute("PRAGMA foreign_keys=ON")
            local.execute("PRAGMA busy_timeout=30000")
            barrier.wait(timeout=10)
            try:
                result = payments.approve_order_receipt(
                    local, receipt_id=race_receipt["receipt_id"],
                    reviewer_tg_id=reviewer, bot_id=9001,
                    nonce=nonce, now=now + 141,
                )
                item = ("approved", result)
            except payments.ReviewConflict as exc:
                item = ("lost", str(exc))
            finally:
                local.close()
            with result_lock:
                outcomes.append(item)

        threads = [
            threading.Thread(target=approve_race, args=(9090, "w20-race-admin-a")),
            threading.Thread(target=approve_race, args=(9091, "w20-race-admin-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert all(not thread.is_alive() for thread in threads)
        assert sorted(item[0] for item in outcomes) == ["approved", "lost"]
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_ledger WHERE business_key=?",
                (f"receipt:{race_receipt['receipt_id']}:wallet-credit",),
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT status FROM payment_receipts WHERE id=?",
                (race_receipt["receipt_id"],),
            ).fetchone()[0] == "approved"
            assert conn.execute(
                "SELECT status FROM payment_orders WHERE order_id=?",
                (race_order["order_id"],),
            ).fetchone()[0] == "accepted"

        # A late payment on an expired top-up credits only the payer wallet.
        with database.get_db() as conn:
            late_quote = quotes.create_quote(
                conn, actor_tg_id=103, bot_id=9001, merchant_key="root",
                purpose="wallet_topup", amount_toman=75,
                spec={"purpose": "wallet_topup", "payer_tg_id": 103},
                price_edges=[], now=now + 1000,
            )
            late_order = orders.create_order(
                conn, quote_id=late_quote["quote_id"], actor_tg_id=103,
                bot_id=9001, payment_rail="card_to_card", now=now + 1000,
            )
            late_attempt = payments.create_card_attempt(
                conn, order_id=late_order["order_id"], actor_tg_id=103,
                bot_id=9001, now=now + 1600,
            )
            expired = orders.expire_unpaid_orders(conn, now=now + 2810)
            assert late_order["order_id"] in expired["expired_order_ids"]
            late_receipt = payments.submit_receipt(
                conn, attempt_id=late_attempt["attempt_id"], actor_tg_id=103,
                bot_id=9001, amount_toman=75, receipt_photo_id="w20-late-topup",
                now=now + 2900,
            )
            assert late_receipt["order_status"] == "expired"
            _nonce(
                conn, nonce="w20-late-approve", reviewer=9090, bot=9001,
                receipt=late_receipt["receipt_id"], expires=now + 3400,
            )
            late_result = payments.approve_order_receipt(
                conn, receipt_id=late_receipt["receipt_id"],
                reviewer_tg_id=9090, bot_id=9001,
                nonce="w20-late-approve", now=now + 2901,
            )
            assert late_result["late_payment"] is True
            assert late_result["wallet_credit"] == 75
            assert late_result["panel_job"] is False
            assert conn.execute(
                "SELECT status FROM payment_orders WHERE order_id=?",
                (late_order["order_id"],),
            ).fetchone()[0] == "expired"
            assert conn.execute(
                "SELECT COUNT(*) FROM scheduled_jobs WHERE job_key=?",
                (f"fulfill:order:{late_order['order_id']}",),
            ).fetchone()[0] == 0
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_ledger WHERE order_id=? "
                "AND business_key LIKE '%:capture:%'",
                (late_order["order_id"],),
            ).fetchone()[0] == 0

        assert Path(sentinel) != Path(database.DB_PATH)
    assert network_attempts == []
