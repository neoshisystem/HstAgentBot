"""Three-mode registration control, referral codes and per-tenant isolation.

Registration state lives on ``bot_users.registration_status`` so every legacy row
is automatically ``approved`` and no existing customer is ever locked out. The
policy is resolved per tenant: a representative's own bot overrides the bot
registry, which overrides the global ``registration_mode`` setting.

Nothing in this module performs network I/O; the caller owns the Telegram
notification and the transaction boundary.
"""
from __future__ import annotations

import secrets
import sqlite3
from typing import Any, Mapping

# Modes an operator may configure.
REGISTRATION_MODES = ("open", "approval_required", "referral_only")
# ``inherit`` is the stored value that means "ask the parent scope".
INHERIT = "inherit"

# ``awaiting_referral`` is the fourth, internal status: mode ``referral_only``
# needs somewhere to park a user who has not supplied a valid code yet.
STATUS_APPROVED = "approved"
STATUS_PENDING = "pending_approval"
STATUS_AWAITING_REFERRAL = "awaiting_referral"
STATUS_REJECTED = "rejected"
REGISTRATION_STATUSES = (
    STATUS_APPROVED, STATUS_PENDING, STATUS_AWAITING_REFERRAL, STATUS_REJECTED,
)

REFERRAL_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
REFERRAL_CODE_LENGTH = 8

# Reward kinds recorded in ``referral_rewards``.
REWARD_SIGNUP = "signup"
REWARD_FIRST_PURCHASE = "first_purchase"


class RegistrationError(ValueError):
    """A registration decision cannot be made under the current state."""


class RegistrationConflict(RegistrationError):
    """The requested transition was already applied or is not allowed."""


def normalize_mode(value: Any) -> str:
    """Return a configured mode or ``inherit``; anything else is rejected."""
    text = str(value or "").strip().lower()
    if text in REGISTRATION_MODES:
        return text
    if text in ("", INHERIT):
        return INHERIT
    raise RegistrationError(f"registration mode is invalid: {value!r}")


def normalize_referral_code(value: Any) -> str:
    """Upper-case, strip separators and reject anything outside the alphabet."""
    text = "".join(ch for ch in str(value or "").upper() if ch.isalnum())
    if not text:
        return ""
    if len(text) > 24 or any(ch not in REFERRAL_ALPHABET for ch in text):
        raise RegistrationError("referral code is invalid")
    return text


def _row(row: Any) -> dict[str, Any]:
    """Materialise a sqlite3.Row (or mapping) into a plain dict."""
    if row is None:
        return {}
    try:
        return dict(row)
    except (TypeError, ValueError):
        return {key: row[key] for key in row.keys()}


def _setting(conn: sqlite3.Connection, key: str, default: str = "") -> str:
    try:
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key=?", (key,)
        ).fetchone()
    except sqlite3.OperationalError:
        return default
    if row is None:
        return default
    value = row["value"] if not isinstance(row, tuple) else row[0]
    return default if value is None else str(value)


def _flag(conn: sqlite3.Connection, key: str, default: str = "0") -> bool:
    return _setting(conn, key, default).strip().lower() in {"1", "true", "yes", "on", "enabled"}


def _int_setting(conn: sqlite3.Connection, key: str, default: int = 0) -> int:
    try:
        return int(str(_setting(conn, key, str(default))).strip() or default)
    except (TypeError, ValueError):
        return default


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def ensure_tenant_columns(conn: sqlite3.Connection) -> None:
    """Add the per-tenant registration columns the first time they are needed.

    ``bot_registry`` is created by migration 006, which runs *after* ``init_db``,
    so the column cannot be added there; a nineteenth migration file would change
    the frozen ``schema_migrations`` count. This helper is idempotent and cheap,
    and the caller owns the commit.
    """
    for table in ("representatives", "bot_registry"):
        if not _table_exists(conn, table):
            continue
        present = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if "registration_mode" not in present:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN registration_mode "
                "TEXT NOT NULL DEFAULT 'inherit'"
            )


def resolve_mode(
    conn: sqlite3.Connection, *, bot_id: int | None = None, rep_id: int | None = None
) -> str:
    """Effective registration mode: rep bot -> bot registry -> global setting."""
    ensure_tenant_columns(conn)
    if rep_id is not None:
        try:
            row = conn.execute(
                "SELECT registration_mode FROM representatives WHERE id=?", (int(rep_id),)
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is not None:
            candidate = normalize_mode(row["registration_mode"])
            if candidate != INHERIT:
                return candidate
    if bot_id is not None:
        try:
            row = conn.execute(
                "SELECT registration_mode FROM bot_registry WHERE bot_id=?", (int(bot_id),)
            ).fetchone()
        except sqlite3.OperationalError:
            row = None
        if row is not None:
            candidate = normalize_mode(row["registration_mode"])
            if candidate != INHERIT:
                return candidate
    return normalize_mode(_setting(conn, "registration_mode", "open")) or "open"


def set_mode(
    conn: sqlite3.Connection, *, mode: str, bot_id: int | None = None, rep_id: int | None = None
) -> str:
    """Persist a per-tenant mode; with neither scope it writes the global setting."""
    ensure_tenant_columns(conn)
    selected = normalize_mode(mode)
    if rep_id is not None:
        conn.execute(
            "UPDATE representatives SET registration_mode=? WHERE id=?",
            (selected, int(rep_id)),
        )
        return selected
    if bot_id is not None:
        conn.execute(
            "UPDATE bot_registry SET registration_mode=? WHERE bot_id=?",
            (selected, int(bot_id)),
        )
        return selected
    conn.execute(
        "INSERT INTO system_settings(key,value,label,description) "
        "VALUES('registration_mode',?, 'حالت ثبت‌نام کاربران', 'open|approval_required|referral_only') "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP",
        (selected,),
    )
    return selected


def generate_referral_code(conn: sqlite3.Connection, user_id: int) -> str:
    """Return the actor's existing code or mint a fresh unique one."""
    existing = conn.execute(
        "SELECT referral_code FROM bot_users WHERE telegram_id=?", (int(user_id),)
    ).fetchone()
    if existing is not None and existing["referral_code"]:
        return str(existing["referral_code"])
    for _ in range(12):
        candidate = "".join(secrets.choice(REFERRAL_ALPHABET) for _ in range(REFERRAL_CODE_LENGTH))
        clash = conn.execute(
            "SELECT 1 FROM bot_users WHERE referral_code=?", (candidate,)
        ).fetchone()
        if clash is None:
            conn.execute(
                "UPDATE bot_users SET referral_code=? WHERE telegram_id=?",
                (candidate, int(user_id)),
            )
            return candidate
    raise RegistrationError("could not allocate a unique referral code")


def find_referrer_by_code(conn: sqlite3.Connection, code: Any) -> dict[str, Any] | None:
    """Resolve an approved referrer by code; a rejected actor cannot refer."""
    normalized = normalize_referral_code(code)
    if not normalized:
        return None
    row = conn.execute(
        "SELECT telegram_id, full_name, username, registration_status, referral_code "
        "FROM bot_users WHERE referral_code=?",
        (normalized,),
    ).fetchone()
    referrer = _row(row)
    if not referrer or str(referrer.get("registration_status")) == STATUS_REJECTED:
        return None
    return referrer


def _current(conn: sqlite3.Connection, user_id: int) -> dict[str, Any] | None:
    row = _row(conn.execute(
        "SELECT * FROM bot_users WHERE telegram_id=?", (int(user_id),)
    ).fetchone())
    return row or None


def _apply_status(
    conn: sqlite3.Connection, user_id: int, status: str, *,
    decided_by: int | None = None,
) -> None:
    if status not in REGISTRATION_STATUSES:
        raise RegistrationError("registration status is invalid")
    conn.execute(
        "UPDATE bot_users SET registration_status=?,"
        "registration_decided_at=CASE WHEN ?='approved' OR ?='rejected' "
        "THEN CURRENT_TIMESTAMP ELSE registration_decided_at END,"
        "registration_decided_by=COALESCE(?, registration_decided_by) "
        "WHERE telegram_id=?",
        (status, status, status, decided_by, int(user_id)),
    )


def register(
    conn: sqlite3.Connection,
    *,
    user_id: int,
    username: str = "",
    full_name: str = "",
    bot_id: int | None = None,
    rep_id: int | None = None,
    invite_code: Any = None,
    typed_code: Any = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Apply the tenant's registration policy to one ``/start``.

    ``invite_code`` comes from ``/start ref_<CODE>``; ``typed_code`` is a code the
    user sent afterwards. Returns the resulting status plus the resolved context
    the caller needs to render the right page and notify the right owner.
    """
    actor = int(user_id)
    mode = resolve_mode(conn, bot_id=bot_id, rep_id=rep_id)
    row = _current(conn, actor)
    if row is None:
        raise RegistrationError("actor row must exist before registration is decided")

    status = str(row.get("registration_status") or STATUS_APPROVED)
    # Root and representatives are provisioned by the operator, never queued.
    if str(row.get("role") or "") in ("super_admin", "rep"):
        return {"status": STATUS_APPROVED, "mode": mode, "changed": False,
                "referrer": None, "privileged": True}

    # ``registration_status`` defaults to 'approved' so a row that predates this
    # feature is never locked out. A row that has never been through a decision
    # (``registration_decided_at`` is NULL) is therefore *not* approved yet: the
    # tenant policy decides it now.
    fresh = status == STATUS_APPROVED and not row.get("registration_decided_at")

    supplied = invite_code if invite_code is not None else typed_code
    referrer = find_referrer_by_code(conn, supplied) if supplied else None
    if supplied and referrer is None:
        raise RegistrationError("referral code is not valid")
    if referrer is not None and int(referrer["telegram_id"]) == actor:
        raise RegistrationError("an actor cannot refer themselves")

    if referrer is not None and not row.get("referred_by_user_id"):
        conn.execute(
            "UPDATE bot_users SET referred_by_user_id=?, referred_by_code=? "
            "WHERE telegram_id=?",
            (int(referrer["telegram_id"]), str(referrer["referral_code"]), actor),
        )

    already_referred = bool(row.get("referred_by_user_id")) or referrer is not None
    # ``status`` is the already-decided state; ``fresh`` means "decide me now".
    undecided = fresh or status == STATUS_PENDING
    if mode == "open":
        target = STATUS_APPROVED
    elif mode == "approval_required":
        target = STATUS_PENDING if undecided else status
    else:  # referral_only
        if not already_referred:
            target = STATUS_AWAITING_REFERRAL
        elif _flag(conn, "referral_requires_approval", "0"):
            target = STATUS_PENDING if undecided else status
        else:
            target = STATUS_APPROVED

    # A rejected actor stays rejected until an operator decides otherwise.
    if status == STATUS_REJECTED:
        target = STATUS_REJECTED

    changed = target != status
    if changed:
        _apply_status(conn, actor, target)
    conn.execute(
        "UPDATE bot_users SET origin_bot_id=COALESCE(origin_bot_id, ?), "
        "assigned_rep_id=COALESCE(assigned_rep_id, ?) WHERE telegram_id=?",
        (int(bot_id) if bot_id else None, int(rep_id) if rep_id else None, actor),
    )
    return {
        "status": target,
        "previous_status": status,
        "mode": mode,
        "changed": changed,
        "referrer": referrer,
        "privileged": False,
    }


def approve(
    conn: sqlite3.Connection, *, user_id: int, actor_id: int | None = None
) -> dict[str, Any]:
    """Approve one pending registration; replaying an approval is a no-op."""
    row = _current(conn, int(user_id))
    if row is None:
        raise RegistrationError("user does not exist")
    if str(row.get("registration_status")) == STATUS_APPROVED:
        return {"status": STATUS_APPROVED, "changed": False, "user": row}
    _apply_status(conn, int(user_id), STATUS_APPROVED, decided_by=actor_id)
    row["registration_status"] = STATUS_APPROVED
    return {"status": STATUS_APPROVED, "changed": True, "user": row}


def reject(
    conn: sqlite3.Connection, *, user_id: int, actor_id: int | None = None
) -> dict[str, Any]:
    """Reject one registration; an already-rejected row is a no-op."""
    row = _current(conn, int(user_id))
    if row is None:
        raise RegistrationError("user does not exist")
    if str(row.get("registration_status")) == STATUS_REJECTED:
        return {"status": STATUS_REJECTED, "changed": False, "user": row}
    _apply_status(conn, int(user_id), STATUS_REJECTED, decided_by=actor_id)
    row["registration_status"] = STATUS_REJECTED
    return {"status": STATUS_REJECTED, "changed": True, "user": row}


def _pending_scope(
    bot_id: int | None, rep_id: int | None
) -> tuple[str, list[Any]]:
    """Tenant filter for the approval queue.

    A representative sees only the users of their own bot/workspace; the root
    sees everything. Without a scope the queue is global.
    """
    if rep_id is not None:
        return (
            "AND (u.assigned_rep_id=? OR u.origin_bot_id IN "
            "(SELECT bot_id FROM bot_registry WHERE rep_id=?))",
            [int(rep_id), int(rep_id)],
        )
    if bot_id is not None:
        return "AND u.origin_bot_id=?", [int(bot_id)]
    return "", []


def list_pending(
    conn: sqlite3.Connection, *, bot_id: int | None = None, rep_id: int | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Users waiting for approval or for a referral code, newest first."""
    clause, params = _pending_scope(bot_id, rep_id)
    rows = conn.execute(
        "SELECT u.telegram_id, u.username, u.full_name, u.registration_status, "
        "u.referral_code, u.referred_by_code, u.origin_bot_id, u.assigned_rep_id, "
        "u.created_at FROM bot_users u "
        "WHERE u.registration_status IN ('pending_approval','awaiting_referral') "
        f"{clause} ORDER BY u.created_at DESC LIMIT ?",
        (*params, max(1, int(limit))),
    ).fetchall()
    return [_row(r) for r in rows]


def pending_count(
    conn: sqlite3.Connection, *, bot_id: int | None = None, rep_id: int | None = None
) -> int:
    clause, params = _pending_scope(bot_id, rep_id)
    row = conn.execute(
        "SELECT COUNT(*) AS total FROM bot_users u "
        "WHERE u.registration_status IN ('pending_approval','awaiting_referral') "
        f"{clause}",
        tuple(params),
    ).fetchone()
    return int((row["total"] if row is not None else 0) or 0)


def referral_stats(conn: sqlite3.Connection, user_id: int) -> dict[str, Any]:
    """Invite totals and paid rewards for one referrer."""
    actor = int(user_id)
    invited = conn.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN registration_status='approved' THEN 1 ELSE 0 END) AS approved "
        "FROM bot_users WHERE referred_by_user_id=?",
        (actor,),
    ).fetchone()
    rewards = conn.execute(
        "SELECT COALESCE(SUM(amount_toman),0) AS total, COUNT(*) AS count "
        "FROM referral_rewards WHERE referrer_user_id=?",
        (actor,),
    ).fetchone()
    return {
        "invited": int((invited["total"] if invited is not None else 0) or 0),
        "approved": int((invited["approved"] if invited is not None else 0) or 0),
        "reward_total": int((rewards["total"] if rewards is not None else 0) or 0),
        "reward_count": int((rewards["count"] if rewards is not None else 0) or 0),
    }


def list_referrals(
    conn: sqlite3.Connection, user_id: int, *, limit: int = 20
) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT telegram_id, full_name, username, registration_status, created_at "
        "FROM bot_users WHERE referred_by_user_id=? ORDER BY created_at DESC LIMIT ?",
        (int(user_id), max(1, int(limit))),
    ).fetchall()
    return [_row(r) for r in rows]


def reward_plan(conn: sqlite3.Connection, *, order_amount: int = 0) -> dict[str, int]:
    """Configured referral rewards; a zero value means the reward is disabled."""
    signup = max(0, _int_setting(conn, "referral_signup_bonus", 0))
    first_purchase = max(0, _int_setting(conn, "referral_first_purchase_bonus", 0))
    percent = max(0, min(100, _int_setting(conn, "referral_purchase_reward_percent", 0)))
    percent_amount = (max(0, int(order_amount)) * percent) // 100
    return {
        "signup_bonus": signup,
        "first_purchase_bonus": first_purchase,
        "purchase_percent": percent,
        "purchase_amount": percent_amount,
    }


def referral_of(conn: sqlite3.Connection, user_id: int) -> int | None:
    """The referrer of one actor, if any."""
    row = conn.execute(
        "SELECT referred_by_user_id FROM bot_users WHERE telegram_id=?", (int(user_id),)
    ).fetchone()
    if row is None:
        return None
    value = row["referred_by_user_id"]
    return int(value) if value else None


def reward_already_paid(
    conn: sqlite3.Connection, *, referrer_user_id: int, referred_user_id: int, reward_type: str
) -> bool:
    row = conn.execute(
        "SELECT 1 FROM referral_rewards WHERE referrer_user_id=? AND "
        "referred_user_id=? AND reward_type=?",
        (int(referrer_user_id), int(referred_user_id), str(reward_type)),
    ).fetchone()
    return row is not None


def record_reward(
    conn: sqlite3.Connection, *, referrer_user_id: int, referred_user_id: int,
    reward_type: str, amount_toman: int, ledger_id: int | None = None,
    order_id: str | None = None,
) -> bool:
    """Insert the reward row; returns False when it was already paid."""
    cursor = conn.execute(
        "INSERT INTO referral_rewards(referrer_user_id,referred_user_id,reward_type,"
        "amount_toman,order_id,ledger_id) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(referrer_user_id,referred_user_id,reward_type) DO NOTHING",
        (int(referrer_user_id), int(referred_user_id), str(reward_type),
         int(amount_toman), order_id, ledger_id),
    )
    return cursor.rowcount == 1


def invite_link(bot_username: str, code: str) -> str:
    """Telegram deep link that pre-fills the referral code."""
    username = str(bot_username or "").lstrip("@").strip()
    normalized = normalize_referral_code(code)
    if not username or not normalized:
        return ""
    return f"https://t.me/{username}?start=ref_{normalized}"


def parse_start_payload(args: Any) -> str:
    """Extract a referral code from ``/start ref_<CODE>`` arguments."""
    if not args:
        return ""
    first = str(args[0] if not isinstance(args, (list, tuple)) else args[0]).strip()
    if not first.lower().startswith("ref_"):
        return ""
    try:
        return normalize_referral_code(first[4:])
    except RegistrationError:
        return ""


def pay_signup_bonus(
    referred_user_id: int, *, actor_id: int = 0, actor_name: str = "Registration"
) -> dict[str, Any]:
    """Credit the configured signup bonus to the referrer of an approved user.

    Runs after the approval is committed so the wallet credit and the reward row
    are their own atomic unit; a disabled or already-paid reward is reported, not
    silently swallowed.
    """
    import database

    with database.get_db() as conn:
        referrer = referral_of(conn, referred_user_id)
        if not referrer:
            return {"status": "no_referrer"}
        amount = reward_plan(conn)["signup_bonus"]
    if amount <= 0:
        return {"status": "disabled", "referrer_user_id": referrer}
    result = database.credit_referral_reward(
        referrer, int(referred_user_id), REWARD_SIGNUP, amount,
        f"هدیه تایید عضویت زیرمجموعه {int(referred_user_id)}",
        actor_id=actor_id, actor_name=actor_name,
    )
    result["referrer_user_id"] = referrer
    return result


def pay_first_purchase_reward(
    referred_user_id: int, *, order_amount: int, order_id: str | None = None,
    actor_id: int = 0, actor_name: str = "ReferralReward",
) -> dict[str, Any]:
    """Credit the first-purchase reward to the referrer of a buying user.

    The fixed bonus and the percentage reward are paid as one ``first_purchase``
    reward, which is also the idempotency key: a second purchase pays nothing.
    """
    import database

    with database.get_db() as conn:
        referrer = referral_of(conn, referred_user_id)
        if not referrer:
            return {"status": "no_referrer"}
        plan = reward_plan(conn, order_amount=order_amount)
    amount = int(plan["first_purchase_bonus"]) + int(plan["purchase_amount"])
    if amount <= 0:
        return {"status": "disabled", "referrer_user_id": referrer}
    result = database.credit_referral_reward(
        referrer, int(referred_user_id), REWARD_FIRST_PURCHASE, amount,
        f"پاداش اولین خرید زیرمجموعه {int(referred_user_id)}",
        order_id=order_id, actor_id=actor_id, actor_name=actor_name,
    )
    result["referrer_user_id"] = referrer
    return result


__all__ = [
    "RegistrationError", "RegistrationConflict",
    "REGISTRATION_MODES", "REGISTRATION_STATUSES", "INHERIT",
    "STATUS_APPROVED", "STATUS_PENDING", "STATUS_AWAITING_REFERRAL", "STATUS_REJECTED",
    "REWARD_SIGNUP", "REWARD_FIRST_PURCHASE",
    "normalize_mode", "normalize_referral_code", "resolve_mode", "set_mode",
    "ensure_tenant_columns",
    "generate_referral_code", "find_referrer_by_code", "register",
    "approve", "reject", "list_pending", "pending_count",
    "referral_stats", "list_referrals", "reward_plan", "referral_of",
    "reward_already_paid", "record_reward", "invite_link", "parse_start_payload",
    "pay_signup_bonus", "pay_first_purchase_reward",
]
