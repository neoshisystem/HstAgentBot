from __future__ import annotations

import socket
import urllib.request

import account_actions_ui
import config


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
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9901)
    return attempts


def test_root_only_modes_and_units(monkeypatch):
    attempts = _guard_network(monkeypatch)
    root = {"actor_tg_id": 9901}
    manager = {"actor_tg_id": 7002, "rep_id": 22, "persona": "rep"}

    finite = account_actions_ui.begin_traffic(root, 42, "set_quota_bytes", 3, unit="GiB")
    unlimited = account_actions_ui.begin_traffic(root, 42, "set_quota_bytes", unlimited=True)
    zero = account_actions_ui.begin_traffic(root, 42, "set_quota_bytes", 0, unit="GiB")
    denied = account_actions_ui.begin_traffic(manager, 42, "reset_usage")
    expiry = account_actions_ui.begin_expiry(root, 42, "set_expiry_utc", "2026-12-01T12:00:00")
    days = account_actions_ui.begin_expiry(root, 42, "set_remaining_days", 5)

    assert finite.kind == "preview" and "3221225472" in finite.text
    assert unlimited.kind == "preview" and "نامحدود" in unlimited.text
    assert zero.kind == "error"
    assert denied.kind == "error"
    assert "UTC epoch seconds" in expiry.text
    assert "5 روز" in days.text
    assert attempts == []
