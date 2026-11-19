"""S60 Task 3 — the service card, the QR delivery and the 360° service list.

One test per behaviour that was wrong or missing:

1. ``build_account_card`` falls back to the local ``accounts`` row when the panel is
   silent, and formats an epoch expiry as a date with the days remaining;
2. the injected-loader contract is preserved: a caller that supplies its own loader
   still sees the plain "unavailable" notice;
3. ``QRDelivery`` exposes ``status`` — ``rep_bot`` read ``.state`` and crashed on
   every QR press;
4. ``config_delivery_ui`` can build a config item from the subscription URL when the
   panel returns an empty list;
5. ``profile_360._services_page`` offers the status card and the config directly.

Offline only: no Telegram, no network, no live database.
"""
from __future__ import annotations

import asyncio

import pytest

import account_card_service
import config
import config_delivery_ui
import profile_360

NOW = 1_800_000_200
ACCOUNT_ID = 303
ROOT_ID = 777


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class _Client:
    def __init__(self, **fields):
        self.observed_at = fields.pop("observed_at", 1_800_000_123)
        for key, value in fields.items():
            setattr(self, key, value)


class _Lookup:
    def __init__(self, client=None, state="found"):
        self.state = state
        self.client = client


class _Adapter:
    def __init__(self, result):
        self._result = result

    async def get_client(self, email):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    async def get_client_configs(self, email):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    async def get_client_configs_page(self, email, cursor=None):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _Factory:
    def __init__(self, result):
        self._result = result

    def resolve_account_adapter(self, account_id):
        return _Adapter(self._result)


def _account_row(**overrides):
    row = {"id": ACCOUNT_ID, "account_code": "ACC-77", "email": "a@b.invalid",
           "enable": 1, "total_gb": 30, "expiry_time": 1_900_000_000, "sub_id": "SUB123"}
    row.update(overrides)
    return row


_MISSING = object()


def _card(factory, *, loader=_MISSING, account=None, authorizer=None):
    """Build a card.

    ``loader`` defaults to a stub returning ``account``; pass ``loader=None``
    explicitly to exercise the production default loader instead.
    """
    if loader is _MISSING:
        loader = (lambda _id: account)
    return asyncio.run(account_card_service.build_account_card(
        {"actor_tg_id": ROOT_ID, "persona": "admin"}, ACCOUNT_ID,
        adapter_factory=factory, account_loader=loader,
        authorizer=authorizer or (lambda *a: True), now=NOW))


@pytest.fixture(autouse=True)
def _root(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", ROOT_ID, raising=False)


# --------------------------------------------------------------------------- #
# 1. Date formatting
# --------------------------------------------------------------------------- #

def test_an_epoch_is_rendered_as_a_tehran_date():
    assert account_card_service._format_epoch(1_900_000_000) == "2030-03-17 21:16"


@pytest.mark.parametrize("value", [0, 999_999_999, None, "abc", ""])
def test_a_non_epoch_is_left_alone(value):
    assert account_card_service._format_epoch(value) == ""


def test_the_days_remaining_are_reported():
    assert account_card_service._remaining_days(1_900_000_000, NOW) == "1157 روز باقی‌مانده"
    assert account_card_service._remaining_days(1_700_000_000, NOW) == "1158 روز گذشته"


def test_a_fresh_card_shows_a_date_and_the_days_left():
    card = _card(_Factory(_Lookup(_Client(
        enabled=True, quota_mode="limited", quota_bytes=1024 ** 3, used_bytes=1024 ** 2,
        time_mode="limited", expires_at=1_900_000_000))), account=_account_row())
    assert "2030-03-17 21:16" in card.text
    assert "روز باقی‌مانده" in card.text


# --------------------------------------------------------------------------- #
# 2. The cached fallback
# --------------------------------------------------------------------------- #

def test_the_cached_row_is_shown_when_the_panel_is_silent(monkeypatch):
    monkeypatch.setattr(account_card_service, "_load_account", lambda _id: _account_row())
    card = _card(_Factory(RuntimeError("panel offline")), loader=None)
    assert card.status == "unavailable"
    assert "آخرین مقادیر ثبت‌شده" in card.text
    assert "فعال" in card.text
    assert "30 گیگابایت" in card.text


def test_a_caller_that_injects_its_loader_keeps_the_plain_notice():
    """The s54/s58 contract: an injected loader is testing the unavailable path."""
    card = _card(_Factory(RuntimeError("panel offline")), loader=lambda _id: _account_row())
    assert card.status == "unavailable"
    assert "آخرین مقادیر ثبت‌شده" not in card.text
    assert "مقادیر مصرف و انقضا نامعلوم‌اند." in card.text


def test_the_cached_lines_drop_what_the_row_does_not_have():
    assert account_card_service._cached_lines({"enable": 0}, NOW) == ["وضعیت: غیرفعال"]
    assert account_card_service._cached_lines({}, NOW) == []


# --------------------------------------------------------------------------- #
# 3. QRDelivery exposes status
# --------------------------------------------------------------------------- #

def test_the_qr_delivery_carries_a_status_field():
    delivery = config_delivery_ui.QRDelivery(1, 0, "uri", "uri", b"png", None, "ok")
    assert delivery.status == "ok"
    assert not hasattr(delivery, "state")


def test_rep_bot_reads_the_field_that_exists():
    from pathlib import Path
    source = (Path(__file__).resolve().parent.parent / "rep_bot.py").read_text("utf-8")
    assert "result.status == \"ok\"" in source
    assert "result.state == \"ok\"" not in source


# --------------------------------------------------------------------------- #
# 4. The subscription-URL fallback
# --------------------------------------------------------------------------- #

def test_a_config_item_is_built_from_the_subscription_url(monkeypatch):
    import sys, types
    fake = types.ModuleType("internal_panel_compat")
    fake.get_sub_url = lambda sub_id, account_ref=None: f"https://sub.invalid/hsubx/{sub_id}"
    monkeypatch.setitem(sys.modules, "internal_panel_compat", fake)
    item = config_delivery_ui._subscription_fallback({"id": 1, "sub_id": "SUB123"})
    assert item == {"uri": "https://sub.invalid/hsubx/SUB123", "label": "لینک اشتراک"}


def test_no_subscription_id_means_no_fallback():
    assert config_delivery_ui._subscription_fallback({"id": 1, "sub_id": ""}) is None


def test_the_first_page_falls_back_but_a_later_page_does_not(monkeypatch):
    monkeypatch.setattr(config_delivery_ui, "_subscription_fallback",
                        lambda account: {"uri": "https://sub.invalid/x", "label": "ل"})
    account = _account_row()
    first = asyncio.run(config_delivery_ui._fetch(
        ACCOUNT_ID, adapter_factory=_Factory([]),
        account_loader=lambda _id: account, authorizer=lambda *a: True, ctx={}))
    assert len(first[1]) == 1
    later = asyncio.run(config_delivery_ui._fetch(
        ACCOUNT_ID, adapter_factory=_Factory([]),
        account_loader=lambda _id: account, authorizer=lambda *a: True, ctx={},
        cursor="next"))
    assert later[1] == []


# --------------------------------------------------------------------------- #
# 5. The 360° service list
# --------------------------------------------------------------------------- #

def test_the_service_list_offers_the_card_and_the_config(monkeypatch):
    monkeypatch.setattr(profile_360.database, "get_user_accounts",
                        lambda user_id: [_account_row()])
    _text, rows = profile_360._services_page(4242, "repnet", "41")
    callbacks = [b.callback_data for row in rows for b in row]
    assert f"v52:account:{ACCOUNT_ID}" in callbacks
    assert f"v52:sub:{ACCOUNT_ID}" in callbacks
    # The contextual card is still there, so the profile remains the way back.
    assert any(cb.startswith(f"v52:p360acc:{ACCOUNT_ID}:4242") for cb in callbacks)
    assert "p360:view:4242:repnet:41" in callbacks
