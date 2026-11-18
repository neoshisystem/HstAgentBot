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


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def ensure_domain_columns(conn: sqlite3.Connection) -> None:
    """Add the v5.2 domain-management columns and the ``drain`` state on demand.

    ``sub_domains`` is created by migration 014; a nineteenth migration file
    would change the frozen ``schema_migrations`` count, so the schema is
    upgraded lazily and idempotently here instead. The caller owns the commit.
    """
    if not _table_exists(conn, "sub_domains"):
        return
    info = {row[1] for row in conn.execute("PRAGMA table_info(sub_domains)")}
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='sub_domains'"
    ).fetchone()
    table_sql = str(row[0] or "") if row is not None else ""
    if "drain" not in table_sql:
        # SQLite cannot ALTER a CHECK constraint in place, so the table is
        # rebuilt once with the full v5.2 schema; existing rows are preserved.
        conn.execute(
            "CREATE TABLE sub_domains_v52 ("
            " domain_id INTEGER PRIMARY KEY,"
            " rep_id INTEGER NOT NULL REFERENCES representatives(id),"
            " hostname TEXT NOT NULL UNIQUE COLLATE NOCASE,"
            " challenge TEXT NOT NULL,"
            " status TEXT NOT NULL CHECK(status IN('pending','verified','active','drain','disabled')),"
            " role TEXT NOT NULL DEFAULT 'subscription' CHECK(role IN('subscription','node')),"
            " panel_id INTEGER REFERENCES panels(id),"
            " is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN(0,1)),"
            " verified_at INTEGER, tls_checked_at INTEGER, created_at INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO sub_domains_v52("
            " domain_id,rep_id,hostname,challenge,status,verified_at,tls_checked_at,created_at)"
            " SELECT domain_id,rep_id,hostname,challenge,status,verified_at,tls_checked_at,created_at"
            " FROM sub_domains"
        )
        conn.execute("DROP TABLE sub_domains")
        conn.execute("ALTER TABLE sub_domains_v52 RENAME TO sub_domains")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_active_rep_domain "
                     "ON sub_domains(rep_id) WHERE status='active'")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_primary_rep_domain "
                     "ON sub_domains(rep_id) WHERE is_primary=1")
        return
    if "role" not in info:
        conn.execute("ALTER TABLE sub_domains ADD COLUMN role TEXT NOT NULL DEFAULT 'subscription'")
    if "panel_id" not in info:
        conn.execute("ALTER TABLE sub_domains ADD COLUMN panel_id INTEGER")
    if "is_primary" not in info:
        conn.execute("ALTER TABLE sub_domains ADD COLUMN is_primary INTEGER NOT NULL DEFAULT 0")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_primary_rep_domain "
                 "ON sub_domains(rep_id) WHERE is_primary=1")


def register_domain(
    conn: sqlite3.Connection, actor_tg_id: int, rep_id: int, hostname: str,
    *, now: int | None = None, challenge: str | None = None,
    role: str = "subscription", panel_id: int | None = None, is_primary: bool = False,
) -> DomainResult:
    """Create a pending domain and return its random TXT ownership challenge.

    ``role`` is ``subscription`` (a subscription-gateway host) or ``node`` (a
    node/panel-connect host). ``is_primary`` promotes the new domain to the
    representative's primary host, demoting any previous primary in the same
    transaction so the partial unique index never conflicts.
    """
    selected = normalize_hostname(hostname)
    selected_role = str(role or "subscription").strip().lower()
    if selected_role not in {"subscription", "node"}:
        raise DomainError("domain role must be subscription or node")
    if panel_id is not None:
        panel_id = int(panel_id)
        if panel_id <= 0:
            raise DomainError("panel_id must be a positive integer")
    timestamp = int(time.time()) if now is None else int(now)
    ensure_domain_columns(conn)
    with conn:
        rep = _actor(conn, actor_tg_id, rep_id)
        nonce = challenge or secrets.token_urlsafe(32)
        if not isinstance(nonce, str) or not 32 <= len(nonce) <= 128:
            raise DomainError("domain challenge must contain 32..128 characters")
        if is_primary:
            conn.execute(
                "UPDATE sub_domains SET is_primary=0 WHERE rep_id=? AND is_primary=1",
                (int(rep["id"]),),
            )
        try:
            cur = conn.execute(
                "INSERT INTO sub_domains(rep_id,hostname,challenge,status,role,panel_id,is_primary,created_at) "
                "VALUES(?,?,?,'pending',?,?,?,?)",
                (int(rep["id"]), selected, nonce, selected_role, panel_id,
                 1 if is_primary else 0, timestamp),
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
        # The panel this domain was registered for now serves its subscription
        # links from this host; writing it here is what makes the domain live.
        linked_panel = _row_value(row, "panel_id")
        if linked_panel is not None:
            sync_panel_sub_base_url(conn, int(linked_panel))
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
        linked_panel = _row_value(row, "panel_id")
        if linked_panel is not None:
            _release_panel_host(conn, int(linked_panel), str(row["hostname"]))
            sync_panel_sub_base_url(conn, int(linked_panel))
        return DomainResult(int(row["domain_id"]), int(row["rep_id"]), str(row["hostname"]),
                            str(row["challenge"]), "disabled", row["verified_at"], row["tls_checked_at"])


_LIVE_STATUSES = ("active", "drain", "disabled")


def set_domain_status(
    conn: sqlite3.Connection, actor_tg_id: int, domain_id: int, *, status: str,
) -> DomainResult:
    """Move a live domain between active, drain and disabled.

    Mirrors ``disable_domain``: the ownership check runs inside a short
    ``BEGIN IMMEDIATE`` transaction and a compare-and-swap on the prior status
    raises ``DomainError`` when a concurrent writer already changed the row.
    """
    target = str(status or "").strip().lower()
    if target not in _LIVE_STATUSES:
        raise DomainError("domain status must be active, drain or disabled")
    ensure_domain_columns(conn)
    with conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM sub_domains WHERE domain_id=?", (int(domain_id),)).fetchone()
        if row is None:
            raise DomainError("domain does not exist")
        current = str(row["status"])
        if current not in _LIVE_STATUSES:
            raise DomainError("only a live (active/drain/disabled) domain can change state")
        _require_owner(conn, actor_tg_id, int(row["rep_id"]))
        if current == target:
            raise DomainError("domain already has this status")
        changed = conn.execute(
            "UPDATE sub_domains SET status=? WHERE domain_id=? AND status=?",
            (target, int(domain_id), current),
        ).rowcount
        if changed != 1:
            raise DomainError("domain state changed during update")
        linked_panel = _row_value(row, "panel_id")
        if linked_panel is not None:
            if target != "active":
                _release_panel_host(conn, int(linked_panel), str(row["hostname"]))
            sync_panel_sub_base_url(conn, int(linked_panel))
        return DomainResult(int(row["domain_id"]), int(row["rep_id"]), str(row["hostname"]),
                            str(row["challenge"]), target, row["verified_at"], row["tls_checked_at"])


def delete_domain(
    conn: sqlite3.Connection, actor_tg_id: int, domain_id: int,
) -> DomainResult:
    """Hard-delete a domain after an ownership check.

    ``sub_domains`` has no ``deleted_at`` column and its ``status`` CHECK does
    not admit a tombstone value, so a soft delete is not representable; the row
    is removed. The delete follows ``disable_domain``: ownership via
    ``_require_owner``, a short ``BEGIN IMMEDIATE`` transaction, and a
    compare-and-swap on the prior status that raises ``DomainError`` on a lost
    concurrent update.
    """
    ensure_domain_columns(conn)
    with conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM sub_domains WHERE domain_id=?", (int(domain_id),)).fetchone()
        if row is None:
            raise DomainError("domain does not exist")
        _require_owner(conn, actor_tg_id, int(row["rep_id"]))
        changed = conn.execute(
            "DELETE FROM sub_domains WHERE domain_id=? AND status=?",
            (int(domain_id), str(row["status"])),
        ).rowcount
        if changed != 1:
            raise DomainError("domain state changed during deletion")
        linked_panel = _row_value(row, "panel_id")
        if linked_panel is not None:
            _release_panel_host(conn, int(linked_panel), str(row["hostname"]))
            sync_panel_sub_base_url(conn, int(linked_panel))
        return DomainResult(int(row["domain_id"]), int(row["rep_id"]), str(row["hostname"]),
                            str(row["challenge"]), "deleted", row["verified_at"], row["tls_checked_at"])


# --------------------------------------------------------------------------- #
# Domain -> panel subscription link
#
# ``sub_manager`` builds a subscription URL from ``panels.sub_base_url``, but
# nothing connected a verified subscription domain to that column: an operator who
# registered and activated a domain for a panel still had to type the same host
# into the panel's own field, and the two could drift apart. The resolver below is
# the single place that decides which host a panel's subscription links use, and
# the sync writes that decision back into the column ``sub_manager`` already reads.
# --------------------------------------------------------------------------- #

def panel_subscription_base(
    conn: sqlite3.Connection, panel_id: Any, *, panel_row: Any = None,
) -> str:
    """The subscription base URL for one panel.

    Resolution order, highest priority first:

    1. the panel's active primary ``subscription`` domain;
    2. any other active ``subscription`` domain bound to the same panel;
    3. ``panels.sub_base_url`` as the operator typed it;
    4. ``config.SUB_DOMAIN`` as the installation-wide fallback.

    An empty string means no host is configured anywhere, which the callers render
    as «دامنهٔ اشتراک تنظیم نشده» rather than as a broken link.
    """
    if isinstance(panel_id, bool):
        raise DomainError("panel_id must be a positive integer")
    try:
        selected = int(panel_id)
    except (TypeError, ValueError) as exc:
        raise DomainError("panel_id must be a positive integer") from exc
    if selected <= 0:
        raise DomainError("panel_id must be a positive integer")
    if _table_exists(conn, "sub_domains"):
        row = conn.execute(
            "SELECT hostname FROM sub_domains "
            "WHERE panel_id=? AND status='active' AND role='subscription' "
            "ORDER BY is_primary DESC, domain_id LIMIT 1",
            (selected,),
        ).fetchone()
        if row is not None:
            host = str(row[0] or "").strip()
            if host:
                return f"https://{host}"
    panel = panel_row
    if panel is None:
        panel = conn.execute(
            "SELECT sub_base_url FROM panels WHERE id=?", (selected,)).fetchone()
    if panel is not None:
        stored = str(
            panel["sub_base_url"] if hasattr(panel, "keys") else panel[0] or ""
        ).strip()
        if stored:
            return stored
    import config
    fallback = str(getattr(config, "SUB_DOMAIN", "") or "").strip()
    if not fallback:
        return ""
    port = int(getattr(config, "SUB_PORT", 443) or 443)
    port_part = "" if port == 443 else f":{port}"
    return f"https://{fallback}{port_part}"


def sync_panel_sub_base_url(conn: sqlite3.Connection, panel_id: Any) -> str:
    """Write the resolved subscription base back into ``panels.sub_base_url``.

    Returns the value now stored. A panel whose resolved base already matches is
    left untouched, so an unrelated domain edit never bumps the panel revision.
    """
    if isinstance(panel_id, bool):
        raise DomainError("panel_id must be a positive integer")
    try:
        selected = int(panel_id)
    except (TypeError, ValueError) as exc:
        raise DomainError("panel_id must be a positive integer") from exc
    if selected <= 0:
        raise DomainError("panel_id must be a positive integer")
    row = conn.execute(
        "SELECT sub_base_url FROM panels WHERE id=?", (selected,)).fetchone()
    if row is None:
        return ""
    resolved = panel_subscription_base(conn, selected, panel_row=row)
    if str(row[0] or "").strip() == resolved:
        return resolved
    conn.execute(
        "UPDATE panels SET sub_base_url=?, revision=revision+1 WHERE id=?",
        (resolved, selected),
    )
    return resolved


def _row_value(row: Any, key: str, default=None):
    """Read one column from a ``sqlite3.Row`` even when the column is absent."""
    try:
        keys = row.keys()
    except AttributeError:
        return default
    return row[key] if key in keys else default


def _release_panel_host(conn: sqlite3.Connection, panel_id: Any, hostname: str) -> None:
    """Clear a panel column that still points at a domain which is no longer active.

    Without this, disabling or deleting the only active subscription domain would
    leave the panel's own column holding that same host, and the resolver's
    «operator typed it» fallback would keep handing out a link to a host the
    operator just retired.
    """
    if panel_id is None:
        return
    try:
        selected = int(panel_id)
    except (TypeError, ValueError):
        return
    row = conn.execute(
        "SELECT sub_base_url FROM panels WHERE id=?", (selected,)).fetchone()
    if row is None:
        return
    current = str(row[0] or "").strip().rstrip("/").lower()
    if current and current == f"https://{str(hostname).strip()}".rstrip("/").lower():
        conn.execute(
            "UPDATE panels SET sub_base_url='', revision=revision+1 WHERE id=?",
            (selected,),
        )


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


def set_primary_domain(
    conn: sqlite3.Connection, actor_tg_id: int, domain_id: int,
) -> DomainResult:
    """Promote one live domain to its representative's primary host.

    ``register_domain`` could set ``is_primary`` only at creation time, so an
    operator who later wanted a different host to front the panel had to delete and
    re-register it. The promotion demotes the previous primary in the same
    transaction — the partial unique index ``ux_primary_rep_domain`` would otherwise
    reject the write — and then re-syncs the linked panel's subscription base.
    """
    ensure_domain_columns(conn)
    with conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM sub_domains WHERE domain_id=?", (int(domain_id),)
        ).fetchone()
        if row is None:
            raise DomainError("domain does not exist")
        if str(row["status"]) != "active":
            raise DomainError("only an active domain can become the primary host")
        _require_owner(conn, actor_tg_id, int(row["rep_id"]))
        if int(_row_value(row, "is_primary", 0) or 0) == 1:
            return DomainResult(int(row["domain_id"]), int(row["rep_id"]),
                                str(row["hostname"]), str(row["challenge"]), "active",
                                row["verified_at"], row["tls_checked_at"])
        conn.execute(
            "UPDATE sub_domains SET is_primary=0 WHERE rep_id=? AND is_primary=1",
            (int(row["rep_id"]),),
        )
        conn.execute(
            "UPDATE sub_domains SET is_primary=1 WHERE domain_id=?", (int(domain_id),))
        linked_panel = _row_value(row, "panel_id")
        if linked_panel is not None:
            sync_panel_sub_base_url(conn, int(linked_panel))
        return DomainResult(int(row["domain_id"]), int(row["rep_id"]),
                            str(row["hostname"]), str(row["challenge"]), "active",
                            row["verified_at"], row["tls_checked_at"])


__all__ = ["DomainError", "DomainPermissionError", "DomainProofError", "DomainResult",
           "normalize_hostname", "ensure_domain_columns", "register_domain", "verify_domain",
           "activate_domain", "disable_domain", "set_domain_status", "delete_domain",
           "set_primary_domain", "panel_subscription_base", "sync_panel_sub_base_url",
           "resolve_profile_host", "active_domain_rep"]
