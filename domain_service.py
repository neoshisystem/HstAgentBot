"""Verified representative domain lifecycle for subscription links.

DNS/TLS work is deliberately completed before the short SQLite state transition.
All external resolvers and probes are injectable so tests remain offline.
"""
from __future__ import annotations

import ipaddress
import os
import secrets
import socket
import sqlite3
import ssl
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from confirmation_service import consume_confirmation


class DomainError(RuntimeError):
    """A domain cannot be registered, verified, or routed safely."""


class DomainPermissionError(DomainError):
    """The actor does not own the representative domain."""


class DomainProofError(DomainError):
    """Ownership, destination, or TLS proof is incomplete."""


@dataclass(frozen=True)
class DomainResult:
    domain_id: int
    rep_id: int
    hostname: str
    challenge: str
    status: str
    verified_at: int | None = None
    tls_checked_at: int | None = None


def normalize_hostname(value: str) -> str:
    if not isinstance(value, str):
        raise DomainError("hostname must be text")
    raw = value.strip().rstrip(".")
    if not raw or raw != value.strip() or any(ch in raw for ch in "/:@?#[]\\"):
        raise DomainError("hostname must not contain a URL, path, port, or credentials")
    try:
        host = raw.encode("idna").decode("ascii").lower()
        ipaddress.ip_address(host)
    except ValueError:
        pass
    except UnicodeError:
        raise DomainError("hostname is not valid IDNA") from None
    else:
        raise DomainError("IP addresses are not valid hostnames")
    if len(host) > 253 or not all(
        label and len(label) <= 63 and label[0].isalnum() and label[-1].isalnum()
        and all(ch.isalnum() or ch == "-" for ch in label)
        for label in host.split(".")
    ):
        raise DomainError("hostname is not a valid DNS name")
    if "." not in host or host in {"localhost", "local"} or host.endswith(".localhost"):
        raise DomainError("local hostnames are not allowed")
    return host


def _actor(conn: sqlite3.Connection, actor_tg_id: int, rep_id: int):
    row = conn.execute(
        "SELECT id,telegram_id,is_active,deleted_at FROM representatives WHERE id=?",
        (int(rep_id),),
    ).fetchone()
    if row is None or row["deleted_at"] is not None or not bool(row["is_active"]):
        raise DomainPermissionError("representative is unavailable")
    root_id = int(os.getenv("ADMIN_TELEGRAM_ID", "0") or "0")
    if root_id and int(actor_tg_id) == root_id:
        return row
    if int(row["telegram_id"] or 0) != int(actor_tg_id):
        raise DomainPermissionError("actor does not own this representative domain")
    return row


def register_domain(
    conn: sqlite3.Connection, actor_tg_id: int, rep_id: int, hostname: str,
    *, now: int | None = None, challenge: str | None = None,
) -> DomainResult:
    """Create a pending domain and return its random TXT ownership challenge."""
    selected = normalize_hostname(hostname)
    timestamp = int(time.time()) if now is None else int(now)
    with conn:
        rep = _actor(conn, actor_tg_id, rep_id)
        nonce = challenge or secrets.token_urlsafe(32)
        if not isinstance(nonce, str) or not 32 <= len(nonce) <= 128:
            raise DomainError("domain challenge must contain 32..128 characters")
        try:
            cur = conn.execute(
                "INSERT INTO sub_domains(rep_id,hostname,challenge,status,created_at) "
                "VALUES(?,?,?,'pending',?)",
                (int(rep["id"]), selected, nonce, timestamp),
            )
        except sqlite3.IntegrityError:
            raise DomainError("hostname is already registered") from None
        return DomainResult(int(cur.lastrowid), int(rep["id"]), selected, nonce, "pending")


def _records(resolver, name: str, kind: str) -> list[str]:
    try:
        if callable(resolver):
            answer = resolver(name, kind)
        else:
            answer = resolver.resolve(name, kind, lifetime=4)
    except Exception:
        return []
    values = []
    for item in answer or ():
        if kind == "TXT" and hasattr(item, "strings"):
            value = b"".join(item.strings).decode("utf-8", "replace")
        elif kind == "TXT" and hasattr(item, "to_text"):
            value = str(item.to_text()).strip('"')
        elif kind == "CNAME" and hasattr(item, "target"):
            value = str(item.target)
        else:
            value = str(item)
        values.append(value.strip().strip('"').rstrip(".").lower())
    return values


def _public_ips(values: Iterable[str]) -> set[str]:
    output = set()
    for value in values:
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            continue
        if not ip.is_global:
            raise DomainProofError("DNS destination contains a non-public address")
        output.add(str(ip))
    return output


def _default_resolver(name: str, kind: str):
    import dns.resolver
    return dns.resolver.resolve(name, kind, lifetime=4)


def _tls_probe(hostname: str, addresses: set[str]) -> bool:
    context = ssl.create_default_context()
    last_error = None
    for address in sorted(addresses):
        try:
            with socket.create_connection((address, 443), timeout=4) as raw:
                with context.wrap_socket(raw, server_hostname=hostname) as tls:
                    cert = tls.getpeercert()
                    if cert and tls.version():
                        return True
        except (OSError, ssl.SSLError) as exc:
            last_error = exc
    if last_error:
        return False
    return False


def verify_domain(
    conn: sqlite3.Connection, domain_id: int, *,
    resolver: Callable[[str, str], Iterable[Any]] | Any | None = None,
    tls_probe: Callable[[str, set[str]], Any] | None = None,
    gateway_hosts: Iterable[str] | None = None,
    now: int | None = None,
) -> DomainResult:
    """Verify TXT ownership, gateway routing, public DNS and SNI/TLS without credentials."""
    row = conn.execute(
        "SELECT domain_id,rep_id,hostname,challenge,status FROM sub_domains WHERE domain_id=?",
        (int(domain_id),),
    ).fetchone()
    if row is None or row["status"] != "pending":
        raise DomainError("only a pending domain can be verified")
    host = normalize_hostname(row["hostname"])
    dns = resolver or _default_resolver
    targets = tuple(gateway_hosts or filter(None, os.getenv("HST_SUB_GATEWAY_HOSTS", "").split(",")))
    if not targets:
        try:
            import config
            targets = (config.SUB_DOMAIN,) if getattr(config, "SUB_DOMAIN", "") else ()
        except Exception:
            targets = ()
    allowed_hosts = {normalize_hostname(item) for item in targets if str(item).strip()}
    if not allowed_hosts:
        raise DomainProofError("gateway destination allowlist is not configured")
    if _records(dns, f"_hst-sub.{host}", "TXT") != [str(row["challenge"]).lower()]:
        raise DomainProofError("ownership TXT proof does not match")
    cname = _records(dns, host, "CNAME")
    host_a = _records(dns, host, "A")
    if cname and not any(item in allowed_hosts for item in cname):
        raise DomainProofError("domain CNAME is outside the gateway allowlist")
    gateway_ips = set()
    for gateway in sorted(allowed_hosts):
        gateway_ips |= _public_ips(_records(dns, gateway, "A"))
    if not gateway_ips:
        raise DomainProofError("gateway allowlist has no public A record")
    if host_a:
        host_ips = _public_ips(host_a)
        if not host_ips or not host_ips.issubset(gateway_ips):
            raise DomainProofError("domain A records do not point to the gateway")
        destination_ips = host_ips
    elif cname:
        destination_ips = gateway_ips
    else:
        raise DomainProofError("domain has neither an allowed CNAME nor a gateway A record")
    probe = tls_probe or _tls_probe
    try:
        proof = probe(host, set(destination_ips))
    except Exception:
        raise DomainProofError("TLS health proof failed") from None
    if isinstance(proof, dict):
        if not proof.get("ok") or proof.get("server_hostname", host) != host:
            raise DomainProofError("TLS/SNI health proof is invalid")
        peer = proof.get("peer_ip")
        if peer and str(peer) not in destination_ips:
            raise DomainProofError("TLS probe connected outside the verified destination")
    elif proof is not True:
        raise DomainProofError("TLS health proof is invalid")
    # Resolve again after TLS: a DNS change during the probe is a rebinding attempt.
    second_cname = _records(dns, host, "CNAME")
    second_a = _records(dns, host, "A")
    if second_cname != cname or second_a != host_a:
        raise DomainProofError("DNS changed during TLS verification")
    timestamp = int(time.time()) if now is None else int(now)
    with conn:
        changed = conn.execute(
            "UPDATE sub_domains SET status='verified',verified_at=?,tls_checked_at=? "
            "WHERE domain_id=? AND status='pending'",
            (timestamp, timestamp, int(domain_id)),
        ).rowcount
        if changed != 1:
            raise DomainError("domain state changed during verification")
    return DomainResult(int(row["domain_id"]), int(row["rep_id"]), host,
                        str(row["challenge"]), "verified", timestamp, timestamp)


def _require_owner(conn, actor_tg_id: int, rep_id: int):
    root_id = int(os.getenv("ADMIN_TELEGRAM_ID", "0") or "0")
    if root_id and int(actor_tg_id) == root_id:
        return
    row = conn.execute(
        "SELECT 1 FROM representatives WHERE id=? AND telegram_id=? "
        "AND is_active=1 AND deleted_at IS NULL",
        (int(rep_id), int(actor_tg_id)),
    ).fetchone()
    if row is None:
        raise DomainPermissionError("actor does not own this domain")


def activate_domain(
    conn: sqlite3.Connection, actor_tg_id: int, domain_id: int, *,
    nonce: str, bot_id: int, now: int, tls_max_age: int = 86400,
) -> DomainResult:
    """Activate a freshly verified domain using an actor/bot-bound one-use nonce."""
    with conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM sub_domains WHERE domain_id=?", (int(domain_id),)
        ).fetchone()
        timestamp = int(now)
        if row is None or row["status"] != "verified":
            raise DomainProofError("domain must have ownership and TLS proof before activation")
        if row["tls_checked_at"] is None or timestamp - int(row["tls_checked_at"]) > int(tls_max_age):
            raise DomainProofError("TLS proof is stale")
        _require_owner(conn, actor_tg_id, int(row["rep_id"]))
        consume_confirmation(
            conn, nonce=nonce, actor_tg_id=int(actor_tg_id), bot_id=int(bot_id),
            action="domain.activate", target_type="sub_domain", target_id=int(domain_id),
            expected_revision=int(row["tls_checked_at"]), now=timestamp,
        )
        changed = conn.execute(
            "UPDATE sub_domains SET status='active',tls_checked_at=? "
            "WHERE domain_id=? AND status='verified'",
            (timestamp, int(domain_id)),
        ).rowcount
        if changed != 1:
            raise DomainError("domain state changed during activation")
        return DomainResult(int(row["domain_id"]), int(row["rep_id"]), str(row["hostname"]),
                            str(row["challenge"]), "active", row["verified_at"], timestamp)


def disable_domain(
    conn: sqlite3.Connection, actor_tg_id: int, domain_id: int, *,
    nonce: str, bot_id: int, now: int,
) -> DomainResult:
    with conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM sub_domains WHERE domain_id=?", (int(domain_id),)).fetchone()
        if row is None or row["status"] != "active":
            raise DomainError("only an active domain can be disabled")
        _require_owner(conn, actor_tg_id, int(row["rep_id"]))
        consume_confirmation(
            conn, nonce=nonce, actor_tg_id=int(actor_tg_id), bot_id=int(bot_id),
            action="domain.disable", target_type="sub_domain", target_id=int(domain_id),
            expected_revision=int(row["tls_checked_at"]), now=int(now),
        )
        conn.execute("UPDATE sub_domains SET status='disabled' WHERE domain_id=? AND status='active'",
                     (int(domain_id),))
        return DomainResult(int(row["domain_id"]), int(row["rep_id"]), str(row["hostname"]),
                            str(row["challenge"]), "disabled", row["verified_at"], row["tls_checked_at"])


def resolve_profile_host(
    conn: sqlite3.Connection, profile: Any, host: str, canonical_host: str,
) -> bool:
    """A default profile is canonical-only; branded profiles use only their own active rep host."""
    try:
        selected = normalize_hostname(host)
        canonical = normalize_hostname(canonical_host)
    except DomainError:
        return False
    brand = profile["brand_rep_id"] if hasattr(profile, "__getitem__") else getattr(profile, "brand_rep_id", None)
    is_default = bool(profile["is_default"] if hasattr(profile, "__getitem__") else getattr(profile, "is_default", False))
    if selected == canonical:
        return True
    if brand is None or is_default:
        return False
    row = conn.execute(
        "SELECT 1 FROM sub_domains WHERE hostname=? AND rep_id=? AND status='active'",
        (selected, int(brand)),
    ).fetchone()
    return row is not None


def active_domain_rep(conn: sqlite3.Connection, host: str) -> int | None:
    try:
        selected = normalize_hostname(host)
    except DomainError:
        return None
    row = conn.execute(
        "SELECT d.rep_id FROM sub_domains d JOIN representatives r ON r.id=d.rep_id "
        "WHERE d.hostname=? AND d.status='active' AND r.is_active=1 AND r.deleted_at IS NULL",
        (selected,),
    ).fetchone()
    return int(row["rep_id"]) if row else None


__all__ = ["DomainError", "DomainPermissionError", "DomainProofError", "DomainResult",
           "normalize_hostname", "register_domain", "verify_domain", "activate_domain",
           "disable_domain", "resolve_profile_host", "active_domain_rep"]
