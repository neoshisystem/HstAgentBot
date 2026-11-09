import asyncio

from panel_adapters.xui_openapi import XUIOpenAPIAdapter


class Response:
    def __init__(self, status, payload): self.status_code, self._payload = status, payload
    def json(self): return self._payload


class Session:
    def __init__(self, *responses): self.responses, self.calls = list(responses), []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def make(session):
    return XUIOpenAPIAdapter({"id": 7, "raw_url": "https://p.test/panel/api-docs", "auth_cipher": "C"},
                             session, decryptor=lambda *_: "TOKEN", transaction_probe=lambda: False,
                             account_id_resolver=lambda *_: 101)


def wire(quota=10, expiry=1_800_000_000_000):
    return {"email": "a@example.test", "enable": True, "subId": "sub", "totalGB": quota,
            "expiryTime": expiry, "traffic": {"up": 2, "down": 3}, "comment": "keep",
            "limitIp": 1, "reset": 0, "resetDay": 0, "resetMax": 0, "resetWeekday": 0,
            "security": "auto", "tgId": 1, "id": "uuid-1", "inboundIds": [1]}


def test_extend_is_read_modify_write_and_preserves_security():
    s = Session(Response(200, {"success": True, "obj": wire()}), Response(200, {"success": True}),
                Response(200, {"success": True, "obj": wire(quota=15, expiry=1_900_000_000_000)}))
    result = asyncio.run(make(s).extend_client("a@example.test",
        {"quota_mode": "finite", "quota_bytes": 15, "expires_at": 1_900_000_000}, "op-1"))
    assert result.state == "success"
    payload = s.calls[1][2]["json"]
    assert payload["security"] == "auto" and payload["comment"] == "keep"
    assert payload["totalGB"] == 15 and payload["expiryTime"] == 1_900_000_000_000


def test_update_4xx_is_unknown_because_partial_application_is_possible():
    s = Session(Response(200, {"success": True, "obj": wire()}), Response(400, {"success": False, "msg": "inbound 7: failed"}))
    result = asyncio.run(make(s).set_expiry("a@example.test", 1_900_000_000, "op-2"))
    assert result.state == "unknown"
