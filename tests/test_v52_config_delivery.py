import asyncio
import socket
import urllib.request
from types import SimpleNamespace

import pytest

import config_delivery_ui


class FakeAdapter:
    def __init__(self, configs):
        self.configs = configs
        self.calls = []

    async def get_client_configs(self, identifier):
        self.calls.append(identifier)
        return self.configs


class FakeFactory:
    def __init__(self, adapter):
        self.adapter = adapter

    def resolve_account_adapter(self, account_id):
        return self.adapter


class FakeMessage:
    def __init__(self):
        self.documents = []
        self.photos = []
        self.texts = []

    async def reply_document(self, *, document, filename, caption=None):
        self.documents.append((filename, document.getvalue(), caption))

    async def reply_photo(self, **kwargs):
        self.photos.append(kwargs)

    async def reply_text(self, text, **kwargs):
        self.texts.append((text, kwargs))


def _callbacks(markup):
    return [item.callback_data for row in markup.inline_keyboard for item in row]


def test_two_configs_qr_and_long_payload(monkeypatch):
    attempts = []

    def blocked(*args, **kwargs):
        attempts.append((args, kwargs))
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(urllib.request, "urlopen", blocked)

    uri_one = "vless://uuid-one@node.example.invalid:443?security=tls#Phone"
    uri_two = "vmess://encoded-two@node.example.invalid:443#Laptop"
    adapter = FakeAdapter([
        SimpleNamespace(label="Phone", uri=uri_one),
        SimpleNamespace(label="Laptop", uri=uri_two),
    ])
    factory = FakeFactory(adapter)
    account_loader = lambda _id: {"id": 303, "email": "303@example.invalid",
                                   "account_code": "ACC-303"}
    allowed = lambda _ctx, account_id, action: account_id == 303 and action == "view_config"
    ctx = {"actor_tg_id": 101, "persona": "user", "chat_type": "private"}

    page = asyncio.run(config_delivery_ui.show_configs(
        ctx, 303, adapter_factory=factory, account_loader=account_loader,
        authorizer=allowed,
    ))
    assert len(page.configs) == 2
    assert f"<code>{uri_one}</code>" in page.text
    assert f"<code>{uri_two}</code>" in page.text
    callbacks = _callbacks(page.reply_markup)
    assert "v52:qr:303:0" in callbacks and "v52:qr:303:1" in callbacks
    assert "v52:allcfg:303" in callbacks
    assert all(uri_one not in item and uri_two not in item for item in callbacks)

    qr = asyncio.run(config_delivery_ui.deliver_config_qr(
        ctx, 303, 1, adapter_factory=factory, account_loader=account_loader,
        authorizer=allowed, qr_factory=lambda exact_uri: exact_uri.encode("utf-8"),
    ))
    assert qr.status == "ok"
    assert qr.uri == uri_two and qr.qr_data == uri_two
    assert qr.image == uri_two.encode("utf-8")

    invalid = asyncio.run(config_delivery_ui.deliver_config_qr(
        ctx, 303, 9, adapter_factory=factory, account_loader=account_loader,
        authorizer=allowed, qr_factory=lambda value: value.encode(),
    ))
    assert invalid.status == "invalid_index"
    assert "v52:home:user" in _callbacks(invalid.reply_markup)
    assert "v52:cfg:303" in _callbacks(invalid.reply_markup)

    long_uri = "vless://" + ("x" * 4100) + "@long.example.invalid:443"
    adapter.configs = [SimpleNamespace(label="Long", uri=long_uri)]
    message = FakeMessage()
    delivered = asyncio.run(config_delivery_ui.deliver_all_configs(
        ctx, 303, adapter_factory=factory, account_loader=account_loader,
        authorizer=allowed, message=message,
    ))
    assert delivered["status"] == "document"
    assert delivered["payload"] == long_uri.encode("utf-8")
    assert message.documents[0][1] == long_uri.encode("utf-8")

    adapter.calls.clear()
    with pytest.raises(PermissionError):
        asyncio.run(config_delivery_ui.show_configs(
            ctx, 303, adapter_factory=factory, account_loader=account_loader,
            authorizer=lambda *_: False,
        ))
    assert adapter.calls == []
    assert attempts == []
