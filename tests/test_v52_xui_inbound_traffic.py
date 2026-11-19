import asyncio
import pytest

from panel_adapters.xui_openapi import XUIOpenAPIAdapter

class Response:
    def __init__(self, payload): self.status_code, self._payload = 200, payload
    def json(self): return self._payload

class Session:
    def __init__(self, *responses): self.responses, self.calls = list(responses), []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

def test_get_inbound_traffic_uses_documented_get_route():
    s = Session(Response({"success": True, "obj": {"id": 7, "up": 100, "down": 250, "lastTrafficResetTime": 1234}}))
    adapter = XUIOpenAPIAdapter({"id": 1, "raw_url": "https://p.test", "auth_cipher": "C"}, s,
                                decryptor=lambda *_: "TOKEN", transaction_probe=lambda: False)
    result = asyncio.run(adapter.get_inbound_traffic(7))
    assert result["total_bytes"] == 350 and result["last_traffic_reset"] == 1234
    assert s.calls[0][1].endswith("/panel/api/inbounds/get/7")

def test_get_inbound_traffic_rejects_malformed_counter():
    s = Session(Response({"success": True, "obj": {"id": 7, "up": -1, "down": 1}}))
    adapter = XUIOpenAPIAdapter({"id": 1, "raw_url": "https://p.test", "auth_cipher": "C"}, s,
                                decryptor=lambda *_: "TOKEN", transaction_probe=lambda: False)
    with pytest.raises(Exception):
        asyncio.run(adapter.get_inbound_traffic(7))
