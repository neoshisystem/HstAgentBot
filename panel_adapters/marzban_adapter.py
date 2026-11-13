"""Evidence-gated Marzban adapter using a protocol-neutral injected transport."""
from __future__ import annotations
from collections.abc import Mapping
from copy import deepcopy
import hashlib, inspect, json, time
from typing import Any, Callable
from urllib.parse import urlsplit
from .base import AdapterUnavailable, BasePanelAdapter, CanonicalServerStatus, LookupResult, MutationResult, PanelCapabilities, ReconcileResult, UnsupportedCapability
from .models import CanonicalClient, ClientCreateSpec, NodeConfig

SCHEMA_VERSION = "hstagent-panel-contract/1"
ADAPTER_VERSION = "marzban-v1"
SUPPORTED_CAPABILITIES = frozenset({"get_client","get_client_configs","create_client","renew_client","add_addon","rotate_subscription_url","rotate_connection_credentials","set_client_enabled","set_traffic","set_expiry","delete_client"})


class MarzbanAdapter(BasePanelAdapter):
    """No URL, HTTP verb, auth scheme, or retry policy is assumed here."""
    ADAPTER_VERSION = ADAPTER_VERSION
    SCHEMA_VERSION = SCHEMA_VERSION

    def __init__(self, panel: Mapping[str, Any], transport: Any = None, *,
                 contract: Mapping[str, Any] | None = None,
                 account_id_resolver: Callable[[int, str], int | None] | None = None,
                 operation_preflight: Callable[[str, Any, str], bool] | None = None,
                 clock: Callable[[], float] = time.time,
                 _test_only_recorded_contract: bool = False):
        if not isinstance(panel, Mapping):
            raise TypeError("panel must be a mapping")
        if transport is not None and not callable(getattr(transport, "invoke", None)):
            raise TypeError("transport must implement invoke(operation, payload, operation_id)")
        if contract is not None and not isinstance(contract, Mapping):
            raise TypeError("contract must be a mapping")
        self.panel_id = panel.get("id", panel.get("panel_id"))
        if isinstance(self.panel_id, bool) or not isinstance(self.panel_id, int) or self.panel_id <= 0:
            raise ValueError("panel id must be a positive integer")
        self._contract = deepcopy(dict(contract or {}))
        self._transport, self._account_id_resolver = transport, account_id_resolver
        self._operation_preflight, self._clock = operation_preflight, clock
        self._capability_specs = self._contract.get("capability")
        if not isinstance(self._capability_specs, Mapping):
            self._capability_specs = {}
        test_mode = (_test_only_recorded_contract is True and self._contract.get("synthetic") is True
                     and self._contract.get("production_approved") is False)
        approved = (self._contract.get("production_approved") is True
                    and self._contract.get("live_approval") == "APPROVED")
        required_status = "recorded" if test_mode else "verified"
        shape_ok = (self._contract.get("schema_version") == self.SCHEMA_VERSION
                    and self._contract.get("adapter_version") == self.ADAPTER_VERSION
                    and all(isinstance(self._contract.get(k), Mapping) for k in
                            ("request","response","expected","unit_mapping","client_fields","config_fields")))
        enabled = set()
        if transport is not None and shape_ok and (test_mode or approved):
            for name, spec in self._capability_specs.items():
                if (name in SUPPORTED_CAPABILITIES and isinstance(spec, Mapping)
                    and spec.get("status") == required_status and isinstance(spec.get("operation"), str)
                    and spec.get("operation") and isinstance(spec.get("request_fields"), Mapping)):
                    enabled.add(name)
        self._test_mode, self._contract_approved = test_mode, approved
        super().__init__(PanelCapabilities(frozenset(enabled)))

    def __repr__(self):
        return (f"MarzbanAdapter(panel_id={self.panel_id!r}, contract_approved={self._contract_approved!r}, "
                f"capabilities={self.capabilities.methods!r}, transport=<injected>)")

    def capability_manifest(self) -> dict[str, dict[str, str]]:
        return {
            name: ({"status":"recorded_test_only","reason":"synthetic_contract_and_fake_transport"}
                   if self.capabilities.supports(name) and self._test_mode else
                   {"status":"verified","reason":"approved_contract"}
                   if self.capabilities.supports(name) else
                   {"status":"unsupported","reason":"transport_or_approved_contract_missing"})
            for name in sorted(SUPPORTED_CAPABILITIES)
        }

    def _require(self, name: str) -> Mapping[str, Any]:
        self.capabilities.require(name)
        spec = self._capability_specs.get(name)
        if not isinstance(spec, Mapping):
            raise UnsupportedCapability(f"panel capability is not verified: {name}")
        return spec

    @staticmethod
    def _positive(value: Any) -> bool:
        return isinstance(value, int) and not isinstance(value, bool) and value > 0

    def _field(self, name: str) -> str:
        value = self._contract.get("client_fields", {}).get(name)
        if not isinstance(value, str) or not value:
            raise AdapterUnavailable("recorded client field mapping is incomplete")
        return value

    def _volume_factor(self) -> int:
        unit = self._contract["unit_mapping"].get("volume")
        factors = {"bytes":1,"kibibytes":1024,"mebibytes":1024**2,"gibibytes":1024**3}
        if unit not in factors:
            raise AdapterUnavailable("recorded volume unit is unsupported")
        return factors[unit]

    def _volume_from_panel(self, value: Any) -> int | None:
        if value is None: return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AdapterUnavailable("recorded volume value is malformed")
        return value * self._volume_factor()

    def _volume_to_panel(self, value: int | None) -> int | None:
        if value is None: return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("volume must be a non-negative integer or None")
        factor = self._volume_factor()
        if value % factor: raise ValueError("volume cannot be represented exactly in the recorded panel unit")
        return value // factor

    def _expiry_to_seconds(self, value: Any) -> int | None:
        if value is None: return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise AdapterUnavailable("recorded expiry value is malformed")
        unit = self._contract["unit_mapping"].get("expiry")
        if unit == "unix_seconds": return value
        if unit == "unix_milliseconds" and value % 1000 == 0: return value // 1000
        raise AdapterUnavailable("recorded expiry unit is unsupported or imprecise")

    def _expiry_from_seconds(self, value: int | None) -> int | None:
        if value is None: return None
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("expiry must be a positive UTC epoch-second integer or None")
        unit = self._contract["unit_mapping"].get("expiry")
        if unit == "unix_seconds": return value
        if unit == "unix_milliseconds": return value * 1000
        raise AdapterUnavailable("recorded expiry unit is unsupported")

    def _canonical(self, wire: Mapping[str, Any]) -> CanonicalClient:
        identifier = wire.get(self._field("identifier"))
        if not isinstance(identifier, str) or not identifier:
            raise AdapterUnavailable("client identifier is malformed")
        if self._account_id_resolver is None:
            raise AdapterUnavailable("canonical account_id mapping is required")
        try: account_id = self._account_id_resolver(self.panel_id, identifier)
        except Exception: raise AdapterUnavailable("canonical account_id mapping failed") from None
        if not self._positive(account_id):
            raise AdapterUnavailable("canonical account_id mapping is unavailable")
        enabled_raw = wire.get(self._field("enabled"))
        enabled = enabled_raw if isinstance(enabled_raw, bool) else None
        quota_mode = wire.get(self._field("quota_mode"))
        if quota_mode not in {"finite","unlimited","unknown"}:
            raise AdapterUnavailable("client quota mode is malformed")
        quota_raw = wire.get(self._field("quota_bytes"))
        quota_bytes = self._volume_from_panel(quota_raw) if quota_mode == "finite" else None
        if (quota_mode == "finite" and quota_bytes is None) or quota_mode == "unknown":
            raise AdapterUnavailable("client quota state is unavailable")
        time_mode = wire.get(self._field("time_mode"))
        if time_mode not in {"dated","unlimited","on_hold","unknown"} or time_mode == "unknown":
            raise AdapterUnavailable("client expiry state is unavailable")
        expires_at = self._expiry_to_seconds(wire.get(self._field("expires_at"))) if time_mode == "dated" else None
        used_raw = wire.get(self._field("used_bytes"))
        used_bytes = self._volume_from_panel(used_raw)
        subscription_id = wire.get(self._field("subscription_id"))
        if subscription_id is not None and not isinstance(subscription_id, str):
            raise AdapterUnavailable("client subscription reference is malformed")
        credentials = wire.get(self._field("credentials"), {})
        if credentials is not None and not isinstance(credentials, Mapping):
            raise AdapterUnavailable("client credential set is malformed")
        digest = json.dumps(dict(credentials or {}), sort_keys=True, separators=(",",":"), default=str)
        revision = hashlib.sha256(digest.encode()).hexdigest()[:16]
        return CanonicalClient(
            ref=(account_id,self.panel_id,identifier), enabled=enabled, quota_mode=quota_mode,
            quota_bytes=quota_bytes, used_bytes=used_bytes, time_mode=time_mode,
            expires_at=expires_at, first_connected_at=None, last_connected_at=None,
            client_app=None, subscription_id=subscription_id, credential_revision=revision,
            observed_at=int(self._clock()), adapter_revision=self.ADAPTER_VERSION)

    def _map_request(self, capability: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        spec = self._require(capability)
        mapping = spec.get("request_fields")
        if not isinstance(mapping, Mapping): raise AdapterUnavailable("recorded request mapping is unavailable")
        wire = {}
        for key, value in payload.items():
            target = mapping.get(key)
            if not isinstance(target, str) or not target:
                raise AdapterUnavailable("recorded request field mapping is incomplete")
            wire[target] = deepcopy(value)
        return wire

    async def _invoke(self, capability: str, payload: Mapping[str, Any], operation_id: str | None = None) -> Mapping[str, Any]:
        spec = self._require(capability)
        wire_payload = self._map_request(capability, payload)
        try:
            result = self._transport.invoke(spec["operation"], wire_payload, operation_id)
            if inspect.isawaitable(result): result = await result
        except TimeoutError: return {"state":"unknown","reason":"timeout"}
        except Exception: return {"state":"unavailable","reason":"transport_error"}
        if not isinstance(result, Mapping): return {"state":"unavailable","reason":"malformed_transport_result"}
        if result.get("state") not in {"success","failure","unknown","unavailable","not_found"}:
            return {"state":"unavailable","reason":"unknown_transport_state"}
        return result

    def _response_data(self, capability: str, result: Mapping[str, Any]) -> Any:
        data = result.get("data")
        spec = self._capability_specs.get(capability, {})
        field = spec.get("response_field") if isinstance(spec, Mapping) else None
        return data.get(field) if field and isinstance(data, Mapping) else (None if field else data)

    async def _read_wire(self, identifier: str) -> tuple[str, Mapping[str, Any] | None]:
        if not isinstance(identifier, str) or not identifier: return "unavailable", None
        try: result = await self._invoke("get_client", {"identifier":identifier})
        except (UnsupportedCapability, AdapterUnavailable): return "unavailable", None
        if result.get("state") == "not_found": return "not_found", None
        if result.get("state") != "success": return "unavailable", None
        data = self._response_data("get_client", result)
        if not isinstance(data, Mapping) or data.get(self._field("identifier")) != identifier:
            return "unavailable", None
        return "found", data

    async def verify_connection(self) -> CanonicalServerStatus:
        return CanonicalServerStatus(False,self.ADAPTER_VERSION,None,None,None,None,int(self._clock()))

    async def get_client(self, identifier: str) -> LookupResult:
        state, wire = await self._read_wire(identifier)
        if state != "found" or wire is None:
            return LookupResult(state,evidence={"reason":"recorded_read_unavailable"})
        try: return LookupResult("found",self._canonical(wire),{"source":"recorded_contract","panel_id":self.panel_id})
        except AdapterUnavailable: return LookupResult("unavailable",evidence={"reason":"malformed_client"})

    async def list_clients(self, scope_filter: str | None = None) -> list[CanonicalClient]:
        raise UnsupportedCapability("panel capability is not verified: list_clients")

    async def get_client_configs(self, identifier: str) -> list[NodeConfig]:
        result = await self._invoke("get_client_configs",{"identifier":identifier})
        if result.get("state") != "success": raise AdapterUnavailable("recorded client-config read is unavailable")
        rows = self._response_data("get_client_configs",result)
        if not isinstance(rows,list): raise AdapterUnavailable("recorded client-config response is malformed")
        fields = self._contract.get("config_fields")
        configs=[]
        for row in rows:
            if not isinstance(row,Mapping): raise AdapterUnavailable("recorded client-config entry is malformed")
            vals=[row.get(fields.get(k)) for k in ("node_key","label","protocol","uri")]
            if not all(isinstance(v,str) and v for v in vals): raise AdapterUnavailable("recorded client-config fields are incomplete")
            key,label,protocol,uri=vals
            parsed=urlsplit(uri)
            if parsed.scheme.lower()!=protocol.lower() or not parsed.hostname: raise AdapterUnavailable("recorded client-config URI is malformed")
            configs.append(NodeConfig(key,label,protocol.lower(),uri))
        return configs

    def _candidate_ready(self, operation_id: str, candidate: Any, capability: str) -> bool:
        if not isinstance(operation_id,str) or not operation_id or self._operation_preflight is None: return False
        try: return self._operation_preflight(operation_id,deepcopy(candidate),capability) is True
        except Exception: return False

    @staticmethod
    def _write_failure(result: Mapping[str, Any]) -> str:
        return "definite_failure" if result.get("state")=="failure" and result.get("effect")=="none" else "unknown"

    def _target_payload(self,target:Mapping[str,Any])->tuple[dict[str,Any],dict[str,Any]]:
        required=("quota_mode","quota_bytes","used_bytes","time_mode","expires_at")
        if any(k not in target for k in required): raise ValueError("renewal target is incomplete")
        qm,tm=target["quota_mode"],target["time_mode"]
        if qm not in {"finite","unlimited"} or tm not in {"dated","unlimited","on_hold"}: raise ValueError("renewal target mode is invalid")
        if qm=="finite" and (isinstance(target["quota_bytes"],bool) or not isinstance(target["quota_bytes"],int) or target["quota_bytes"]<0):
            raise ValueError("renewal quota is invalid")
        if target["used_bytes"] is not None and (isinstance(target["used_bytes"],bool) or not isinstance(target["used_bytes"],int) or target["used_bytes"]<0):
            raise ValueError("renewal usage is invalid")
        quota=self._volume_to_panel(target["quota_bytes"]) if qm=="finite" else None
        used=self._volume_to_panel(target["used_bytes"]) if target["used_bytes"] is not None else None
        expiry=self._expiry_from_seconds(target["expires_at"]) if tm=="dated" else None
        request={"quota_mode":qm,"quota_bytes":quota,"used_bytes":used,"time_mode":tm,"expires_at":expiry}
        expected={"quota_mode":qm,"quota_bytes":target["quota_bytes"] if qm=="finite" else None,
                  "used_bytes":target["used_bytes"],"time_mode":tm,"expires_at":target["expires_at"] if tm=="dated" else None}
        return request,expected

    async def _update_and_verify(self,identifier:str,capability:str,payload:Mapping[str,Any],operation_id:str,
                                 expected:Mapping[str,Any],preserve:tuple[str,...]=())->MutationResult:
        if not isinstance(operation_id,str) or not operation_id:
            return MutationResult("definite_failure",{"reason":"invalid_operation_id","network_call_made":False})
        state,before_wire=await self._read_wire(identifier)
        if state!="found" or before_wire is None: return MutationResult("unknown",{"stage":"pre_read","lookup":state})
        try:
            before=self._canonical(before_wire)
            request={"identifier":identifier,**dict(payload),"operation_id":operation_id}
            written=await self._invoke(capability,request,operation_id)
        except (AdapterUnavailable,UnsupportedCapability,ValueError):
            return MutationResult("definite_failure",{"stage":"preflight","network_call_made":False})
        if written.get("state")!="success":
            read_state,_=await self._read_wire(identifier)
            return MutationResult(self._write_failure(written),{"stage":"write","write_state":written.get("state"),
                                    "effect":written.get("effect"),"readback_state":read_state})
        after_state,after_wire=await self._read_wire(identifier)
        if after_state!="found" or after_wire is None: return MutationResult("unknown",{"stage":"readback","lookup":after_state})
        try: after=self._canonical(after_wire)
        except AdapterUnavailable: return MutationResult("unknown",{"stage":"readback_malformed"})
        matches=all(getattr(after,k)==v for k,v in expected.items())
        preserved=all(getattr(after,k)==getattr(before,k) for k in preserve)
        return MutationResult("success" if matches and preserved else "unknown",
            {"stage":"readback","expected_fields_match":matches,"unrelated_fields_preserved":preserved},
            {"operation_id":operation_id,"read_back":True})

    async def create_client(self,spec:ClientCreateSpec)->MutationResult:
        if not isinstance(spec,Mapping): return MutationResult("definite_failure",{"reason":"invalid_create_spec"})
        op,candidate=spec.get("operation_id"),spec.get("candidate_credentials")
        identifier=candidate.get("username") if isinstance(candidate,Mapping) else None
        if not isinstance(identifier,str) or not identifier: identifier=spec.get("email")
        if not isinstance(identifier,str) or not identifier or not isinstance(candidate,Mapping) or not candidate:
            return MutationResult("definite_failure",{"reason":"stable_identifier_or_credentials_missing"})
        if not self._candidate_ready(op,candidate,"create_client"):
            return MutationResult("definite_failure",{"reason":"stable_candidate_not_persisted","network_call_made":False})
        state,prior=await self._read_wire(identifier)
        if state=="found":
            op_field=self._contract["client_fields"].get("operation_id")
            if op_field and prior.get(op_field)==op: return MutationResult("success",{"stage":"idempotent_readback","existing_operation":True})
            return MutationResult("definite_failure",{"reason":"identifier_owned_by_another_client","network_call_made":False})
        if state!="not_found": return MutationResult("unknown",{"stage":"collision_preflight","lookup":state})
        try:
            request={"identifier":identifier,"operation_id":op,"quota_mode":spec.get("quota_mode"),
                "quota_bytes":self._volume_to_panel(spec.get("quota_bytes")) if spec.get("quota_mode")=="finite" else None,
                "duration_seconds":spec.get("duration_seconds"),"on_first_connect":spec.get("on_first_connect"),
                "inbound_ids":list(spec.get("inbound_ids") or ()),"comment":spec.get("comment",""),"credentials":dict(candidate)}
            result=await self._invoke("create_client",request,op)
        except (AdapterUnavailable,UnsupportedCapability,ValueError):
            return MutationResult("definite_failure",{"reason":"create_contract_preflight_failed","network_call_made":False})
        if result.get("state")!="success":
            read_state,_=await self._read_wire(identifier)
            return MutationResult(self._write_failure(result),{"stage":"create","write_state":result.get("state"),"readback_state":read_state})
        read_state,created=await self._read_wire(identifier)
        if read_state!="found" or created is None: return MutationResult("unknown",{"stage":"create_readback","lookup":read_state})
        try: canonical=self._canonical(created)
        except AdapterUnavailable: return MutationResult("unknown",{"stage":"create_readback_malformed"})
        actual=created.get(self._field("credentials"))
        op_field=self._contract["client_fields"].get("operation_id")
        credentials_match=isinstance(actual,Mapping) and all(actual.get(k)==v for k,v in candidate.items())
        op_match=not op_field or created.get(op_field)==op
        return MutationResult("success" if credentials_match and op_match else "unknown",
            {"stage":"create_readback","credentials_match":credentials_match,"operation_match":op_match},
            {"canonical_ref":canonical.ref,"operation_id":op})

    async def renew_client(self,identifier:str,target:dict,operation_id:str)->MutationResult:
        if not isinstance(target,Mapping): return MutationResult("definite_failure",{"reason":"invalid_renewal_target"})
        if target.get("time_mode")=="on_hold": return MutationResult("definite_failure",{"reason":"on_hold_renewal_forbidden","network_call_made":False})
        try: payload,expected=self._target_payload(target)
        except (AdapterUnavailable,ValueError): return MutationResult("definite_failure",{"reason":"invalid_renewal_units","network_call_made":False})
        return await self._update_and_verify(identifier,"renew_client",payload,operation_id,expected)

    async def add_addon(self,identifier:str,target:dict,operation_id:str)->MutationResult:
        if not isinstance(target,Mapping): return MutationResult("definite_failure",{"reason":"invalid_addon_target"})
        days=target.get("add_days",0)
        if target.get("time_mode")=="on_hold" and isinstance(days,int) and not isinstance(days,bool) and days>0:
            return MutationResult("definite_failure",{"reason":"on_hold_add_days_forbidden","network_call_made":False})
        try:
            payload,expected=self._target_payload(target)
            payload["add_bytes"]=self._volume_to_panel(target.get("add_bytes",0)); payload["add_days"]=days
        except (AdapterUnavailable,ValueError): return MutationResult("definite_failure",{"reason":"invalid_addon_units","network_call_made":False})
        return await self._update_and_verify(identifier,"add_addon",payload,operation_id,expected)

    async def rotate_subscription_url(self,identifier:str,candidate:str,operation_id:str)->MutationResult:
        if not isinstance(candidate,str) or not candidate or not self._candidate_ready(operation_id,candidate,"rotate_subscription_url"):
            return MutationResult("definite_failure",{"reason":"stable_subscription_candidate_not_persisted","network_call_made":False})
        state,before_wire=await self._read_wire(identifier)
        if state!="found" or before_wire is None: return MutationResult("unknown",{"stage":"pre_read","lookup":state})
        try:
            before=self._canonical(before_wire)
            result=await self._invoke("rotate_subscription_url",{"identifier":identifier,"candidate":candidate,"operation_id":operation_id},operation_id)
        except (AdapterUnavailable,UnsupportedCapability): return MutationResult("definite_failure",{"stage":"preflight","network_call_made":False})
        if result.get("state")!="success":
            read_state,_=await self._read_wire(identifier)
            return MutationResult(self._write_failure(result),{"stage":"write","write_state":result.get("state"),"readback_state":read_state})
        state,after_wire=await self._read_wire(identifier)
        if state!="found" or after_wire is None: return MutationResult("unknown",{"stage":"readback","lookup":state})
        try: after=self._canonical(after_wire)
        except AdapterUnavailable: return MutationResult("unknown",{"stage":"readback_malformed"})
        fields=("enabled","quota_mode","quota_bytes","used_bytes","time_mode","expires_at","credential_revision")
        preserved=all(getattr(after,k)==getattr(before,k) for k in fields)
        matches=after.subscription_id==candidate
        return MutationResult("success" if matches and preserved else "unknown",
            {"stage":"readback","subscription_url_matches":matches,"unrelated_fields_preserved":preserved},
            {"operation_id":operation_id,"existing_sessions_disconnected":False})

    async def rotate_connection_credentials(self,identifier:str,candidate:dict,operation_id:str)->MutationResult:
        if not isinstance(candidate,Mapping) or not candidate: return MutationResult("definite_failure",{"reason":"invalid_credential_candidate"})
        spec=self._require("rotate_connection_credentials"); allowed=spec.get("credential_fields")
        if not isinstance(allowed,list) or set(candidate)!=set(allowed):
            return MutationResult("definite_failure",{"reason":"all_recorded_credential_fields_are_required"})
        if not self._candidate_ready(operation_id,candidate,"rotate_connection_credentials"):
            return MutationResult("definite_failure",{"reason":"stable_candidate_not_persisted","network_call_made":False})
        state,before_wire=await self._read_wire(identifier)
        if state!="found" or before_wire is None: return MutationResult("unknown",{"stage":"pre_read","lookup":state})
        cred_field=self._field("credentials")
        if not isinstance(before_wire.get(cred_field),Mapping): return MutationResult("unknown",{"stage":"pre_read","reason":"credential_set_unavailable"})
        try:
            before=self._canonical(before_wire)
            result=await self._invoke("rotate_connection_credentials",
                {"identifier":identifier,"credentials":dict(candidate),"operation_id":operation_id},operation_id)
        except (AdapterUnavailable,UnsupportedCapability): return MutationResult("definite_failure",{"stage":"preflight","network_call_made":False})
        if result.get("state")!="success":
            read_state,_=await self._read_wire(identifier)
            return MutationResult(self._write_failure(result),{"stage":"write","write_state":result.get("state"),"readback_state":read_state})
        state,after_wire=await self._read_wire(identifier)
        if state!="found" or after_wire is None: return MutationResult("unknown",{"stage":"readback","lookup":state})
        actual=after_wire.get(cred_field); all_match=isinstance(actual,Mapping) and all(actual.get(k)==v for k,v in candidate.items())
        try: after=self._canonical(after_wire)
        except AdapterUnavailable: return MutationResult("unknown",{"stage":"readback_malformed"})
        fields=("enabled","quota_mode","quota_bytes","used_bytes","time_mode","expires_at","subscription_id")
        preserved=all(getattr(after,k)==getattr(before,k) for k in fields)
        return MutationResult("success" if all_match and preserved else "unknown",
            {"stage":"readback","all_candidate_credentials_match":all_match,"unrelated_fields_preserved":preserved,"fully_disconnected":False,"ev03_verified":False},
            {"credentials_rotated":all_match,"existing_sessions_disconnected":False})

    async def set_client_enabled(self,identifier:str,enabled:bool,operation_id:str)->MutationResult:
        if not isinstance(enabled,bool): return MutationResult("definite_failure",{"reason":"enabled_must_be_boolean"})
        return await self._update_and_verify(identifier,"set_client_enabled",{"enabled":enabled},operation_id,{"enabled":enabled},
            ("quota_mode","quota_bytes","used_bytes","time_mode","expires_at","subscription_id"))

    async def set_traffic(self,identifier:str,mode:str,value:int|None,operation_id:str)->MutationResult:
        if mode=="unlimited": qm,qb="unlimited",None
        elif mode=="set_quota_bytes":
            if isinstance(value,bool) or not isinstance(value,int) or value<0:
                return MutationResult("definite_failure",{"reason":"invalid_quota_bytes","network_call_made":False})
            qm,qb="finite",value
        else: return MutationResult("definite_failure",{"reason":"unsupported_traffic_mode","network_call_made":False})
        try: wire_q=self._volume_to_panel(qb)
        except (AdapterUnavailable,ValueError): return MutationResult("definite_failure",{"reason":"invalid_quota_units","network_call_made":False})
        return await self._update_and_verify(identifier,"set_traffic",{"quota_mode":qm,"quota_bytes":wire_q},operation_id,
            {"quota_mode":qm,"quota_bytes":qb},("enabled","used_bytes","time_mode","expires_at","subscription_id"))

    async def set_expiry(self,identifier:str,expires_at:int|None,operation_id:str)->MutationResult:
        try: mode="unlimited" if expires_at is None else "dated"; wire_expiry=self._expiry_from_seconds(expires_at)
        except (AdapterUnavailable,ValueError): return MutationResult("definite_failure",{"reason":"invalid_expiry","network_call_made":False})
        return await self._update_and_verify(identifier,"set_expiry",{"time_mode":mode,"expires_at":wire_expiry},operation_id,
            {"time_mode":mode,"expires_at":expires_at},("enabled","quota_mode","quota_bytes","used_bytes","subscription_id"))

    async def delete_client(self,identifier:str,operation_id:str)->MutationResult:
        if not isinstance(operation_id,str) or not operation_id: return MutationResult("definite_failure",{"reason":"invalid_operation_id","network_call_made":False})
        state,before=await self._read_wire(identifier)
        if state!="found" or before is None: return MutationResult("unknown",{"stage":"pre_read","lookup":state})
        try: result=await self._invoke("delete_client",{"identifier":identifier,"operation_id":operation_id},operation_id)
        except (AdapterUnavailable,UnsupportedCapability): return MutationResult("definite_failure",{"stage":"preflight","network_call_made":False})
        if result.get("state")!="success":
            read_state,_=await self._read_wire(identifier)
            return MutationResult(self._write_failure(result),{"stage":"delete","write_state":result.get("state"),"readback_state":read_state})
        after_state,_=await self._read_wire(identifier)
        if after_state=="not_found":
            return MutationResult("success",{"stage":"delete_readback","not_found_confirmed":True},{"client_deleted":True,"operation_id":operation_id})
        return MutationResult("unknown",{"stage":"delete_readback","lookup":after_state},{"client_deleted":False,"operation_id":operation_id})

    async def disconnect_existing_sessions(self,identifier:str,operation_id:str)->MutationResult:
        return MutationResult("unknown",{"reason":"EV-03_evidence_required","network_call_made":False,"fully_disconnected":False},
            {"existing_sessions_disconnected":False})

    async def reconcile(self,operation:dict)->ReconcileResult:
        return ReconcileResult("still_unknown",{"reason":"operation_ledger_reconciliation_not_bound"})
