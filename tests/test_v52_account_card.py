import asyncio
import socket
import urllib.request
from types import SimpleNamespace

from panel_adapters.base import LookupResult
import account_card_service


class FakeAdapter:
    def __init__(self, lookup):
        self.lookup = lookup
        self.calls = []

    async def get_client(self, identifier):
        self.calls.append(identifier)
        if isinstance(self.lookup, Exception):
            raise self.lookup
        return self.lookup


class Factory:
    def __init__(self, adapter):
        self.adapter = adapter
        self.calls = []

    def resolve_account_adapter(self, account_id):
        self.calls.append(account_id)
        return self.adapter


class FailedMessage:
    async def edit_text(self, *args, **kwargs):
        raise RuntimeError("synthetic Telegram edit failure")


def test_root_buttons_and_inplace_refresh(monkeypatch):
    network_attempts = []

    def blocked(*args, **kwargs):
        network_attempts.append((args, kwargs))
        raise AssertionError("network access is forbidden")

    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(urllib.request, "urlopen", blocked)
    monkeypatch.setattr(account_card_service.config, "ADMIN_TELEGRAM_ID", 9001)

    client = SimpleNamespace(
        enabled=True, quota_mode="limited", quota_bytes=10_000,
        used_bytes=2_500, time_mode="limited", expires_at=1_900_000_000,
        observed_at=1_800_000_123, subscription_id="fresh-panel-sub-id",
        adapter_revision="panel-r9",
    )
    adapter = FakeAdapter(LookupResult("found", client))
    factory = Factory(adapter)
    account = {"id": 303, "account_code": "ACC-303",
               "email": "303@example.invalid", "sub_id_cipher": "never-render-this"}

    def authorizer(ctx, account_id, action):
        assert account_id == 303
        if action == "view_config":
            return True
        if action in {"rotate", "renew"}:
            return ctx["actor_tg_id"] == 101 or ctx["actor_tg_id"] == 9001
        return ctx["actor_tg_id"] == 9001

    viewer = asyncio.run(account_card_service.build_account_card(
        {"actor_tg_id": 202, "persona": "user"}, 303,
        adapter_factory=factory, account_loader=lambda _id: account,
        authorizer=authorizer, now=1_800_000_200,
    ))
    assert viewer.status == "fresh"
    assert viewer.observed_at == 1_800_000_123
    assert viewer.snapshot["used_bytes"] == 2_500
    viewer_callbacks = [
        item.callback_data for row in viewer.reply_markup.inline_keyboard for item in row
    ]
    assert "v52:sub:303" in viewer_callbacks
    assert not any(item.startswith(("v52:traffic:", "v52:expiry:", "v52:renew:"))
                   for item in viewer_callbacks)

    owner = asyncio.run(account_card_service.build_account_card(
        {"actor_tg_id": 101, "persona": "user"}, 303,
        adapter_factory=factory, account_loader=lambda _id: account,
        authorizer=authorizer, now=1_800_000_200,
    ))
    owner_callbacks = [
        item.callback_data for row in owner.reply_markup.inline_keyboard for item in row
    ]
    assert "v52:renew:303" in owner_callbacks

    root = asyncio.run(account_card_service.build_account_card(
        {"actor_tg_id": 9001, "persona": "admin"}, 303,
        adapter_factory=factory, account_loader=lambda _id: account,
        authorizer=authorizer, now=1_800_000_200,
    ))
    root_callbacks = [
        item.callback_data for row in root.reply_markup.inline_keyboard for item in row
    ]
    assert {"v52:traffic:303", "v52:expiry:303"} <= set(root_callbacks)
    assert "fresh-panel-sub-id" not in root.text
    assert "never-render-this" not in root.text

    unavailable = Factory(FakeAdapter(RuntimeError("panel offline")))
    failed_refresh = asyncio.run(account_card_service.refresh_account_card(
        {"actor_tg_id": 202, "persona": "user"}, 303,
        message=FailedMessage(), adapter_factory=unavailable,
        account_loader=lambda _id: account, authorizer=authorizer,
        now=1_800_000_300,
    ))
    assert failed_refresh["status"] == "unavailable"
    assert failed_refresh["snapshot"]["used_bytes"] is None
    assert failed_refresh["snapshot"]["quota_bytes"] is None
    assert "نامعلوم" in failed_refresh["text"]
    assert "0 B" not in failed_refresh["text"]
    assert failed_refresh["edited"] is False
    assert failed_refresh["render_error"] == "telegram_edit_failed"
    assert network_attempts == []
