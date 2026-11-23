"""W21.1 contract gate and shared synthetic transport helpers."""
from __future__ import annotations
import json
from collections import defaultdict, deque
from copy import deepcopy
from pathlib import Path
import pytest
from panel_adapters.base import UnsupportedCapability
from panel_adapters.marzban_adapter import MarzbanAdapter

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "marzban_contract.json"

def fixture_contract():
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

class FakeTransport:
    """Non-HTTP transport with explicit recorded responses and call capture."""
    def __init__(self):
        self.responses = defaultdict(deque)
        self.calls = []
    def queue(self, operation, *responses):
        self.responses[operation].extend(responses)
    def invoke(self, operation, payload, operation_id):
        self.calls.append((operation, deepcopy(payload), operation_id))
        if not self.responses[operation]:
            return {"state": "unavailable"}
        value = self.responses[operation].popleft()
        if isinstance(value, BaseException):
            raise value
        if callable(value):
            return value(operation, payload, operation_id)
        return deepcopy(value)

def make_client(**overrides):
    wire = {
        "username":"synthetic-user-001","enabled":True,"quota_mode":"finite",
        "data_limit":1048576,"used_traffic":131072,"time_mode":"dated",
        "expire":1800000000000,"subscription_url":"SYNTHETIC-REDACTED-SUBSCRIPTION",
        "credentials":{"uuid":"REDACTED:SYNTHETIC-UUID","password":"REDACTED:SYNTHETIC-PASSWORD",
                       "flow":"SYNTHETIC-FLOW","security":"SYNTHETIC-SECURITY"},
        "operation_id":None,"comment":"synthetic fixture","opaque_unrelated_field":"must-be-preserved"
    }
    wire.update(overrides)
    return wire

def make_adapter(transport, *, operation_preflight=None):
    return MarzbanAdapter(
        {"id":71,"panel_type":"marzban","adapter_version":"marzban-v1"},
        transport, contract=fixture_contract(),
        account_id_resolver=lambda panel_id, identifier:7001,
        operation_preflight=operation_preflight or (lambda op, candidate, capability:True),
        clock=lambda:1800000100, _test_only_recorded_contract=True,
    )

def test_protocol_is_local_and_disabled():
    transport=FakeTransport()
    adapter=MarzbanAdapter({"id":71},transport)
    manifest=adapter.capability_manifest()
    assert manifest and all(item["status"]=="unsupported" for item in manifest.values())
    assert adapter.capabilities.methods==frozenset()
    with pytest.raises(UnsupportedCapability):
        adapter.capabilities.require("get_client")
    assert transport.calls==[]
