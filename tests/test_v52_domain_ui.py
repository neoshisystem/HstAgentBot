from __future__ import annotations

import socket
import urllib.request

import domain_ui


def _guard_network(monkeypatch):
    attempts = []
    def blocked(kind):
        def call(*args, **kwargs):
            attempts.append(kind)
            raise AssertionError("network must remain disabled")
        return call
    monkeypatch.setattr(socket, "create_connection", blocked("socket.create_connection"))
    monkeypatch.setattr(socket.socket, "connect", blocked("socket.socket.connect"))
    monkeypatch.setattr(urllib.request, "urlopen", blocked("urllib.request.urlopen"))
    return attempts


def test_pending_not_active(monkeypatch):
    attempts = _guard_network(monkeypatch)
    pending = {
        "domain_id": 12, "rep_id": 202, "hostname": "brand.example.test",
        "challenge": "SYNTHETIC_TX_CHALLENGE_0123456789ABCDEF",
        "status": "pending", "verified_at": None, "tls_checked_at": None,
    }
    instructions = domain_ui.show_verification(pending, gateway_host="gateway.example.test")
    assert instructions.kind == "verification"
    assert "_hst-sub.brand.example.test" in instructions.text
    assert "SYNTHETIC_TX_CHALLENGE" in instructions.text
    assert "وضعیت فعلی: pending" in instructions.text
    page = domain_ui.preview_activation(
        {"actor_tg_id": 2020, "rep_id": 202}, pending, now=2000
    )
    assert page.kind == "error"
    assert "فعال‌سازی مجاز نیست" in page.text
    assert "v52:domainactivate:12" not in page.text
    assert attempts == []
