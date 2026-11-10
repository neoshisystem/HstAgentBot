"""Offline-safe bank-card selection, receipt submission and atomic review."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import re
import sqlite3
import uuid
from typing import Iterator
from zoneinfo import ZoneInfo

from crypto_store import encrypt_secret, decrypt_secret
from financial_service import (
    FinancialError,
    _edge_debt,
    _sync_projection_cache,
    _wallet_balance,
    post_entry,
)
from order_service import release_order_resources


PAYMENT_ATTEMPT_TTL_SECONDS = 1800
RECEIPT_COOLDOWN_SECONDS = 120
_TEHRAN = ZoneInfo("Asia/Tehran")


class PaymentError(ValueError):
    """A card, payment attempt, receipt or review action is invalid."""


class NoEligibleCard(PaymentError):
    """No active card has compatible scope and remaining daily capacity."""


class AttemptExpired(PaymentError):
    """The payment attempt has expired and cannot be reused."""


class ReceiptCooldown(PaymentError):
    """A receipt was submitted inside the cross-bot user cooldown."""


class ReviewConflict(PaymentError):
    """A receipt or reviewer nonce has already been consumed."""


class ReviewerNotAuthorized(PaymentError):
    """The reviewer lacks authority over this order's merchant."""


@contextmanager
def _atomic(conn: sqlite3.Connection) -> Iterator[None]:
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    if conn.in_transaction:
        conn.execute("SAVEPOINT w08_payment")
        try:
            yield
            conn.execute("RELEASE SAVEPOINT w08_payment")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT w08_payment")
            conn.execute("RELEASE SAVEPOINT w08_payment")
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
        raise PaymentError(f"{name} must be a positive integer")
    return value


def _timestamp(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PaymentError("now must be a non-negative UTC epoch-seconds integer")
    return value


def _tehran_date(now: int) -> str:
    return datetime.fromtimestamp(now, timezone.utc).astimezone(_TEHRAN).date().isoformat()


def _bot_merchant(conn: sqlite3.Connection, bot_id: int) -> tuple[str, int | None]:
    row = conn.execute(
        "SELECT rep_id,is_primary,status FROM bot_registry WHERE bot_id=?",
        (bot_id,),
    ).fetchone()
    if row is None or str(row[2]) != "active":
        raise PaymentError("active bot registry identity is unavailable")
    if bool(row[1]):
        if row[0] is not None:
            raise PaymentError("primary bot cannot be assigned to a representative")
        return "root", None
    if row[0] is None:
        raise PaymentError("bot has no merchant identity")
    rep_id = int(row[0])
    merchant = conn.execute(
        "SELECT is_active,deleted_at FROM representatives WHERE id=?",
        (rep_id,),
    ).fetchone()
    if merchant is None or not bool(merchant[0]) or merchant[1] is not None:
        raise PaymentError("representative merchant is inactive")
    return f"rep:{rep_id}", rep_id


def _parent_merchant(conn: sqlite3.Connection, rep_id: int) -> str | None:
    row = conn.execute(
        "SELECT parent_key FROM rep_edges WHERE child_rep_id=? AND active=1 ORDER BY id",
        (rep_id,),
    ).fetchone()
    return None if row is None else str(row[0])


def _scope_allowed(
    scope: str, *, order_merchant: str, card_merchant: str,
    purpose: str, parent_merchant: str | None,
) -> bool:
    normalized = str(scope or "").strip().lower()
    if card_merchant == order_merchant:
        if normalized in {"all", "merchant", "root" if order_merchant == "root" else "rep"}:
            return True
        return normalized == order_merchant
    if (
        purpose == "rep_settlement"
        and parent_merchant is not None
        and card_merchant == parent_merchant
        and normalized in {"all", "rep_settlement", "settlement", "children"}
    ):
        return True
    return False


def select_card(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    actor_tg_id: int,
    bot_id: int,
    amount_toman: int | None = None,
    now: int,
) -> dict:
    """Select a compatible active card by priority/id using Tehran-day receipt spend."""
    actor_tg_id = _positive(actor_tg_id, "actor_tg_id")
    bot_id = _positive(bot_id, "bot_id")
    now = _timestamp(now)
    order = conn.execute(
        "SELECT actor_tg_id,bot_id,merchant_key,purpose,payment_rail,amount_toman,status "
        "FROM payment_orders WHERE order_id=?",
        (order_id,),
    ).fetchone()
    if order is None:
        raise PaymentError("payment order does not exist")
    if int(order[0]) != actor_tg_id or int(order[1]) != bot_id:
        raise PaymentError("payment order belongs to another actor or bot")
    if str(order[4]) != "card_to_card" or str(order[6]) != "awaiting_payment":
        raise PaymentError("order is not waiting for a card-to-card payment")
    merchant, rep_id = _bot_merchant(conn, bot_id)
    if merchant != str(order[2]):
        raise PaymentError("order merchant no longer matches its registered bot")
    requested = int(order[5]) if amount_toman is None else amount_toman
    if isinstance(requested, bool) or not isinstance(requested, int) or requested <= 0:
        raise PaymentError("card attempt amount must be a positive integer")
    if now >= int(conn.execute(
        "SELECT expires_at FROM payment_orders WHERE order_id=?", (order_id,)
    ).fetchone()[0]):
        raise AttemptExpired("payment order has expired")
    parent = _parent_merchant(conn, rep_id) if rep_id is not None else None
    date = _tehran_date(now)
    cards = conn.execute(
        "SELECT id,merchant_key,card_number_cipher,sheba_cipher,card_holder,bank_name,"
        "target_scope,priority,daily_limit_toman,is_active "
        "FROM bank_cards WHERE is_active=1 ORDER BY priority ASC,id ASC"
    ).fetchall()
    for card in cards:
        card_merchant = str(card[1])
        if card_merchant not in {merchant, parent}:
            continue
        if not _scope_allowed(
            str(card[6]), order_merchant=merchant, card_merchant=card_merchant,
            purpose=str(order[3]), parent_merchant=parent,
        ):
            continue
        spent = int(conn.execute(
            "SELECT COALESCE(SUM(amount_toman),0) FROM payment_receipts "
            "WHERE bank_card_id=? AND submitted_tehran_date=? AND status IN ('pending','approved')",
            (int(card[0]), date),
        ).fetchone()[0])
        limit = int(card[8])
        if limit > 0 and spent + requested > limit:
            continue
        try:
            number = decrypt_secret(str(card[2]), "hstagent/columns/v1")
            sheba = (
                decrypt_secret(str(card[3]), "hstagent/columns/v1")
                if card[3] is not None else None
            )
        except Exception as exc:
            raise PaymentError("bank-card encrypted snapshot cannot be authenticated") from exc
        return {
            "bank_card_id": int(card[0]),
            "merchant_key": card_merchant,
            "card_number": number,
            "sheba": sheba,
            "card_holder": str(card[4]),
            "bank_name": str(card[5]),
            "target_scope": str(card[6]),
            "priority": int(card[7]),
            "daily_limit_toman": limit,
            "spent_today_toman": spent,
            "remaining_today_toman": None if limit == 0 else max(limit - spent, 0),
            "tehran_date": date,
        }
    raise NoEligibleCard("no active card has compatible scope and remaining daily limit")


def _open_attempt_result(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    try:
        snapshot = json.loads(decrypt_secret(str(row["evidence_cipher"]), "hstagent/columns/v1"))
    except Exception as exc:
        raise PaymentError("payment-attempt snapshot cannot be authenticated") from exc
    return {
        "attempt_id": str(row["attempt_id"]),
        "order_id": str(row["order_id"]),
        "state": str(row["state"]),
        "created_at": int(row["created_at"]),
        "expires_at": int(row["created_at"]) + PAYMENT_ATTEMPT_TTL_SECONDS,
        "bank_card_id": int(row["assigned_card_id"]),
        "card": snapshot,
        "replayed": True,
    }


def create_card_attempt(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    actor_tg_id: int,
    bot_id: int,
    now: int,
) -> dict:
    """Create one UUID attempt; expiry is derived from created_at (no new DB column)."""
    actor_tg_id = _positive(actor_tg_id, "actor_tg_id")
    bot_id = _positive(bot_id, "bot_id")
    now = _timestamp(now)
    with _atomic(conn):
        order = conn.execute(
            "SELECT actor_tg_id,bot_id,status,expires_at,merchant_key,amount_toman FROM payment_orders WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if order is None or int(order[0]) != actor_tg_id or int(order[1]) != bot_id:
            raise PaymentError("payment order is missing or belongs to another actor/bot")
        if str(order[2]) != "awaiting_payment" or now >= int(order[3]):
            raise AttemptExpired("payment order is no longer open for card attempts")
        existing_receipt = conn.execute(
            "SELECT 1 FROM payment_receipts WHERE order_id=? AND status='pending' LIMIT 1",
            (order_id,),
        ).fetchone()
        if existing_receipt is not None:
            raise PaymentError("order already has a pending receipt")
        prior = conn.execute(
            "SELECT attempt_id,order_id,assigned_card_id,state,evidence_cipher,created_at "
            "FROM payment_attempts WHERE order_id=? AND state='pending' ORDER BY created_at DESC LIMIT 1",
            (order_id,),
        ).fetchone()
        if prior is not None:
            if now < int(prior[5]) + PAYMENT_ATTEMPT_TTL_SECONDS:
                return _open_attempt_result(conn, prior)
            conn.execute(
                "UPDATE payment_attempts SET state='expired' WHERE attempt_id=? AND state='pending'",
                (str(prior[0]),),
            )

        card = select_card(
            conn, order_id=order_id, actor_tg_id=actor_tg_id, bot_id=bot_id, now=now
        )
        attempt_id = str(uuid.uuid4())
        evidence = json.dumps(card, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        evidence_cipher = encrypt_secret(evidence, "hstagent/columns/v1")
        conn.execute(
            "INSERT INTO payment_attempts(attempt_id,order_id,rail,merchant_key,assigned_card_id,"
            "amount_toman,state,external_payment_key,evidence_cipher,created_at,verified_at) "
            "VALUES(?,?,'card_to_card',?,?,?,'pending',NULL,?,?,NULL)",
            (
                attempt_id, order_id, str(order[4]), int(card["bank_card_id"]),
                int(order[5]), evidence_cipher, now,
            ),
        )
        return {
            "attempt_id": attempt_id, "order_id": order_id, "state": "pending",
            "created_at": now, "expires_at": now + PAYMENT_ATTEMPT_TTL_SECONDS,
            "bank_card_id": int(card["bank_card_id"]), "card": card, "replayed": False,
        }


def _created_epoch(value: object) -> int:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        raise PaymentError("receipt creation timestamp is invalid")
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError as exc:
        raise PaymentError("receipt creation timestamp is invalid") from exc
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return int(stamp.timestamp())


def submit_receipt(
    conn: sqlite3.Connection,
    *,
    attempt_id: str,
    actor_tg_id: int,
    bot_id: int,
    amount_toman: int,
    receipt_photo_id: str,
    now: int,
    media_group_id: str | None = None,
) -> dict:
    """Insert one receipt for one active attempt with cross-bot cooldown under a write lock."""
    actor_tg_id = _positive(actor_tg_id, "actor_tg_id")
    bot_id = _positive(bot_id, "bot_id")
    now = _timestamp(now)
    if isinstance(amount_toman, bool) or not isinstance(amount_toman, int) or amount_toman <= 0:
        raise PaymentError("receipt amount must be a positive integer")
    photo = str(receipt_photo_id or "").strip()
    if not photo:
        raise PaymentError("one photo file id is required")
    if media_group_id is not None:
        raise PaymentError("media groups/albums are not accepted; submit one photo")
    if not isinstance(attempt_id, str):
        raise PaymentError("attempt UUID is invalid")
    try:
        uuid.UUID(attempt_id)
    except ValueError as exc:
        raise PaymentError("attempt UUID is invalid") from exc

    with _atomic(conn):
        attempt = conn.execute(
            "SELECT a.attempt_id,a.order_id,a.rail,a.merchant_key,a.assigned_card_id,"
            "a.amount_toman,a.state,a.created_at,o.actor_tg_id,o.bot_id,o.status,"
            "o.expires_at,o.purpose,o.merchant_key "
            "FROM payment_attempts a JOIN payment_orders o ON o.order_id=a.order_id "
            "WHERE a.attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if attempt is None or int(attempt[8]) != actor_tg_id or int(attempt[9]) != bot_id:
            raise PaymentError("payment attempt is missing or belongs to another actor/bot")
        if str(attempt[2]) != "card_to_card" or str(attempt[3]) != str(attempt[13]):
            raise PaymentError("payment attempt rail/merchant binding is invalid")

        # Idempotent duplicate delivery is resolved before checking terminal attempt
        # state or TTL. The same attempt/photo/amount always maps to its one receipt;
        # a different payload on that UUID remains a hard conflict.
        prior_receipt = conn.execute(
            "SELECT id,amount_toman,receipt_photo_id,status FROM payment_receipts "
            "WHERE payment_attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if prior_receipt is not None:
            if int(prior_receipt[1]) == amount_toman and str(prior_receipt[2] or "") == photo:
                return {
                    "receipt_id": int(prior_receipt[0]), "attempt_id": attempt_id,
                    "status": str(prior_receipt[3]), "replayed": True,
                }
            raise PaymentError("attempt already owns a different receipt")

        if str(attempt[6]) != "pending" or now >= int(attempt[7]) + PAYMENT_ATTEMPT_TTL_SECONDS:
            raise AttemptExpired("payment attempt has expired or is terminal")

        if str(attempt[10]) not in {"awaiting_payment", "expired"}:
            raise PaymentError("order is not eligible for a new receipt")
        latest = conn.execute(
            "SELECT created_at FROM payment_receipts WHERE user_id=? ORDER BY id DESC LIMIT 1",
            (actor_tg_id,),
        ).fetchone()
        if latest is not None:
            previous = _created_epoch(latest[0])
            elapsed = now - previous
            if elapsed < RECEIPT_COOLDOWN_SECONDS:
                raise ReceiptCooldown(
                    f"receipt cooldown active: {max(RECEIPT_COOLDOWN_SECONDS - elapsed, 0)} seconds remain"
                )

        user = conn.execute(
            "SELECT role,rep_id FROM bot_users WHERE telegram_id=? AND is_blocked=0",
            (actor_tg_id,),
        ).fetchone()
        if user is None:
            raise PaymentError("active Telegram user identity is unavailable")
        role = "rep" if str(attempt[12]) == "rep_settlement" or str(user[0]) == "rep" else "end_user"
        rep_id = int(user[1]) if user[1] is not None else None
        tracking_code = uuid.uuid4().hex[:16].upper()
        submitted_date = _tehran_date(now)
        cur = conn.execute(
            "INSERT INTO payment_receipts(tracking_code,user_id,role,rep_id,amount_toman,"
            "receipt_photo_id,status,order_id,payment_attempt_id,bot_id,bank_card_id,"
            "merchant_key,submitted_tehran_date) "
            "VALUES(?,?,?,?,?,?,'pending',?,?,?,?,?,?)",
            (
                tracking_code, actor_tg_id, role, rep_id, amount_toman, photo,
                str(attempt[1]), attempt_id, bot_id, int(attempt[4]),
                str(attempt[13]), submitted_date,
            ),
        )
        receipt_id = int(cur.lastrowid)
        conn.execute(
            "UPDATE payment_attempts SET state='submitted' "
            "WHERE attempt_id=? AND state='pending'",
            (attempt_id,),
        )

        if str(attempt[10]) == "awaiting_payment":
            if now < int(attempt[11]):
                moved = conn.execute(
                    "UPDATE payment_orders SET status='pending_review',revision=revision+1 "
                    "WHERE order_id=? AND status='awaiting_payment' AND expires_at>?",
                    (str(attempt[1]), now),
                )
                if moved.rowcount != 1:
                    raise PaymentError("order expiry raced receipt submission; retry with a fresh attempt")
            else:
                conn.execute(
                    "UPDATE payment_orders SET status='expired',revision=revision+1 "
                    "WHERE order_id=? AND status='awaiting_payment' AND expires_at<=?",
                    (str(attempt[1]), now),
                )
                from order_service import release_order_resources
                release_order_resources(conn, order_id=str(attempt[1]), now=now)

        conn.execute(
            "INSERT OR IGNORE INTO receipt_notification_outbox(receipt_id,status,next_attempt_at) "
            "VALUES(?,'pending',0)",
            (receipt_id,),
        )
        return {
            "receipt_id": receipt_id, "tracking_code": tracking_code,
            "attempt_id": attempt_id, "status": "pending", "replayed": False,
            "order_status": (
                "expired" if str(attempt[10]) == "expired" or now >= int(attempt[11])
                else "pending_review"
            ),
        }


def _reviewer_authorized(
    conn: sqlite3.Connection, *, reviewer_tg_id: int, merchant_key: str,
) -> bool:
    row = conn.execute(
        "SELECT role,rep_id,is_blocked FROM bot_users WHERE telegram_id=?",
        (reviewer_tg_id,),
    ).fetchone()
    if row is None or bool(row[2]):
        return False
    role = str(row[0]).strip().lower()
    if role in {"admin", "super_admin"}:
        return True
    match = re.fullmatch(r"rep:([1-9][0-9]*)", merchant_key)
    return role == "rep" and match is not None and row[1] is not None and int(row[1]) == int(match.group(1))


def _consume_nonce(
    conn: sqlite3.Connection, *, nonce: str, reviewer_tg_id: int,
    bot_id: int, action: str, receipt_id: int, now: int,
) -> None:
    actions = {
        "approve": {"receipt.approve", "approve_receipt", "v52:receipt:approve"},
        "reject": {"receipt.reject", "reject_receipt", "v52:receipt:reject"},
    }[action]
    target_types = {"receipt", "payment_receipt"}
    row = conn.execute(
        "SELECT action,target_type,target_id,expires_at,consumed_at,bot_id,actor_tg_id "
        "FROM action_confirmations WHERE nonce=?",
        (nonce,),
    ).fetchone()
    if (
        row is None or str(row[0]) not in actions or str(row[1]) not in target_types
        or str(row[2]) != str(receipt_id) or int(row[3]) <= now
        or row[4] is not None or int(row[5]) != bot_id or int(row[6]) != reviewer_tg_id
    ):
        raise ReviewConflict("review nonce is invalid, expired, already consumed or out of scope")
    cur = conn.execute(
        "UPDATE action_confirmations SET consumed_at=? "
        "WHERE nonce=? AND actor_tg_id=? AND bot_id=? AND consumed_at IS NULL AND expires_at>?",
        (now, nonce, reviewer_tg_id, bot_id, now),
    )
    if cur.rowcount != 1:
        raise ReviewConflict("review nonce was consumed by another action")


def _receipt_context(conn: sqlite3.Connection, receipt_id: int):
    return conn.execute(
        "SELECT r.id,r.status,r.amount_toman,r.user_id,r.order_id,r.payment_attempt_id,"
        "r.bot_id,r.merchant_key,o.status,o.purpose,o.amount_toman,o.received_toman,"
        "o.expires_at,o.actor_tg_id,o.merchant_key,a.assigned_card_id "
        "FROM payment_receipts r JOIN payment_orders o ON o.order_id=r.order_id "
        "LEFT JOIN payment_attempts a ON a.attempt_id=r.payment_attempt_id WHERE r.id=?",
        (receipt_id,),
    ).fetchone()


def _credit_wallet_once(
    conn: sqlite3.Connection, *, receipt_id: int, order_id: str,
    user_id: int, merchant_key: str, amount: int,
    actor_id: int, policy_version: str = "v1.0",
) -> int:
    existing = conn.execute(
        "SELECT id,amount,merchant_key,ledger_scope,user_id FROM financial_ledger "
        "WHERE business_key=?",
        (f"receipt:{receipt_id}:wallet-credit",),
    ).fetchone()
    if existing is not None:
        if (
            int(existing[1]) != amount or str(existing[2]) != merchant_key
            or str(existing[3]) != "wallet" or existing[4] is None
            or int(existing[4]) != user_id
        ):
            raise ReviewConflict("receipt wallet posting key belongs to another posting")
        return int(existing[0])
    rep_id = int(merchant_key.split(":", 1)[1]) if merchant_key.startswith("rep:") else None
    balance_after = _wallet_balance(conn, merchant_key, user_id) + amount
    entry_id = post_entry(
        conn, rep_id=rep_id, user_id=user_id, transaction_type="credit",
        amount=amount, balance_after=balance_after,
        description=f"Card receipt {receipt_id} wallet top-up for order {order_id}",
        actor_id=actor_id, business_key=f"receipt:{receipt_id}:wallet-credit",
        merchant_key=merchant_key, ledger_scope="wallet", edge_id=None,
        order_id=order_id, operation_id=f"receipt:{receipt_id}:topup",
        policy_version=policy_version,
    )
    _sync_projection_cache(
        conn, ledger_scope="wallet", merchant_key=merchant_key, user_id=user_id
    )
    return entry_id


def _post_rep_settlement(
    conn: sqlite3.Connection, *, receipt_id: int, order_id: str,
    merchant_key: str, amount: int, actor_id: int,
) -> int:
    rep_id = int(merchant_key.split(":", 1)[1])
    edge = conn.execute(
        "SELECT id,parent_key FROM rep_edges WHERE child_rep_id=? AND active=1",
        (rep_id,),
    ).fetchall()
    if len(edge) != 1:
        raise ReviewConflict("representative settlement edge is missing or ambiguous")
    edge_id, parent_key = int(edge[0][0]), str(edge[0][1])
    debt = _edge_debt(conn, edge_id)
    if amount > debt:
        raise ReviewConflict("settlement exceeds the recorded edge debt; operator review required")
    entry_id = post_entry(
        conn, rep_id=rep_id, user_id=None, transaction_type="credit",
        amount=amount, balance_after=debt - amount,
        description=f"Representative settlement receipt {receipt_id}",
        actor_id=actor_id, business_key=f"receipt:{receipt_id}:rep-settlement",
        merchant_key=parent_key, ledger_scope="edge", edge_id=edge_id,
        order_id=order_id, operation_id=f"receipt:{receipt_id}:settlement",
        policy_version="v1.0",
    )
    _sync_projection_cache(
        conn, ledger_scope="edge", merchant_key=parent_key, edge_id=edge_id
    )
    return entry_id


def _queue_fulfillment(conn: sqlite3.Connection, *, order_id: str, receipt_id: int, now: int) -> None:
    payload = json.dumps(
        {"order_id": order_id, "receipt_id": receipt_id, "source": "card_to_card"},
        sort_keys=True, separators=(",", ":"),
    )
    key = f"fulfill:order:{order_id}"
    conn.execute(
        "INSERT OR IGNORE INTO scheduled_jobs(job_key,kind,due_at,state,result_json) "
        "VALUES(?,'fulfill_order',?,'pending',?)",
        (key, now, payload),
    )
    stored = conn.execute(
        "SELECT kind,result_json FROM scheduled_jobs WHERE job_key=?", (key,)
    ).fetchone()
    if stored is None or str(stored[0]) != "fulfill_order" or str(stored[1]) != payload:
        raise ReviewConflict("fulfilment job key is already bound to another receipt")


def _amount_exception(conn: sqlite3.Connection, *, receipt_id: int, order_id: str,
                      expected: int, received: int, now: int) -> dict:
    payload = json.dumps(
        {"receipt_id": receipt_id, "order_id": order_id,
         "expected_toman": expected, "received_toman": received,
         "requires_operator_review": True},
        sort_keys=True, separators=(",", ":"),
    )
    conn.execute(
        "INSERT OR IGNORE INTO scheduled_jobs(job_key,kind,due_at,state,result_json) "
        "VALUES(?,?,?,?,?)",
        (f"payment-amount-exception:{receipt_id}", "payment_amount_exception",
         now, "operator_review", payload),
    )
    conn.execute(
        "UPDATE payment_orders SET status='exception',received_toman=?,revision=revision+1 "
        "WHERE order_id=? AND status IN ('pending_review','awaiting_payment','expired')",
        (received, order_id),
    )
    return {
        "receipt_id": receipt_id, "order_id": order_id,
        "status": "exception", "expected_toman": expected,
        "received_toman": received, "posting_created": False,
    }


def approve_order_receipt(
    conn: sqlite3.Connection,
    *,
    receipt_id: int,
    reviewer_tg_id: int,
    bot_id: int,
    nonce: str,
    now: int,
) -> dict:
    """CAS-approve one pending receipt and post wallet/settlement or enqueue fulfillment."""
    receipt_id = _positive(receipt_id, "receipt_id")
    reviewer_tg_id = _positive(reviewer_tg_id, "reviewer_tg_id")
    bot_id = _positive(bot_id, "bot_id")
    now = _timestamp(now)
    with _atomic(conn):
        row = _receipt_context(conn, receipt_id)
        if row is None or str(row[1]) != "pending" or int(row[6]) != bot_id:
            raise ReviewConflict("receipt is missing, not pending or belongs to another bot")
        if not _reviewer_authorized(conn, reviewer_tg_id=reviewer_tg_id, merchant_key=str(row[7])):
            raise ReviewerNotAuthorized("reviewer is not root admin or an operator of this merchant")
        _consume_nonce(
            conn, nonce=nonce, reviewer_tg_id=reviewer_tg_id,
            bot_id=bot_id, action="approve", receipt_id=receipt_id, now=now,
        )
        expected, received = int(row[10]), int(row[2])
        if received != expected:
            return _amount_exception(
                conn, receipt_id=receipt_id, order_id=str(row[4]),
                expected=expected, received=received, now=now,
            )

        cur = conn.execute(
            "UPDATE payment_receipts SET status='approved',reviewed_by=?,"
            "reviewed_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
            (reviewer_tg_id, receipt_id),
        )
        if cur.rowcount != 1:
            raise ReviewConflict("receipt pending status was already consumed")
        attempt_id = row[5]
        if attempt_id is not None:
            updated = conn.execute(
                "UPDATE payment_attempts SET state='succeeded',verified_at=? "
                "WHERE attempt_id=? AND state='submitted'",
                (now, str(attempt_id)),
            )
            if updated.rowcount != 1:
                raise ReviewConflict("payment attempt is no longer submitted")

        order_id, purpose = str(row[4]), str(row[9])
        order_status, merchant_key = str(row[8]), str(row[14])
        late_expired = order_status == "expired" or now >= int(row[12])
        if late_expired and order_status != "expired":
            conn.execute(
                "UPDATE payment_orders SET status='expired',received_toman=?,revision=revision+1 "
                "WHERE order_id=? AND status IN ('awaiting_payment','pending_review')",
                (received, order_id),
            )
            from order_service import release_order_resources
            release_order_resources(conn, order_id=order_id, now=now)
        elif late_expired and order_status == "expired":
            conn.execute(
                "UPDATE payment_orders SET received_toman=received_toman+?,revision=revision+1 "
                "WHERE order_id=? AND status='expired'",
                (received, order_id),
            )
        elif not late_expired:
            moved = conn.execute(
                "UPDATE payment_orders SET status='accepted',received_toman=?,revision=revision+1 "
                "WHERE order_id=? AND status='pending_review'",
                (received, order_id),
            )
            if moved.rowcount != 1:
                raise ReviewConflict("order is no longer pending review")

        if late_expired and purpose != "wallet_topup":
            return {
                "receipt_id": receipt_id, "order_id": order_id,
                "status": "exception", "late_payment": True,
                "wallet_credit": 0, "panel_job": False,
            }
        if purpose == "wallet_topup":
            entry_id = _credit_wallet_once(
                conn, receipt_id=receipt_id, order_id=order_id,
                user_id=int(row[3]), merchant_key=merchant_key,
                amount=received, actor_id=reviewer_tg_id,
            )
            return {
                "receipt_id": receipt_id, "order_id": order_id,
                "status": "accepted" if not late_expired else "expired",
                "late_payment": late_expired, "wallet_ledger_id": entry_id,
                "wallet_credit": received, "panel_job": False,
            }
        if purpose == "rep_settlement":
            entry_id = _post_rep_settlement(
                conn, receipt_id=receipt_id, order_id=order_id,
                merchant_key=merchant_key, amount=received,
                actor_id=reviewer_tg_id,
            )
            return {
                "receipt_id": receipt_id, "order_id": order_id,
                "status": "accepted", "rep_settlement_ledger_id": entry_id,
                "wallet_credit": 0, "panel_job": False,
            }
        if purpose in {"vpn_buy", "vpn_renew", "vpn_addon", "digital_product_buy"}:
            _queue_fulfillment(
                conn, order_id=order_id, receipt_id=receipt_id, now=now
            )
            return {
                "receipt_id": receipt_id, "order_id": order_id,
                "status": "accepted", "wallet_credit": 0, "panel_job": True,
            }
        raise ReviewConflict("receipt purpose is not supported by W08-B01")


def reject_order_receipt(
    conn: sqlite3.Connection,
    *,
    receipt_id: int,
    reviewer_tg_id: int,
    bot_id: int,
    nonce: str,
    now: int,
) -> dict:
    """CAS-reject a pending receipt; a failed attempt is terminal and never reused."""
    receipt_id = _positive(receipt_id, "receipt_id")
    reviewer_tg_id = _positive(reviewer_tg_id, "reviewer_tg_id")
    bot_id = _positive(bot_id, "bot_id")
    now = _timestamp(now)
    with _atomic(conn):
        row = _receipt_context(conn, receipt_id)
        if row is None or str(row[1]) != "pending" or int(row[6]) != bot_id:
            raise ReviewConflict("receipt is missing, not pending or already reviewed")
        if not _reviewer_authorized(conn, reviewer_tg_id=reviewer_tg_id, merchant_key=str(row[7])):
            raise ReviewerNotAuthorized("reviewer is not authorized for this merchant")
        _consume_nonce(
            conn, nonce=nonce, reviewer_tg_id=reviewer_tg_id,
            bot_id=bot_id, action="reject", receipt_id=receipt_id, now=now,
        )
        cur = conn.execute(
            "UPDATE payment_receipts SET status='rejected',reviewed_by=?,"
            "reviewed_at=CURRENT_TIMESTAMP,rejection_reason='rejected_by_authorized_reviewer' "
            "WHERE id=? AND status='pending'",
            (reviewer_tg_id, receipt_id),
        )
        if cur.rowcount != 1:
            raise ReviewConflict("receipt pending status was already consumed")
        if row[5] is not None:
            conn.execute(
                "UPDATE payment_attempts SET state='failed' "
                "WHERE attempt_id=? AND state='submitted'",
                (str(row[5]),),
            )
        if str(row[8]) == "pending_review" and now < int(row[12]):
            conn.execute(
                "UPDATE payment_orders SET status='awaiting_payment',revision=revision+1 "
                "WHERE order_id=? AND status='pending_review'",
                (str(row[4]),),
            )
        return {
            "receipt_id": receipt_id, "order_id": str(row[4]),
            "status": "rejected", "attempt_reusable": False,
        }


__all__ = [
    "PaymentError", "NoEligibleCard", "AttemptExpired", "ReceiptCooldown",
    "ReviewConflict", "ReviewerNotAuthorized", "select_card",
    "create_card_attempt", "submit_receipt",
    "approve_order_receipt", "reject_order_receipt",
]


def _additional_root_admin_id() -> int:
    import config
    value = getattr(config, "ADMIN_TELEGRAM_ID", 0)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReviewerNotAuthorized("configured root administrator identity is unavailable")
    return value


def handle_additional_received_payment(
    conn: sqlite3.Connection,
    *,
    order_id: str,
    reviewer_tg_id: int,
    bot_id: int,
    nonce: str,
    external_payment_key: str,
    destination: str,
    amount_toman: int,
    evidence_note: str,
    now: int,
) -> dict:
    """Root-certify a second real receipt, credit its tenant wallet, and never dispatch again.

    A Telegram file_id/photo is deliberately not accepted as payment proof. The
    confirmation must carry a bank reference, exact snapshotted destination and amount.
    """
    reviewer_tg_id = _positive(reviewer_tg_id, "reviewer_tg_id")
    bot_id = _positive(bot_id, "bot_id")
    now = _timestamp(now)
    if not isinstance(nonce, str) or not re.fullmatch(r"[A-Za-z0-9_-]{24}", nonce):
        raise PaymentError("a valid 24-character one-use confirmation nonce is required")
    reference = str(external_payment_key or "").strip()
    target = str(destination or "").strip()
    note = str(evidence_note or "").strip()
    if not reference or len(reference) > 256 or not target or len(note) < 8:
        raise PaymentError("bank reference, destination and root evidence note are required")
    if isinstance(amount_toman, bool) or not isinstance(amount_toman, int) or amount_toman <= 0:
        raise PaymentError("additional payment amount must be a positive integer")
    if reviewer_tg_id != _additional_root_admin_id():
        raise ReviewerNotAuthorized("additional payment review is root-only")

    from crypto_store import decrypt_secret, encrypt_secret
    from financial_service import _sync_projection_cache, _wallet_balance, post_entry

    with _atomic(conn):
        order = conn.execute(
            "SELECT actor_tg_id,bot_id,merchant_key,status,amount_toman,received_toman,revision "
            "FROM payment_orders WHERE order_id=?",
            (order_id,),
        ).fetchone()
        if order is None or int(order[1]) != bot_id:
            raise ReviewConflict("payment order is missing or belongs to another bot")
        if str(order[3]) not in {"accepted", "completed"}:
            raise ReviewConflict("additional receipts are only valid after the original order was accepted")
        merchant_key = str(order[2])
        duplicate = conn.execute(
            "SELECT attempt_id,order_id,amount_toman FROM payment_attempts WHERE external_payment_key=?",
            (reference,),
        ).fetchone()
        if duplicate is not None:
            if str(duplicate[1]) == order_id and int(duplicate[2]) == amount_toman:
                return {
                    "status": "duplicate", "credited": False, "replayed": True,
                    "payment_attempt_id": str(duplicate[0]), "order_id": order_id,
                }
            raise ReviewConflict("external payment reference is already bound to another payment")
        original = conn.execute(
            "SELECT attempt_id,rail,assigned_card_id,evidence_cipher FROM payment_attempts "
            "WHERE order_id=? AND merchant_key=? AND state='succeeded' "
            "AND assigned_card_id IS NOT NULL AND evidence_cipher IS NOT NULL "
            "ORDER BY verified_at DESC,created_at DESC LIMIT 1",
            (order_id, merchant_key),
        ).fetchone()
        if original is None:
            raise ReviewConflict("original successful card attempt and immutable card snapshot are required")
        try:
            snapshot = json.loads(decrypt_secret(str(original[3]), "hstagent/columns/v1"))
        except Exception as exc:
            raise ReviewConflict("original card snapshot failed authentication") from exc
        accepted_destinations = {
            str(snapshot.get("card_number") or "").strip(),
            str(snapshot.get("sheba") or "").strip(),
        } - {""}
        if target not in accepted_destinations:
            raise ReviewConflict("confirmed destination does not match the immutable attempt card snapshot")

        confirmation = conn.execute(
            "SELECT action,target_type,target_id,expected_revision,expires_at,consumed_at,bot_id,actor_tg_id "
            "FROM action_confirmations WHERE nonce=?",
            (nonce,),
        ).fetchone()
        if (
            confirmation is None or str(confirmation[0]) != "payment.additional_received"
            or str(confirmation[1]) != "payment_order" or str(confirmation[2]) != order_id
            or int(confirmation[3]) != int(order[6]) or int(confirmation[4]) <= now
            or confirmation[5] is not None or int(confirmation[6]) != bot_id
            or int(confirmation[7]) != reviewer_tg_id
        ):
            raise ReviewConflict("root confirmation is invalid, stale, expired or already consumed")

        attempt_id = str(uuid.uuid4())
        evidence = {
            "order_id": order_id, "external_payment_key": reference,
            "destination": target, "amount_toman": amount_toman,
            "reviewer_tg_id": reviewer_tg_id, "note": note,
        }
        evidence_cipher = encrypt_secret(
            json.dumps(evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            "hstagent/columns/v1",
        )
        operation_id = f"additional-payment:{attempt_id}"
        consumed = conn.execute(
            "UPDATE action_confirmations SET consumed_at=?,operation_id=? "
            "WHERE nonce=? AND actor_tg_id=? AND bot_id=? AND expected_revision=? "
            "AND consumed_at IS NULL AND expires_at>?",
            (now, operation_id, nonce, reviewer_tg_id, bot_id, int(order[6]), now),
        )
        if consumed.rowcount != 1:
            raise ReviewConflict("additional-payment confirmation was consumed concurrently")
        conn.execute(
            "INSERT INTO payment_attempts(attempt_id,order_id,rail,merchant_key,assigned_card_id,"
            "amount_toman,state,external_payment_key,evidence_cipher,created_at,verified_at) "
            "VALUES(?,?,?,?,?,?,'succeeded',?,?,?,?)",
            (attempt_id, order_id, str(original[1]), merchant_key, int(original[2]),
             amount_toman, reference, str(original[3]), now, now),
        )
        moved = conn.execute(
            "UPDATE payment_orders SET received_toman=received_toman+?,revision=revision+1 "
            "WHERE order_id=? AND status IN ('accepted','completed') AND revision=?",
            (amount_toman, order_id, int(order[6])),
        )
        if moved.rowcount != 1:
            raise ReviewConflict("order revision changed before additional payment was recorded")

        user_id = int(order[0])
        rep_id = int(merchant_key.split(":", 1)[1]) if merchant_key.startswith("rep:") else None
        balance = _wallet_balance(conn, merchant_key, user_id)
        ledger_id = post_entry(
            conn, rep_id=rep_id, user_id=user_id, transaction_type="credit",
            amount=amount_toman, balance_after=balance + amount_toman,
            description=f"Additional received payment for completed order {order_id}",
            actor_id=reviewer_tg_id,
            business_key=f"payment:{reference}:additional-wallet-credit",
            merchant_key=merchant_key, ledger_scope="wallet", order_id=order_id,
            operation_id=operation_id,
        )
        _sync_projection_cache(conn, ledger_scope="wallet", merchant_key=merchant_key, user_id=user_id)
        safe_details = json.dumps(
            {"order_id": order_id, "amount_toman": amount_toman, "status": "wallet_credited"},
            sort_keys=True, separators=(",", ":"),
        )
        conn.execute(
            "INSERT INTO audit_logs(rep_id,account_code,email,action,actor_id,actor_name,"
            "details_json,result_status,details_cipher) VALUES(NULL,'','',? ,?,'root-admin',?,'recorded',?)",
            ("payment.additional_received", reviewer_tg_id, safe_details, evidence_cipher),
        )
        return {
            "status": "wallet_credited", "credited": True, "replayed": False,
            "order_id": order_id, "payment_attempt_id": attempt_id,
            "ledger_id": ledger_id, "wallet_credit_toman": amount_toman,
            "order_received_toman": int(order[5]) + amount_toman,
            "dispatch_created": False,
        }


__all__.append("handle_additional_received_payment")
