"""S54 (Batch #04) regression tests — service-card contract, ghost button, live traffic.

Offline only: no panel, no Telegram, no network. Every panel interaction is a
fake adapter or a queued fake HTTP session.
"""
import asyncio
from types import SimpleNamespace

import account_card_service
import internal_panel_compat as compat
import user_portal
from panel_adapters.base import LookupResult
from panel_adapters.models import CanonicalClient
from panel_adapters.xui_openapi import XUIOpenAPIAdapter

_EMAIL = "acc-303@example.test"
_BINDING = {"id": 303, "panel_id": 4, "email": _EMAIL}


def _client(**overrides):
    base = dict(
        ref=(303, 4, _EMAIL),
        enabled=True,
        quota_mode="finite",
        quota_bytes=20 * 1024 ** 3,
        used_bytes=2 * 1024 ** 3,
        time_mode="dated",
        expires_at=1_900_000_000,
        first_connected_at=None,
        last_connected_at=None,
        client_app=None,
        subscription_id="panel-sub-id",
        credential_revision=None,
        observed_at=1_800_000_000,
        adapter_revision="panel-r9",
    )
    base.update(overrides)
    return CanonicalClient(**base)


def _lookup_found(client):
    return lambda *a, **k: (
        dict(_BINDING),
        {"state": "found", "client": client, "info": compat._canonical_to_legacy(client)},
    )


# --------------------------------------------------------------------------
# 1. The facade must speak the legacy ``parse_client_info`` contract.
# --------------------------------------------------------------------------

def test_canonical_to_legacy_emits_full_legacy_contract():
    legacy = compat._canonical_to_legacy(_client(), dict(_BINDING))
    for key in ("email", "group", "enabled", "is_expired", "is_depleted", "total_gb",
                "used_gb", "remaining_gb", "expiry_str", "days_left", "is_relative",
                "last_online", "inbound_ids", "sub_id", "raw"):
        assert key in legacy, f"legacy key missing: {key}"
    assert legacy["sub_id"] == "panel-sub-id"
    assert legacy["total_gb"] == 20.0
    assert legacy["used_gb"] == 2.0
    assert legacy["remaining_gb"] == 18.0
    assert legacy["is_expired"] is False
    assert legacy["is_depleted"] is False
    assert legacy["enabled"] is True
    assert legacy["raw"]["subId"] == "panel-sub-id"
    # canonical aliases kept so the newer contract keeps working
    assert legacy["state"] == "found"
    assert legacy["enable"] is True
    assert legacy["totalGB"] == 20.0


def test_canonical_to_legacy_flags_depleted_and_expired():
    depleted = compat._canonical_to_legacy(
        _client(used_bytes=20 * 1024 ** 3), dict(_BINDING)
    )
    assert depleted["is_depleted"] is True
    assert depleted["remaining_gb"] == 0.0
    expired = compat._canonical_to_legacy(
        _client(expires_at=1_000_000_000), dict(_BINDING)
    )
    assert expired["is_expired"] is True
    assert expired["days_left"] is not None and expired["days_left"] < 0


def test_unlimited_quota_reports_unlimited_remaining():
    legacy = compat._canonical_to_legacy(
        _client(quota_mode="unlimited", quota_bytes=None), dict(_BINDING)
    )
    assert legacy["remaining_gb"] == "Unlimited"
    assert legacy["is_depleted"] is False


# --------------------------------------------------------------------------
# 2. inspect_client / find_client_by_code / get_client_links envelopes.
# --------------------------------------------------------------------------

def test_inspect_client_returns_legacy_status_envelope(monkeypatch):
    monkeypatch.setattr(compat, "_lookup", _lookup_found(_client()))
    insp = compat.inspect_client(_EMAIL, scope="all", by_code_only=True)
    assert insp["status"] == "ok"
    assert insp["found"] is True
    assert insp["state"] == "found"
    assert insp["data"]["sub_id"] == "panel-sub-id"
    assert "raw" in insp["data"]


def test_inspect_client_not_found_uses_status_key(monkeypatch):
    monkeypatch.setattr(compat, "_lookup", lambda *a, **k: (None, {"state": "not_found"}))
    insp = compat.inspect_client("missing@example.test")
    assert insp["status"] == "not_found"
    assert insp["found"] is False
    assert insp["data"] is None


def test_find_client_by_code_matches_legacy_shape(monkeypatch):
    monkeypatch.setattr(compat, "_lookup", _lookup_found(_client()))
    found = compat.find_client_by_code(_EMAIL)
    assert found["found"] is True
    assert found["data"]["email"] == _EMAIL
    assert found["data"]["raw"]["email"] == _EMAIL


def test_get_client_links_returns_plain_uri_list(monkeypatch):
    class _Adapter:
        async def get_client_configs(self, identifier):
            return [SimpleNamespace(uri="vless://one"), SimpleNamespace(uri="vless://two")]

    monkeypatch.setattr(compat, "_lookup", _lookup_found(_client()))
    monkeypatch.setattr(
        compat, "_factory",
        lambda: SimpleNamespace(resolve_account_adapter=lambda _id: _Adapter()),
    )
    links = compat.get_client_links(_EMAIL)
    assert isinstance(links, list)
    assert links == ["vless://one", "vless://two"]


def test_get_client_links_empty_list_when_unresolved(monkeypatch):
    monkeypatch.setattr(compat, "_lookup", lambda *a, **k: (None, {"state": "unknown"}))
    assert compat.get_client_links("nope") == []


# --------------------------------------------------------------------------
# 3. format_status_report must accept both contract generations.
# --------------------------------------------------------------------------

def test_format_status_report_accepts_both_shapes():
    legacy = compat._canonical_to_legacy(_client(), dict(_BINDING))
    canonical = compat.parse_client_info({
        "email": "a@example.test", "enable": True, "totalGB": 20,
        "expiryTime": 0, "traffic": {"up": 0, "down": 0},
    })
    for shape in (legacy, canonical):
        text = compat.format_status_report(shape, is_admin=False)
        assert "در دسترس نیست" not in text
        assert "حجم کل" in text
        assert "فعال" in text
    assert "2026" in compat.format_status_report(legacy, is_admin=False) or \
        "20" in compat.format_status_report(legacy, is_admin=False)


# --------------------------------------------------------------------------
# 4. Live traffic must be merged from clients/traffic when clients/get omits it.
# --------------------------------------------------------------------------

class _Response:
    def __init__(self, status, payload):
        self.status_code, self._payload = status, payload

    def json(self):
        return self._payload


class _Session:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _adapter(session):
    return XUIOpenAPIAdapter(
        {"id": 4, "raw_url": "https://p.test/panel/api-docs", "auth_cipher": "x"}, session,
        decryptor=lambda *_: "FAKE_TOKEN", transaction_probe=lambda: False,
        account_id_resolver=lambda *_: 303,
    )


def test_get_client_merges_live_traffic_when_record_has_none():
    record = {"email": _EMAIL, "enable": True, "totalGB": 20 * 1024 ** 3,
              "expiryTime": 1_900_000_000_000, "subId": "panel-sub-id"}
    session = _Session(
        _Response(200, {"success": True, "obj": record}),
        _Response(200, {"success": True, "obj": {"up": 512, "down": 1024,
                                                 "total": 20 * 1024 ** 3}}),
    )
    found = asyncio.run(_adapter(session).get_client(_EMAIL))
    assert found.state == "found"
    assert found.client.used_bytes == 1536
    assert session.calls[1][1].endswith("clients/traffic/acc-303%40example.test")


def test_get_client_does_not_refetch_when_traffic_already_present():
    record = {"email": _EMAIL, "enable": True, "totalGB": 1024,
              "expiryTime": 1_900_000_000_000, "subId": "s",
              "traffic": {"up": 10, "down": 20}}
    session = _Session(_Response(200, {"success": True, "obj": record}))
    found = asyncio.run(_adapter(session).get_client(_EMAIL))
    assert found.client.used_bytes == 30
    assert len(session.calls) == 1


# --------------------------------------------------------------------------
# 5. BUG-DB-001 / BUG-UI-003: no ghost per-account ticket button in the menu.
# --------------------------------------------------------------------------

def _portal_update(sent):
    class _Msg:
        message_thread_id = None

        async def reply_text(self, text, **kwargs):
            sent["text"], sent["markup"] = text, kwargs.get("reply_markup")

    user = SimpleNamespace(id=88001, first_name="Ali")
    chat = SimpleNamespace(id=88001)
    return SimpleNamespace(
        effective_user=user, effective_chat=chat, callback_query=None,
        message=_Msg(), effective_message=SimpleNamespace(message_thread_id=None),
    )


def test_portal_main_menu_has_no_per_account_ticket_buttons(monkeypatch):
    linked = [{"id": 303, "account_code": "default-pane-768db29180"},
              {"id": 304, "account_code": "Hst-Ae3x-abc"}]
    monkeypatch.setattr(user_portal.database, "get_user_role", lambda *a, **k: ("end_user", None))
    monkeypatch.setattr(user_portal, "user_portal_access", lambda _role: True)
    monkeypatch.setattr(user_portal, "ensure_portal_customer_rows", lambda _user: None)
    monkeypatch.setattr(user_portal.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(user_portal.database, "get_user_wallet_balance", lambda _uid: 0)
    monkeypatch.setattr(user_portal.database, "get_user_linked_accounts", lambda _uid: linked)
    monkeypatch.setattr(user_portal, "_v52_ui_enabled", lambda: True)

    sent = {}
    asyncio.run(user_portal.show_user_portal(_portal_update(sent), SimpleNamespace(user_data={})))
    callbacks = [b.callback_data for row in sent["markup"].inline_keyboard for b in row]
    assert not any(c.startswith("v52:support:") for c in callbacks)
    assert not any("default-pane" in b.text for row in sent["markup"].inline_keyboard for b in row)
    assert "v52:tickets" in callbacks


def test_account_card_offers_per_service_support_button(monkeypatch):
    monkeypatch.setattr(account_card_service.config, "ADMIN_TELEGRAM_ID", 9001)

    class _Adapter:
        async def get_client(self, identifier):
            return LookupResult("found", _client())

    class _Factory:
        def resolve_account_adapter(self, account_id):
            return _Adapter()

    account = {"id": 303, "account_code": "ACC-303", "email": _EMAIL}
    card = asyncio.run(account_card_service.build_account_card(
        {"actor_tg_id": 202, "persona": "user"}, 303,
        adapter_factory=_Factory(), account_loader=lambda _id: account,
        authorizer=lambda *a: True, now=1_800_000_200,
    ))
    callbacks = [b.callback_data for row in card.reply_markup.inline_keyboard for b in row]
    assert "v52:support:303" in callbacks
