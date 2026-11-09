"""3X-UI central-client adapter. Network I/O is injected and always offline-testable."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
import inspect
import json
import time
from typing import Any, Callable, Mapping
from urllib.parse import quote, unquote, urlsplit

from .base import (
    AdapterUnavailable, BasePanelAdapter, CanonicalServerStatus, LookupResult,
    MutationResult, PanelCapabilities, ReconcileResult,
)
from .models import (
    CanonicalClient, ClientCreateSpec, NodeConfig, expiry_seconds_to_panel_ms,
    normalize_expiry, normalize_volume,
)
from .url_parser import parse_panel_url


@dataclass(frozen=True, slots=True)
class _HttpResult:
    status_code: int
    payload: Any = field(repr=False, compare=False)


class XUIOpenAPIAdapter(BasePanelAdapter):
    """Client API adapter; it never logs raw responses, Bearer tokens or URIs.

    panel is the decoded panel row/mapping with raw_url and auth_cipher.
    The auth_cipher is opened with crypto_store's approved columns/v1 domain.
    transaction_probe, when supplied by the DB integration, must return True
    while a write transaction is active; requests are refused in that state.
    """

    ADAPTER_REVISION = "xui-openapi-v1"
    _EDITABLE_FIELDS = frozenset({
        "email", "subId", "id", "uuid", "password", "auth", "flow",
        "totalGB", "expiryTime", "limitIp", "limitHwid", "tgId", "comment",
        "enable", "secret", "adTag", "privateKey", "publicKey",
        "preSharedKey", "allowedIPs", "keepAlive", "group", "groupName",
        "reset", "resetDay", "resetWeekday", "resetMax", "autoRenew",
        "autoRenewInterval", "autoRenewOnDate",
    })
    _CREDENTIAL_ALIASES = {"uuid": "id"}
    _CREDENTIAL_FIELDS = frozenset({
        "id", "uuid", "password", "auth", "flow", "secret", "adTag",
        "privateKey", "publicKey", "preSharedKey", "allowedIPs", "keepAlive",
    })
    _SUPPORTED_PROTOCOLS = frozenset({
        "vless", "vmess", "trojan", "ss", "shadowsocks", "hysteria", "hy2",
    })

    def __init__(
        self,
        panel: Mapping[str, Any],
        session: Any,
        *,
        timeout: float | tuple[float, float] = 10.0,
        decryptor: Callable[[str, str], str] | None = None,
        transaction_probe: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.time,
        account_id_resolver: Callable[[int, str], int | None] | None = None,
        operation_preflight: Callable[[str, Any, str], bool] | None = None,
    ):
        if not isinstance(panel, Mapping):
            raise TypeError("panel must be a mapping")
        if session is None or not callable(getattr(session, "request", None)):
            raise TypeError("an injected session with request() is required")
        if isinstance(timeout, (int, float)):
            if timeout <= 0:
                raise ValueError("timeout must be positive")
        elif (len(timeout) != 2 or any(float(item) <= 0 for item in timeout)):
            raise ValueError("timeout tuple must contain positive connect/read values")

        raw_url = panel.get("raw_url") or panel.get("base_url") or panel.get("url")
        self.endpoint = parse_panel_url(raw_url)
        cipher = panel.get("auth_cipher") or panel.get("api_token_cipher")
        if not isinstance(cipher, str) or not cipher:
            raise ValueError("panel bearer credential is missing")
        if decryptor is None:
            from crypto_store import decrypt_secret
            decryptor = decrypt_secret
        try:
            bearer = decryptor(cipher, "hstagent/columns/v1")
        except Exception:
            raise ValueError("panel bearer credential could not be decrypted") from None
        if not isinstance(bearer, str) or not bearer.strip():
            raise ValueError("panel bearer credential is invalid")

        self.panel_id = int(panel.get("id") or panel.get("panel_id") or 0)
        if self.panel_id <= 0:
            raise ValueError("panel id must be a positive integer")
        self._account_id_resolver = account_id_resolver
        self._operation_preflight = operation_preflight
        self._session = session
        self._bearer = bearer.strip()
        self._timeout = timeout
        self._transaction_probe = transaction_probe
        self._clock = clock

        verified = {
            "verify_connection", "get_client", "list_clients", "get_client_configs",
            "rotate_subscription_url", "rotate_connection_credentials",
            "set_traffic", "set_expiry",
        }
        # EV-03 is deliberately absent until the installed panel/Xray build has
        # demonstrated termination of existing live sessions.
        super().__init__(PanelCapabilities(frozenset(verified)))

    def __repr__(self) -> str:
        return (
            f"XUIOpenAPIAdapter(panel_id={self.panel_id!r}, "
            f"endpoint={self.endpoint.normalized_url!r}, bearer=<redacted>, "
            f"capabilities={self.capabilities.methods!r})"
        )

    async def _request(
        self,
        method: str,
        api_path: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any = None,
    ) -> _HttpResult:
        """Send exactly one request, with TLS and redirects fail-closed."""
        if self._transaction_probe is None:
            raise AdapterUnavailable("transaction-state probe is required before panel I/O")
        try:
            in_transaction = bool(self._transaction_probe())
        except Exception:
            raise AdapterUnavailable("database transaction state is unavailable") from None
        if in_transaction:
            raise AdapterUnavailable("panel I/O refused while a DB transaction is active")
        if not isinstance(api_path, str) or not api_path or api_path.startswith("/"):
            raise ValueError("API path must be relative to panel API base")
        if "?" in api_path or "#" in api_path or ".." in api_path.split("/"):
            raise ValueError("API path is invalid")
        url = self.endpoint.url_for(api_path)
        kwargs = {
            "headers": {
                "Accept": "application/json",
                "Authorization": f"Bearer {self._bearer}",
            },
            "timeout": self._timeout,
            "allow_redirects": False,
            "verify": True,
        }
        if params is not None:
            kwargs["params"] = dict(params)
        if json_body is not None:
            kwargs["json"] = json_body
        request = self._session.request
        try:
            if inspect.iscoroutinefunction(request):
                response = await request(method.upper(), url, **kwargs)
            else:
                response = await asyncio.to_thread(request, method.upper(), url, **kwargs)
                if inspect.isawaitable(response):
                    response = await response
            status = int(getattr(response, "status_code"))
            try:
                payload = response.json()
            except Exception:
                payload = None
            return _HttpResult(status, payload)
        except AdapterUnavailable:
            raise
        except Exception:
            # Do not chain the raw exception: request exceptions can contain URL,
            # headers or other credentials depending on the HTTP implementation.
            raise AdapterUnavailable("panel transport failed or timed out") from None

    @staticmethod
    def _api_ok(result: _HttpResult) -> bool:
        return (
            200 <= result.status_code < 300
            and isinstance(result.payload, Mapping)
            and result.payload.get("success") is True
        )

    @staticmethod
    def _explicit_failure(result: _HttpResult) -> bool:
        return (
            400 <= result.status_code < 500
            and result.status_code not in {408, 429}
            and isinstance(result.payload, Mapping)
            and result.payload.get("success") is False
        )

    @staticmethod
    def _obj(result: _HttpResult) -> Any:
        if not XUIOpenAPIAdapter._api_ok(result):
            raise AdapterUnavailable("panel returned a non-success or malformed response")
        return result.payload.get("obj")

    @staticmethod
    def _ms_to_seconds(value: Any) -> int | None:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            return None
        return value // 1000

    @staticmethod
    def _credential_revision(wire: Mapping[str, Any]) -> str | None:
        fields = {
            key: wire[key] for key in sorted(XUIOpenAPIAdapter._CREDENTIAL_FIELDS)
            if key in wire
        }
        if not fields:
            return None
        try:
            encoded = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        except (TypeError, ValueError):
            return None
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    def _canonical(self, wire: Mapping[str, Any]) -> CanonicalClient:
        email = wire.get("email")
        if not isinstance(email, str) or not email:
            raise AdapterUnavailable("client response has no valid email")
        enabled = wire.get("enable") if isinstance(wire.get("enable"), bool) else None
        # The current 3x-ui Clients API defines totalGB as bytes and zero as
        # unlimited; that wire contract is normalized here, never in core.
        quota_mode, quota_bytes = normalize_volume(wire.get("totalGB"), zero_is_unlimited=True)
        expiry_raw = wire.get("expiryTime")
        time_mode, expires_at = normalize_expiry(expiry_raw, zero_is_unlimited=True)
        traffic = wire.get("traffic")
        used = None
        if isinstance(traffic, Mapping):
            up, down = traffic.get("up"), traffic.get("down")
            if (isinstance(up, int) and not isinstance(up, bool) and up >= 0
                    and isinstance(down, int) and not isinstance(down, bool) and down >= 0):
                used = up + down
        first = self._ms_to_seconds(wire.get("firstConnectedAt"))
        last = self._ms_to_seconds(wire.get("lastOnline"))
        app = wire.get("clientApp") if isinstance(wire.get("clientApp"), str) else None
        sub_id = wire.get("subId") if isinstance(wire.get("subId"), str) else None
        if self._account_id_resolver is None:
            raise AdapterUnavailable("canonical account_id mapping is required")
        try:
            account_id = self._account_id_resolver(self.panel_id, email)
        except Exception:
            raise AdapterUnavailable("canonical account_id mapping failed") from None
        if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
            raise AdapterUnavailable("canonical account_id mapping is unavailable")
        return CanonicalClient(
            ref=(account_id, self.panel_id, email),
            enabled=enabled,
            quota_mode=quota_mode,
            quota_bytes=quota_bytes,
            used_bytes=used,
            time_mode=time_mode,
            expires_at=expires_at,
            first_connected_at=first,
            last_connected_at=last,
            client_app=app,
            subscription_id=sub_id,
            credential_revision=self._credential_revision(wire),
            observed_at=int(self._clock()),
            adapter_revision=self.ADAPTER_REVISION,
        )

    async def _get_wire_client(self, email: str) -> tuple[str, Mapping[str, Any] | None, _HttpResult | None]:
        try:
            result = await self._request("GET", f"clients/get/{quote(email, safe='')}")
        except AdapterUnavailable:
            return "unavailable", None, None
        if result.status_code == 404:
            return "not_found", None, result
        if result.status_code >= 500 or result.status_code < 200 or result.status_code >= 300:
            return "unavailable", None, result
        if not self._api_ok(result):
            return "unavailable", None, result
        obj = result.payload.get("obj")
        if not isinstance(obj, Mapping) or obj.get("email") != email:
            return "unavailable", None, result
        return "found", obj, result

    async def verify_connection(self) -> CanonicalServerStatus:
        try:
            result = await self._request("GET", "server/status")
            obj = self._obj(result)
            if not isinstance(obj, Mapping):
                raise AdapterUnavailable("server status payload is malformed")
            cpu = obj.get("cpu")
            cpu_value = float(cpu) if isinstance(cpu, (int, float)) and not isinstance(cpu, bool) else None
            if cpu_value is not None and (cpu_value < 0 or cpu_value > 100):
                cpu_value = None
            mem = obj.get("mem") if isinstance(obj.get("mem"), Mapping) else {}
            ram_used = mem.get("current") if isinstance(mem.get("current"), int) and not isinstance(mem.get("current"), bool) and mem.get("current") >= 0 else None
            ram_total = mem.get("total") if isinstance(mem.get("total"), int) and not isinstance(mem.get("total"), bool) and mem.get("total") >= 0 else None
            xray = obj.get("xray") if isinstance(obj.get("xray"), Mapping) else {}
            state = xray.get("state")
            return CanonicalServerStatus(
                reachable=True,
                panel_version=(obj.get("panelVersion") if isinstance(obj.get("panelVersion"), str) else None),
                core_running=(state == "running") if isinstance(state, str) else None,
                cpu_percent=cpu_value,
                ram_used_bytes=ram_used,
                ram_total_bytes=ram_total,
                observed_at=int(self._clock()),
            )
        except AdapterUnavailable:
            raise
        except Exception:
            raise AdapterUnavailable("server status could not be normalized") from None

    async def get_client(self, identifier: str) -> LookupResult:
        if not isinstance(identifier, str) or not identifier:
            return LookupResult("unavailable", evidence={"reason": "invalid_identifier"})
        state, wire, result = await self._get_wire_client(identifier)
        if state != "found":
            return LookupResult(state, evidence={"http_status": result.status_code if result else None})
        try:
            client = self._canonical(wire)
        except AdapterUnavailable:
            return LookupResult("unavailable", evidence={"reason": "malformed_client"})
        return LookupResult("found", client, {"http_status": 200, "panel_id": self.panel_id})

    async def list_clients(self, scope_filter: str | None = None) -> list[CanonicalClient]:
        try:
            result = await self._request("GET", "clients/list")
            obj = self._obj(result)
            if not isinstance(obj, list):
                raise AdapterUnavailable("clients/list returned a malformed collection")
            clients = [self._canonical(row) for row in obj if isinstance(row, Mapping)]
            if len(clients) != len(obj):
                raise AdapterUnavailable("clients/list contains malformed client rows")
            if scope_filter:
                clients = [c for c in clients if scope_filter.casefold() in c.ref[2].casefold()]
            return clients
        except AdapterUnavailable:
            raise
        except Exception:
            raise AdapterUnavailable("client list could not be normalized") from None

    async def get_client_configs(self, identifier: str) -> list[NodeConfig]:
        try:
            result = await self._request("GET", f"clients/links/{quote(identifier, safe='')}")
            obj = self._obj(result)
            if not isinstance(obj, list):
                raise AdapterUnavailable("clients/links returned a malformed collection")
            configs: list[NodeConfig] = []
            for index, raw_uri in enumerate(obj):
                if not isinstance(raw_uri, str):
                    raise AdapterUnavailable("client config entry is malformed")
                parsed = urlsplit(raw_uri)
                protocol = parsed.scheme.lower()
                if protocol not in self._SUPPORTED_PROTOCOLS:
                    raise AdapterUnavailable("panel returned an unsupported config protocol")
                label = unquote(parsed.fragment) if parsed.fragment else f"{protocol}-{index + 1}"
                node_key = hashlib.sha256(raw_uri.encode("utf-8")).hexdigest()[:16]
                configs.append(NodeConfig(node_key, label, protocol, raw_uri))
            return configs
        except AdapterUnavailable:
            raise
        except Exception:
            raise AdapterUnavailable("client configs could not be normalized") from None

    def _editable_payload(self, wire: Mapping[str, Any]) -> dict[str, Any]:
        payload = {key: deepcopy(wire[key]) for key in self._EDITABLE_FIELDS if key in wire}
        # The central API response may expose uuid; its update input is id.
        if "id" not in payload and isinstance(payload.get("uuid"), str):
            payload["id"] = payload["uuid"]
        payload.pop("uuid", None)
        if not isinstance(payload.get("email"), str):
            raise AdapterUnavailable("client snapshot lacks email")
        return payload

    async def _post_full_client(self, email: str, payload: dict[str, Any]) -> _HttpResult:
        return await self._request("POST", f"clients/update/{quote(email, safe='')}", json_body=payload)

    async def rotate_subscription_url(self, identifier: str, candidate: str, operation_id: str) -> MutationResult:
        if not isinstance(candidate, str) or not candidate or not isinstance(operation_id, str) or not operation_id:
            return MutationResult("definite_failure", {"reason": "invalid_candidate_or_operation"})
        if self._operation_preflight is None:
            return MutationResult("definite_failure", {"reason": "encrypted_candidate_preflight_not_configured", "network_call_made": False})
        try:
            prepared = bool(self._operation_preflight(operation_id, candidate, "subscription_id"))
        except Exception:
            prepared = False
        if not prepared:
            return MutationResult("definite_failure", {"reason": "encrypted_candidate_not_confirmed", "network_call_made": False})
        state, wire, _ = await self._get_wire_client(identifier)
        if state != "found":
            return MutationResult("unknown", {"stage": "pre_read", "lookup": state})
        try:
            old = self._canonical(wire)
            payload = self._editable_payload(wire)
            payload["subId"] = candidate
            sent = await self._post_full_client(identifier, payload)
        except AdapterUnavailable:
            return MutationResult("unknown", {"stage": "write_or_preflight"})
        if not self._api_ok(sent):
            state = "definite_failure" if self._explicit_failure(sent) else "unknown"
            return MutationResult(state, {"stage": "write", "http_status": sent.status_code})
        new_state, new_wire, read = await self._get_wire_client(identifier)
        if new_state != "found" or new_wire is None:
            return MutationResult("unknown", {"stage": "readback", "lookup": new_state})
        try:
            current = self._canonical(new_wire)
        except AdapterUnavailable:
            return MutationResult("unknown", {"stage": "readback_malformed"})
        matches = (
            current.subscription_id == candidate
            and current.enabled == old.enabled
            and current.quota_mode == old.quota_mode
            and current.quota_bytes == old.quota_bytes
            and current.time_mode == old.time_mode
            and current.expires_at == old.expires_at
            and current.used_bytes == old.used_bytes
        )
        return MutationResult(
            "success" if matches else "unknown",
            {"stage": "readback", "matches": matches, "http_status": read.status_code if read else None,
             "candidate_sha256": hashlib.sha256(candidate.encode()).hexdigest()[:16]},
            {"subscription_id": "rotated", "enable_and_quota_preserved": matches},
        )

    def _normalize_credential_candidate(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        if not isinstance(candidate, Mapping) or not candidate:
            raise ValueError("credential candidate must be a non-empty mapping")
        snapshot = deepcopy(dict(candidate))
        normalized: dict[str, Any] = {}
        for key, value in snapshot.items():
            if key not in self._CREDENTIAL_FIELDS:
                raise ValueError("credential candidate contains an unsupported field")
            target = self._CREDENTIAL_ALIASES.get(key, key)
            if target in normalized:
                raise ValueError("credential aliases collide")
            if not isinstance(value, (str, int, bool)) or value == "":
                raise ValueError("credential value is invalid")
            normalized[target] = value
        return normalized

    @staticmethod
    def _load_settings(inbound: Mapping[str, Any]) -> Mapping[str, Any] | None:
        settings = inbound.get("settings")
        if isinstance(settings, str):
            try:
                settings = json.loads(settings)
            except (TypeError, ValueError):
                return None
        return settings if isinstance(settings, Mapping) else None

    async def _verify_inbound_credentials(
        self, email: str, inbound_ids: list[int], candidate: Mapping[str, Any]
    ) -> bool:
        if not inbound_ids:
            return False
        try:
            result = await self._request("GET", "inbounds/list")
            rows = self._obj(result)
        except AdapterUnavailable:
            return False
        if not isinstance(rows, list):
            return False
        by_id = {row.get("id"): row for row in rows if isinstance(row, Mapping) and isinstance(row.get("id"), int)}
        if any(inbound_id not in by_id for inbound_id in inbound_ids):
            return False
        for inbound_id in inbound_ids:
            inbound = by_id[inbound_id]
            settings = self._load_settings(inbound)
            clients = settings.get("clients") if settings else None
            if not isinstance(clients, list):
                return False
            matched = [c for c in clients if isinstance(c, Mapping) and c.get("email") == email]
            if len(matched) != 1:
                return False
            client = matched[0]
            for field_name, expected in candidate.items():
                aliases = (field_name, "uuid") if field_name == "id" else (field_name,)
                if not any(alias in client and client.get(alias) == expected for alias in aliases):
                    return False
        return True

    async def rotate_connection_credentials(self, identifier: str, candidate: dict, operation_id: str) -> MutationResult:
        if not isinstance(operation_id, str) or not operation_id:
            return MutationResult("definite_failure", {"reason": "invalid_operation_id"})
        try:
            # Freeze the exact candidate once, before persistence and network.
            stable_candidate = self._normalize_credential_candidate(candidate)
        except (TypeError, ValueError):
            return MutationResult("definite_failure", {"reason": "invalid_credential_candidate"})
        if self._operation_preflight is None:
            return MutationResult("definite_failure", {"reason": "encrypted_candidate_preflight_not_configured", "network_call_made": False})
        try:
            prepared = bool(self._operation_preflight(operation_id, stable_candidate, "connection_credentials"))
        except Exception:
            prepared = False
        if not prepared:
            return MutationResult("definite_failure", {"reason": "encrypted_candidate_not_confirmed", "network_call_made": False})
        state, wire, _ = await self._get_wire_client(identifier)
        if state != "found" or wire is None:
            return MutationResult("unknown", {"stage": "pre_read", "lookup": state})
        inbound_ids = wire.get("inboundIds")
        if (not isinstance(inbound_ids, list) or not inbound_ids
                or any(isinstance(i, bool) or not isinstance(i, int) or i <= 0 for i in inbound_ids)):
            return MutationResult("unknown", {"stage": "pre_read", "reason": "inbound_scope_unavailable"})
        try:
            payload = self._editable_payload(wire)
            payload.update(stable_candidate)
            sent = await self._post_full_client(identifier, payload)
        except AdapterUnavailable:
            return MutationResult("unknown", {"stage": "write_or_preflight"})
        if not self._api_ok(sent):
            state = "definite_failure" if self._explicit_failure(sent) else "unknown"
            return MutationResult(state, {"stage": "write", "http_status": sent.status_code})
        read_state, read_wire, _ = await self._get_wire_client(identifier)
        if read_state != "found" or read_wire is None:
            return MutationResult("unknown", {"stage": "client_readback", "lookup": read_state})
        central_match = all(
            read_wire.get(field_name) == expected
            or (field_name == "id" and read_wire.get("uuid") == expected)
            for field_name, expected in stable_candidate.items()
        )
        try:
            before_canonical = self._canonical(wire)
            after_canonical = self._canonical(read_wire)
        except AdapterUnavailable:
            return MutationResult("unknown", {"stage": "client_readback_malformed"})
        same_inbounds = read_wire.get("inboundIds") == inbound_ids
        unrelated_preserved = (
            after_canonical.enabled == before_canonical.enabled
            and after_canonical.subscription_id == before_canonical.subscription_id
            and after_canonical.quota_mode == before_canonical.quota_mode
            and after_canonical.quota_bytes == before_canonical.quota_bytes
            and after_canonical.time_mode == before_canonical.time_mode
            and after_canonical.expires_at == before_canonical.expires_at
            and after_canonical.used_bytes == before_canonical.used_bytes
            and same_inbounds
        )
        if not central_match or not unrelated_preserved:
            return MutationResult("unknown", {"stage": "client_readback", "credentials_match": central_match, "unrelated_fields_preserved": unrelated_preserved})
        inbound_match = await self._verify_inbound_credentials(identifier, inbound_ids, stable_candidate)
        return MutationResult(
            "success" if inbound_match else "unknown",
            {"stage": "inbound_readback", "client_and_all_inbounds_match": inbound_match,
             "unrelated_fields_preserved": unrelated_preserved, "inbound_count": len(inbound_ids), "fully_disconnected": False,
             "ev03_verified": False},
            {"credentials_rotated": True, "existing_sessions_disconnected": False},
        )

    async def disconnect_existing_sessions(self, identifier: str, operation_id: str) -> MutationResult:
        # RFP EV-03 is absent: a changed UUID/credential is not proof that a
        # live Xray session has ended. No guessed endpoint or panel restart.
        return MutationResult(
            "unknown",
            {"reason": "EV-03_not_verified", "network_call_made": False,
             "fully_disconnected": False, "operation_id_present": bool(operation_id)},
            {"existing_sessions_disconnected": False},
        )

    async def set_traffic(self, identifier: str, mode: str, value: int | None, operation_id: str) -> MutationResult:
        if not isinstance(operation_id, str) or not operation_id:
            return MutationResult("definite_failure", {"reason": "invalid_operation_id"})
        if mode not in {"set_quota_bytes", "reset_usage"}:
            return MutationResult("definite_failure", {"reason": "unsupported_traffic_mode"})
        state, wire, _ = await self._get_wire_client(identifier)
        if state != "found" or wire is None:
            return MutationResult("unknown", {"stage": "pre_read", "lookup": state})
        try:
            before = self._canonical(wire)
            if mode == "set_quota_bytes":
                if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                    return MutationResult("definite_failure", {"reason": "invalid_quota_bytes"})
                payload = self._editable_payload(wire)
                # This API's totalGB field is byte-counted despite its legacy name.
                payload["totalGB"] = 0 if value is None else value
                sent = await self._post_full_client(identifier, payload)
                if not self._api_ok(sent):
                    state = "definite_failure" if self._explicit_failure(sent) else "unknown"
                    return MutationResult(state, {"stage": "write", "http_status": sent.status_code})
                new_state, new_wire, read = await self._get_wire_client(identifier)
                if new_state != "found" or new_wire is None:
                    return MutationResult("unknown", {"stage": "readback", "lookup": new_state})
                after = self._canonical(new_wire)
                expected_mode, expected_bytes = normalize_volume(0 if value is None else value, zero_is_unlimited=True)
                matches = (
                    after.quota_mode == expected_mode and after.quota_bytes == expected_bytes
                    and after.used_bytes == before.used_bytes
                    and after.time_mode == before.time_mode and after.expires_at == before.expires_at
                    and after.enabled == before.enabled
                    and after.subscription_id == before.subscription_id
                )
                return MutationResult("success" if matches else "unknown",
                                      {"stage": "readback", "quota_matches": after.quota_mode == expected_mode and after.quota_bytes == expected_bytes,
                                       "usage_preserved": after.used_bytes == before.used_bytes,
                                       "expiry_preserved": after.time_mode == before.time_mode and after.expires_at == before.expires_at},
                                      {"quota_bytes": value, "usage_reset": False})

            sent = await self._request("POST", f"clients/resetTraffic/{quote(identifier, safe='')}")
            if not self._api_ok(sent):
                state = "definite_failure" if self._explicit_failure(sent) else "unknown"
                return MutationResult(state, {"stage": "reset_write", "http_status": sent.status_code})
            traffic_result = await self._request("GET", f"clients/traffic/{quote(identifier, safe='')}")
            traffic = self._obj(traffic_result)
            if not isinstance(traffic, Mapping):
                return MutationResult("unknown", {"stage": "reset_readback_malformed"})
            read_state, read_wire, _ = await self._get_wire_client(identifier)
            if read_state != "found" or read_wire is None:
                return MutationResult("unknown", {"stage": "reset_client_readback", "lookup": read_state})
            after = self._canonical(read_wire)
            zeroed = traffic.get("up") == 0 and traffic.get("down") == 0
            quota_preserved = after.quota_mode == before.quota_mode and after.quota_bytes == before.quota_bytes
            expiry_preserved = after.time_mode == before.time_mode and after.expires_at == before.expires_at
            depleted = before.quota_mode == "finite" and before.quota_bytes is not None and before.used_bytes is not None and before.used_bytes >= before.quota_bytes
            enable_safe = depleted or after.enabled == before.enabled
            return MutationResult(
                "success" if zeroed and quota_preserved and expiry_preserved and enable_safe else "unknown",
                {"stage": "reset_readback", "counters_zero": zeroed, "quota_preserved": quota_preserved,
                 "expiry_preserved": expiry_preserved, "enable_after": after.enabled,
                 "enable_preserved_or_documented_depletion_reenable": enable_safe},
                {"quota_preserved": True, "expiry_preserved": True,
                 "may_reenable_if_quota_depleted": bool(depleted)},
            )
        except AdapterUnavailable:
            return MutationResult("unknown", {"stage": "transport_or_readback"})
        except Exception:
            return MutationResult("unknown", {"stage": "normalization"})

    async def set_expiry(self, identifier: str, expires_at: int | None, operation_id: str) -> MutationResult:
        if not isinstance(operation_id, str) or not operation_id:
            return MutationResult("definite_failure", {"reason": "invalid_operation_id"})
        try:
            panel_ms = expiry_seconds_to_panel_ms(expires_at)
        except (TypeError, ValueError):
            return MutationResult("definite_failure", {"reason": "invalid_expiry_seconds"})
        state, wire, _ = await self._get_wire_client(identifier)
        if state != "found" or wire is None:
            return MutationResult("unknown", {"stage": "pre_read", "lookup": state})
        try:
            before = self._canonical(wire)
            payload = self._editable_payload(wire)
            payload["expiryTime"] = panel_ms
            sent = await self._post_full_client(identifier, payload)
            if not self._api_ok(sent):
                state = "definite_failure" if self._explicit_failure(sent) else "unknown"
                return MutationResult(state, {"stage": "write", "http_status": sent.status_code})
            new_state, new_wire, read = await self._get_wire_client(identifier)
            if new_state != "found" or new_wire is None:
                return MutationResult("unknown", {"stage": "readback", "lookup": new_state})
            after = self._canonical(new_wire)
            expected_mode, expected_seconds = normalize_expiry(panel_ms, zero_is_unlimited=True)
            matches = (
                after.time_mode == expected_mode and after.expires_at == expected_seconds
                and after.quota_mode == before.quota_mode and after.quota_bytes == before.quota_bytes
                and after.used_bytes == before.used_bytes
                and after.enabled == before.enabled
                and after.subscription_id == before.subscription_id
            )
            return MutationResult(
                "success" if matches else "unknown",
                {"stage": "readback", "expiry_matches": after.time_mode == expected_mode and after.expires_at == expected_seconds,
                 "quota_preserved": after.quota_mode == before.quota_mode and after.quota_bytes == before.quota_bytes,
                 "usage_preserved": after.used_bytes == before.used_bytes},
                {"expires_at_utc_seconds": expires_at, "quota_preserved": True},
            )
        except AdapterUnavailable:
            return MutationResult("unknown", {"stage": "transport_or_readback"})
        except Exception:
            return MutationResult("unknown", {"stage": "normalization"})

    # Other C11 operations remain intentionally unavailable in this adapter
    # batch; no unsupported operation is translated into a guessed HTTP call.
    async def create_client(self, spec: ClientCreateSpec) -> MutationResult:
        return MutationResult("unknown", {"reason": "capability_not_implemented_in_W04"})

    async def renew_client(self, identifier: str, target: dict, operation_id: str) -> MutationResult:
        return MutationResult("unknown", {"reason": "capability_not_implemented_in_W04"})

    async def add_addon(self, identifier: str, target: dict, operation_id: str) -> MutationResult:
        return MutationResult("unknown", {"reason": "capability_not_implemented_in_W04"})

    async def set_client_enabled(self, identifier: str, enabled: bool, operation_id: str) -> MutationResult:
        return MutationResult("unknown", {"reason": "capability_not_implemented_in_W04"})

    async def delete_client(self, identifier: str, operation_id: str) -> MutationResult:
        return MutationResult("unknown", {"reason": "capability_not_implemented_in_W04"})

    async def reconcile(self, operation: dict) -> ReconcileResult:
        return ReconcileResult("still_unknown", {"reason": "operation_ledger_reconciliation_not_bound_to_adapter"})
