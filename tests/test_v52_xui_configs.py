import asyncio
from panel_adapters.base import AdapterUnavailable
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
    def __init__(self, *responses): self.responses, self.calls = list(responses), []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception): raise item
        return item

def make(session):
    return XUIOpenAPIAdapter({"id": 2, "raw_url": "https://p.test/panel/api-docs", "auth_cipher": "x"}, session,
                             decryptor=lambda *_: "TOKEN", transaction_probe=lambda: False, account_id_resolver=lambda *_: 303)

def test_configs_and_missing_metrics(monkeypatch):
    _, network_attempts = _guard_offline(monkeypatch)
    s = Session(Response({"success": True, "obj": [
        "vless://uuid-secret@node.test:443?security=tls#Phone",
        "vmess://encoded-secret@node.test:443#Laptop",
    ]}), Response({"success": True, "obj": {"xray": {"state": "running", "version": "x"}}}))
    a = make(s)
    nodes = asyncio.run(a.get_client_configs("user@example.test"))
    assert [n.protocol for n in nodes] == ["vless", "vmess"]
    assert nodes[0].label == "Phone" and "uuid-secret" not in repr(nodes[0])
    status = asyncio.run(a.verify_connection())
    assert status.reachable and status.cpu_percent is None
    assert status.ram_used_bytes is None and status.ram_total_bytes is None
    bad = make(Session(Response({"success": True, "obj": ["ftp://bad.test/x"]})))
    try:
        asyncio.run(bad.get_client_configs("user@example.test"))
    except AdapterUnavailable:
        pass
    else:
        raise AssertionError("unsupported config protocol accepted")
    assert "TOKEN" not in repr(a)
    assert network_attempts == []
