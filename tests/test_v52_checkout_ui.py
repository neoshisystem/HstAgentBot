from __future__ import annotations

import socket
import urllib.request

import checkout_ui


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
    return attempts


def test_full_payment_and_pending_status(monkeypatch):
    attempts = _guard_network(monkeypatch)
    ctx = {"actor_tg_id": 7001}
    order = {"order_id": "ord-1", "actor_tg_id": 7001, "amount_toman": 900, "status": "awaiting_payment"}
    insufficient = checkout_ui.choose_rail(ctx, order, "wallet", wallet_balance=899)
    card = checkout_ui.choose_rail(ctx, order, "card_to_card")
    instructions = checkout_ui.preview_receipt(
        ctx,
        {"attempt_id": "attempt-1", "amount_toman": 900},
        order=order,
        bank_card={"card_number": "1111222233334444", "card_holder": "Test Holder", "bank_name": "Synthetic"},
    )
    pending = checkout_ui.show_order(
        ctx, order,
        payment={"status": "pending"},
        operation={"state": "not_started"},
        delivery={"state": "not_started"},
    )
    mixed = checkout_ui.choose_rail(ctx, order, "mixed", wallet_balance=1000)

    assert insufficient.kind == "error" and "موجودی ناکافی" in insufficient.text
    assert card.kind == "preview"
    assert instructions.kind == "payment" and "1111222233334444" in instructions.text
    assert "وضعیت پرداخت: pending" in pending.text
    assert "وضعیت عملیات: not_started" in pending.text
    assert "وضعیت تحویل: not_started" in pending.text
    assert mixed.kind == "error"
    assert attempts == []
