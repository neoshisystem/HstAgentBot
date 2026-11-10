"""Manual bank-refund requests backed by ledger-derived wallet holds."""
from __future__ import annotations

from contextlib import contextmanager
import json
import re
import sqlite3
import uuid
from collections.abc import Iterator, Mapping

from crypto_store import decrypt_secret, encrypt_secret
from financial_service import (
    _sync_projection_cache, _wallet_balance, available_balance, post_entry,
)


class RefundError(ValueError):
    """A manual refund cannot be safely requested or finalized."""


class RefundConflict(RefundError):
    """A refund key or terminal decision conflicts with an earlier action."""


class RefundUnauthorized(RefundError):
    """Only the configured root administrator may finalize a bank refund."""


class RefundFundsUnavailable(RefundError):
    """The wallet cannot fund the requested refund without spending held funds."""


@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[None]:
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    if conn.in_transaction:
        conn.execute("SAVEPOINT w08_refund")
        try:
            yield
            conn.execute("RELEASE SAVEPOINT w08_refund")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT w08_refund")
            conn.execute("RELEASE SAVEPOINT w08_refund")
            raise
    else:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise


def _positive(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RefundError(f"{name} must be a positive integer")
    return value


def _root_admin_id() -> int:
    import config
    value = getattr(config, "ADMIN_TELEGRAM_ID", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise RefundUnauthorized("configured root administrator identity is unavailable")
    return value


def _merchant(merchant_key: str) -> tuple[str, int | None]:
    if merchant_key == "root":
        return "wallet:root", None
    match = re.fullmatch(r"rep:([1-9][0-9]*)", merchant_key or "")
    if match is None:
        raise RefundError("merchant_key is invalid")
    rep_id = int(match.group(1))
    return f"wallet:rep:{rep_id}", rep_id


def _audit(conn: sqlite3.Connection, *, actor_id: int, refund_id: str,
           decision: str, amount: int, now: int, cipher: str) -> None:
    details = json.dumps(
        {"refund_id": refund_id, "decision": decision, "amount_toman": amount},
        sort_keys=True, separators=(",", ":"),
    )
    conn.execute(
        "INSERT INTO audit_logs(rep_id,account_code,email,action,actor_id,actor_name,"
        "details_json,result_status,details_cipher) VALUES(NULL,'','',?,?, ?,?,'recorded',?)",
        (f"bank_refund.{decision}", actor_id, "root-admin", details, cipher),
    )


def request_bank_refund(
    conn: sqlite3.Connection,
    *,
    refund_id: str,
    merchant_key: str,
    user_tg_id: int,
    requester_tg_id: int,
    amount_toman: int,
    bank_details: Mapping[str, object],
    now: int,
    order_id: str | None = None,
) -> dict:
    """Create a processing request and an active hold without changing posted balance."""
    user_tg_id = _positive(user_tg_id, "user_tg_id")
    requester_tg_id = _positive(requester_tg_id, "requester_tg_id")
    amount_toman = _positive(amount_toman, "amount_toman")
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise RefundError("now must be a non-negative UTC epoch integer")
    if not isinstance(refund_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,120}", refund_id):
        raise RefundError("refund_id must be a stable non-empty idempotency key")
    if not isinstance(bank_details, Mapping):
        raise RefundError("bank_details must be a mapping")
    destination = str(bank_details.get("destination") or "").strip()
    if not destination:
        raise RefundError("bank destination is required")
    wallet_prefix, _ = _merchant(merchant_key)
    source_key = f"{wallet_prefix}:{user_tg_id}"
    details_json = json.dumps(dict(bank_details), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    details_cipher = encrypt_secret(details_json, "hstagent/columns/v1")
    hold_key = f"refund:{refund_id}:wallet"
    if requester_tg_id != user_tg_id and requester_tg_id != _root_admin_id():
        raise RefundUnauthorized("only the wallet owner or configured root may request this refund")

    with _atomic(conn):
        prior = conn.execute(
            "SELECT order_id,merchant_key,user_tg_id,amount_toman,state,bank_details_cipher,wallet_hold_key "
            "FROM refund_requests WHERE refund_id=?",
            (refund_id,),
        ).fetchone()
        if prior is not None:
            try:
                same_details = decrypt_secret(str(prior[5]), "hstagent/columns/v1") == details_json
            except Exception as exc:
                raise RefundConflict("stored refund bank details failed authentication") from exc
            if (prior[0] == order_id and str(prior[1]) == merchant_key and int(prior[2]) == user_tg_id
                    and int(prior[3]) == amount_toman and str(prior[6]) == hold_key and same_details):
                return {"refund_id": refund_id, "state": str(prior[4]), "replayed": True}
            raise RefundConflict("refund_id is already bound to different request data")
        if order_id is not None:
            order = conn.execute(
                "SELECT actor_tg_id,merchant_key,status FROM payment_orders WHERE order_id=?",
                (order_id,),
            ).fetchone()
            if order is None or int(order[0]) != user_tg_id or str(order[1]) != merchant_key:
                raise RefundError("refund order does not belong to this payer and merchant")
            if str(order[2]) not in {"accepted", "completed", "exception", "closed_refunded"}:
                raise RefundError("only a previously paid or failed-paid order can be refunded")
        available = available_balance(conn, source_key)
        if available is None or amount_toman > available:
            raise RefundFundsUnavailable(
                f"refund amount {amount_toman} exceeds spendable wallet balance {available}"
            )
        conn.execute(
            "INSERT INTO refund_requests(refund_id,order_id,merchant_key,user_tg_id,amount_toman,"
            "state,wallet_hold_key,bank_details_cipher,evidence_cipher,reviewer_tg_id,created_at,settled_at) "
            "VALUES(?,?,?,?,?,'processing',?,?,NULL,NULL,?,NULL)",
            (refund_id, order_id, merchant_key, user_tg_id, amount_toman, hold_key, details_cipher, now),
        )
        conn.execute(
            "INSERT INTO wallet_refund_holds(hold_key,merchant_key,user_tg_id,amount_toman,state,created_at) "
            "VALUES(?,?,?,?,'active',?)",
            (hold_key, merchant_key, user_tg_id, amount_toman, now),
        )
        return {
            "refund_id": refund_id, "state": "processing", "amount_toman": amount_toman,
            "available_after_hold": available - amount_toman, "replayed": False,
        }


def _consume_refund_nonce(
    conn: sqlite3.Connection, *, nonce: str, bot_id: int, reviewer_tg_id: int,
    refund_id: str, now: int,
) -> None:
    if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24}", nonce):
        raise RefundUnauthorized("a valid 24-character one-use confirmation nonce is required")
    row = conn.execute(
        "SELECT action,target_type,target_id,expected_revision,expires_at,consumed_at,bot_id,actor_tg_id "
        "FROM action_confirmations WHERE nonce=?",
        (nonce,),
    ).fetchone()
    if (
        row is None or str(row[0]) != "refund.finalize" or str(row[1]) != "refund_request"
        or str(row[2]) != refund_id or int(row[3]) != 1 or int(row[4]) <= now
        or row[5] is not None or int(row[6]) != bot_id or int(row[7]) != reviewer_tg_id
    ):
        raise RefundUnauthorized("refund confirmation is invalid, expired, consumed or out of scope")
    changed = conn.execute(
        "UPDATE action_confirmations SET consumed_at=? WHERE nonce=? AND action='refund.finalize' "
        "AND target_type='refund_request' AND target_id=? AND expected_revision=1 "
        "AND actor_tg_id=? AND bot_id=? AND consumed_at IS NULL AND expires_at>?",
        (now, nonce, refund_id, reviewer_tg_id, bot_id, now),
    )
    if changed.rowcount != 1:
        raise RefundConflict("refund confirmation was consumed concurrently")


def finalize_bank_refund(
    conn: sqlite3.Connection,
    *,
    refund_id: str,
    reviewer_tg_id: int,
    bot_id: int,
    nonce: str,
    decision: str,
    review_note: str,
    now: int,
    bank_reference: str | None = None,
    destination: str | None = None,
    amount_toman: int | None = None,
) -> dict:
    """Root-only second step: settle once, release on rejection, or retain hold on UNKNOWN."""
    reviewer_tg_id = _positive(reviewer_tg_id, "reviewer_tg_id")
    bot_id = _positive(bot_id, "bot_id")
    if reviewer_tg_id != _root_admin_id():
        raise RefundUnauthorized("bank refunds require the configured root administrator")
    if decision not in {"settled", "rejected", "unknown"}:
        raise RefundError("decision must be settled, rejected or unknown")
    note = str(review_note or "").strip()
    if len(note) < 8:
        raise RefundError("a root review note of at least eight characters is required")
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise RefundError("now must be a non-negative UTC epoch integer")

    with _atomic(conn):
        row = conn.execute(
            "SELECT order_id,merchant_key,user_tg_id,amount_toman,state,wallet_hold_key,"
            "bank_details_cipher,evidence_cipher FROM refund_requests WHERE refund_id=?",
            (refund_id,),
        ).fetchone()
        if row is None:
            raise RefundError("refund request does not exist")
        state, amount, hold_key = str(row[4]), int(row[3]), str(row[5])
        if state != "processing":
            if state == decision:
                ledger = conn.execute(
                    "SELECT id FROM financial_ledger WHERE business_key=?",
                    (f"refund:{refund_id}:wallet-debit",),
                ).fetchone() if decision == "settled" else None
                return {"refund_id": refund_id, "state": state, "replayed": True,
                        "ledger_id": int(ledger[0]) if ledger else None}
            raise RefundConflict("refund request is already terminal; UNKNOWN is never auto-retried")
        _consume_refund_nonce(
            conn, nonce=nonce, bot_id=bot_id, reviewer_tg_id=reviewer_tg_id,
            refund_id=refund_id, now=now,
        )
        hold = conn.execute(
            "SELECT state,amount_toman,merchant_key,user_tg_id FROM wallet_refund_holds WHERE hold_key=?",
            (hold_key,),
        ).fetchone()
        if hold is None or str(hold[0]) != "active" or int(hold[1]) != amount:
            raise RefundConflict("active refund hold is missing or inconsistent")

        evidence = {"refund_id": refund_id, "decision": decision, "review_note": note}
        if decision == "settled":
            reference = str(bank_reference or "").strip()
            target = str(destination or "").strip()
            if not reference or not target or isinstance(amount_toman, bool) or amount_toman != amount:
                raise RefundError("settlement requires bank reference, destination and exact amount")
            try:
                requested_details = json.loads(decrypt_secret(str(row[6]), "hstagent/columns/v1"))
            except Exception as exc:
                raise RefundConflict("encrypted bank destination cannot be authenticated") from exc
            if target != str(requested_details.get("destination") or ""):
                raise RefundError("confirmed bank destination does not match the requested destination")
            evidence.update({"bank_reference": reference, "destination": target, "amount_toman": amount})
        elif decision == "rejected":
            evidence["hold_action"] = "release"
        else:
            evidence["hold_action"] = "retain_unknown_no_retry"

        evidence_cipher = encrypt_secret(
            json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "hstagent/columns/v1",
        )
        ledger_id = None
        if decision == "settled":
            merchant_key, user_id = str(row[1]), int(row[2])
            balance = _wallet_balance(conn, merchant_key, user_id)
            if balance < amount:
                raise RefundFundsUnavailable("reserved wallet funds are no longer present in the ledger")
            rep_id = int(merchant_key.split(":", 1)[1]) if merchant_key.startswith("rep:") else None
            ledger_id = post_entry(
                conn, rep_id=rep_id, user_id=user_id, transaction_type="debit",
                amount=amount, balance_after=balance - amount,
                description=f"Manual bank refund {refund_id}", actor_id=reviewer_tg_id,
                business_key=f"refund:{refund_id}:wallet-debit", merchant_key=merchant_key,
                ledger_scope="wallet", order_id=str(row[0]) if row[0] is not None else None,
                operation_id=f"refund:{refund_id}:settle",
            )
            _sync_projection_cache(conn, ledger_scope="wallet", merchant_key=merchant_key, user_id=user_id)
            changed = conn.execute(
                "UPDATE wallet_refund_holds SET state='captured' WHERE hold_key=? AND state='active'",
                (hold_key,),
            )
            if changed.rowcount != 1:
                raise RefundConflict("refund hold was consumed concurrently")
        elif decision == "rejected":
            changed = conn.execute(
                "UPDATE wallet_refund_holds SET state='released' WHERE hold_key=? AND state='active'",
                (hold_key,),
            )
            if changed.rowcount != 1:
                raise RefundConflict("refund hold was consumed concurrently")

        changed = conn.execute(
            "UPDATE refund_requests SET state=?,reviewer_tg_id=?,evidence_cipher=?,settled_at=? "
            "WHERE refund_id=? AND state='processing'",
            (decision, reviewer_tg_id, evidence_cipher, now, refund_id),
        )
        if changed.rowcount != 1:
            raise RefundConflict("refund request changed before finalization")
        details = {"refund_id": refund_id, "decision": decision, "amount_toman": amount}
        conn.execute(
            "INSERT INTO audit_logs(rep_id,account_code,email,action,actor_id,actor_name,details_json,"
            "result_status,details_cipher) VALUES(NULL,'','',?,?, ?,?,'recorded',?)",
            (f"bank_refund.{decision}", reviewer_tg_id, "root-admin",
             json.dumps(details, sort_keys=True, separators=(",", ":")), evidence_cipher),
        )
        return {"refund_id": refund_id, "state": decision, "ledger_id": ledger_id,
                "hold_state": "active" if decision == "unknown" else ("captured" if decision == "settled" else "released"),
                "replayed": False}


__all__ = [
    "RefundError", "RefundConflict", "RefundUnauthorized", "RefundFundsUnavailable",
    "request_bank_refund", "finalize_bank_refund",
]
