from __future__ import annotations

import socket
import urllib.request

import config
import finance_admin_ui


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


def test_scope_and_price_configuration(monkeypatch):
    attempts = _guard_network(monkeypatch)
    root = {"actor_tg_id": 9901}
    rep = {"actor_tg_id": 7002, "rep_id": 22, "persona": "rep"}

    root_card = finance_admin_ui.edit_bank_card(
        root, {"id": 4, "merchant_key": "root", "card_number": "1111222233334444",
               "card_holder": "Root Holder", "bank_name": "Synthetic", "target_scope": "root"}
    )
    forbidden = finance_admin_ui.edit_bank_card(
        rep, {"id": 4, "merchant_key": "root", "card_number": "1111222233334444",
              "card_holder": "Root Holder", "target_scope": "root"}
    )
    all_preview = finance_admin_ui.edit_plan(
        root, {"plan_code": "legacy", "seller_key": "root"},
        panel_scope="all_explicit", current_panels=[{"id": 1, "name": "Panel A"}, {"id": 2, "name": "Panel B"}],
        confirm_all_explicit=False,
    )
    plan = finance_admin_ui.edit_plan(
        root, {"plan_code": "legacy", "seller_key": "root"},
        panel_scope="selected", selected_panel_ids=[2],
        allow_panels=["3X-UI", "X-UI"], deny_panels=["other"],
        sale_status="paused", tier="silver", custom_prices={"rep:22": 1200},
    )
    settlement = finance_admin_ui.preview_settlement(
        root, {"purpose": "rep_settlement", "edge_id": 7, "merchant_key": "root",
               "edge_debt_toman": 5000, "amount_toman": 3500, "retail_sales_toman": 999999}
    )

    all_callbacks = [b.callback_data for row in all_preview.reply_markup.inline_keyboard for b in row]
    assert root_card.kind == "preview" and "•••• •••• •••• 4444" in root_card.text
    assert forbidden.kind == "error"
    assert "Panel A" in all_preview.text and "Panel B" in all_preview.text
    assert any("all_explicit" in callback for callback in all_callbacks)
    assert "Sale status: paused" in plan.text
    assert "1200" in plan.text and "silver" in plan.text
    assert "5,000" in settlement.text and "3,500" in settlement.text
    assert "999999" not in settlement.text
    assert attempts == []
