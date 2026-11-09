import asyncio

from panel_adapters.xui_openapi import XUIOpenAPIAdapter


class Response:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


class Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def make(session):
    return XUIOpenAPIAdapter(
        {"id": 7, "raw_url": "https://p.test/panel/api-docs", "auth_cipher": "C"},
        session, decryptor=lambda *_: "TOKEN", transaction_probe=lambda: False,
        account_id_resolver=lambda *_: 101,
    )


def wire(quota=10, expiry=1_800_000_000_000, *, up=2, down=3, enabled=True):
    return {
        "email": "a@example.test", "enable": enabled, "subId": "sub",
        "totalGB": quota, "expiryTime": expiry, "traffic": {"up": up, "down": down},
        "comment": "keep", "limitIp": 1, "reset": 0, "resetDay": 0,
        "resetMax": 0, "resetWeekday": 0, "security": "auto",
        "tgId": 1, "id": "uuid-1", "inboundIds": [1],
    }


def test_renew_contract_and_partial_unknown():
    s = Session(
        Response(200, {"success": True, "obj": wire()}),
        Response(200, {"success": True}),
        Response(200, {"success": True, "obj": wire(quota=15, expiry=1_900_000_000_000)}),
        Response(200, {"success": True, "obj": {"up": 2, "down": 3}}),
    )
    result = asyncio.run(make(s).extend_client(
        "a@example.test",
        {"quota_mode": "finite", "quota_bytes": 15, "expires_at": 1_900_000_000, "used_bytes": 5},
        "op-1",
    ))
    assert result.state == "success"
    assert [call[0] for call in s.calls] == ["GET", "POST", "GET", "GET"]
    assert s.calls[1][2]["json"]["security"] == "auto"
    assert s.calls[1][2]["json"]["totalGB"] == 15
    assert s.calls[1][2]["json"]["expiryTime"] == 1_900_000_000_000
    assert not any("resetTraffic" in call[1] for call in s.calls)


def test_renewal_reset_usage_precedes_full_update_and_uses_post_reset_enable():
    s = Session(
        Response(200, {"success": True, "obj": wire(quota=10, up=12, down=0, enabled=False)}),
        Response(200, {"success": True}),
        Response(200, {"success": True, "obj": {"up": 0, "down": 0}}),
        Response(200, {"success": True, "obj": wire(quota=10, up=0, down=0, enabled=True)}),
        Response(200, {"success": True}),
        Response(200, {"success": True, "obj": wire(quota=5, expiry=1_900_000_000_000, up=0, down=0, enabled=True)}),
        Response(200, {"success": True, "obj": {"up": 0, "down": 0}}),
    )
    result = asyncio.run(make(s).extend_client(
        "a@example.test",
        {"quota_mode": "finite", "quota_bytes": 5, "expires_at": 1_900_000_000, "used_bytes": 0},
        "op-2",
    ))
    assert result.state == "success"
    assert s.calls[1][1].endswith("/panel/api/clients/resetTraffic/a%40example.test")
    update = next(call for call in s.calls if call[1].endswith("/panel/api/clients/update/a%40example.test"))
    assert update[2]["json"]["enable"] is True
    assert update[2]["json"]["security"] == "auto"
    assert s.calls[-1][1].endswith("/panel/api/clients/traffic/a%40example.test")


def test_update_4xx_is_unknown_because_partial_application_is_possible():
    s = Session(
        Response(200, {"success": True, "obj": wire()}),
        Response(400, {"success": False, "msg": "inbound 7: failed"}),
    )
    result = asyncio.run(make(s).set_expiry("a@example.test", 1_900_000_000, "op-3"))
    assert result.state == "unknown"
