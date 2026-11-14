import asyncio
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
    def __init__(self, status, payload): self.status_code, self._payload = status, payload
    def json(self): return self._payload

class Session:
    def __init__(self, *responses): self.responses, self.calls = list(responses), []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception): raise item
        return item

def make(session):
    return XUIOpenAPIAdapter({"id": 4, "raw_url": "https://p.test/custom/panel/api-docs", "auth_cipher": "x"}, session,
                             decryptor=lambda *_: "FAKE_TOKEN", transaction_probe=lambda: False, account_id_resolver=lambda *_: 303)

def test_lookup_distinguishes_absent_and_unavailable(monkeypatch):
    _, network_attempts = _guard_offline(monkeypatch)
    email = "alice+tag/one@example.test"
    client = {"email": email, "enable": True, "totalGB": 1024, "expiryTime": 1_800_000_000_000,
              "subId": "sub-1", "traffic": {"up": 10, "down": 20}, "inboundIds": [1]}
    s = Session(Response(200, {"success": True, "obj": client}), Response(404, {"success": False}),
                Response(503, {}), Response(200, {"success": True, "obj": "broken"}), TimeoutError("timeout"))
    a = make(s)
    found = asyncio.run(a.get_client(email))
    assert found.state == "found" and found.client.used_bytes == 30
    assert s.calls[0][1].endswith("clients/get/alice%2Btag%2Fone%40example.test")
    assert "%252B" not in s.calls[0][1] and "%252F" not in s.calls[0][1]
    assert asyncio.run(a.get_client("absent@example.test")).state == "not_found"
    assert asyncio.run(a.get_client("upstream@example.test")).state == "unavailable"
    assert asyncio.run(a.get_client("malformed@example.test")).state == "unavailable"
    assert asyncio.run(a.get_client("timeout@example.test")).state == "unavailable"
    assert "FAKE_TOKEN" not in repr(a)
    assert network_attempts == []
