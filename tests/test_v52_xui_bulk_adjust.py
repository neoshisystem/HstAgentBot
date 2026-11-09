import asyncio

from panel_adapters.xui_openapi import XUIOpenAPIAdapter


class Response:
    def __init__(self, payload): self.status_code, self._payload = 200, payload
    def json(self): return self._payload


class Session:
    def __init__(self, *responses): self.responses, self.calls = list(responses), []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def wire(quota=10, expiry=1_800_000_000):
    return {"email": "a@example.test", "enable": True, "subId": "sub", "totalGB": quota,
            "expiryTime": expiry * 1000, "traffic": {"up": 2, "down": 3}, "comment": "keep",
            "limitIp": 1, "reset": 0, "resetDay": 0, "resetMax": 0, "resetWeekday": 0,
            "security": "auto", "tgId": 1, "id": "uuid-1"}


def test_bulk_adjust_readback():
    s = Session(Response({"success": True, "obj": wire()}),
                Response({"success": True, "obj": {"adjusted": 1}}),
                Response({"success": True, "obj": wire(quota=15, expiry=1_800_000_000 + 3 * 86400)}))
    adapter = XUIOpenAPIAdapter({"id": 1, "raw_url": "https://p.test", "auth_cipher": "C"}, s,
                                decryptor=lambda *_: "TOKEN", transaction_probe=lambda: False,
                                account_id_resolver=lambda *_: 101)
    result = asyncio.run(adapter.add_traffic_and_days("a@example.test", 5, 3))
    assert result.state == "success"
    assert s.calls[1][1].endswith("/panel/api/clients/bulkAdjust")
    assert s.calls[1][2]["json"] == {"emails": ["a@example.test"], "addBytes": 5, "addDays": 3}
