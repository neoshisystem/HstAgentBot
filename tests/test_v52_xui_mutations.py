import asyncio

from panel_adapters.xui_openapi import XUIOpenAPIAdapter


class Response:
    def __init__(self, status, payload): self.status_code, self._payload = status, payload
    def json(self): return self._payload


class Session:
    def __init__(self, *responses): self.responses, self.calls = list(responses), []
    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs)); return self.responses.pop(0)


def adapter(session):
    return XUIOpenAPIAdapter({"id": 1, "raw_url": "https://p.test", "auth_cipher": "C"}, session,
                             decryptor=lambda *_: "TOKEN", transaction_probe=lambda: False,
                             account_id_resolver=lambda *_: 101)


def wire(enable=True):
    return {"email": "a@example.test", "enable": enable, "subId": "s", "totalGB": 10,
            "expiryTime": 1800000000000, "traffic": {"up": 1, "down": 2},
            "comment": "c", "limitIp": 1, "reset": 0, "resetDay": 0, "resetMax": 0,
            "resetWeekday": 0, "security": "auto", "tgId": 1, "id": "uuid"}


def test_create_collision_enable_delete():
    create = Session(Response(200, {"success": True}), Response(200, {"success": True, "obj": wire()}))
    result = asyncio.run(adapter(create).create_client({"email": "a@example.test", "quota_mode": "finite",
        "quota_bytes": 10, "inbound_ids": [7], "duration_seconds": 86400, "comment": "c"}))
    assert result.state == "success"
    assert create.calls[0][1].endswith("/panel/api/clients/add")

    enable = Session(Response(200, {"success": True}), Response(200, {"success": True, "obj": wire(True)}))
    result = asyncio.run(adapter(enable).set_client_enabled("a@example.test", True, "op-e"))
    assert result.state == "success"

    delete = Session(Response(200, {"success": True}), Response(404, {"success": False}))
    result = asyncio.run(adapter(delete).delete_client("a@example.test", "op-d"))
    assert result.state == "success"


def test_mutation_partial_is_unknown():
    s = Session(Response(200, {"success": True, "obj": wire()}), Response(400, {"success": False, "msg": "inbound 7: failed"}))
    result = asyncio.run(adapter(s).set_client_enabled("a@example.test", True, "op-x"))
    assert result.state == "unknown"
