"""Offline tier policy, Tehran test quota and first-purchase eligibility."""
from __future__ import annotations

from datetime import datetime
import json
import secrets
import sqlite3
import time
from typing import Any
from zoneinfo import ZoneInfo


TEHRAN = ZoneInfo("Asia/Tehran")
_PURCHASE_PURPOSES = frozenset({"vpn_buy", "vpn_renew", "vpn_addon", "digital_product_buy"})


class TierServiceError(ValueError):
    """Base validation or persistence error for tier-dependent test access."""


class TierPolicyUnavailable(TierServiceError):
    """Actor identity or its approved tier policy is unavailable."""


class DailyTestQuotaExceeded(TierServiceError):
    """The actor has used the full daily test quota for the Tehran day."""


def _positive_id(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _timestamp(now: int | None) -> int:
    value = int(time.time()) if now is None else now
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("now must be a non-negative UTC epoch-seconds integer")
    return value


def _begin(conn: sqlite3.Connection) -> None:
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    if conn.in_transaction:
        raise TierServiceError("tier operations require a clean connection")
    conn.execute("BEGIN IMMEDIATE")


def _actor_tier(conn: sqlite3.Connection, actor_tg_id: int) -> tuple[str, str]:
    reps = conn.execute(
        "SELECT id,tier,is_active,deleted_at FROM representatives "
        "WHERE telegram_id=? ORDER BY id",
        (actor_tg_id,),
    ).fetchall()
    if len(reps) > 1:
        raise TierPolicyUnavailable("representative Telegram identity is ambiguous")
    if reps:
        rep = reps[0]
        if not bool(rep["is_active"]) or rep["deleted_at"] is not None:
            raise TierPolicyUnavailable("inactive representative cannot reserve test quota")
        return "rep", str(rep["tier"]).strip().lower()

    actor = conn.execute(
        "SELECT role,tier,rep_id,is_blocked FROM bot_users WHERE telegram_id=?",
        (actor_tg_id,),
    ).fetchone()
    if actor is None or bool(actor["is_blocked"]):
        raise TierPolicyUnavailable("active actor identity is unavailable")
    role = str(actor["role"] or "").strip().lower()
    if role in {"rep", "representative"}:
        if actor["rep_id"] is None:
            raise TierPolicyUnavailable("representative actor has no representative identity")
        rep = conn.execute(
            "SELECT tier,is_active,deleted_at FROM representatives WHERE id=?",
            (int(actor["rep_id"]),),
        ).fetchone()
        if rep is None or not bool(rep["is_active"]) or rep["deleted_at"] is not None:
            raise TierPolicyUnavailable("active representative tier is unavailable")
        return "rep", str(rep["tier"]).strip().lower()
    if role not in {"end_user", "user", "admin", "root"}:
        raise TierPolicyUnavailable("actor role has no approved test tier")
    return "end_user", str(actor["tier"] or "").strip().lower()


def reserve_test_quota(
    conn: sqlite3.Connection,
    actor_tg_id: int,
    *,
    now: int | None = None,
) -> dict[str, Any]:
    """Atomically reserve one daily test slot using the actor's current tier.

    The reservation ticket is also written to the existing scheduled_jobs
    table. This provides durable, one-time release semantics without adding a
    migration or a parallel quota counter. UNKNOWN remains in reserved state.
    """
    actor_tg_id = _positive_id(actor_tg_id, "actor_tg_id")
    timestamp = _timestamp(now)
    try:
        tehran_date = datetime.fromtimestamp(timestamp, TEHRAN).date().isoformat()
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("now is outside the supported Tehran calendar range") from exc

    _begin(conn)
    try:
        actor_type, tier_code = _actor_tier(conn, actor_tg_id)
        policy = conn.execute(
            "SELECT daily_test_quota FROM tier_policies WHERE actor_type=? AND tier_code=?",
            (actor_type, tier_code),
        ).fetchone()
        if policy is None:
            raise TierPolicyUnavailable("actor tier is missing from tier_policies")
        quota = policy["daily_test_quota"]
        if isinstance(quota, bool) or not isinstance(quota, int) or quota < 0:
            raise TierPolicyUnavailable("daily_test_quota must be a non-negative integer")
        if quota == 0:
            raise DailyTestQuotaExceeded("the actor's tier has no daily test quota")

        # Exact W06.6 upsert contract: one write wins under concurrent issuers.
        cursor = conn.execute(
            "INSERT INTO daily_test_usage(actor_tg_id,tehran_date,used) VALUES(?,?,1) "
            "ON CONFLICT(actor_tg_id,tehran_date) DO UPDATE SET used=used+1 WHERE used<?",
            (actor_tg_id, tehran_date, quota),
        )
        if cursor.rowcount != 1:
            raise DailyTestQuotaExceeded("daily test quota is exhausted for the Tehran date")

        reservation_id = secrets.token_hex(16)
        job_key = f"test-quota:{reservation_id}"
        payload = {
            "actor_tg_id": actor_tg_id,
            "tehran_date": tehran_date,
            "daily_limit": quota,
        }
        conn.execute(
            "INSERT INTO scheduled_jobs(job_key,kind,due_at,state,lease_token,lease_until,result_json) "
            "VALUES(?, 'test_quota_reservation', ?, 'reserved', NULL, NULL, ?)",
            (job_key, timestamp, json.dumps(payload, sort_keys=True, separators=(",", ":"))),
        )
        conn.commit()
        return {
            "reservation_id": reservation_id,
            "job_key": job_key,
            "actor_tg_id": actor_tg_id,
            "tehran_date": tehran_date,
            "daily_limit": quota,
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def finish_test_quota(
    conn: sqlite3.Connection,
    reservation: dict[str, Any] | str,
    *,
    outcome: str,
) -> dict[str, Any] | bool:
    """Commit, retain, or release one quota ticket; definite failure releases once."""
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    job_key = reservation if isinstance(reservation, str) else (
        reservation.get("job_key") if isinstance(reservation, dict) else None
    )
    if not isinstance(job_key, str) or not job_key.startswith("test-quota:"):
        raise ValueError("reservation must contain a valid test-quota job_key")
    normalized = str(outcome or "").strip().lower()
    if normalized not in {"success", "definite_failure", "unknown"}:
        raise ValueError("outcome must be success, definite_failure, or unknown")

    _begin(conn)
    try:
        row = conn.execute(
            "SELECT state,result_json FROM scheduled_jobs "
            "WHERE job_key=? AND kind='test_quota_reservation'",
            (job_key,),
        ).fetchone()
        if row is None:
            raise TierServiceError("test quota reservation does not exist")
        state = str(row["state"])
        if state != "reserved":
            conn.commit()
            return False
        try:
            payload = json.loads(row["result_json"])
            actor_tg_id = _positive_id(payload["actor_tg_id"], "actor_tg_id")
            tehran_date = str(payload["tehran_date"])
            if len(tehran_date) != 10:
                raise ValueError
        except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
            raise TierServiceError("stored quota reservation evidence is invalid") from exc

        if normalized == "unknown":
            conn.commit()
            return {
                "state": "reserved",
                "job_key": job_key,
                "quota_retained": True,
                "released": False,
            }

        target_state = "completed" if normalized == "success" else "released"
        cursor = conn.execute(
            "UPDATE scheduled_jobs SET state=? "
            "WHERE job_key=? AND kind='test_quota_reservation' AND state='reserved'",
            (target_state, job_key),
        )
        if cursor.rowcount != 1:
            conn.commit()
            return False
        if normalized == "definite_failure":
            usage = conn.execute(
                "UPDATE daily_test_usage SET used=used-1 "
                "WHERE actor_tg_id=? AND tehran_date=? AND used>0",
                (actor_tg_id, tehran_date),
            )
            if usage.rowcount != 1:
                raise TierServiceError("quota usage could not be released atomically")
        conn.commit()
        return {
            "state": target_state,
            "job_key": job_key,
            "quota_retained": normalized == "success",
            "released": normalized == "definite_failure",
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def _test_marker(spec_json: str) -> bool | None:
    try:
        payload = json.loads(spec_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    marker = payload.get("is_test", payload.get("test"))
    if isinstance(marker, bool):
        return marker
    if isinstance(marker, int) and marker in (0, 1):
        return bool(marker)
    return None


def is_first_purchase(conn: sqlite3.Connection, actor_tg_id: int) -> bool:
    """Return True only when no completed non-test order or legacy debit exists.

    All merchants are considered. Malformed or unclassifiable completed-order
    history fails closed (False), so uncertainty can never grant first-purchase
    promotion eligibility.
    """
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    actor_tg_id = _positive_id(actor_tg_id, "actor_tg_id")
    try:
        legacy = conn.execute(
            "SELECT 1 FROM financial_ledger "
            "WHERE amount>0 AND transaction_type='debit' "
            "AND (user_id=? OR actor_id=? OR rep_id IN "
            "(SELECT id FROM representatives WHERE telegram_id=?)) LIMIT 1",
            (actor_tg_id, actor_tg_id, actor_tg_id),
        ).fetchone()
        if legacy is not None:
            return False

        orders = conn.execute(
            "SELECT o.purpose,o.amount_toman,q.spec_json,q.account_id "
            "FROM payment_orders o JOIN quotes q ON q.quote_id=o.quote_id "
            "WHERE o.actor_tg_id=? AND o.status='completed' AND o.amount_toman>0 "
            "ORDER BY o.created_at,o.order_id",
            (actor_tg_id,),
        ).fetchall()
        for order in orders:
            if str(order["purpose"]) not in _PURCHASE_PURPOSES:
                continue
            marker = _test_marker(order["spec_json"])
            if marker is True:
                continue
            if marker is False:
                return False
            if order["account_id"] is not None:
                account = conn.execute(
                    "SELECT is_test FROM accounts WHERE id=?",
                    (int(order["account_id"]),),
                ).fetchone()
                if account is None or account["is_test"] not in (0, 1):
                    return False
                if bool(account["is_test"]):
                    continue
                return False
            if str(order["purpose"]) == "digital_product_buy":
                return False
            # A completed VPN order with no trustworthy test/non-test marker
            # is unknown history and therefore cannot receive first-purchase.
            return False
        return True
    except sqlite3.Error:
        return False
