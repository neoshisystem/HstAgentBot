"""One-time, idempotent bootstrap helpers for the v5.2 legacy cutover."""

from __future__ import annotations

from contextlib import contextmanager
from collections.abc import Mapping
import json
import re
import sqlite3
from typing import Any


_PANEL_COLUMNS = frozenset({
    "id", "panel_code", "name", "panel_type", "adapter_version", "raw_url",
    "protocol", "host", "port", "base_path", "auth_type", "auth_cipher",
    "sub_base_url", "lifecycle", "is_hidden", "max_clients", "for_end_users",
    "for_reps", "access_type", "min_rep_rank", "min_user_rank",
    "username_strategy", "custom_prefix", "renewal_method",
    "connect_first_guard", "allow_test_account", "test_duration_hours",
    "test_volume_mib", "allow_addons", "extra_gib_rep_price",
    "extra_gib_user_price", "extra_day_rep_price", "extra_day_user_price",
    "default_inbound_ids_json", "extra_config_json", "revision",
})
_CUTOFF_SETTING = "v52.bootstrap.financial_cutoff"
_CUTOFF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_REP_MERCHANT_RE = re.compile(r"^rep:([1-9][0-9]*)$")


class BootstrapError(RuntimeError):
    """Raised when legacy data cannot be mapped without ambiguity or loss."""


@contextmanager
def _atomic(conn: sqlite3.Connection):
    """Use a short immediate transaction, or an isolated savepoint if nested."""
    if conn.in_transaction:
        savepoint = "v52_bootstrap"
        conn.execute(f"SAVEPOINT {savepoint}")
        try:
            yield
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except Exception:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
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


def _spec_value(panel_spec: Any, key: str, default=None):
    if isinstance(panel_spec, Mapping):
        return panel_spec.get(key, default)
    return getattr(panel_spec, key, default)


def _normalise_panel_spec(panel_spec: Any, now: int) -> dict[str, Any]:
    if panel_spec is None:
        raise ValueError("panel_spec is required")
    values = dict(panel_spec) if isinstance(panel_spec, Mapping) else {
        key: getattr(panel_spec, key)
        for key in _PANEL_COLUMNS | {"panel_id"}
        if hasattr(panel_spec, key)
    }
    if "panel_id" in values and "id" not in values:
        values["id"] = values.pop("panel_id")
    unknown = set(values) - _PANEL_COLUMNS - {"is_default", "created_at"}
    if unknown:
        raise ValueError(f"unsupported panel_spec fields: {sorted(unknown)}")
    for required in ("panel_code", "name", "panel_type"):
        if not str(values.get(required, "")).strip():
            raise ValueError(f"panel_spec.{required} must be non-empty")
        values[required] = str(values[required]).strip()
    if "id" in values:
        panel_id = values["id"]
        if isinstance(panel_id, bool) or not isinstance(panel_id, int) or panel_id <= 0:
            raise ValueError("panel_spec.id must be a positive integer")
    values.pop("is_default", None)
    values.pop("created_at", None)
    values["lifecycle"] = values.get("lifecycle", "active")
    if values["lifecycle"] not in {"active", "capacity", "drain", "maintenance"}:
        raise ValueError("unsupported panel lifecycle")
    values["is_default"] = 1
    values["created_at"] = int(now)
    return values


def _ensure_default_panel(conn: sqlite3.Connection, panel_spec: Any, now: int) -> int:
    spec = _normalise_panel_spec(panel_spec, now)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(panels)")}
    missing = {"id", "panel_code", "name", "panel_type", "is_default", "lifecycle", "created_at"} - columns
    if missing:
        raise BootstrapError(f"panels schema is incomplete: {sorted(missing)}")

    default = conn.execute(
        "SELECT * FROM panels WHERE is_default=1"
    ).fetchall()
    if len(default) > 1:
        raise BootstrapError("multiple default panels exist")
    if default:
        current = default[0]
        for key in ("id", "panel_code", "name", "panel_type"):
            if key in spec and spec[key] != current[key]:
                raise BootstrapError(f"default panel conflicts with panel_spec.{key}")
        panel_id = int(current["id"])
    else:
        keys = [key for key in spec if key in columns]
        names = ", ".join(f'"{key}"' for key in keys)
        placeholders = ", ".join("?" for _ in keys)
        conn.execute(
            f"INSERT INTO panels ({names}) VALUES ({placeholders})",
            tuple(spec[key] for key in keys),
        )
        panel_id = int(spec["id"]) if "id" in spec else int(
            conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        )

    conn.execute(
        "INSERT INTO panel_counters(panel_id,next_value) VALUES (?,1) "
        "ON CONFLICT(panel_id) DO NOTHING",
        (panel_id,),
    )
    return panel_id


def _map_reserved_packages(conn: sqlite3.Connection, default_panel_id: int) -> int:
    rows = conn.execute(
        "SELECT id,account_code,email,panel_id,account_id "
        "FROM reserved_packages ORDER BY id"
    ).fetchall()
    mapped = 0
    for reservation in rows:
        target_panel_id = (
            int(reservation["panel_id"])
            if reservation["panel_id"] is not None
            else default_panel_id
        )
        candidates = conn.execute(
            "SELECT id,account_code,email FROM accounts "
            "WHERE panel_id=? AND (account_code=? OR email=? COLLATE NOCASE) "
            "ORDER BY id",
            (
                target_panel_id,
                str(reservation["account_code"]),
                str(reservation["email"]),
            ),
        ).fetchall()
        if len(candidates) != 1:
            reason = "ambiguous" if len(candidates) > 1 else "unmapped"
            raise BootstrapError(
                f"reserved_packages row {reservation['id']} is {reason}; "
                "account_code/email do not identify exactly one account on its panel"
            )
        account = candidates[0]
        if (
            str(account["account_code"]) != str(reservation["account_code"])
            or str(account["email"]).casefold() != str(reservation["email"]).casefold()
        ):
            raise BootstrapError(
                f"reserved_packages row {reservation['id']} has mismatched account_code/email"
            )
        account_id = int(account["id"])
        if reservation["account_id"] is not None and int(reservation["account_id"]) != account_id:
            raise BootstrapError(
                f"reserved_packages row {reservation['id']} points at a different account"
            )
        conn.execute(
            "UPDATE reserved_packages SET panel_id=?,account_id=? "
            "WHERE id=? AND (panel_id IS NULL OR panel_id=?) "
            "AND (account_id IS NULL OR account_id=?)",
            (target_panel_id, account_id, int(reservation["id"]), target_panel_id, account_id),
        )
        mapped += 1
    return mapped


def bootstrap_legacy_panel(conn, panel_spec, now):
    """Map only legacy, unscoped data to one validated default panel.

    Price, owner and balance columns are deliberately never assigned here.
    A reservation with a conflicting code/email pair aborts the whole unit.
    """
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise TypeError("now must be a non-negative integer epoch-seconds value")
    with _atomic(conn):
        panel_id = _ensure_default_panel(conn, panel_spec, now)
        conn.execute(
            "UPDATE accounts SET panel_id=?,"
            "seller_key=CASE WHEN rep_id IS NULL THEN 'root' ELSE 'rep:'||rep_id END "
            "WHERE panel_id IS NULL",
            (panel_id,),
        )
        conn.execute(
            "INSERT INTO plan_panels(plan_code,panel_id,allowed) "
            "SELECT plan_code,?,1 FROM pricing_plans WHERE 1 "
            "ON CONFLICT DO NOTHING",
            (panel_id,),
        )
        mapped_reservations = _map_reserved_packages(conn, panel_id)
        return {
            "panel_id": panel_id,
            "mapped_reservations": mapped_reservations,
        }


def _insert_opening_anchor(
    conn: sqlite3.Connection,
    *,
    rep_id: int | None,
    user_id: int | None,
    balance: int,
    description: str,
    actor_id: int,
    business_key: str,
    merchant_key: str,
    ledger_scope: str,
    edge_id: int | None,
) -> None:
    conn.execute(
        "INSERT INTO financial_ledger("
        "rep_id,user_id,transaction_type,amount,balance_after,description,actor_id,"
        "business_key,merchant_key,ledger_scope,edge_id,policy_version"
        ") VALUES(?,?,'opening_anchor',ABS(?),?,?,?, ?,?, ?,?,'v1.0') "
        "ON CONFLICT DO NOTHING",
        (
            rep_id,
            user_id,
            balance,
            balance,
            description,
            actor_id,
            business_key,
            merchant_key,
            ledger_scope,
            edge_id,
        ),
    )
    row = conn.execute(
        "SELECT rep_id,user_id,transaction_type,amount,balance_after,description,"
        "actor_id,business_key,merchant_key,ledger_scope,edge_id,policy_version "
        "FROM financial_ledger WHERE business_key=?",
        (business_key,),
    ).fetchone()
    expected = (
        rep_id, user_id, "opening_anchor", abs(balance), balance, description,
        actor_id, business_key, merchant_key, ledger_scope, edge_id, "v1.0",
    )
    if row is None or tuple(row) != expected:
        raise BootstrapError(f"opening anchor key conflicts with existing ledger data: {business_key}")


def bootstrap_financial_edges(conn, cutoff_id, now):
    """Record a single immutable financial cutoff and opening anchors.

    This function is intended for the controlled offline cutover after new
    financial intake is paused and legacy in-flight intents have been resolved
    or explicitly mapped. It does not pause intake or mutate any balance cache.
    """
    cutoff_id = str(cutoff_id or "").strip()
    if not _CUTOFF_RE.fullmatch(cutoff_id):
        raise ValueError("cutoff_id must be a stable 1..128 character identifier")
    if isinstance(now, bool) or not isinstance(now, int) or now < 0:
        raise TypeError("now must be a non-negative integer epoch-seconds value")

    with _atomic(conn):
        previous = conn.execute(
            "SELECT value FROM system_settings WHERE key=?",
            (_CUTOFF_SETTING,),
        ).fetchone()
        if previous is not None:
            try:
                registered = json.loads(previous["value"])
            except (TypeError, ValueError) as exc:
                raise BootstrapError("financial cutoff marker is malformed") from exc
            if registered.get("cutoff_id") != cutoff_id:
                raise BootstrapError("a different financial cutoff has already been registered")
            return {
                "cutoff_id": cutoff_id,
                "already_applied": True,
                "edge_count": int(registered.get("edge_count", 0)),
                "anchor_count": int(registered.get("anchor_count", 0)),
            }

        root = conn.execute(
            "SELECT id,telegram_id FROM representatives WHERE rep_code='root_admin_rep'"
        ).fetchall()
        if len(root) != 1 or root[0]["telegram_id"] is None or int(root[0]["telegram_id"]) <= 0:
            raise BootstrapError("a unique root_admin_rep with a valid Telegram ID is required")
        root_actor_id = int(root[0]["telegram_id"])

        representatives = conn.execute(
            "SELECT id,rep_code,balance_toman,credit_limit_toman,billing_mode,"
            "is_active,deleted_at FROM representatives "
            "WHERE COALESCE(rep_code,'')<>'root_admin_rep' ORDER BY id"
        ).fetchall()
        edge_count = 0
        anchor_count = 0
        for rep in representatives:
            rep_id = int(rep["id"])
            balance = int(rep["balance_toman"] or 0)
            billing_mode = str(rep["billing_mode"] or "credit")
            if billing_mode not in {"credit", "prepaid_wallet"}:
                raise BootstrapError(f"representative {rep_id} has an unsupported billing mode")
            active = int(bool(rep["is_active"]) and rep["deleted_at"] is None)
            ended_at = None if active else int(now)
            existing = conn.execute(
                "SELECT id,parent_key,billing_mode,debt_toman,credit_limit_toman,active "
                "FROM rep_edges WHERE child_rep_id=? ORDER BY id",
                (rep_id,),
            ).fetchall()
            if len(existing) > 1:
                raise BootstrapError(f"representative {rep_id} has multiple existing financial edges")
            if existing:
                edge = existing[0]
                if (
                    edge["parent_key"] != "root"
                    or edge["billing_mode"] != billing_mode
                    or int(edge["debt_toman"]) != balance
                    or int(edge["credit_limit_toman"]) != int(rep["credit_limit_toman"] or 0)
                    or int(edge["active"]) != active
                ):
                    raise BootstrapError(
                        f"representative {rep_id} has an incompatible pre-existing edge"
                    )
                edge_id = int(edge["id"])
            else:
                cursor = conn.execute(
                    "INSERT INTO rep_edges("
                    "parent_key,child_rep_id,billing_mode,debt_toman,credit_limit_toman,"
                    "active,started_at,ended_at"
                    ") VALUES('root',?,?,?,?,?,?,?)",
                    (
                        rep_id,
                        billing_mode,
                        balance,
                        int(rep["credit_limit_toman"] or 0),
                        active,
                        int(now),
                        ended_at,
                    ),
                )
                edge_id = int(cursor.lastrowid)
            edge_count += 1
            _insert_opening_anchor(
                conn,
                rep_id=rep_id,
                user_id=None,
                balance=balance,
                description=f"opening_anchor cutoff={cutoff_id} edge={edge_id}",
                actor_id=root_actor_id,
                business_key=f"v52:opening:{cutoff_id}:edge:{edge_id}",
                merchant_key="root",
                ledger_scope="edge",
                edge_id=edge_id,
            )
            anchor_count += 1

        root_wallets = conn.execute(
            "SELECT user_id,balance_toman FROM user_wallets ORDER BY user_id"
        ).fetchall()
        for wallet in root_wallets:
            user_id = int(wallet["user_id"])
            balance = int(wallet["balance_toman"] or 0)
            _insert_opening_anchor(
                conn,
                rep_id=None,
                user_id=user_id,
                balance=balance,
                description=f"opening_anchor cutoff={cutoff_id} root_wallet={user_id}",
                actor_id=root_actor_id,
                business_key=f"v52:opening:{cutoff_id}:wallet:root:{user_id}",
                merchant_key="root",
                ledger_scope="wallet",
                edge_id=None,
            )
            anchor_count += 1

        merchant_wallets = conn.execute(
            "SELECT merchant_key,user_tg_id,balance_toman "
            "FROM merchant_wallets ORDER BY merchant_key,user_tg_id"
        ).fetchall()
        for wallet in merchant_wallets:
            merchant_key = str(wallet["merchant_key"])
            match = _REP_MERCHANT_RE.fullmatch(merchant_key)
            if match is None:
                raise BootstrapError(f"unsupported merchant wallet namespace: {merchant_key}")
            rep_id = int(match.group(1))
            if conn.execute(
                "SELECT 1 FROM representatives WHERE id=?", (rep_id,)
            ).fetchone() is None:
                raise BootstrapError(f"merchant wallet references unknown representative {rep_id}")
            user_id = int(wallet["user_tg_id"])
            balance = int(wallet["balance_toman"] or 0)
            _insert_opening_anchor(
                conn,
                rep_id=rep_id,
                user_id=user_id,
                balance=balance,
                description=f"opening_anchor cutoff={cutoff_id} merchant_wallet={merchant_key}:{user_id}",
                actor_id=root_actor_id,
                business_key=f"v52:opening:{cutoff_id}:wallet:{merchant_key}:{user_id}",
                merchant_key=merchant_key,
                ledger_scope="wallet",
                edge_id=None,
            )
            anchor_count += 1

        marker = {
            "cutoff_id": cutoff_id,
            "registered_at": int(now),
            "actor_id": root_actor_id,
            "edge_count": edge_count,
            "anchor_count": anchor_count,
            "policy_version": "v1.0",
        }
        conn.execute(
            "INSERT INTO system_settings(key,value,label,description) "
            "VALUES(?,?,?,?)",
            (
                _CUTOFF_SETTING,
                json.dumps(marker, sort_keys=True, separators=(",", ":")),
                "v5.2 financial opening cutoff",
                "Immutable opening-anchor cutover marker; does not change balance caches.",
            ),
        )
        return {
            "cutoff_id": cutoff_id,
            "already_applied": False,
            "edge_count": edge_count,
            "anchor_count": anchor_count,
        }
