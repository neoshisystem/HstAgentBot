from __future__ import annotations

import socket
import urllib.request

import v52_router


def _guard_network(monkeypatch):
    attempts = []
    def blocked(name):
        def call(*args, **kwargs):
            attempts.append(name); raise AssertionError("network=0 violation")
        return call
    monkeypatch.setattr(socket, "create_connection", blocked("socket.create_connection"))
    monkeypatch.setattr(socket.socket, "connect", blocked("socket.socket.connect"))
    monkeypatch.setattr(urllib.request, "urlopen", blocked("urllib.request.urlopen"))
    return attempts


def test_every_callback_registered_and_isolated(monkeypatch):
    attempts = _guard_network(monkeypatch)
    callbacks = [
        "v52:home:user", "v52:sub:12", "v52:profiles", "v52:profile:3",
        "v52:padd:3:12", "v52:premove:3:12", "v52:pnew:0", "v52:plink:3",
        "v52:pqr:3", "v52:prot:3", "v52:ppause:3:1", "v52:pdel:3", "v52:pclear:3",
        "v52:prename:3", "v52:rotate:sub:44", "v52:traffic:44", "v52:expiry:44",
        "v52:grants:44", "v52:gset:44:viewer", "v52:grevoke:44:101", "v52:bot:55",
        "v52:domain:55", "v52:panel:7", "v52:life:7:drain", "v52:buy:basic",
        "v52:renew:44:basic", "v52:addon:44:gib", "v52:pay:order-1:card",
        "v52:receipt:approve:receipt-1", "v52:shop", "v52:goods:17", "v52:fulfill:order-1",
        "v52:support:44", "v52:abuse", "v52:ticket:ticket-1", "v52:escalate:ticket-1",
        "v52:transfer:44", "v52:ops", "v52:reconcile:op-1", "v52:refund:rf-1",
        "v52:forums:root", "v52:poolimport:7", "v52:inventory:12", "v52:q:abcdefghijklmnop",
        "v52:ok:abcdefghijklmnopqrstuvwx", "v52:no:abcdefghijklmnopqrstuvwx",
    ]
    routes = [v52_router.route_callback(value) for value in callbacks]
    assert all(isinstance(route, v52_router.Route) for route in routes)
    assert len(routes) == len(callbacks)
    assert next(x for x in routes if x.name == "shop").handler == "show_catalog"
    assert next(x for x in routes if x.name == "goods").params["product_id"] == 17
    assert next(x for x in routes if x.name == "abuse").scope.startswith("all actors")
    assert next(x for x in routes if x.name == "forums").handler == "sync_topics"
    assert v52_router.route_callback("v52:goods:17:forged").kind == "error"
    assert v52_router.route_callback("v52:goods:" + "9" * 60).kind == "error"
    assert v52_router.route_callback("x" * 65).kind == "error"

    fsm = v52_router.DraftFSM(ttl_seconds=60)
    ctx_a = {"bot_id": 9001, "actor_tg_id": 101, "chat_id": 101}
    ctx_b = {"bot_id": 9002, "actor_tg_id": 101, "chat_id": 101}
    fsm.set(ctx_a, "inventory_digital_batch", payload={"product_id": 12}, parent_callback="v52:inventory:12", now=100)
    fsm.set(ctx_b, "support_ticket_body", payload={"account_id": 44}, parent_callback="v52:support:44", now=100)
    text_a = v52_router.route_text(ctx_a, "LICENSE-LINE", now=101, fsm=fsm)
    text_b = v52_router.route_text(ctx_b, "Ticket body", now=101, fsm=fsm)
    assert text_a.handler == "inventory_digital_batch" and text_a.params["payload"]["product_id"] == 12
    assert text_b.handler == "support_ticket_body" and text_b.params["text"] == "Ticket body"
    assert v52_router.route_text(ctx_a, "legacy command", now=102, fsm=fsm) is False

    # Only explicit support states consume photos; an expired or unknown state falls through.
    fsm.set(ctx_a, "support_reply", payload={"ticket_id": "ticket-1"}, now=200)
    photo = v52_router.route_photo(ctx_a, "SYNTHETIC-FILE-ID", now=201, fsm=fsm)
    assert photo.handler == "support_reply"
    fsm.set(ctx_a, "goods_manual_secret", payload={}, now=300)
    assert v52_router.route_photo(ctx_a, "SYNTHETIC-FILE-ID", now=301, fsm=fsm) is False
    assert v52_router.route_text(ctx_a, "expired", now=400, fsm=fsm) is False
    assert attempts == []