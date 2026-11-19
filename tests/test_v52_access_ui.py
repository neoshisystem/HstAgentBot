from __future__ import annotations

import socket
import urllib.request

import access_ui


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


def test_grant_actor_and_preview(monkeypatch):
    attempts = _guard_network(monkeypatch)
    monkeypatch.setattr(
        access_ui.access_service, "authorize_account",
        lambda ctx, account_id, action: (
            action == "grant" and account_id == 71
            and isinstance(ctx, dict) and ctx.get("actor_tg_id") in {7001, 7002}
        ),
    )
    owner = {"actor_tg_id": 7001, "rep_id": None}
    grant = {"grantee_tg_id": 7003, "access_level": "manager", "revoked_at": None}
    page = access_ui.show_grants(owner, 71, grants=[grant], owner_tg_id=7001)
    assert page.kind == "access"
    assert "مالک اصلی: 7001" in page.text
    assert "کاربر 7003" in page.text
    assert "manager" in page.text
    callbacks = [b.callback_data for row in page.reply_markup.inline_keyboard for b in row]
    assert "v52:gset:71:viewer" in callbacks
    assert "v52:gset:71:manager" in callbacks
    assert "v52:grevoke:71:7003" in callbacks

    preview = access_ui.preview_revoke(owner, 71, 7003, grant=grant)
    assert preview.kind == "preview"
    assert "مالک اصلی و سایر grantها دست‌نخورده" in preview.text
    assert "v52:grevoke:71:7003" in [
        b.callback_data for row in preview.reply_markup.inline_keyboard for b in row
    ]

    denied = access_ui.show_grants({"actor_tg_id": 7999}, 71, grants=[grant], owner_tg_id=7001)
    assert denied.kind == "error"
    assert attempts == []
