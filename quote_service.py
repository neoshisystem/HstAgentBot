"""Immutable tenant-bound v5.2 quote snapshots."""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
from collections.abc import Mapping
from typing import Any


QUOTE_TTL_SECONDS = 1800
POLICY_VERSION = "v1.0"
_ALLOWED_PURPOSES = frozenset({
    "wallet_topup", "rep_settlement", "vpn_buy", "vpn_renew",
    "vpn_addon", "digital_product_buy",
})
_FORBIDDEN_KEYS = frozenset({
    "action", "target", "target_payload", "callback", "callback_data",
    "command", "dispatch", "route", "url", "token", "api_token",
    "secret", "cipher", "password", "credential", "authorization",
    "private_key", "sub_id", "uuid",
})


class QuoteError(ValueError):
    """A quote is invalid, expired, inaccessible, or has changed."""


class QuoteTenantMismatch(QuoteError):
    """The actor or bot does not own this quote."""


class QuoteExpired(QuoteError):
    """The immutable quote has reached its exact expiry timestamp."""


def _positive_id(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise QuoteError(f"{label} must be a positive integer")
    return value


def _money(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise QuoteError("amount_toman must be a non-negative integer")
    return value


def _timestamp(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise QuoteError("now must be a non-negative UTC epoch-seconds integer")
    return value


def _clean_json(value: Any, depth: int = 0) -> Any:
    if depth > 7:
        raise QuoteError("quote snapshot is too deeply nested")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        raise QuoteError("floating point values are not accepted in quote snapshots")
    if isinstance(value, list):
        if len(value) > 100:
            raise QuoteError("quote snapshot list is too large")
        return [_clean_json(item, depth + 1) for item in value]
    if isinstance(value, Mapping):
        cleaned = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise QuoteError("quote snapshot keys must be strings")
            normalized = key.strip().lower()
            if normalized in _FORBIDDEN_KEYS or any(
                word in normalized
                for word in ("secret", "token", "cipher", "password", "credential")
            ):
                raise QuoteError("quote snapshot cannot contain actions, targets, or secrets")
            cleaned[key] = _clean_json(item, depth + 1)
        return cleaned
    raise QuoteError("quote snapshot contains a non-JSON value")


def _canonical(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _bot_context(conn: sqlite3.Connection, bot_id: int) -> dict:
    row = conn.execute(
        "SELECT bot_id,rep_id,is_primary,status,revision FROM bot_registry WHERE bot_id=?",
        (bot_id,),
    ).fetchone()
    if row is None or str(row[3]).lower() != "active":
        raise QuoteTenantMismatch("active bot registry identity is unavailable")
    if bool(row[2]) and row[1] is None:
        merchant_key = "root"
    elif row[1] is not None:
        merchant_key = f"rep:{int(row[1])}"
    else:
        raise QuoteTenantMismatch("bot registry cannot establish a merchant tenant")
    return {
        "bot_id": int(row[0]),
        "rep_id": None if row[1] is None else int(row[1]),
        "is_primary": bool(row[2]),
        "revision": int(row[4]),
        "merchant_key": merchant_key,
    }


def _fingerprint(
    quote_id: str, actor_tg_id: int, bot_id: int, merchant_key: str, purpose: str,
    amount_toman: int, spec_json: str, price_edges_json: str, policy_version: str,
    created_at: int, expires_at: int,
) -> str:
    material = {
        "quote_id": quote_id,
        "actor_tg_id": actor_tg_id,
        "bot_id": bot_id,
        "merchant_key": merchant_key,
        "purpose": purpose,
        "amount_toman": amount_toman,
        "spec_json": spec_json,
        "price_edges_json": price_edges_json,
        "policy_version": policy_version,
        "created_at": created_at,
        "expires_at": expires_at,
    }
    return hashlib.sha256(_canonical(material).encode("utf-8")).hexdigest()


def create_quote(
    conn: sqlite3.Connection,
    *,
    actor_tg_id: int,
    bot_id: int,
    merchant_key: str,
    purpose: str,
    amount_toman: int,
    spec: Mapping[str, Any],
    price_edges: list[Mapping[str, Any]],
    account_id: int | None = None,
    plan_code: str | None = None,
    product_id: int | None = None,
    panel_id: int | None = None,
    policy_version: str = POLICY_VERSION,
    now: int,
) -> dict:
    """INSERT one frozen quote; never reprices or updates an existing quote."""
    actor_tg_id = _positive_id(actor_tg_id, "actor_tg_id")
    bot_id = _positive_id(bot_id, "bot_id")
    amount_toman = _money(amount_toman)
    created_at = _timestamp(now)
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    purpose = str(purpose or "").strip()
    if purpose not in _ALLOWED_PURPOSES:
        raise QuoteError("quote purpose is not an approved checkout action")
    context = _bot_context(conn, bot_id)
    if merchant_key != context["merchant_key"]:
        raise QuoteTenantMismatch("merchant_key does not match the registered bot tenant")
    if not isinstance(spec, Mapping):
        raise QuoteError("spec must be a JSON object")
    if not isinstance(price_edges, list):
        raise QuoteError("price_edges must be a list")

    clean_spec = _clean_json(dict(spec))
    payer_tg_id = clean_spec.get("payer_tg_id", actor_tg_id)
    payer_tg_id = _positive_id(payer_tg_id, "payer_tg_id")
    clean_spec["payer_tg_id"] = payer_tg_id
    if clean_spec.get("purpose", purpose) != purpose:
        raise QuoteError("spec purpose conflicts with quote purpose")
    clean_edges = _clean_json(list(price_edges))
    if any(not isinstance(edge, dict) for edge in clean_edges):
        raise QuoteError("each price edge must be an object")
    for edge in clean_edges:
        if "price_toman" in edge:
            _money(edge["price_toman"])
    if clean_edges and clean_edges[-1].get("price_toman") != amount_toman:
        raise QuoteError("final edge price must equal quote amount_toman")
    if not isinstance(policy_version, str) or not policy_version.strip():
        raise QuoteError("policy_version is required")
    for label, value in (
        ("account_id", account_id), ("product_id", product_id), ("panel_id", panel_id)
    ):
        if value is not None:
            _positive_id(value, label)
    if plan_code is not None and (not isinstance(plan_code, str) or not plan_code.strip()):
        raise QuoteError("plan_code must be a non-empty string")

    snapshot = {
        "policy_version": policy_version,
        "bot": context,
        "merchant_key": merchant_key,
        "payer_tg_id": payer_tg_id,
        "purpose": purpose,
        "spec": clean_spec,
        "before_after_renewal": {
            key: clean_spec[key]
            for key in ("before", "after", "renewal")
            if key in clean_spec
        },
    }
    spec_json = _canonical(snapshot)
    price_edges_json = _canonical(clean_edges)
    quote_id = secrets.token_hex(16)
    expires_at = created_at + QUOTE_TTL_SECONDS
    fingerprint = _fingerprint(
        quote_id, actor_tg_id, bot_id, merchant_key, purpose, amount_toman,
        spec_json, price_edges_json, policy_version, created_at, expires_at,
    )

    conn.execute(
        "INSERT INTO quotes(quote_id,actor_tg_id,bot_id,merchant_key,purpose,account_id,"
        "plan_code,product_id,panel_id,amount_toman,spec_json,price_edges_json,"
        "policy_version,created_at,expires_at,fingerprint) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            quote_id, actor_tg_id, bot_id, merchant_key, purpose, account_id,
            plan_code, product_id, panel_id, amount_toman, spec_json,
            price_edges_json, policy_version, created_at, expires_at, fingerprint,
        ),
    )
    return {
        "quote_id": quote_id,
        "actor_tg_id": actor_tg_id,
        "bot_id": bot_id,
        "merchant_key": merchant_key,
        "purpose": purpose,
        "amount_toman": amount_toman,
        "created_at": created_at,
        "expires_at": expires_at,
        "fingerprint": fingerprint,
        "snapshot": snapshot,
        "price_edges": clean_edges,
    }


def load_quote_for_actor(
    conn: sqlite3.Connection,
    quote_id: str,
    actor_tg_id: int,
    bot_id: int,
    *,
    now: int,
) -> dict:
    """Load only the exact actor/bot quote and verify its immutable fingerprint."""
    actor_tg_id = _positive_id(actor_tg_id, "actor_tg_id")
    bot_id = _positive_id(bot_id, "bot_id")
    timestamp = _timestamp(now)
    if not isinstance(conn, sqlite3.Connection):
        raise TypeError("conn must be a sqlite3.Connection")
    if not isinstance(quote_id, str) or re.fullmatch(r"[0-9a-f]{32}", quote_id) is None:
        raise QuoteError("quote_id is invalid")

    row = conn.execute(
        "SELECT quote_id,actor_tg_id,bot_id,merchant_key,purpose,account_id,plan_code,"
        "product_id,panel_id,amount_toman,spec_json,price_edges_json,policy_version,"
        "created_at,expires_at,fingerprint FROM quotes WHERE quote_id=?",
        (quote_id,),
    ).fetchone()
    if row is None:
        raise QuoteError("quote does not exist")
    if int(row[1]) != actor_tg_id or int(row[2]) != bot_id:
        raise QuoteTenantMismatch("quote belongs to another actor or bot")
    if timestamp >= int(row[14]):
        raise QuoteExpired("quote expired at its exact expires_at timestamp")
    context = _bot_context(conn, bot_id)
    if context["merchant_key"] != str(row[3]):
        raise QuoteTenantMismatch("registered bot tenant changed")

    expected = _fingerprint(
        str(row[0]), int(row[1]), int(row[2]), str(row[3]), str(row[4]),
        int(row[9]), str(row[10]), str(row[11]), str(row[12]), int(row[13]), int(row[14]),
    )
    if not secrets.compare_digest(expected, str(row[15])):
        raise QuoteError("quote fingerprint integrity check failed")
    try:
        snapshot = json.loads(row[10])
        edges = json.loads(row[11])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise QuoteError("quote snapshot is not valid JSON") from exc
    _clean_json(snapshot)
    _clean_json(edges)
    return {
        "quote_id": str(row[0]),
        "actor_tg_id": int(row[1]),
        "bot_id": int(row[2]),
        "merchant_key": str(row[3]),
        "purpose": str(row[4]),
        "account_id": row[5],
        "plan_code": row[6],
        "product_id": row[7],
        "panel_id": row[8],
        "amount_toman": int(row[9]),
        "spec_json": str(row[10]),
        "price_edges_json": str(row[11]),
        "policy_version": str(row[12]),
        "created_at": int(row[13]),
        "expires_at": int(row[14]),
        "fingerprint": str(row[15]),
        "snapshot": snapshot,
        "spec": snapshot["spec"],
        "price_edges": edges,
    }
