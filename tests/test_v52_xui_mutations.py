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
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def adapter(session, *, operation_preflight=lambda *_: True, clock=lambda: 1_700_000_000):
    return XUIOpenAPIAdapter(
        {"id": 1, "raw_url": "https://p.test", "auth_cipher": "C"},
        session, decryptor=lambda *_: "TOKEN", transaction_probe=lambda: False,
        account_id_resolver=lambda *_: 101, operation_preflight=operation_preflight,
        clock=clock,
    )


def wire(enable=True, *, quota=10, expiry=1_800_000_000_000,
         client_id="uuid", inbound_ids=None):
    return {
        "email": "a@example.test", "enable": enable, "subId": "s",
        "totalGB": quota, "expiryTime": expiry, "traffic": {"up": 1, "down": 2},
        "comment": "c", "limitIp": 0, "limitHwid": 0, "reset": 0,
        "resetDay": 0, "resetMax": 0, "resetWeekday": 0,
        "security": "auto", "tgId": 1, "id": client_id,
        "inboundIds": [7] if inbound_ids is None else inbound_ids,
    }


def create_spec():
    return {
        "operation_id": "op-create",
        "email": "a@example.test",
        "quota_mode": "finite",
        "quota_bytes": 10,
        "duration_seconds": 86400,
        "on_first_connect": False,
        "inbound_ids": [7],
        "comment": "c",
        "candidate_credentials": {"uuid": "uuid-create"},
        "enable": True,
    }


def test_create_collision_enable_delete():
    expiry = (1_700_000_000 + 86400) * 1000
    create = Session(
        Response(404, {"success": False}),
        Response(200, {"success": True}),
        Response(200, {"success": True, "obj": wire(
            quota=10, expiry=expiry, client_id="uuid-create", inbound_ids=[7]
        )}),
        Response(200, {"success": True, "obj": [
            {"id": 7, "settings": {"clients": [
                {"email": "a@example.test", "id": "uuid-create"}
            ]}}
        ]}),
    )
    result = asyncio.run(adapter(create).create_client(create_spec()))
    assert result.state == "success"
    assert [call[0] for call in create.calls] == ["GET", "POST", "GET", "GET"]
    assert create.calls[1][1].endswith("/panel/api/clients/add")
    sent = create.calls[1][2]["json"]
    assert sent["client"]["email"] == "a@example.test"
    assert sent["client"]["id"] == "uuid-create"
    assert sent["client"]["expiryTime"] == expiry
    assert sent["inboundIds"] == [7]
    assert create.calls[-1][1].endswith("/panel/api/inbounds/list")

    enable = Session(
        Response(200, {"success": True, "obj": wire(False)}),
        Response(200, {"success": True}),
        Response(200, {"success": True, "obj": wire(True)}),
    )
    result = asyncio.run(adapter(enable).set_client_enabled("a@example.test", True, "op-e"))
    assert result.state == "success"
    assert [call[0] for call in enable.calls] == ["GET", "POST", "GET"]

    delete = Session(
        Response(200, {"success": True, "obj": wire()}),
        Response(200, {"success": True}),
        Response(404, {"success": False}),
    )
    result = asyncio.run(adapter(delete).delete_client("a@example.test", "op-d"))
    assert result.state == "success"
    assert [call[0] for call in delete.calls] == ["GET", "POST", "GET"]


def test_create_rejects_existing_identifier_before_mutation():
    s = Session(Response(200, {"success": True, "obj": wire(client_id="foreign-id")}))
    result = asyncio.run(adapter(s).create_client(create_spec()))
    assert result.state == "definite_failure"
    assert result.evidence["reason"] == "external_identifier_conflict"
    assert len(s.calls) == 1 and s.calls[0][0] == "GET"


def test_create_timeout_after_dispatch_is_unknown():
    s = Session(Response(404, {"success": False}), TimeoutError("simulated lost response"))
    result = asyncio.run(adapter(s).create_client(create_spec()))
    assert result.state == "unknown"
    assert len(s.calls) == 2


def test_mutation_partial_is_unknown():
    s = Session(
        Response(200, {"success": True, "obj": wire()}),
        Response(400, {"success": False, "msg": "inbound 7: failed"}),
    )
    result = asyncio.run(adapter(s).set_client_enabled("a@example.test", True, "op-x"))
    assert result.state == "unknown"
