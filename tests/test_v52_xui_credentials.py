import asyncio
import json
from panel_adapters.xui_openapi import XUIOpenAPIAdapter

def _guard_offline(monkeypatch):
    import importlib.util
    from pathlib import Path
    helper_path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_v52_panel_offline_helpers", helper_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    attempts = module.guard_network(monkeypatch)
    return module, attempts

class Response:
    def __init__(self, payload): self.status_code, self._payload = 200, payload
    def json(self): return self._payload

class Session:
    def __init__(self, *responses, on_first=None):
        self.responses, self.calls, self.on_first = list(responses), [], on_first
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if len(self.calls) == 1 and self.on_first:
            self.on_first()
        return self.responses.pop(0)

def client(uuid="old-uuid"):
    return {"email": "u@example.test", "enable": True, "uuid": uuid, "subId": "s1",
            "totalGB": 1000, "expiryTime": 1_800_000_000_000, "traffic": {"up": 1, "down": 2},
            "inboundIds": [11, 12]}

def inbound(inbound_id, uuid):
    return {"id": inbound_id, "settings": json.dumps({"clients": [{"email": "u@example.test", "id": uuid}]})}

def make(session):
    return XUIOpenAPIAdapter({"id": 12, "raw_url": "https://p.test/panel/api-docs", "auth_cipher": "cipher"}, session,
                             decryptor=lambda *_: "TOKEN", transaction_probe=lambda: False, account_id_resolver=lambda *_: 303, operation_preflight=lambda op, candidate, kind: True)

def test_rotation_not_equal_disconnect(monkeypatch):
    _, network_attempts = _guard_offline(monkeypatch)
    candidate = {"uuid": "candidate-uuid"}
    s = Session(
        Response({"success": True, "obj": client()}),
        Response({"success": True}),
        Response({"success": True, "obj": client("candidate-uuid")}),
        Response({"success": True, "obj": [inbound(11, "candidate-uuid"), inbound(12, "candidate-uuid")]}),
        on_first=lambda: candidate.update(uuid="mutated-after-dispatch"),
    )
    a = make(s)
    result = asyncio.run(a.rotate_connection_credentials("u@example.test", candidate, "op-credential"))
    assert result.state == "success"
    update_payload = s.calls[1][2]["json"]
    assert update_payload["id"] == "candidate-uuid"
    assert result.evidence["client_and_all_inbounds_match"] is True
    assert result.expected_effect["existing_sessions_disconnected"] is False
    before = len(s.calls)
    disconnect = asyncio.run(a.disconnect_existing_sessions("u@example.test", "op-disconnect"))
    assert disconnect.state == "unknown"
    assert disconnect.evidence["fully_disconnected"] is False
    assert len(s.calls) == before

    partial = Session(Response({"success": True, "obj": client()}), Response({"success": True}),
                      Response({"success": True, "obj": client("candidate-uuid")}),
                      Response({"success": True, "obj": [inbound(11, "candidate-uuid"), inbound(12, "old-uuid")]}))
    assert asyncio.run(make(partial).rotate_connection_credentials("u@example.test", {"uuid": "candidate-uuid"}, "op-2")).state == "unknown"
    assert network_attempts == []
