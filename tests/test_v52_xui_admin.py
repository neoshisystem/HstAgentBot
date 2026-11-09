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
    def __init__(self, payload, status=200): self.status_code, self._payload = status, payload
    def json(self): return self._payload

class Session:
    def __init__(self, *responses): self.responses, self.calls = list(responses), []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception): raise item
        return item

def client(quota=1000, expiry=1_800_000_000_000, up=2, down=3, enabled=True):
    return {"email": "a@example.test", "enable": enabled, "subId": "sub",
            "totalGB": quota, "expiryTime": expiry, "traffic": {"up": up, "down": down}, "inboundIds": [1]}

def make(session):
    return XUIOpenAPIAdapter({"id": 3, "raw_url": "https://p.test/custom/panel/api-docs", "auth_cipher": "x"}, session,
                             decryptor=lambda *_: "TOKEN", transaction_probe=lambda: False, account_id_resolver=lambda *_: 303)

def test_admin_adjustments_preserve_unrelated_fields(monkeypatch):
    _, network_attempts = _guard_offline(monkeypatch)
    s = Session(
        Response({"success": True, "obj": client()}), Response({"success": True}),
        Response({"success": True, "obj": client(quota=2048)}),
        Response({"success": True, "obj": client(quota=2048)}), Response({"success": True}),
        Response({"success": True, "obj": client(quota=2048, expiry=1_900_000_000_000)}),
        Response({"success": True, "obj": client(quota=2048, expiry=1_900_000_000_000)}),
        Response({"success": True}),
        Response({"success": True, "obj": {"up": 0, "down": 0, "enable": True}}),
        Response({"success": True, "obj": client(quota=2048, expiry=1_900_000_000_000)}),
    )
    a = make(s)
    quota = asyncio.run(a.set_traffic("a@example.test", "set_quota_bytes", 2048, "op-q"))
    assert quota.state == "success" and s.calls[1][2]["json"]["totalGB"] == 2048
    expiry = asyncio.run(a.set_expiry("a@example.test", 1_900_000_000, "op-e"))
    assert expiry.state == "success" and s.calls[4][2]["json"]["expiryTime"] == 1_900_000_000_000
    assert s.calls[1][2]["json"]["expiryTime"] == 1_800_000_000_000
    assert s.calls[4][2]["json"]["totalGB"] == 2048
    reset = asyncio.run(a.set_traffic("a@example.test", "reset_usage", None, "op-r"))
    assert reset.state == "success"
    assert s.calls[7][1].endswith("/clients/resetTraffic/a%40example.test")
    assert reset.expected_effect["quota_preserved"] is True

    partial = Session(Response({"success": True, "obj": client()}), Response({"success": True}),
                      Response({"success": True, "obj": client()}))
    failed_readback = asyncio.run(make(partial).set_traffic("a@example.test", "set_quota_bytes", 2048, "op-partial"))
    assert failed_readback.state == "unknown"
    assert network_attempts == []
