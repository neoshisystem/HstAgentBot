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
        response = self.responses.pop(0)
        if isinstance(response, Exception): raise response
        return response

def adapter(session, transaction_probe=lambda: False):
    return XUIOpenAPIAdapter(
        {"id": 7, "raw_url": "https://example.test:2053/custom/panel/api-docs", "auth_cipher": "cipher-marker"},
        session, decryptor=lambda cipher, label: "SYNTHETIC_BEARER_SECRET",
        transaction_probe=transaction_probe,
    )

def test_bearer_basepath_no_redirect(monkeypatch):
    _, network_attempts = _guard_offline(monkeypatch)
    s = Session(Response(200, {"success": True, "obj": {"cpu": 12.5, "mem": {"current": 1, "total": 2}, "xray": {"state": "running", "version": "v-test"}}}), Response(302, {}))
    a = adapter(s)
    status = asyncio.run(a.verify_connection())
    method, url, kwargs = s.calls[0]
    assert method == "GET" and url == "https://example.test:2053/custom/panel/api/server/status"
    assert kwargs["headers"]["Authorization"] == "Bearer SYNTHETIC_BEARER_SECRET"
    assert kwargs["allow_redirects"] is False and kwargs["verify"] is True
    assert status.cpu_percent == 12.5 and status.ram_used_bytes == 1 and status.core_running is True
    redirect = asyncio.run(a._request("GET", "server/status"))
    assert redirect.status_code == 302 and len(s.calls) == 2
    blocked = adapter(Session(Response(200, {})), transaction_probe=lambda: True)
    try:
        asyncio.run(blocked._request("GET", "server/status"))
    except Exception:
        pass
    else:
        raise AssertionError("network request ran during a DB transaction")
    assert network_attempts == []
