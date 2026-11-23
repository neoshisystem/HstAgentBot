"""ACL-first subscription resolution and deterministic, short-lived panel config cache."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import ipaddress
import time
from dataclasses import dataclass
from typing import Any, Iterable

import domain_service


@dataclass(frozen=True)
class GatewayResult:
    status: int
    body: bytes = b""
    profile_id: int | None = None
    failures: int = 0


_CONFIG_CACHE: dict[tuple[int, int, str], tuple[float, tuple[str, ...]]] = {}


def _host(value: str) -> str:
    if not isinstance(value, str) or not value or "," in value:
        raise ValueError("invalid host")
    value = value.strip()
    if "://" in value or "@" in value or "/" in value or "?" in value or "#" in value:
        raise ValueError("invalid host")
    # Host headers may contain a numeric port; the port is never used for routing.
    if value.startswith("["):
        raise ValueError("IP literals are not accepted")
    name = value.rsplit(":", 1)[0] if value.count(":") == 1 and value.rsplit(":", 1)[1].isdigit() else value
    return domain_service.normalize_hostname(name)


def select_request_host(
    host: str, forwarded_host: str | None, remote_ip: str | None,
    trusted_proxy_cidrs: Iterable[str] = (),
) -> str:
    """Only honor X-Forwarded-Host when the socket peer is in an explicit CIDR allowlist."""
    selected = host
    if forwarded_host:
        trusted = False
        try:
            peer = ipaddress.ip_address(remote_ip or "")
            trusted = any(peer in ipaddress.ip_network(item, strict=False) for item in trusted_proxy_cidrs)
        except ValueError:
            trusted = False
        if trusted:
            if "," in forwarded_host:
                raise ValueError("multiple forwarded hosts are not accepted")
            selected = forwarded_host
    return _host(selected)


def _can_view_account(conn, owner_tg_id: int, account) -> bool:
    if account["deleted_at"] is not None:
        return False
    if account["owner_tg_id"] is not None and int(account["owner_tg_id"]) == int(owner_tg_id):
        return True
    if account["rep_id"] is not None:
        seller = conn.execute(
            "SELECT 1 FROM representatives WHERE id=? AND telegram_id=? "
            "AND is_active=1 AND deleted_at IS NULL",
            (int(account["rep_id"]), int(owner_tg_id)),
        ).fetchone()
        if seller:
            return True
    grant = conn.execute(
        "SELECT 1 FROM account_access_grants WHERE account_id=? AND grantee_tg_id=? "
        "AND access_level IN ('viewer','manager') AND revoked_at IS NULL",
        (int(account["id"]), int(owner_tg_id)),
    ).fetchone()
    return grant is not None


def _cache_key(account) -> tuple[int, int, str]:
    panel_id = int(account["panel_id"])
    account_id = int(account["id"])
    revision = str(account["credential_revision"] if "credential_revision" in account.keys()
                   else account["revision"])
    return panel_id, account_id, revision


def invalidate_cache(account_id: int | None = None) -> None:
    if account_id is None:
        _CONFIG_CACHE.clear()
    else:
        for key in tuple(_CONFIG_CACHE):
            if key[1] == int(account_id):
                _CONFIG_CACHE.pop(key, None)


async def collect_configs(
    accounts: list[Any], adapter_factory: Any, *,
    timeout: float = 5.0, cache_ttl: float = 30.0, concurrency: int = 8,
    cache: dict | None = None,
) -> tuple[list[str], int]:
    """Fetch account configs with bounded parallelism; cache payloads, never ACL decisions."""
    selected_cache = _CONFIG_CACHE if cache is None else cache
    now = time.monotonic()
    semaphore = asyncio.Semaphore(max(1, min(int(concurrency), 32)))

    async def one(account):
        key = _cache_key(account)
        cached = selected_cache.get(key)
        if cached and cached[0] > now:
            return list(cached[1]), False
        if cached:
            selected_cache.pop(key, None)
        async with semaphore:
            try:
                adapter = adapter_factory.resolve_account_adapter(int(account["id"]))
                if asyncio.iscoroutine(adapter):
                    adapter = await adapter
                configs = await asyncio.wait_for(
                    adapter.get_client_configs(str(account["email"])), timeout=float(timeout)
                )
                uris = []
                for node in configs or ():
                    uri = getattr(node, "uri", None)
                    if isinstance(uri, str) and uri and "\n" not in uri and "\r" not in uri:
                        uris.append(uri)
                selected_cache[key] = (now + min(float(cache_ttl), 30.0), tuple(uris))
                return uris, False
            except Exception:
                return [], True

    results = await asyncio.gather(*(one(account) for account in accounts))
    ordered, seen, failures = [], set(), 0
    for uris, failed in results:
        failures += int(failed)
        for uri in uris:
            if uri not in seen:
                seen.add(uri)
                ordered.append(uri)
    return ordered, failures


async def resolve_subscription_request(
    conn, token: str, host: str, *, canonical_host: str,
    forwarded_host: str | None = None, remote_ip: str | None = None,
    trusted_proxy_cidrs: Iterable[str] = (), adapter_factory: Any = None,
    cache: dict | None = None,
) -> GatewayResult:
    """Validate routing and current per-account ACLs before any cache lookup or panel read."""
    try:
        selected_host = select_request_host(host, forwarded_host, remote_ip, trusted_proxy_cidrs)
        canonical = domain_service.normalize_hostname(canonical_host)
    except Exception:
        return GatewayResult(404)
    if selected_host != canonical and domain_service.active_domain_rep(conn, selected_host) is None:
        return GatewayResult(404)
    if not isinstance(token, str) or not 40 <= len(token) <= 64:
        return GatewayResult(404)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    profile = conn.execute(
        "SELECT * FROM subscription_profiles WHERE token_hash=? AND enabled=1 AND deleted_at IS NULL",
        (token_hash,),
    ).fetchone()
    if profile is None or not domain_service.resolve_profile_host(conn, profile, selected_host, canonical):
        return GatewayResult(404)
    accounts = conn.execute(
        "SELECT a.* FROM subscription_profile_accounts m "
        "JOIN accounts a ON a.id=m.account_id "
        "WHERE m.profile_id=? ORDER BY m.sort_order,m.added_at,m.account_id",
        (int(profile["profile_id"]),),
    ).fetchall()
    # Every current owner/representative/grant decision is checked for every request,
    # even when the panel config cache is warm.
    authorized = [account for account in accounts if _can_view_account(conn, int(profile["owner_tg_id"]), account)]
    if not authorized:
        return GatewayResult(200, b"", int(profile["profile_id"]))
    if adapter_factory is None:
        return GatewayResult(503, profile_id=int(profile["profile_id"]))
    uris, failures = await collect_configs(authorized, adapter_factory, cache=cache)
    if failures and not uris:
        return GatewayResult(503, profile_id=int(profile["profile_id"]), failures=failures)
    return GatewayResult(200, "\n".join(uris).encode("utf-8"),
                         int(profile["profile_id"]), failures)


def encode_subscription(uris_or_body: Iterable[str] | bytes, format_name: str) -> bytes:
    if format_name not in {"raw", "base64"}:
        raise ValueError("unsupported subscription format")
    if isinstance(uris_or_body, bytes):
        raw = uris_or_body
    else:
        raw = "\n".join(uris_or_body).encode("utf-8")
    return raw if format_name == "raw" else base64.b64encode(raw)


__all__ = ["GatewayResult", "select_request_host", "collect_configs",
           "resolve_subscription_request", "encode_subscription", "invalidate_cache"]
