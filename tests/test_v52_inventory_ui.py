from __future__ import annotations

import socket
import urllib.request

import config

import inventory_admin_ui


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


def test_preview_before_import_and_scope(monkeypatch):
    attempts = _guard_network(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9901)
    root = {"actor_tg_id": 9901, "chat_type": "private", "persona": "admin"}
    uris = ["vless://SYNTHETIC-CREDENTIAL-ONE@node.invalid:443", "vmess://SYNTHETIC-CREDENTIAL-TWO@node.invalid:443"]
    repo_calls, digital_calls = [], []

    class FakeRepository:
        def import_client(self, **kwargs):
            repo_calls.append(kwargs)
            return {"id": 41}

    preview_pool = inventory_admin_ui.begin_manual_import(
        root, 7, "panel-7-batch", uris,
        encryptor=lambda text, label: "CIPHERTEXT:" + str(len(text)), repository=FakeRepository(),
    )
    assert preview_pool.kind == "preview" and "تعداد URI معتبر: 2" in preview_pool.text
    assert "SYNTHETIC-CREDENTIAL" not in preview_pool.text
    assert repo_calls == []

    saved_pool = inventory_admin_ui.begin_manual_import(
        root, 7, "panel-7-batch", uris, confirm=True, repository=FakeRepository(),
        encryptor=lambda text, label: "CIPHERTEXT:" + str(len(text)), now=100,
    )
    assert saved_pool.kind == "result" and len(repo_calls) == 1
    assert repo_calls[0]["actor_is_root"] is True and repo_calls[0]["config_cipher"].startswith("CIPHERTEXT:")
    assert all(secret not in repo_calls[0]["config_cipher"] for secret in uris)

    bad = inventory_admin_ui.begin_manual_import(
        root, 7, "bad-batch", [uris[0], "https://invalid.example/path"],
        confirm=True, repository=FakeRepository(), encryptor=lambda text, label: "cipher",
    )
    assert bad.kind == "error" and len(repo_calls) == 1

    digital_secrets = ["LICENSE-SECRET-A", "LICENSE-SECRET-B"]
    preview_digital = inventory_admin_ui.begin_digital_import(
        root, 12, digital_secrets,
        import_fn=lambda *a, **k: digital_calls.append((a, k)) or [1, 2],
    )
    assert preview_digital.kind == "preview" and "تعداد اقلام: 2" in preview_digital.text
    assert all(secret not in preview_digital.text for secret in digital_secrets)
    assert digital_calls == []

    saved_digital = inventory_admin_ui.begin_digital_import(
        root, 12, digital_secrets, confirm=True,
        import_fn=lambda *a, **k: digital_calls.append((a, k)) or [101, 102], now=200,
    )
    assert saved_digital.kind == "result" and len(digital_calls) == 1
    assert digital_calls[0][0][1] == digital_secrets

    nonroot = inventory_admin_ui.begin_digital_import(
        {"actor_tg_id": 303, "chat_type": "private", "persona": "rep"}, 12, digital_secrets,
        confirm=True, import_fn=lambda *a, **k: digital_calls.append((a, k)) or [3, 4],
    )
    group = inventory_admin_ui.begin_manual_import(
        {**root, "chat_type": "group"}, 7, "batch", uris,
        confirm=True, repository=FakeRepository(), encryptor=lambda text, label: "cipher",
    )
    assert nonroot.kind == group.kind == "error"
    assert len(digital_calls) == 1 and len(repo_calls) == 1
    assert attempts == []