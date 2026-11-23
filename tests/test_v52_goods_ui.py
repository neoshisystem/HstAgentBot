from __future__ import annotations

import socket
import urllib.request

import config

import pytest

# Keep the selector runnable in a tiny offline harness without python-telegram-bot.
import goods_ui


def _guard_network(monkeypatch):
    attempts = []
    def blocked(name):
        def call(*args, **kwargs):
            attempts.append(name)
            raise AssertionError("network=0 violation")
        return call
    monkeypatch.setattr(socket, "create_connection", blocked("socket.create_connection"))
    monkeypatch.setattr(socket.socket, "connect", blocked("socket.socket.connect"))
    monkeypatch.setattr(urllib.request, "urlopen", blocked("urllib.request.urlopen"))
    return attempts


def _callbacks(page):
    return [button.callback_data for row in page.reply_markup.inline_keyboard for button in row]


def test_three_delivery_flows(tmp_path, monkeypatch):
    attempts = _guard_network(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9901)
    user = {"actor_tg_id": 101, "bot_id": 9001, "chat_id": 101, "chat_type": "private", "persona": "user", "merchant_key": "root"}
    products = [
        {"id": 1, "product_code": "AUTO-1", "title": "Auto code", "delivery_type": "auto_inventory", "user_price": 100, "rep_price": 80, "available_count": 0, "is_active": True, "seller_key": "root"},
        {"id": 2, "product_code": "FILE-1", "title": "File pack", "delivery_type": "file_document", "user_price": 120, "rep_price": 90, "file_bot_id": 9001, "file_telegram_id": "FILE_SECRET_MARKER", "is_active": True, "seller_key": "root"},
        {"id": 3, "product_code": "MANUAL-1", "title": "Manual setup", "delivery_type": "manual_fulfill", "user_price": 150, "rep_price": 110, "is_active": True, "seller_key": "root"},
    ]

    # App-shop entry is available to every persona, but zero-stock automatic goods are not purchasable.
    for persona in ("user", "rep", "admin"):
        ctx = {**user, "persona": persona}
        catalog = goods_ui.show_catalog(ctx, products)
        assert catalog.kind == "catalog"
        callbacks = _callbacks(catalog)
        assert "v52:goods:2" in callbacks and "v52:goods:3" in callbacks
        assert "v52:goods:1" not in callbacks
        assert "تومان" in catalog.text
        expected_price = "100" if persona == "user" else "80"
        assert expected_price in catalog.text

    quote = {
        "purpose": "digital_product_buy", "product_id": 2, "amount_toman": 120,
        "quote_nonce": "abcdefghijklmnopqrstuvwx", "snapshot": {"product_id": 2},
    }
    file_preview = goods_ui.preview_product(user, products[1], quote=quote)
    assert file_preview.kind == "preview"
    assert "120" in file_preview.text
    assert "FILE_SECRET_MARKER" not in file_preview.text
    assert "v52:q:abcdefghijklmnopqrstuvwx" in _callbacks(file_preview)

    wrong_bot = goods_ui.preview_product({**user, "bot_id": 9002}, products[1], quote=quote)
    assert wrong_bot.kind == "error"

    # Assigned merchant can claim one manual order; the token and payload never render.
    merchant_ctx = {**user, "actor_tg_id": 303, "merchant_key": "rep:55", "rep_id": 55}
    order = {
        "order_id": "ord-manual-55", "purpose": "digital_product_buy", "delivery_type": "manual_fulfill",
        "status": "accepted", "actor_tg_id": 101, "merchant_key": "rep:55", "product_title": "Manual setup",
    }
    claims, completions, resends = [], [], []
    def claim_service(order_id, operator_id, *, now=None):
        claims.append((order_id, operator_id))
        return {"order_id": order_id, "operator_id": operator_id, "claim_token": "NEVER_RENDER_THIS_CLAIM_TOKEN"}
    def complete_service(order_id, operator_id, payload, *, claim_token, now=None):
        completions.append((order_id, operator_id, payload, claim_token))
        return {"order_id": order_id, "state": "ready", "replayed": False}
    result = goods_ui.prepare_manual_delivery(
        merchant_ctx, order, secret_payload="MANUAL_SECRET_MARKER", confirm=True,
        claim_service=claim_service, complete_service=complete_service, now=200,
    )
    assert result.kind == "result"
    assert claims == [("ord-manual-55", 303)]
    assert completions == [("ord-manual-55", 303, "MANUAL_SECRET_MARKER", "NEVER_RENDER_THIS_CLAIM_TOKEN")]
    assert "MANUAL_SECRET_MARKER" not in result.text
    assert "NEVER_RENDER_THIS_CLAIM_TOKEN" not in result.text

    # A resend targets the same accepted order and never claims a fresh inventory item.
    def resend_service(order_id):
        resends.append(order_id)
        return {"order_id": order_id, "state": "delivered", "replayed": True}
    resend = goods_ui.prepare_manual_delivery(
        merchant_ctx, order, fulfillment={"state": "ready"}, confirm=True,
        resend_service=resend_service, now=201,
    )
    assert resend.kind == "delivery"
    assert resends == ["ord-manual-55"]
    assert claims == [("ord-manual-55", 303)]
    assert "v52:fulfill:ord-manual-55" in _callbacks(resend)

    unauthorized = goods_ui.prepare_manual_delivery(
        {**merchant_ctx, "merchant_key": "rep:56", "rep_id": 56}, order,
        secret_payload="OTHER_SECRET", confirm=True, claim_service=claim_service,
    )
    assert unauthorized.kind == "error"
    assert len(claims) == 1
    assert attempts == []