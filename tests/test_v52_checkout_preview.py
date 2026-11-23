from __future__ import annotations

import socket
import urllib.request

import checkout_ui


def _guard_network(monkeypatch):
    attempts = []

    def blocked(name):
        def call(*args, **kwargs):
            attempts.append(name)
            raise AssertionError("network must remain disabled")
        return call

    monkeypatch.setattr(socket, "create_connection", blocked("socket.create_connection"))
    monkeypatch.setattr(socket.socket, "connect", blocked("socket.socket.connect"))
    monkeypatch.setattr(urllib.request, "urlopen", blocked("urllib.request.urlopen"))
    return attempts


def test_quote_preview_matches_execution(monkeypatch):
    attempts = _guard_network(monkeypatch)
    quote = {
        "quote_id": "a" * 32,
        "purpose": "vpn_renew",
        "amount_toman": 875,
        "price_edges": [
            {"seller_key": "root", "buyer_key": "user:7001", "price_toman": 875}
        ],
        "snapshot": {
            "spec": {
                "before": {"quota_bytes": 1073741824, "expires_at": 1900000000},
                "after": {"quota_bytes": 2147483648, "expires_at": 1902592000},
            },
            "before_after_renewal": {
                "before": {"quota_bytes": 1073741824, "expires_at": 1900000000},
                "after": {"quota_bytes": 2147483648, "expires_at": 1902592000},
            },
        },
        "coupon": {"code": "SAVE", "discount_toman": 125},
    }
    buy = checkout_ui.preview_buy({"actor_tg_id": 7001}, "basic", quote={**quote, "purpose": "vpn_buy"})
    renew = checkout_ui.preview_renew({"actor_tg_id": 7001}, 12, "basic", quote=quote)
    addon = checkout_ui.begin_addon({"actor_tg_id": 7001}, 12, "gib", 2, on_hold=True)
    addon_day = checkout_ui.begin_addon({"actor_tg_id": 7001}, 12, "day", 5, on_hold=True)

    assert buy.kind == "preview" and "875" in buy.text
    assert renew.kind == "preview" and "875" in renew.text
    assert "1073741824" in renew.text and "2147483648" in renew.text
    assert addon.kind == "preview" and "2 GiB" in addon.text
    assert addon_day.kind == "error"
    assert "1000" not in buy.text
    assert attempts == []
