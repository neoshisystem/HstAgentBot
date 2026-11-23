"""Offline acceptance for nonce-bound atomic review, late top-up and no panel I/O."""
from __future__ import annotations

import base64
from pathlib import Path
import importlib.util
import json
import sqlite3
import threading
import time

import pytest

from crypto_store import encrypt_secret
from financial_service import post_entry
from migrations_runner import apply_migrations
from order_service import create_order, expire_unpaid_orders
from payment_service import (
    ReviewConflict, approve_order_receipt, create_card_attempt,
    reject_order_receipt, submit_receipt,
)
from quote_service import create_quote


def _helpers():
    p=Path(__file__).with_name("v52_helpers.py")
    spec=importlib.util.spec_from_file_location("_w08_review_helpers",p)
    assert spec is not None and spec.loader is not None
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _quote(conn, *, actor, bot, merchant, purpose, amount, now, panel=None):
    spec={"purpose":purpose,"payer_tg_id":actor}
    if panel is not None:
        spec["panel"]=panel
    return create_quote(
        conn,actor_tg_id=actor,bot_id=bot,merchant_key=merchant,
        purpose=purpose,amount_toman=amount,spec=spec,price_edges=[],
        panel_id=None if panel is None else panel["panel_id"],now=now,
    )


def _seed_nonce(conn, *, nonce, actor, bot, receipt, action, expires):
    conn.execute(
        "INSERT INTO action_confirmations(nonce,actor_tg_id,bot_id,action,target_type,target_id,"
        "expected_revision,payload_json,expires_at,consumed_at,operation_id) "
        "VALUES(?,?,?,?,?,?,1,'{}',?,NULL,NULL)",
        (nonce,actor,bot,f"receipt.{action}","receipt",str(receipt),expires),
    )


def _create_attempt(database, *, actor, bot, merchant, purpose, amount, now, panel=None):
    with database.get_db() as conn:
        quote=_quote(
            conn,actor=actor,bot=bot,merchant=merchant,purpose=purpose,
            amount=amount,now=now,panel=panel,
        )
        order=create_order(
            conn,quote_id=quote["quote_id"],actor_tg_id=actor,bot_id=bot,
            payment_rail="card_to_card",now=now,
            capacity_snapshot_count=0 if panel is not None else None,
            capacity_snapshot_at=now if panel is not None else None,
        )
        return order


def test_two_reviewers_and_late_topup(tmp_path, monkeypatch):
    root=Path(__file__).resolve().parents[1]
    helpers=_helpers()
    network_attempts=helpers.guard_network(monkeypatch)
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    database,sentinel=helpers.load_database_module(root,monkeypatch,tmp_path)
    now=int(time.time())

    with helpers.make_db(database,monkeypatch,tmp_path,filename="w08-review.sqlite3") as db_path:
        with database.get_db() as conn:
            assert len(apply_migrations(conn,root/"migrations",now=now))==18
            helpers.seed_actor(database,actor_tg_id=101,rep_id=202,rep_tg_id=303)
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision) "
                "VALUES(9001,NULL,1,NULL,'w08-root','active',?,1)",
                (now,),
            )
            conn.execute(
                "INSERT INTO bank_cards(id,merchant_key,card_number_cipher,card_holder,bank_name,"
                "target_scope,priority,daily_limit_toman,is_active) "
                "VALUES(1,'root',?,'Root Holder','Root Bank','all',1,0,1)",
                (encrypt_secret("4111-1111-1111-1111","hstagent/columns/v1"),),
            )
            for reviewer in (901,902):
                conn.execute(
                    "INSERT INTO bot_users(telegram_id,role,rep_id,username,full_name,is_blocked) "
                    "VALUES(?,'admin',NULL,?,?,0)",
                    (reviewer,f"w08_admin_{reviewer}",f"W08 Admin {reviewer}"),
                )

        # Two authorized reviewers race with distinct one-use nonces. BEGIN IMMEDIATE
        # plus receipt.status='pending' CAS permits exactly one approval and posting.
        first_order=_create_attempt(
            database,actor=101,bot=9001,merchant="root",
            purpose="wallet_topup",amount=100,now=now,
        )
        with database.get_db() as conn:
            attempt=create_card_attempt(
                conn,order_id=first_order["order_id"],actor_tg_id=101,
                bot_id=9001,now=now,
            )
            receipt=submit_receipt(
                conn,attempt_id=attempt["attempt_id"],actor_tg_id=101,
                bot_id=9001,amount_toman=100,receipt_photo_id="w08-receipt-race",
                now=now,
            )
            _seed_nonce(conn,nonce="w08-approve-a",actor=901,bot=9001,
                        receipt=receipt["receipt_id"],action="approve",expires=now+600)
            _seed_nonce(conn,nonce="w08-approve-b",actor=902,bot=9001,
                        receipt=receipt["receipt_id"],action="approve",expires=now+600)

        barrier=threading.Barrier(2)
        outcomes=[]
        lock=threading.Lock()

        def review(reviewer,nonce):
            local=sqlite3.connect(str(db_path),timeout=30)
            local.row_factory=sqlite3.Row
            local.execute("PRAGMA foreign_keys=ON")
            local.execute("PRAGMA busy_timeout=30000")
            barrier.wait(timeout=10)
            try:
                result=approve_order_receipt(
                    local,receipt_id=receipt["receipt_id"],reviewer_tg_id=reviewer,
                    bot_id=9001,nonce=nonce,now=now+1,
                )
                item=("approved",result)
            except ReviewConflict as exc:
                item=("lost",str(exc))
            finally:
                local.close()
            with lock:
                outcomes.append(item)

        threads=[
            threading.Thread(target=review,args=(901,"w08-approve-a")),
            threading.Thread(target=review,args=(902,"w08-approve-b")),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert all(not t.is_alive() for t in threads)
        assert sorted(x[0] for x in outcomes)==["approved","lost"]
        with database.get_db() as conn:
            posting=conn.execute(
                "SELECT id,amount,business_key FROM financial_ledger "
                "WHERE business_key=?",
                (f"receipt:{receipt['receipt_id']}:wallet-credit",),
            ).fetchall()
            assert len(posting)==1
            assert int(posting[0][1])==100
            assert conn.execute(
                "SELECT status FROM payment_receipts WHERE id=?",
                (receipt["receipt_id"],),
            ).fetchone()[0]=="approved"
            assert conn.execute(
                "SELECT status FROM payment_orders WHERE order_id=?",
                (first_order["order_id"],),
            ).fetchone()[0]=="accepted"
            assert conn.execute(
                "SELECT state FROM payment_attempts WHERE attempt_id=?",
                (attempt["attempt_id"],),
            ).fetchone()[0]=="succeeded"
            assert conn.execute(
                "SELECT balance_toman FROM user_wallets WHERE user_id=101",
            ).fetchone()[0]==100
            reject_nonce="w08-reject-after-approve"
            _seed_nonce(conn,nonce=reject_nonce,actor=902,bot=9001,
                        receipt=receipt["receipt_id"],action="reject",expires=now+600)
            with pytest.raises(ReviewConflict):
                reject_order_receipt(
                    conn,receipt_id=receipt["receipt_id"],reviewer_tg_id=902,
                    bot_id=9001,nonce=reject_nonce,now=now+2,
                )

        # An attempt opened shortly before the order deadline can submit after
        # the expiry worker wins the race. Wallet-top-up late receipts credit only
        # the same merchant wallet and leave the order's expired state intact.
        late_order=_create_attempt(
            database,actor=101,bot=9001,merchant="root",
            purpose="wallet_topup",amount=75,now=now+10,
        )
        with database.get_db() as conn:
            late_attempt=create_card_attempt(
                conn,order_id=late_order["order_id"],actor_tg_id=101,
                bot_id=9001,now=now+1600,
            )
            expired=expire_unpaid_orders(conn,now=now+1811)
            assert late_order["order_id"] in expired["expired_order_ids"]
        with database.get_db() as conn:
            late_receipt=submit_receipt(
                conn,attempt_id=late_attempt["attempt_id"],actor_tg_id=101,
                bot_id=9001,amount_toman=75,receipt_photo_id="w08-late-topup",
                now=now+1900,
            )
            assert late_receipt["order_status"]=="expired"
            _seed_nonce(
                conn,nonce="w08-late-approve",actor=901,bot=9001,
                receipt=late_receipt["receipt_id"],action="approve",expires=now+2500,
            )
        with database.get_db() as conn:
            result=approve_order_receipt(
                conn,receipt_id=late_receipt["receipt_id"],reviewer_tg_id=901,
                bot_id=9001,nonce="w08-late-approve",now=now+1901,
            )
            assert result["status"]=="expired"
            assert result["late_payment"] is True
            assert result["wallet_credit"]==75
            assert conn.execute(
                "SELECT status FROM payment_orders WHERE order_id=?",
                (late_order["order_id"],),
            ).fetchone()[0]=="expired"
            assert conn.execute(
                "SELECT balance_toman FROM user_wallets WHERE user_id=101",
            ).fetchone()[0]==175
            assert conn.execute(
                "SELECT COUNT(*) FROM scheduled_jobs WHERE job_key LIKE 'fulfill:%'"
            ).fetchone()[0]==0

        # A direct VPN receipt posts no buyer-wallet credit at approval time; it
        # creates one durable fulfillment job and performs no network/panel call.
        with database.get_db() as conn:
            conn.execute(
                "INSERT INTO panels(id,panel_code,name,panel_type,lifecycle,max_clients) "
                "VALUES(501,'W08-VPN-PANEL','W08 VPN panel','3x-ui','active',100)"
            )
        vpn_order=_create_attempt(
            database,actor=101,bot=9001,merchant="root",purpose="vpn_buy",
            amount=500,now=now+2100,
            panel={"panel_id":501,"active_count":0,"snapshot_at":now+2100},
        )
        with database.get_db() as conn:
            vpn_attempt=create_card_attempt(
                conn,order_id=vpn_order["order_id"],actor_tg_id=101,
                bot_id=9001,now=now+2100,
            )
            vpn_receipt=submit_receipt(
                conn,attempt_id=vpn_attempt["attempt_id"],actor_tg_id=101,
                bot_id=9001,amount_toman=500,receipt_photo_id="w08-vpn-receipt",
                now=now+2100,
            )
            _seed_nonce(
                conn,nonce="w08-vpn-approve",actor=901,bot=9001,
                receipt=vpn_receipt["receipt_id"],action="approve",expires=now+2600,
            )
        with database.get_db() as conn:
            vpn_result=approve_order_receipt(
                conn,receipt_id=vpn_receipt["receipt_id"],reviewer_tg_id=901,
                bot_id=9001,nonce="w08-vpn-approve",now=now+2101,
            )
            assert vpn_result["status"]=="accepted"
            assert vpn_result["wallet_credit"]==0
            assert conn.execute(
                "SELECT balance_toman FROM user_wallets WHERE user_id=101",
            ).fetchone()[0]==175
            jobs=conn.execute(
                "SELECT kind,state,result_json FROM scheduled_jobs WHERE job_key=?",
                (f"fulfill:order:{vpn_order['order_id']}",),
            ).fetchall()
            assert len(jobs)==1
            assert (jobs[0][0],jobs[0][1])==("fulfill_order","pending")
            assert json.loads(jobs[0][2])["receipt_id"]==vpn_receipt["receipt_id"]

        # Representative settlement uses the parent-owned settlement card,
        # reduces the exact edge debt once, and never creates a panel job.
        with database.get_db() as conn:
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
                "INSERT INTO financial_ledger(rep_id,user_id,transaction_type,amount,balance_after,"
                "description,actor_id,business_key,merchant_key,ledger_scope,edge_id,policy_version) "
                "VALUES(204,NULL,'debit',500,500,'synthetic child debt',303,'w08-child-debt-open',"
                "'rep:202','edge',602,'v1.0')"
            )
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision) "
                "VALUES(9002,204,0,NULL,'w08-child','active',?,1)",
                (now,),
            )
            conn.execute(
                "INSERT INTO bot_users(telegram_id,role,rep_id,username,full_name,is_blocked) "
                "VALUES(404,'rep',204,'w08-child','W08 child',0)"
            )
            conn.execute(
                "INSERT INTO bank_cards(id,merchant_key,card_number_cipher,card_holder,bank_name,"
                "target_scope,priority,daily_limit_toman,is_active) "
                "VALUES(2,'rep:202',?,'Parent Holder','Parent Bank','rep_settlement',1,0,1)",
                (encrypt_secret("4222-2222-2222-2222","hstagent/columns/v1"),),
            )

        rep_order=_create_attempt(
            database,actor=404,bot=9002,merchant="rep:204",
            purpose="rep_settlement",amount=100,now=now+2300,
        )
        with database.get_db() as conn:
            rep_attempt=create_card_attempt(
                conn,order_id=rep_order["order_id"],actor_tg_id=404,
                bot_id=9002,now=now+2300,
            )
            assert rep_attempt["bank_card_id"]==2
            rep_receipt=submit_receipt(
                conn,attempt_id=rep_attempt["attempt_id"],actor_tg_id=404,
                bot_id=9002,amount_toman=100,receipt_photo_id="w08-rep-settlement",
                now=now+2300,
            )
            _seed_nonce(
                conn,nonce="w08-rep-approve",actor=901,bot=9002,
                receipt=rep_receipt["receipt_id"],action="approve",expires=now+2800,
            )
        with database.get_db() as conn:
            rep_result=approve_order_receipt(
                conn,receipt_id=rep_receipt["receipt_id"],reviewer_tg_id=901,
                bot_id=9002,nonce="w08-rep-approve",now=now+2301,
            )
            assert rep_result["status"]=="accepted"
            assert rep_result["wallet_credit"]==0
            ledger=conn.execute(
                "SELECT transaction_type,amount,ledger_scope,edge_id FROM financial_ledger "
                "WHERE business_key=?",
                (f"receipt:{rep_receipt['receipt_id']}:rep-settlement",),
            ).fetchone()
            assert (ledger[0],int(ledger[1]),ledger[2],int(ledger[3]))==("credit",100,"edge",602)
            assert conn.execute(
                "SELECT debt_toman FROM rep_edges WHERE id=602"
            ).fetchone()[0]==400
            assert conn.execute(
                "SELECT COUNT(*) FROM scheduled_jobs WHERE job_key=?",
                (f"fulfill:order:{rep_order['order_id']}",),
            ).fetchone()[0]==0

        assert Path(sentinel)!=Path(database.DB_PATH)
    assert network_attempts==[]
