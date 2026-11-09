import asyncio
from panel_adapters.xui_openapi import XUIOpenAPIAdapter


class R:
    def __init__(self, status, payload):
        self.status_code, self.payload = status, payload

    def json(self):
        return self.payload


class S:
    def __init__(self, *r):
        self.r, self.calls = list(r), []

    def request(self, m, u, **kw):
        self.calls.append((m, u, kw))
        return self.r.pop(0)


def wire(q=10, e=1_800_000_000_000, up=2, down=3, expiry=None):
    return {
        "email": "a@example.test", "enable": True, "subId": "s",
        "totalGB": q, "expiryTime": e if expiry is None else expiry,
        "traffic": {"up": up, "down": down}, "comment": "keep",
        "limitIp": 1, "reset": 0, "resetDay": 0, "resetMax": 0,
        "resetWeekday": 0, "security": "auto", "tgId": 1, "id": "u",
    }


def adapter(session, clock=lambda: 1_700_000_000):
    return XUIOpenAPIAdapter(
        {"id": 1, "raw_url": "https://p.test", "auth_cipher": "C"}, session,
        decryptor=lambda *_: "T", transaction_probe=lambda: False,
        account_id_resolver=lambda *_: 101, clock=clock,
    )


def test_addon_preserves_period_and_usage():
    s = S(
        R(200, {"success": True, "obj": wire()}),
        R(200, {"success": True, "obj": {"adjusted": 1}}),
        R(200, {"success": True, "obj": wire(q=15, e=1_800_000_000_000 + 3 * 86400)}),
    )
    result = asyncio.run(adapter(s).add_addon("a@example.test", {"add_bytes": 5, "add_days": 3}, "op"))
    assert result.state == "success" and result.expected_effect["add_bytes"] == 5


def test_overquota_addon_supplies_purchased_headroom_without_resetting_usage():
    s = S(
        R(200, {"success": True, "obj": wire(q=10, up=12, down=0)}),
        R(200, {"success": True, "obj": {"adjusted": 1}}),
        R(200, {"success": True, "obj": wire(q=17, up=12, down=0)}),
    )
    result = asyncio.run(adapter(s).add_traffic_and_days("a@example.test", 5, 0))
    assert result.state == "success"
    assert s.calls[1][2]["json"]["addBytes"] == 7
    assert result.expected_effect["quota_bytes"] == 17


def test_onhold_addon_allows_volume_but_rejects_days_before_network():
    s = S(R(200, {"success": True, "obj": wire(up=4, down=1, expiry=-1)}))
    result = asyncio.run(adapter(s).add_traffic_and_days("a@example.test", 5, 1))
    assert result.state == "definite_failure"
    assert len(s.calls) == 1
