from __future__ import annotations

import socket
import urllib.request

import bot_admin_ui
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


def test_token_never_in_confirmation(monkeypatch):
    attempts = _guard_network(monkeypatch)
    root = {"actor_tg_id": 9901, "chat_type": "private"}
    secret = "123456:SECRET_SYNTHETIC_TOKEN"
    start = bot_admin_ui.begin_register_bot(
        root, 22, declared_bot_id=123456, token=secret, chat_type="private"
    )
    preview = bot_admin_ui.preview_bot_activation(
        root,
        {"bot_id": 123456, "username": "synthetic_bot", "rep_id": 22,
         "status": "pending", "token": secret,
         "confirmation": {"callback_ok": "v52:ok:abcdefghijklmnopqrstuvwx",
                          "callback_no": "v52:no:abcdefghijklmnopqrstuvwx"}},
    )

    assert start.kind == "form" and secret not in start.text
    assert preview.kind == "preview"
    assert "123456" in preview.text and "synthetic_bot" in preview.text
    assert secret not in preview.text
    assert secret not in repr(preview.reply_markup)
    assert attempts == []
