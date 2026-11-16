"""Offline tier policy, Tehran test quota and first-purchase eligibility."""
from __future__ import annotations

from datetime import datetime
import json
import re
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


# --------------------------------------------------------------------------- #
# Tier catalog administration
#
# ``tier_policies`` was read-only from the product: ``pricing_service`` consumed
# ``discount_bps``, ``tier_service`` consumed ``daily_test_quota`` and
# ``rep_tree_service`` validated that a tier code existed, but nothing could
# create, edit or retire a row. These writers are the missing half, and because
# every reader resolves the policy at call time, an edit here takes effect on the
# very next price calculation.
# --------------------------------------------------------------------------- #

TIER_CODE_RE = re.compile(r"^[a-z][a-z0-9_]{1,29}$")
ACTOR_TYPES = ("rep", "end_user")
ACTOR_TYPE_FA = {"rep": "نمایندگی", "end_user": "کاربری"}
MAX_TIER_TITLE = 40
MAX_BPS = 10000
#: The ladder is exact in basis points, but an operator thinks in percent
#: (``2.5`` means 2.5%). The conversion lives here so both the UI and any future
#: caller agree on the rounding.
BPS_PER_PERCENT = 100

#: Every column an operator may set, with its validator. ``rank`` orders the
#: ladder, the two ``*_bps`` fields are basis points (100 bps = 1%), and the
#: remaining three are the quota / credit / sales-floor constraints.
_TIER_FIELDS: dict[str, str] = {
    "title_fa": "عنوان فارسی",
    "rank": "رتبه",
    "discount_bps": "درصد تخفیف",
    "cashback_bps": "درصد کش‌بک",
    "daily_test_quota": "سهمیه تست روزانه",
    "default_credit_limit": "سقف اعتبار پیش‌فرض",
    "min_monthly_sales_toman": "حداقل فروش ماهانه",
}


class TierPolicyConflict(TierServiceError):
    """The tier code is already taken, or the row is still in use."""


def _clean_actor_type(value: Any) -> str:
    actor = str(value or "").strip().lower()
    if actor not in ACTOR_TYPES:
        raise TierServiceError(
            "نقش هدف باید «rep» (نمایندگی) یا «end_user» (کاربری) باشد."
        )
    return actor


def _clean_tier_code(value: Any) -> str:
    code = str(value or "").strip().lower()
    if not TIER_CODE_RE.fullmatch(code):
        raise TierServiceError(
            "کد سطح باید با حرف کوچک لاتین شروع شود و فقط شامل حروف کوچک، "
            "رقم و زیرخط باشد (۲ تا ۳۰ کاراکتر)."
        )
    return code


def _clean_title(value: Any) -> str:
    title = str(value or "").strip()
    if not title:
        raise TierServiceError("عنوان فارسی سطح نمی‌تواند خالی باشد.")
    if len(title) > MAX_TIER_TITLE:
        raise TierServiceError(f"عنوان سطح حداکثر {MAX_TIER_TITLE} کاراکتر است.")
    return title


def _clean_int(value: Any, label: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool):
        raise TierServiceError(f"{label} باید یک عدد صحیح باشد.")
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise TierServiceError(f"{label} باید یک عدد صحیح باشد.") from exc
    if number < minimum or (maximum is not None and number > maximum):
        bound = f"بین {minimum} و {maximum}" if maximum is not None else f"حداقل {minimum}"
        raise TierServiceError(f"{label} باید {bound} باشد.")
    return number


def parse_int(value: Any, label: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    """Validate one operator-supplied integer field, with a Persian message."""
    return _clean_int(value, label, minimum=minimum, maximum=maximum)


def parse_percent_bps(value: Any, label: str) -> int:
    """Convert an operator-typed percentage (``2.5``) into basis points (``250``)."""
    text = str(value if value is not None else "").strip()
    if not text:
        raise TierServiceError(f"{label} نمی‌تواند خالی باشد.")
    try:
        number = float(text)
    except (TypeError, ValueError) as exc:
        raise TierServiceError(f"{label} باید یک عدد باشد.") from exc
    if number != number or number in (float("inf"), float("-inf")):
        raise TierServiceError(f"{label} باید یک عدد متناهی باشد.")
    if number < 0 or number * BPS_PER_PERCENT > MAX_BPS:
        raise TierServiceError(f"{label} باید بین ۰ و ۱۰۰ درصد باشد.")
    return int(round(number * BPS_PER_PERCENT))


def _tier_row_to_dict(row) -> dict[str, Any]:
    return {
        "actor_type": str(row[0]),
        "tier_code": str(row[1]),
        "title_fa": str(row[2]),
        "rank": int(row[3]),
        "discount_bps": int(row[4]),
        "cashback_bps": int(row[5]),
        "daily_test_quota": int(row[6]),
        "default_credit_limit": int(row[7]),
        "min_monthly_sales_toman": int(row[8]),
    }


_TIER_COLUMNS = (
    "actor_type,tier_code,title_fa,rank,discount_bps,cashback_bps,"
    "daily_test_quota,default_credit_limit,min_monthly_sales_toman"
)


def list_tiers(conn, *, actor_type: Any = None) -> list[dict[str, Any]]:
    """Every tier policy, ordered by target role then ladder rank."""
    if actor_type is None:
        rows = conn.execute(
            "SELECT " + _TIER_COLUMNS + " FROM tier_policies "
            "ORDER BY CASE actor_type WHEN 'rep' THEN 0 ELSE 1 END, rank, tier_code"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT " + _TIER_COLUMNS + " FROM tier_policies "
            "WHERE actor_type=? ORDER BY rank, tier_code",
            (_clean_actor_type(actor_type),),
        ).fetchall()
    return [_tier_row_to_dict(row) for row in rows]


def get_tier(conn, actor_type: Any, tier_code: Any) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT " + _TIER_COLUMNS + " FROM tier_policies WHERE actor_type=? AND tier_code=?",
        (_clean_actor_type(actor_type), _clean_tier_code(tier_code)),
    ).fetchone()
    return None if row is None else _tier_row_to_dict(row)


def tier_usage(conn, actor_type: Any, tier_code: Any) -> dict[str, int]:
    """How many actors currently sit on this tier, per side of the catalog."""
    actor = _clean_actor_type(actor_type)
    code = _clean_tier_code(tier_code)
    representatives = 0
    users = 0
    if actor == "rep":
        representatives = int(conn.execute(
            "SELECT COUNT(*) FROM representatives WHERE tier=?", (code,)).fetchone()[0])
    else:
        users = int(conn.execute(
            "SELECT COUNT(*) FROM bot_users WHERE tier=?", (code,)).fetchone()[0])
    return {"representatives": representatives, "users": users,
            "total": representatives + users}


def create_tier(
    conn,
    *,
    actor_type: Any,
    tier_code: Any,
    title_fa: Any,
    rank: Any = 0,
    discount_bps: Any = 0,
    cashback_bps: Any = 0,
    daily_test_quota: Any = 1,
    default_credit_limit: Any = 0,
    min_monthly_sales_toman: Any = 0,
) -> dict[str, Any]:
    """Add one tier policy; the code is unique per target role."""
    actor = _clean_actor_type(actor_type)
    code = _clean_tier_code(tier_code)
    if get_tier(conn, actor, code) is not None:
        raise TierPolicyConflict(f"سطح «{code}» برای این نقش از قبل وجود دارد.")
    conn.execute(
        "INSERT INTO tier_policies(" + _TIER_COLUMNS + ") VALUES(?,?,?,?,?,?,?,?,?)",
        (
            actor, code, _clean_title(title_fa),
            _clean_int(rank, "رتبه"),
            _clean_int(discount_bps, "درصد تخفیف", maximum=MAX_BPS),
            _clean_int(cashback_bps, "درصد کش‌بک", maximum=MAX_BPS),
            _clean_int(daily_test_quota, "سهمیه تست روزانه"),
            _clean_int(default_credit_limit, "سقف اعتبار پیش‌فرض"),
            _clean_int(min_monthly_sales_toman, "حداقل فروش ماهانه"),
        ),
    )
    return get_tier(conn, actor, code)


def update_tier(conn, actor_type: Any, tier_code: Any, **fields) -> dict[str, Any]:
    """Edit one tier policy in place; only the supplied fields change."""
    actor = _clean_actor_type(actor_type)
    code = _clean_tier_code(tier_code)
    current = get_tier(conn, actor, code)
    if current is None:
        raise TierServiceError(f"سطح «{code}» برای این نقش یافت نشد.")
    supplied = {key: value for key, value in fields.items() if value is not None}
    unknown = sorted(set(supplied) - set(_TIER_FIELDS))
    if unknown:
        raise TierServiceError("فیلد ناشناخته: " + ", ".join(unknown))
    if not supplied:
        return current
    assignments: list[str] = []
    params: list[Any] = []
    for key, value in supplied.items():
        if key == "title_fa":
            assignments.append("title_fa=?")
            params.append(_clean_title(value))
        elif key in {"discount_bps", "cashback_bps"}:
            assignments.append(f"{key}=?")
            params.append(_clean_int(value, _TIER_FIELDS[key], maximum=MAX_BPS))
        else:
            assignments.append(f"{key}=?")
            params.append(_clean_int(value, _TIER_FIELDS[key]))
    params.extend([actor, code])
    conn.execute(
        "UPDATE tier_policies SET " + ",".join(assignments)
        + " WHERE actor_type=? AND tier_code=?",
        params,
    )
    return get_tier(conn, actor, code)


def delete_tier(conn, actor_type: Any, tier_code: Any) -> dict[str, Any]:
    """Retire a tier policy, but never while an actor still sits on it.

    Deleting a tier in use would leave ``representatives.tier`` pointing at a code
    that no longer exists, and the next price calculation would silently fall back
    to a 0% discount while ``reserve_test_quota`` refused the actor outright. The
    guard turns that into an explicit, actionable refusal.
    """
    actor = _clean_actor_type(actor_type)
    code = _clean_tier_code(tier_code)
    if get_tier(conn, actor, code) is None:
        raise TierServiceError(f"سطح «{code}» برای این نقش یافت نشد.")
    usage = tier_usage(conn, actor, code)
    if usage["total"] > 0:
        raise TierPolicyConflict(
            f"این سطح در حال استفاده است ({usage['total']} مورد)؛ ابتدا "
            "کاربران/نمایندگان آن را به سطح دیگری منتقل کنید."
        )
    conn.execute(
        "DELETE FROM tier_policies WHERE actor_type=? AND tier_code=?", (actor, code))
    return {"actor_type": actor, "tier_code": code, "deleted": True}


__all__ = [
    "ACTOR_TYPES",
    "ACTOR_TYPE_FA",
    "BPS_PER_PERCENT",
    "DailyTestQuotaExceeded",
    "MAX_BPS",
    "MAX_TIER_TITLE",
    "TEHRAN",
    "TIER_CODE_RE",
    "TierPolicyConflict",
    "TierPolicyUnavailable",
    "TierServiceError",
    "create_tier",
    "delete_tier",
    "finish_test_quota",
    "get_tier",
    "is_first_purchase",
    "list_tiers",
    "parse_int",
    "parse_percent_bps",
    "reserve_test_quota",
    "tier_usage",
    "update_tier",
]
