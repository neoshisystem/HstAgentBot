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

def wire(sub="old"):
    return {"email": "a@example.test", "enable": True, "subId": sub, "totalGB": 1000,
            "expiryTime": 1_800_000_000_000, "traffic": {"up": 2, "down": 3}, "inboundIds": [9],
            "comment": "keep", "limitIp": 1, "reset": 0, "resetDay": 0, "resetMax": 0,
            "resetWeekday": 0, "security": "auto", "tgId": 1}

def make(session):
    return XUIOpenAPIAdapter({"id": 9, "raw_url": "https://p.test/custom/panel/api-docs", "auth_cipher": "cipher"}, session,
                             decryptor=lambda *_: "TOKEN", transaction_probe=lambda: False, account_id_resolver=lambda *_: 303, operation_preflight=lambda op, candidate, kind: True)

def test_central_sub_update_and_unknown(monkeypatch):
    _, network_attempts = _guard_offline(monkeypatch)
    success_session = Session(Response(200, {"success": True, "obj": wire()}),
                              Response(200, {"success": True}),
                              Response(200, {"success": True, "obj": wire("new-sub")}))
    adapter = make(success_session)
    result = asyncio.run(adapter.rotate_subscription_url("a@example.test", "new-sub", "op-1"))
    assert result.state == "success"
    method, url, kwargs = success_session.calls[1]
    assert method == "POST" and url.endswith("/clients/update/a%40example.test")
    assert kwargs["json"]["subId"] == "new-sub" and kwargs["json"]["enable"] is True
    assert kwargs["json"]["totalGB"] == 1000 and kwargs["json"]["expiryTime"] == 1_800_000_000_000
    old_session = Session(Response(200, {"success": True, "obj": wire()}), Response(200, {"success": True}),
                          Response(200, {"success": True, "obj": wire("old")}))
    assert asyncio.run(make(old_session).rotate_subscription_url("a@example.test", "candidate", "op-2")).state == "unknown"
    timeout_session = Session(Response(200, {"success": True, "obj": wire()}), TimeoutError("timeout"))
    assert asyncio.run(make(timeout_session).rotate_subscription_url("a@example.test", "candidate", "op-3")).state == "unknown"
    no_preflight = XUIOpenAPIAdapter({"id": 9, "raw_url": "https://p.test/panel/api-docs", "auth_cipher": "cipher"},
                                    Session(Response(200, {"success": True, "obj": wire()})),
                                    decryptor=lambda *_: "TOKEN", transaction_probe=lambda: False, account_id_resolver=lambda *_: 303)
    denied = asyncio.run(no_preflight.rotate_subscription_url("a@example.test", "candidate", "op-4"))
    assert denied.state == "definite_failure" and denied.evidence["network_call_made"] is False
    assert network_attempts == []
