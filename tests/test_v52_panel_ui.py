from __future__ import annotations

import socket
import urllib.request

import config
import panel_admin_ui


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


def test_new_panel_not_implicitly_sold(monkeypatch):
    attempts = _guard_network(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9001)
    root = {"actor_tg_id": 9001}
    start = panel_admin_ui.begin_add_panel(root)
    assert start.kind == "wizard"
    preview = panel_admin_ui.preview_panel_config(
        root, "3x-ui", "https://panel.example.test:2053/prefix/api-docs",
        name="Synthetic panel", inbound_json='[11, 12]',
        auth_secret="SYNTHETIC_SECRET_DO_NOT_RENDER",
    )
    assert preview.kind == "preview"
    assert "https://panel.example.test:2053/prefix" in preview.text
    assert "پنل پیش‌فرض: خیر" in preview.text
    assert "اتصال به پلن‌ها: هیچ‌کدام" in preview.text
    assert "SYNTHETIC_SECRET_DO_NOT_RENDER" not in preview.text
    assert "جدا از تراکنش DB" in preview.text
    assert attempts == []
