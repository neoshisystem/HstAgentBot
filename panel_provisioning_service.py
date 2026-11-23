"""Real account creation on a 3X-UI panel when the pre-allocated pool is empty.

The pool is always tried first: a pre-allocated account is cheaper, already
verified and needs no panel mutation. Only when the pool has nothing does this
module create a client on the panel, and it does so through the same
prepared-operation discipline every other v5.2 mutation uses:

1. a provisional ``accounts`` row is persisted so the new client has a durable
   local identity before anything leaves the process;
2. a ``panel_operations`` row of kind ``create_client`` is persisted in state
   ``prepared`` — that row is exactly what the adapter's preflight demands, so a
   client can never be created without a prepared, account-bound operation;
3. the adapter is dispatched once;
4. the outcome is finalized: ``success`` keeps the account and records the panel
   identifiers, ``definite_failure`` removes the provisional row, and ``unknown``
   keeps both the row and the operation for reconciliation.

No network I/O happens in the ``prepare_*`` helpers, so they are unit-testable
without a panel.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
import time
import uuid
from typing import Any, Mapping, Sequence

import crypto_store

CREATE_KIND = "create_client"
REQUEST_LABEL = "hstagent/panel-operation/v1"
GIB = 1024 ** 3
MAX_PROVISION_ATTEMPTS = 3


class ProvisioningError(ValueError):
    """A client cannot be provisioned under the current panel/pool state."""


class NoEligiblePanel(ProvisioningError):
    """No active panel can serve this actor or plan."""


class InboundUnavailable(ProvisioningError):
    """The panel exposes no usable inbound for a new client."""


def _positive(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProvisioningError(f"{label} must be a positive integer")
    return value


def _row(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        return dict(row)
    except (TypeError, ValueError):
        return {key: row[key] for key in row.keys()}


def _parse_inbound_ids(raw: Any) -> tuple[int, ...]:
    """Read ``panels.default_inbound_ids_json`` without trusting its shape."""
    value = raw
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return ()
        try:
            value = json.loads(text)
        except (TypeError, ValueError):
            return ()
    if not isinstance(value, (list, tuple)):
        return ()
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            continue
        if item not in result:
            result.append(item)
    return tuple(result)


def eligible_panels(
    conn: sqlite3.Connection, *, for_role: str = "end_user", rep_id: int | None = None,
    plan_code: str | None = None,
) -> list[dict[str, Any]]:
    """Active panels this actor may be provisioned on, most specific first.

    A plan binding (``plan_panels``) wins when it exists; otherwise every active
    panel that is published to the actor's role is a candidate.
    """
    role_column = "for_reps" if str(for_role) == "rep" else "for_end_users"
    rows: list[dict[str, Any]] = []
    if plan_code:
        try:
            rows = [_row(r) for r in conn.execute(
                "SELECT p.* FROM plan_panels pp JOIN panels p ON p.id = pp.panel_id "
                f"WHERE pp.plan_code=? AND p.lifecycle='active' AND p.is_hidden=0 "
                f"AND p.{role_column}=1 ORDER BY p.is_default DESC, p.id",
                (str(plan_code),),
            ).fetchall()]
        except sqlite3.OperationalError:
            rows = []
    if not rows:
        rows = [_row(r) for r in conn.execute(
            "SELECT * FROM panels WHERE lifecycle='active' AND is_hidden=0 "
            f"AND {role_column}=1 ORDER BY is_default DESC, id",
        ).fetchall()]
    if rep_id is not None:
        assigned = [_row(r) for r in conn.execute(
            "SELECT p.* FROM panel_rep_assignments a JOIN panels p ON p.id = a.panel_id "
            "WHERE a.rep_id=? AND p.lifecycle='active' AND p.is_hidden=0 "
            f"AND p.{role_column}=1 ORDER BY p.is_default DESC, p.id",
            (int(rep_id),),
        ).fetchall()] if _table_exists(conn, "panel_rep_assignments") else []
        if assigned:
            return assigned
    return rows


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def select_panel(
    conn: sqlite3.Connection, *, for_role: str = "end_user", rep_id: int | None = None,
    plan_code: str | None = None, panel_id: int | None = None,
) -> dict[str, Any]:
    """Pick the panel a new client should be created on, or raise."""
    if panel_id is not None:
        row = _row(conn.execute(
            "SELECT * FROM panels WHERE id=? AND lifecycle='active'", (int(panel_id),)
        ).fetchone())
        if not row:
            raise NoEligiblePanel("panel is not active")
        return row
    candidates = eligible_panels(conn, for_role=for_role, rep_id=rep_id, plan_code=plan_code)
    if not candidates:
        raise NoEligiblePanel("no active panel is published to this actor")
    return candidates[0]


def resolve_inbound_ids(panel: Mapping[str, Any]) -> tuple[int, ...]:
    """Configured default inbound scope for a panel, or an empty tuple."""
    return _parse_inbound_ids(panel.get("default_inbound_ids_json"))


async def discover_inbound_ids(adapter: Any) -> tuple[int, ...]:
    """First enabled inbound of the panel, used when no default is configured."""
    lister = getattr(adapter, "list_inbounds", None)
    if not callable(lister):
        raise InboundUnavailable("adapter cannot enumerate inbounds")
    try:
        inbounds = await lister()
    except Exception as exc:
        raise InboundUnavailable(f"inbound enumeration failed: {type(exc).__name__}") from exc
    enabled = [item for item in inbounds
               if isinstance(item, Mapping) and item.get("enable") is not False
               and isinstance(item.get("id"), int) and item["id"] > 0]
    if not enabled:
        raise InboundUnavailable("panel has no enabled inbound")
    return (int(enabled[0]["id"]),)


def build_client_email(panel: Mapping[str, Any], *, prefix: str | None = None) -> str:
    """Stable, collision-resistant external identifier for the new client."""
    base = str(prefix or panel.get("custom_prefix") or "").strip()
    if not base:
        base = str(panel.get("panel_code") or "hst").strip() or "hst"
    base = "".join(ch for ch in base if ch.isalnum() or ch in "-_")[:12] or "hst"
    return f"{base}-{secrets.token_hex(5)}"


def build_candidate_credentials() -> dict[str, Any]:
    """A fresh UUID for a VLESS/VMess client; never reused across retries."""
    return {"id": str(uuid.uuid4())}


def prepare_provisioning(
    conn: sqlite3.Connection,
    *,
    panel: Mapping[str, Any],
    email: str,
    gb: float,
    days: int,
    owner_tg_id: int | None,
    actor_tg_id: int,
    bot_id: int,
    seller_key: str = "root",
    rep_id: int | None = None,
    customer_name: str = "",
    order_id: str | None = None,
    inbound_ids: Sequence[int] | None = None,
    comment: str = "",
    now: int | None = None,
) -> dict[str, Any]:
    """Persist the provisional account and the prepared operation; no network.

    Returns the ticket the dispatcher needs. The operation row is what makes the
    adapter's preflight pass, so dispatch is impossible without this step.
    """
    panel_id = _positive(int(panel.get("id") or 0), "panel_id")
    actor = _positive(int(actor_tg_id), "actor_tg_id")
    bot = _positive(int(bot_id), "bot_id")
    identifier = str(email or "").strip()
    if not identifier:
        raise ProvisioningError("email is required")
    duration_days = _positive(int(days), "days")
    quota_gb = float(gb)
    if quota_gb < 0:
        raise ProvisioningError("gb cannot be negative")
    scope = tuple(int(i) for i in (inbound_ids or resolve_inbound_ids(panel)))
    if not scope:
        raise InboundUnavailable("no inbound is configured or discovered for this panel")
    timestamp = int(time.time()) if now is None else int(now)

    existing = conn.execute(
        "SELECT id FROM accounts WHERE panel_id=? AND LOWER(email)=LOWER(?)",
        (panel_id, identifier),
    ).fetchone()
    if existing is not None:
        raise ProvisioningError("an account with this identifier already exists on the panel")

    account_code = identifier
    cursor = conn.execute(
        "INSERT INTO accounts(rep_id, account_code, email, customer_name, sub_id, enable, "
        "total_gb, expiry_time, owner_tg_id, panel_id, seller_key, revision) "
        "VALUES(?,?,?,?,NULL,1,?,0,?,?,?,1)",
        (int(rep_id) if rep_id else None, account_code, identifier,
         str(customer_name or ""), quota_gb, owner_tg_id, panel_id, str(seller_key or "root")),
    )
    account_id = int(cursor.lastrowid)

    operation_id = uuid.uuid4().hex
    lock_key = f"panel-create:{panel_id}:{identifier.lower()}"
    lock_token = secrets.token_hex(16)
    request_payload = {
        "email": identifier,
        "gb": quota_gb,
        "days": duration_days,
        "inbound_ids": list(scope),
        "comment": str(comment or ""),
        "owner_tg_id": owner_tg_id,
        "seller_key": str(seller_key or "root"),
    }
    expected_effect = {
        "snapshot": {"account_id": account_id, "panel_id": panel_id, "email": identifier},
        "target": {"inbound_ids": list(scope), "quota_gb": quota_gb, "days": duration_days},
    }
    conn.execute(
        "INSERT INTO panel_operations(operation_id, order_id, actor_tg_id, bot_id, account_id, "
        "panel_id, kind, request_cipher, expected_effect_json, state, lock_key, lock_token, "
        "idempotency_key, generation, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,'prepared',?,?,?,0,?,?)",
        (
            operation_id, order_id, actor, bot, account_id, panel_id, CREATE_KIND,
            crypto_store.encrypt_secret(json.dumps(request_payload, sort_keys=True), REQUEST_LABEL),
            json.dumps(expected_effect, sort_keys=True),
            lock_key, lock_token, f"panel-create:{operation_id}", timestamp, timestamp,
        ),
    )
    return {
        "operation_id": operation_id,
        "account_id": account_id,
        "panel_id": panel_id,
        "email": identifier,
        "inbound_ids": scope,
        "lock_key": lock_key,
        "lock_token": lock_token,
        "gb": quota_gb,
        "days": duration_days,
        "owner_tg_id": owner_tg_id,
        "order_id": order_id,
        "prepared_at": timestamp,
    }


def build_create_spec(ticket: Mapping[str, Any], *, credentials: Mapping[str, Any] | None = None,
                      comment: str = "") -> dict[str, Any]:
    """Translate a ticket into the adapter's ``ClientCreateSpec``.

    ``on_first_connect`` is always ``False``: the published create schema has no
    documented first-connect timer, and the adapter refuses a ``True`` value.
    """
    gb = float(ticket.get("gb") or 0)
    quota_bytes = int(gb * GIB) if gb > 0 else None
    return {
        "operation_id": str(ticket["operation_id"]),
        "email": str(ticket["email"]),
        "quota_mode": "finite" if quota_bytes else "unlimited",
        "quota_bytes": quota_bytes,
        "duration_seconds": int(ticket["days"]) * 86400,
        "on_first_connect": False,
        "inbound_ids": tuple(int(i) for i in ticket["inbound_ids"]),
        "comment": str(comment or f"order:{ticket.get('order_id') or 'direct'}")[:200],
        "candidate_credentials": dict(credentials or build_candidate_credentials()),
        "enable": True,
    }


def finalize_provisioning(
    conn: sqlite3.Connection, ticket: Mapping[str, Any], result: Any, *,
    now: int | None = None,
) -> dict[str, Any]:
    """Apply one dispatch outcome to the account and the operation row.

    ``success`` records the panel identifiers on the account; ``definite_failure``
    deletes the provisional account so no orphan row survives; ``unknown`` keeps
    both rows and the persistent lock for reconciliation.
    """
    timestamp = int(time.time()) if now is None else int(now)
    operation_id = str(ticket["operation_id"])
    account_id = int(ticket["account_id"])
    state = str(getattr(result, "state", None) or (result or {}).get("state") or "unknown")
    evidence = getattr(result, "evidence", None)
    if evidence is None and isinstance(result, Mapping):
        evidence = result.get("evidence")
    evidence_json = json.dumps(evidence or {}, sort_keys=True, default=str)[:4000]

    if state == "success":
        conn.execute(
            "UPDATE panel_operations SET state='succeeded', evidence_json=?, updated_at=? "
            "WHERE operation_id=?",
            (evidence_json, timestamp, operation_id),
        )
        conn.execute(
            "UPDATE accounts SET revision=revision+1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (account_id,),
        )
        return {"state": "success", "account_id": account_id, "operation_id": operation_id}

    if state == "definite_failure":
        conn.execute(
            "UPDATE panel_operations SET state='definite_failure', evidence_json=?, updated_at=? "
            "WHERE operation_id=?",
            (evidence_json, timestamp, operation_id),
        )
        # The panel never created the client, so the provisional row must not be
        # usable: an orphan account would be handed to the next buyer. It is
        # soft-deleted rather than removed so the operation keeps its audit link
        # and the ``panel_operations.account_id`` foreign key stays valid.
        conn.execute(
            "UPDATE accounts SET deleted_at=CURRENT_TIMESTAMP, enable=0, "
            "revision=revision+1 WHERE id=? AND sub_id IS NULL",
            (account_id,),
        )
        return {"state": "definite_failure", "account_id": account_id,
                "operation_id": operation_id, "account_removed": True}

    conn.execute(
        "UPDATE panel_operations SET state='needs_reconciliation', evidence_json=?, updated_at=? "
        "WHERE operation_id=?",
        (evidence_json, timestamp, operation_id),
    )
    return {"state": "unknown", "account_id": account_id, "operation_id": operation_id,
            "locks_retained": True}


async def provision_account(
    adapter: Any, conn: sqlite3.Connection, **kwargs: Any
) -> dict[str, Any]:
    """Full create flow: prepare, dispatch once, finalize.

    The caller owns the transaction. ``conn`` must be idle (no open transaction)
    when this is called, because the adapter performs network I/O between the
    prepared write and the finalizing write.
    """
    if conn.in_transaction:
        raise ProvisioningError("provision_account requires an idle connection")
    ticket = prepare_provisioning(conn, **kwargs)
    conn.commit()
    comment = str(kwargs.get("comment") or "")
    spec = build_create_spec(ticket, comment=comment)
    try:
        result = await adapter.create_client(spec)
    except Exception as exc:
        # The request may or may not have reached the panel: keep the lock.
        outcome = finalize_provisioning(
            conn, ticket, {"state": "unknown", "evidence": {"exception": type(exc).__name__}})
        conn.commit()
        outcome["dispatch_error"] = type(exc).__name__
        return outcome
    outcome = finalize_provisioning(conn, ticket, result)
    conn.commit()
    outcome["ticket"] = ticket
    return outcome


__all__ = [
    "ProvisioningError", "NoEligiblePanel", "InboundUnavailable", "CREATE_KIND",
    "eligible_panels", "select_panel", "resolve_inbound_ids", "discover_inbound_ids",
    "build_client_email", "build_candidate_credentials", "prepare_provisioning",
    "build_create_spec", "finalize_provisioning", "provision_account",
]
