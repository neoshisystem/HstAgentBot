"""Transactional subscription-profile services for HstAgentBot v5.2."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import base64
import hashlib
import html
import secrets
import sqlite3
import time
import unicodedata
from typing import Any

import config
from confirmation_service import ConfirmationError, consume_confirmation
from crypto_store import CryptoStoreError, encrypt_secret

DEFAULT_PROFILE_NAME = "⭐ پروفایل پیش‌فرض من"
DEFAULT_CUSTOM_PROFILE_LIMIT = 5
_TOKEN_DOMAIN = "hstagent/columns/v1"


class ProfileError(RuntimeError):
    """Invalid profile state or operation."""


class ProfileNotFoundError(ProfileError):
    """The requested profile does not exist or is no longer active."""


class ProfilePermissionError(ProfileError, PermissionError):
    """The actor cannot manage this profile or view the requested account."""


class ProfileQuotaError(ProfileError):
    """The owner has reached the custom-profile quota."""


class ProfileBootstrapError(ProfileError):
    """A verified primary bot is unavailable."""


class ProfileConfirmationError(ProfileError):
    """A confirmation is stale, replayed, or otherwise unusable."""


@dataclass(frozen=True)
class ProfileResult:
    profile_id: int
    owner_tg_id: int
    name: str
    is_default: bool
    enabled: bool
    created_bot_id: int
    token_version: int
    revision: int
    token: str | None = field(default=None, repr=False, compare=False)
    created: bool = False

    @property
    def display_name(self) -> str:
        return html.escape(self.name, quote=True)


@dataclass(frozen=True)
class MemberResult:
    profile_id: int
    account_id: int
    added: bool
    sort_order: int
    visible_profile_names: tuple[str, ...] = ()


def _positive(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{label} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{label} must be a positive integer") from exc
    if result <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return result


def _timestamp(now: int | None) -> int:
    value = int(time.time()) if now is None else now
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TypeError("now must be a non-negative UTC epoch-seconds integer")
    return value


def _root_id() -> int:
    value = getattr(config, "ADMIN_TELEGRAM_ID", 0)
    if isinstance(value, bool):
        return 0
    try:
        result = int(value)
    except (TypeError, ValueError):
        return 0
    return result if result > 0 else 0


def _actor(actor_tg_id: int) -> dict[str, Any]:
    actor_id = _positive(actor_tg_id, "actor_tg_id")
    root_id = _root_id()
    return {"id": actor_id, "root": root_id > 0 and actor_id == root_id}


@contextmanager
def _atomic(conn: sqlite3.Connection):
    """Serialize writes and preserve an enclosing caller transaction."""
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    if conn.in_transaction:
        conn.execute("SAVEPOINT subscription_profile_service")
        try:
            yield
            conn.execute("RELEASE SAVEPOINT subscription_profile_service")
        except Exception:
            conn.execute("ROLLBACK TO SAVEPOINT subscription_profile_service")
            conn.execute("RELEASE SAVEPOINT subscription_profile_service")
            raise
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise


def get_primary_bot_id(conn: sqlite3.Connection) -> int:
    """Use only a getMe-verified active primary bot registration."""
    row = conn.execute(
        "SELECT bot_id FROM bot_registry "
        "WHERE is_primary=1 AND status='active' "
        "AND verified_at IS NOT NULL AND token_cipher IS NOT NULL "
        "AND token_cipher<>''"
    ).fetchone()
    if row is None:
        raise ProfileBootstrapError("a getMe-verified active primary bot is required")
    return int(row["bot_id"] if isinstance(row, sqlite3.Row) else row[0])


def _new_token_material() -> tuple[str, str, str]:
    token = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode("ascii").rstrip("=")
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    try:
        token_cipher = encrypt_secret(token, _TOKEN_DOMAIN)
    except CryptoStoreError as exc:
        raise ProfileError("profile token encryption is unavailable") from exc
    return token, token_hash, token_cipher


def _profile_row(conn: sqlite3.Connection, profile_id: int):
    row = conn.execute(
        "SELECT profile_id,owner_tg_id,name,is_default,enabled,created_bot_id,"
        "token_version,revision,deleted_at FROM subscription_profiles "
        "WHERE profile_id=? AND deleted_at IS NULL",
        (profile_id,),
    ).fetchone()
    if row is None:
        raise ProfileNotFoundError("profile is unavailable")
    return row


def _result(row, *, token: str | None = None, created: bool = False) -> ProfileResult:
    return ProfileResult(
        profile_id=int(row["profile_id"]),
        owner_tg_id=int(row["owner_tg_id"]),
        name=str(row["name"]),
        is_default=bool(row["is_default"]),
        enabled=bool(row["enabled"]),
        created_bot_id=int(row["created_bot_id"]),
        token_version=int(row["token_version"]),
        revision=int(row["revision"]),
        token=token,
        created=created,
    )


def _assert_actor_not_blocked(conn: sqlite3.Connection, actor: dict[str, Any]) -> None:
    if actor["root"]:
        return
    row = conn.execute(
        "SELECT is_blocked FROM bot_users WHERE telegram_id=?",
        (actor["id"],),
    ).fetchone()
    if row is not None and bool(row["is_blocked"]):
        raise ProfilePermissionError("blocked actors cannot manage profiles")


def _require_profile_access(conn: sqlite3.Connection, actor_tg_id: int, profile_id: int):
    actor = _actor(actor_tg_id)
    _assert_actor_not_blocked(conn, actor)
    profile = _profile_row(conn, _positive(profile_id, "profile_id"))
    if not actor["root"] and int(profile["owner_tg_id"]) != actor["id"]:
        raise ProfilePermissionError("actor does not own this profile")
    return profile, actor


def _account_viewable(conn: sqlite3.Connection, actor: dict[str, Any], account_id: int) -> bool:
    account = conn.execute(
        "SELECT id,rep_id,owner_tg_id,deleted_at FROM accounts WHERE id=?",
        (account_id,),
    ).fetchone()
    if account is None or account["deleted_at"] is not None:
        return False
    if actor["root"]:
        return True
    if account["owner_tg_id"] is not None and int(account["owner_tg_id"]) == actor["id"]:
        return True
    if account["rep_id"] is not None:
        seller = conn.execute(
            "SELECT 1 FROM representatives WHERE id=? AND telegram_id=? "
            "AND is_active=1 AND deleted_at IS NULL",
            (int(account["rep_id"]), actor["id"]),
        ).fetchone()
        if seller is not None:
            return True
    grant = conn.execute(
        "SELECT 1 FROM account_access_grants "
        "WHERE account_id=? AND grantee_tg_id=? "
        "AND access_level IN ('viewer','manager') AND revoked_at IS NULL",
        (account_id, actor["id"]),
    ).fetchone()
    return grant is not None


def _require_account_view(conn: sqlite3.Connection, actor: dict[str, Any], account_id: int) -> None:
    if not _account_viewable(conn, actor, account_id):
        raise ProfilePermissionError("actor cannot view this account configuration")


def _quota(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT value FROM system_settings WHERE key='sub_profile_limit'"
    ).fetchone()
    if row is None:
        return DEFAULT_CUSTOM_PROFILE_LIMIT
    try:
        value = int(row["value"] if isinstance(row, sqlite3.Row) else row[0])
    except (TypeError, ValueError):
        raise ProfileError("sub_profile_limit setting is invalid") from None
    if value < 0:
        raise ProfileError("sub_profile_limit cannot be negative")
    return value


def _brand_rep_id(conn: sqlite3.Connection, owner_tg_id: int) -> int | None:
    rows = conn.execute(
        "SELECT id FROM representatives WHERE telegram_id=? "
        "AND is_active=1 AND deleted_at IS NULL ORDER BY id",
        (owner_tg_id,),
    ).fetchall()
    if len(rows) != 1:
        return None
    return int(rows[0]["id"] if isinstance(rows[0], sqlite3.Row) else rows[0][0])


def _insert_profile(
    conn: sqlite3.Connection, *, owner_tg_id: int, name: str, is_default: bool,
    brand_rep_id: int | None, created_bot_id: int, now: int,
) -> tuple[int, str]:
    token, token_hash, token_cipher = _new_token_material()
    cursor = conn.execute(
        "INSERT INTO subscription_profiles("
        "owner_tg_id,name,is_default,brand_rep_id,created_bot_id,token_hash,token_cipher,created_at"
        ") VALUES(?,?,?,?,?,?,?,?)",
        (owner_tg_id, name, int(is_default), brand_rep_id, created_bot_id,
         token_hash, token_cipher, now),
    )
    return int(cursor.lastrowid), token


def _validate_name(name: str) -> str:
    if not isinstance(name, str):
        raise TypeError("profile name must be text")
    selected = name.strip()
    if not 1 <= len(selected) <= 64:
        raise ValueError("profile name must contain 1..64 characters")
    if any(unicodedata.category(ch) == "Cc" for ch in selected):
        raise ValueError("profile name cannot contain control characters")
    return selected


def ensure_default_profile(
    conn: sqlite3.Connection,
    owner_tg_id: int,
    *,
    now: int | None = None,
) -> ProfileResult:
    """Ensure the one global default profile for a Telegram ID."""
    owner = _positive(owner_tg_id, "owner_tg_id")
    timestamp = _timestamp(now)
    with _atomic(conn):
        existing = conn.execute(
            "SELECT profile_id FROM subscription_profiles "
            "WHERE owner_tg_id=? AND is_default=1 AND deleted_at IS NULL",
            (owner,),
        ).fetchone()
        if existing is not None:
            return _result(_profile_row(conn, int(existing[0])))
        primary_bot_id = get_primary_bot_id(conn)
        profile_id, token = _insert_profile(
            conn, owner_tg_id=owner, name=DEFAULT_PROFILE_NAME, is_default=True,
            brand_rep_id=None, created_bot_id=primary_bot_id, now=timestamp,
        )
        return _result(_profile_row(conn, profile_id), token=token, created=True)


def create_profile(
    conn: sqlite3.Connection,
    actor_tg_id: int,
    name: str,
    *,
    owner_tg_id: int | None = None,
    account_id: int | None = None,
    now: int | None = None,
) -> ProfileResult:
    """Create a custom profile and optional first account membership atomically."""
    actor = _actor(actor_tg_id)
    owner = actor["id"] if owner_tg_id is None else _positive(owner_tg_id, "owner_tg_id")
    selected_name = _validate_name(name)
    timestamp = _timestamp(now)
    if not actor["root"] and owner != actor["id"]:
        raise ProfilePermissionError("only root may create a profile for another principal")
    selected_account = None if account_id is None else _positive(account_id, "account_id")

    with _atomic(conn):
        _assert_actor_not_blocked(conn, actor)
        primary_bot_id = get_primary_bot_id(conn)
        if selected_account is not None:
            _require_account_view(conn, actor, selected_account)
        count = int(conn.execute(
            "SELECT COUNT(*) FROM subscription_profiles "
            "WHERE owner_tg_id=? AND is_default=0 AND deleted_at IS NULL",
            (owner,),
        ).fetchone()[0])
        if count >= _quota(conn):
            raise ProfileQuotaError("custom profile quota has been reached")
        profile_id, token = _insert_profile(
            conn, owner_tg_id=owner, name=selected_name, is_default=False,
            brand_rep_id=_brand_rep_id(conn, owner),
            created_bot_id=primary_bot_id, now=timestamp,
        )
        if selected_account is not None:
            _insert_member(conn, profile_id, selected_account, actor["id"], timestamp)
        return _result(_profile_row(conn, profile_id), token=token, created=True)


def _insert_member(
    conn: sqlite3.Connection, profile_id: int, account_id: int,
    actor_tg_id: int, now: int,
) -> tuple[bool, int]:
    existing = conn.execute(
        "SELECT sort_order FROM subscription_profile_accounts WHERE profile_id=? AND account_id=?",
        (profile_id, account_id),
    ).fetchone()
    if existing is not None:
        return False, int(existing["sort_order"] if isinstance(existing, sqlite3.Row) else existing[0])
    sort_order = int(conn.execute(
        "SELECT COALESCE(MAX(sort_order),-1)+1 FROM subscription_profile_accounts WHERE profile_id=?",
        (profile_id,),
    ).fetchone()[0])
    cursor = conn.execute(
        "INSERT INTO subscription_profile_accounts("
        "profile_id,account_id,added_by_tg_id,sort_order,added_at"
        ") VALUES(?,?,?,?,?) ON CONFLICT(profile_id,account_id) DO NOTHING",
        (profile_id, account_id, actor_tg_id, sort_order, now),
    )
    added = cursor.rowcount == 1
    if added:
        conn.execute(
            "UPDATE subscription_profiles SET revision=revision+1 "
            "WHERE profile_id=? AND deleted_at IS NULL",
            (profile_id,),
        )
    else:
        existing = conn.execute(
            "SELECT sort_order FROM subscription_profile_accounts WHERE profile_id=? AND account_id=?",
            (profile_id, account_id),
        ).fetchone()
        sort_order = int(existing[0])
    return added, sort_order


def _visible_member_profile_names(
    conn: sqlite3.Connection, actor: dict[str, Any], account_id: int, current_profile_id: int,
) -> tuple[str, ...]:
    sql = (
        "SELECT p.name FROM subscription_profile_accounts m "
        "JOIN subscription_profiles p ON p.profile_id=m.profile_id "
        "WHERE m.account_id=? AND p.profile_id<>? AND p.deleted_at IS NULL "
    )
    params: list[Any] = [account_id, current_profile_id]
    if not actor["root"]:
        sql += "AND p.owner_tg_id=? "
        params.append(actor["id"])
    sql += "ORDER BY p.created_at,p.profile_id"
    return tuple(
        html.escape(str(row["name"] if isinstance(row, sqlite3.Row) else row[0]), quote=True)
        for row in conn.execute(sql, tuple(params)).fetchall()
    )


def add_profile_member(
    conn: sqlite3.Connection,
    actor_tg_id: int,
    profile_id: int,
    account_id: int,
    *,
    now: int | None = None,
) -> MemberResult:
    """Idempotently add a view-authorized account; no panel API is called."""
    timestamp = _timestamp(now)
    selected_profile = _positive(profile_id, "profile_id")
    selected_account = _positive(account_id, "account_id")
    with _atomic(conn):
        profile, actor = _require_profile_access(conn, actor_tg_id, selected_profile)
        _require_account_view(conn, actor, selected_account)
        added, order = _insert_member(
            conn, selected_profile, selected_account, actor["id"], timestamp
        )
        names = _visible_member_profile_names(conn, actor, selected_account, selected_profile)
        return MemberResult(selected_profile, selected_account, added, order, names)


def _consume_profile_confirmation(
    conn: sqlite3.Connection, *, nonce: str, actor_tg_id: int,
    profile, action: str, now: int, target_type: str = "subscription_profile",
    target_id: str | int | None = None,
) -> None:
    try:
        result = consume_confirmation(
            conn, nonce=nonce, actor_tg_id=actor_tg_id,
            bot_id=int(profile["created_bot_id"]), action=action,
            target_type=target_type,
            target_id=profile["profile_id"] if target_id is None else target_id,
            expected_revision=int(profile["revision"]), now=now,
        )
    except ConfirmationError:
        raise
    if result["replayed"]:
        raise ProfileConfirmationError("profile confirmation was already consumed")


def remove_profile_member(
    conn: sqlite3.Connection, actor_tg_id: int, profile_id: int, account_id: int,
    *, nonce: str, now: int,
) -> bool:
    """Remove the confirmed membership only; retain the source account unchanged."""
    timestamp = _timestamp(now)
    selected_profile = _positive(profile_id, "profile_id")
    selected_account = _positive(account_id, "account_id")
    with _atomic(conn):
        profile, actor = _require_profile_access(conn, actor_tg_id, selected_profile)
        _require_account_view(conn, actor, selected_account)
        _consume_profile_confirmation(
            conn, nonce=nonce, actor_tg_id=actor["id"], profile=profile,
            action="profile.member.remove", target_type="subscription_profile_account",
            target_id=f"{selected_profile}:{selected_account}", now=timestamp,
        )
        changed = conn.execute(
            "DELETE FROM subscription_profile_accounts WHERE profile_id=? AND account_id=?",
            (selected_profile, selected_account),
        ).rowcount
        if changed:
            updated = conn.execute(
                "UPDATE subscription_profiles SET revision=revision+1 "
                "WHERE profile_id=? AND revision=? AND deleted_at IS NULL",
                (selected_profile, int(profile["revision"])),
            ).rowcount
            if updated != 1:
                raise ProfileConfirmationError("profile revision changed during membership removal")
        return changed == 1


def rotate_profile_token(
    conn: sqlite3.Connection, actor_tg_id: int, profile_id: int, *, nonce: str, now: int,
) -> ProfileResult:
    """Rotate the current token; the old hash becomes invalid in the same commit."""
    timestamp = _timestamp(now)
    selected_profile = _positive(profile_id, "profile_id")
    with _atomic(conn):
        profile, actor = _require_profile_access(conn, actor_tg_id, selected_profile)
        _consume_profile_confirmation(
            conn, nonce=nonce, actor_tg_id=actor["id"], profile=profile,
            action="profile.rotate", now=timestamp,
        )
        token, token_hash, token_cipher = _new_token_material()
        changed = conn.execute(
            "UPDATE subscription_profiles SET token_hash=?,token_cipher=?,"
            "token_version=token_version+1,revision=revision+1 "
            "WHERE profile_id=? AND revision=? AND deleted_at IS NULL",
            (token_hash, token_cipher, selected_profile, int(profile["revision"])),
        ).rowcount
        if changed != 1:
            raise ProfileConfirmationError("profile changed after token rotation preview")
        return _result(_profile_row(conn, selected_profile), token=token)


def set_profile_enabled(
    conn: sqlite3.Connection, actor_tg_id: int, profile_id: int, enabled: bool,
    *, nonce: str, now: int,
) -> ProfileResult:
    """Pause/resume without changing bot or panel credentials."""
    if not isinstance(enabled, bool):
        raise TypeError("enabled must be a boolean")
    timestamp = _timestamp(now)
    selected_profile = _positive(profile_id, "profile_id")
    action = "profile.resume" if enabled else "profile.pause"
    with _atomic(conn):
        profile, actor = _require_profile_access(conn, actor_tg_id, selected_profile)
        _consume_profile_confirmation(
            conn, nonce=nonce, actor_tg_id=actor["id"], profile=profile,
            action=action, now=timestamp,
        )
        changed = conn.execute(
            "UPDATE subscription_profiles SET enabled=?,revision=revision+1 "
            "WHERE profile_id=? AND revision=? AND deleted_at IS NULL",
            (int(enabled), selected_profile, int(profile["revision"])),
        ).rowcount
        if changed != 1:
            raise ProfileConfirmationError("profile revision changed during pause/resume")
        return _result(_profile_row(conn, selected_profile))


def clear_profile(
    conn: sqlite3.Connection, actor_tg_id: int, profile_id: int, *, nonce: str, now: int,
) -> int:
    """Clear only memberships; even a default profile remains active and present."""
    timestamp = _timestamp(now)
    selected_profile = _positive(profile_id, "profile_id")
    with _atomic(conn):
        profile, actor = _require_profile_access(conn, actor_tg_id, selected_profile)
        _consume_profile_confirmation(
            conn, nonce=nonce, actor_tg_id=actor["id"], profile=profile,
            action="profile.clear", now=timestamp,
        )
        removed = int(conn.execute(
            "DELETE FROM subscription_profile_accounts WHERE profile_id=?",
            (selected_profile,),
        ).rowcount)
        changed = conn.execute(
            "UPDATE subscription_profiles SET revision=revision+1 "
            "WHERE profile_id=? AND revision=? AND deleted_at IS NULL",
            (selected_profile, int(profile["revision"])),
        ).rowcount
        if changed != 1:
            raise ProfileConfirmationError("profile changed during clear")
        return removed


def delete_profile(
    conn: sqlite3.Connection, actor_tg_id: int, profile_id: int, *, nonce: str, now: int,
) -> bool:
    """Soft-delete a custom profile; the DB trigger independently protects default."""
    timestamp = _timestamp(now)
    selected_profile = _positive(profile_id, "profile_id")
    with _atomic(conn):
        profile, actor = _require_profile_access(conn, actor_tg_id, selected_profile)
        if bool(profile["is_default"]):
            raise ProfileError("the default profile cannot be deleted")
        _consume_profile_confirmation(
            conn, nonce=nonce, actor_tg_id=actor["id"], profile=profile,
            action="profile.delete", now=timestamp,
        )
        changed = conn.execute(
            "UPDATE subscription_profiles SET deleted_at=?,enabled=0,revision=revision+1 "
            "WHERE profile_id=? AND is_default=0 AND deleted_at IS NULL AND revision=?",
            (timestamp, selected_profile, int(profile["revision"])),
        ).rowcount
        if changed != 1:
            raise ProfileConfirmationError("profile changed during delete")
        return True


def rename_profile(
    conn: sqlite3.Connection, actor_tg_id: int, profile_id: int, name: str,
    *, expected_revision: int | None = None,
) -> ProfileResult:
    """Store a trimmed plain-text label; expose only an HTML-escaped view value."""
    selected_profile = _positive(profile_id, "profile_id")
    selected_name = _validate_name(name)
    with _atomic(conn):
        profile, actor = _require_profile_access(conn, actor_tg_id, selected_profile)
        revision = int(profile["revision"])
        if expected_revision is not None and _positive(expected_revision, "expected_revision") != revision:
            raise ProfileConfirmationError("profile rename revision is stale")
        changed = conn.execute(
            "UPDATE subscription_profiles SET name=?,revision=revision+1 "
            "WHERE profile_id=? AND revision=? AND deleted_at IS NULL",
            (selected_name, selected_profile, revision),
        ).rowcount
        if changed != 1:
            raise ProfileConfirmationError("profile changed during rename")
        return _result(_profile_row(conn, selected_profile))


def resolve_profile_by_token(conn: sqlite3.Connection, token: str) -> dict[str, Any] | None:
    """Resolve only the current enabled token; stale/paused tokens return no match."""
    if not isinstance(token, str) or not token:
        return None
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    row = conn.execute(
        "SELECT profile_id,owner_tg_id,name,is_default,enabled,created_bot_id,"
        "token_version,revision FROM subscription_profiles "
        "WHERE token_hash=? AND enabled=1 AND deleted_at IS NULL",
        (token_hash,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    result["display_name"] = html.escape(str(result["name"]), quote=True)
    return result


__all__ = [
    "DEFAULT_PROFILE_NAME", "ProfileError", "ProfileNotFoundError",
    "ProfilePermissionError", "ProfileQuotaError", "ProfileBootstrapError",
    "ProfileConfirmationError", "ProfileResult", "MemberResult", "get_primary_bot_id",
    "ensure_default_profile", "create_profile", "add_profile_member",
    "remove_profile_member", "rotate_profile_token", "set_profile_enabled",
    "clear_profile", "delete_profile", "rename_profile", "resolve_profile_by_token",
]
