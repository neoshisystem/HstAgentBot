from __future__ import annotations

import socket
import urllib.request

import account_actions_ui


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
    monkeypatch.setattr(account_actions_ui.access_service, "authorize_account", lambda *a: True)
    return attempts


def test_distinct_warnings_and_confirmation(monkeypatch):
    attempts = _guard_network(monkeypatch)
    ctx = {"actor_tg_id": 7001, "persona": "user"}
    confirmation = {
        "callback_ok": "v52:ok:abcdefghijklmnopqrstuvwx",
        "callback_no": "v52:no:abcdefghijklmnopqrstuvwx",
    }
    sub = account_actions_ui.preview_rotation(ctx, 91, "sub", confirmation=confirmation)
    cred = account_actions_ui.preview_rotation(ctx, 91, "cred", confirmation=confirmation)

    assert sub.kind == cred.kind == "preview"
    assert "دانلود شده" in sub.text
    assert "قطع قطعی ادعا نمی‌شود" in cred.text
    assert "قبلی" in sub.text and "اعتبارنامه" in cred.text
    callbacks = [b.callback_data for row in cred.reply_markup.inline_keyboard for b in row]
    assert confirmation["callback_ok"] in callbacks
    assert confirmation["callback_no"] in callbacks
    assert attempts == []
