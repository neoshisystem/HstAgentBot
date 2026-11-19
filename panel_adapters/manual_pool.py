"""Manual-pool adapter and CAS-safe repository for offline clients."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .base import AdapterUnavailable, BasePanelAdapter, CanonicalServerStatus, LookupResult, MutationResult, PanelCapabilities, ReconcileResult
from .models import CanonicalClient, ClientCreateSpec, NodeConfig


class ManualPoolError(RuntimeError): pass
class ManualPoolEmpty(ManualPoolError): pass
class ManualPoolConflict(ManualPoolError): pass

_ALLOWED_SCHEMES = {"vless", "vmess", "trojan", "ss", "shadowsocks", "hysteria", "hy2", "mtproto"}


def validate_manual_configs(config_uris: Any) -> tuple[str, ...]:
    if not isinstance(config_uris, (list, tuple)) or not config_uris:
        raise ValueError("at least one manual config URI is required")
    out = []
    for uri in config_uris:
        if not isinstance(uri, str) or not uri.strip():
            raise ValueError("manual config URI must be non-empty")
        if any(ord(c) < 32 or ord(c) == 127 for c in uri):
            raise ValueError("manual config URI contains control characters")
        parsed = urlsplit(uri)
        scheme = parsed.scheme.lower()
        if scheme not in _ALLOWED_SCHEMES or not parsed.hostname:
            raise ValueError("manual config URI has unsupported scheme or missing host")
        if scheme != "mtproto" and parsed.username is None:
            raise ValueError("manual config URI is missing a credential identity")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("manual config URI port is invalid")
        out.append(uri)
    return tuple(out)


def _positive(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(name + " must be a positive integer")
    return value


class ManualPoolRepository:
    def __init__(self, db_path: str | Path, *, timeout: float = 10.0):
        if not str(db_path) or float(timeout) <= 0:
            raise ValueError("valid db_path and positive timeout are required")
        self.db_path, self.timeout = str(db_path), float(timeout)

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=self.timeout, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    @staticmethod
    def _row(row):
        return dict(row) if row is not None else None

    def import_client(self, *, panel_id: int, external_identifier: str, config_cipher: str,
                      config_uris: Any, actor_is_root: bool, quota_mode: str = "unknown",
                      quota_bytes: int | None = None, used_bytes: int | None = None,
                      time_mode: str = "unknown", expires_at: int | None = None,
                      enabled: bool = True, created_at: int | None = None) -> dict[str, Any]:
        if actor_is_root is not True:
            raise PermissionError("manual pool import requires root/operator authority")
        panel_id = _positive(panel_id, "panel_id")
        external_identifier = str(external_identifier or "").strip()
        if not external_identifier:
            raise ValueError("external_identifier is required")
        validate_manual_configs(config_uris)
        if not isinstance(config_cipher, str) or not config_cipher.strip():
            raise ValueError("encrypted config_cipher is required")
        if quota_mode not in {"finite", "unlimited", "unknown"}:
            raise ValueError("invalid quota_mode")
        if time_mode not in {"dated", "unlimited", "on_hold", "unknown"}:
            raise ValueError("invalid time_mode")
        if quota_mode == "finite":
            quota_bytes = _positive(quota_bytes, "quota_bytes")
        else:
            quota_bytes = None
        if used_bytes is not None:
            used_bytes = _positive(used_bytes, "used_bytes")
        if time_mode == "dated":
            expires_at = _positive(expires_at, "expires_at")
        else:
            expires_at = None
        created_at = int(time.time()) if created_at is None else _positive(created_at, "created_at")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                """INSERT INTO manual_pool_clients
                   (panel_id,external_identifier,account_id,status,config_cipher,
                    quota_mode,quota_bytes,used_bytes,time_mode,expires_at,enabled,
                    allocation_key,revision,created_at)
                   VALUES(?,?,NULL,'available',?,?,?,?,?,?,?,NULL,1,?)""",
                (panel_id, external_identifier, config_cipher, quota_mode, quota_bytes,
                 used_bytes, time_mode, expires_at, int(enabled), created_at),
            )
            row = conn.execute("SELECT * FROM manual_pool_clients WHERE id=?", (cur.lastrowid,)).fetchone()
            conn.commit()
            return self._row(row)
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise ManualPoolConflict("manual pool identifier already exists") from exc
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def claim_client(self, *, panel_id: int, operation_id: str, account_id: int) -> dict[str, Any]:
        panel_id, account_id = _positive(panel_id, "panel_id"), _positive(account_id, "account_id")
        operation_id = str(operation_id or "").strip()
        if not operation_id:
            raise ValueError("operation_id is required")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            prior = conn.execute("SELECT * FROM manual_pool_clients WHERE allocation_key=?", (operation_id,)).fetchone()
            if prior is not None:
                if prior["panel_id"] != panel_id or prior["account_id"] != account_id:
                    raise ManualPoolConflict("operation_id is already bound to another allocation")
                conn.commit()
                return self._row(prior)
            cur = conn.execute(
                """UPDATE manual_pool_clients
                   SET status='allocated', allocation_key=?, account_id=?, revision=revision+1
                   WHERE id=(SELECT id FROM manual_pool_clients
                             WHERE panel_id=? AND status='available'
                             ORDER BY id LIMIT 1)
                     AND status='available'""",
                (operation_id, account_id, panel_id),
            )
            if cur.rowcount != 1:
                raise ManualPoolEmpty("no available manual client exists")
            row = conn.execute("SELECT * FROM manual_pool_clients WHERE allocation_key=?", (operation_id,)).fetchone()
            conn.commit()
            return self._row(row)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def get_client(self, *, panel_id: int, identifier: str) -> dict[str, Any] | None:
        panel_id = _positive(panel_id, "panel_id")
        identifier = str(identifier or "").strip()
        if not identifier:
            raise ValueError("identifier is required")
        conn = self._connect()
        try:
            row = conn.execute(
                """SELECT * FROM manual_pool_clients
                   WHERE panel_id=? AND (external_identifier=? OR CAST(account_id AS TEXT)=?)
                   ORDER BY CASE WHEN account_id IS NULL THEN 1 ELSE 0 END, id LIMIT 1""",
                (panel_id, identifier, identifier),
            ).fetchone()
            return self._row(row)
        finally:
            conn.close()

    def list_clients(self, *, panel_id: int, allocated_only: bool = True) -> list[dict[str, Any]]:
        panel_id = _positive(panel_id, "panel_id")
        conn = self._connect()
        try:
            clause = " AND status='allocated'" if allocated_only else ""
            rows = conn.execute("SELECT * FROM manual_pool_clients WHERE panel_id=?" + clause + " ORDER BY id", (panel_id,)).fetchall()
            return [self._row(row) for row in rows]
        finally:
            conn.close()


class ManualPoolAdapter(BasePanelAdapter):
    ADAPTER_REVISION = "manual-pool-v1"
    _PROTOCOLS = _ALLOWED_SCHEMES

    def __init__(self, panel: Mapping[str, Any], repository: ManualPoolRepository,
                 *, decryptor: Callable[[str], str] | None = None,
                 account_id_resolver: Callable[[int, str], int | None] | None = None,
                 clock: Callable[[], float] = time.time):
        if not isinstance(panel, Mapping) or not isinstance(repository, ManualPoolRepository):
            raise TypeError("panel mapping and ManualPoolRepository are required")
        self.panel_id = _positive(int(panel.get("id") or panel.get("panel_id") or 0), "panel_id")
        self.repository, self.decryptor = repository, decryptor
        self.account_id_resolver, self.clock = account_id_resolver, clock
        super().__init__(PanelCapabilities(frozenset({
            "verify_connection", "get_client", "list_clients", "get_client_configs", "claim_existing",
        })))

    def _canonical(self, row: Mapping[str, Any]) -> CanonicalClient:
        account_id = row.get("account_id")
        if account_id is None and self.account_id_resolver:
            account_id = self.account_id_resolver(self.panel_id, str(row["external_identifier"]))
        if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
            raise AdapterUnavailable("manual client has no canonical account mapping")
        return CanonicalClient(
            ref=(account_id, self.panel_id, str(row["external_identifier"])),
            enabled=bool(row["enabled"]), quota_mode=row["quota_mode"],
            quota_bytes=row["quota_bytes"], used_bytes=row["used_bytes"],
            time_mode=row["time_mode"], expires_at=row["expires_at"],
            first_connected_at=None, last_connected_at=None, client_app=None,
            subscription_id=None, credential_revision=None, observed_at=int(self.clock()),
            adapter_revision=self.ADAPTER_REVISION,
        )

    async def verify_connection(self) -> CanonicalServerStatus:
        return CanonicalServerStatus(False, "manual-pool", None, None, None, None, int(self.clock()))

    async def get_client(self, identifier: str) -> LookupResult:
        try:
            row = self.repository.get_client(panel_id=self.panel_id, identifier=identifier)
            if row is None or row["status"] not in {"available", "allocated"}:
                return LookupResult("not_found", evidence={"source": "manual_pool"})
            if row["status"] != "allocated":
                return LookupResult("unavailable", evidence={"reason": "manual_client_not_allocated"})
            return LookupResult("found", self._canonical(row), {"source": "manual_pool"})
        except AdapterUnavailable:
            return LookupResult("unavailable", evidence={"reason": "canonical_account_mapping_missing"})
        except Exception:
            return LookupResult("unavailable", evidence={"reason": "manual_repository_unavailable"})

    async def list_clients(self, scope_filter: str | None = None) -> list[CanonicalClient]:
        try:
            rows = self.repository.list_clients(panel_id=self.panel_id)
            clients = [self._canonical(row) for row in rows]
            return [c for c in clients if not scope_filter or scope_filter.casefold() in c.ref[2].casefold()]
        except Exception as exc:
            if isinstance(exc, AdapterUnavailable): raise
            raise AdapterUnavailable("manual client inventory unavailable") from None

    async def get_client_configs(self, identifier: str) -> list[NodeConfig]:
        row = self.repository.get_client(panel_id=self.panel_id, identifier=identifier)
        if row is None or row["status"] != "allocated":
            raise AdapterUnavailable("manual client is not allocated")
        if self.decryptor is None:
            raise AdapterUnavailable("manual config decryptor is not configured")
        try:
            values = json.loads(self.decryptor(row["config_cipher"]))
        except Exception:
            raise AdapterUnavailable("manual config could not be safely decoded") from None
        if not isinstance(values, list) or not values:
            raise AdapterUnavailable("manual config payload is invalid")
        out = []
        for index, uri in enumerate(values):
            if not isinstance(uri, str): raise AdapterUnavailable("manual config URI is invalid")
            parsed = urlsplit(uri)
            if parsed.scheme.lower() not in self._PROTOCOLS or not parsed.hostname:
                raise AdapterUnavailable("manual config URI is invalid")
            label = parsed.fragment or parsed.scheme.lower() + "-" + str(index + 1)
            out.append(NodeConfig("manual-" + str(row["id"]) + "-" + str(index), label, parsed.scheme.lower(), uri))
        return out

    def claim_existing(self, operation_id: str, account_id: int) -> LookupResult:
        try:
            row = self.repository.claim_client(panel_id=self.panel_id, operation_id=operation_id, account_id=account_id)
            return LookupResult("found", self._canonical(row), {"source": "manual_pool", "allocation_id": row["id"], "revision": row["revision"]})
        except Exception:
            return LookupResult("unavailable", evidence={"reason": "manual_pool_claim_not_confirmed"})

    @staticmethod
    def _unsupported(name: str) -> MutationResult:
        return MutationResult("definite_failure", {"reason": "unsupported_manual_pool_operation:" + name, "network_call_made": False})

    async def create_client(self, spec: ClientCreateSpec) -> MutationResult: return self._unsupported("create_client")
    async def renew_client(self, identifier: str, target: dict, operation_id: str) -> MutationResult: return self._unsupported("renew_client")
    async def add_addon(self, identifier: str, target: dict, operation_id: str) -> MutationResult: return self._unsupported("add_addon")
    async def rotate_subscription_url(self, identifier: str, candidate: str, operation_id: str) -> MutationResult: return self._unsupported("rotate_subscription_url")
    async def rotate_connection_credentials(self, identifier: str, candidate: dict, operation_id: str) -> MutationResult: return self._unsupported("rotate_connection_credentials")
    async def disconnect_existing_sessions(self, identifier: str, operation_id: str) -> MutationResult: return self._unsupported("disconnect_existing_sessions")
    async def set_client_enabled(self, identifier: str, enabled: bool, operation_id: str) -> MutationResult: return self._unsupported("set_client_enabled")
    async def set_traffic(self, identifier: str, mode: str, value: int | None, operation_id: str) -> MutationResult: return self._unsupported("set_traffic")
    async def set_expiry(self, identifier: str, expires_at: int | None, operation_id: str) -> MutationResult: return self._unsupported("set_expiry")
    async def delete_client(self, identifier: str, operation_id: str) -> MutationResult: return self._unsupported("delete_client")
    async def reconcile(self, operation: dict) -> ReconcileResult:
        return ReconcileResult("still_unknown", {"reason": "manual_pool_has_no_remote_effect_to_reconcile"})
