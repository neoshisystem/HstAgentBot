from __future__ import annotations

import socket
import urllib.request

import config

import operations_ui


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


def _callbacks(page):
    return [b.callback_data for row in page.reply_markup.inline_keyboard for b in row]


def test_unknown_not_force_success(monkeypatch):
    attempts = _guard_network(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9901)
    root = {"actor_tg_id": 9901, "persona": "admin"}
    rows = [{"operation_id": "op-unknown", "state": "needs_reconciliation", "panel_id": 4,
             "request_cipher": "DO_NOT_SHOW_CIPHER", "access_token": "DO_NOT_SHOW_TOKEN"}]
    queue = operations_ui.show_operations(root, rows)
    assert queue.kind == "operations"
    assert "needs_reconciliation" in queue.text
    assert "DO_NOT_SHOW_CIPHER" not in queue.text and "DO_NOT_SHOW_TOKEN" not in queue.text
    assert "v52:reconcile:op-unknown" in _callbacks(queue)

    refund = {"refund_id": "rf-1", "user_tg_id": 101, "amount_toman": 700, "state": "unknown",
              "wallet_hold_state": "active"}
    unknown_refund = operations_ui.preview_refund(root, refund, amount_toman=700)
    assert unknown_refund.kind == "preview"
    assert "UNKNOWN" in unknown_refund.text and "hold حفظ می‌شود" in unknown_refund.text
    assert "posting" in unknown_refund.text
    mismatched = operations_ui.preview_refund(root, refund, amount_toman=699)
    assert mismatched.kind == "error"

    # Unknown topic cannot be re-created by sync; valid manual thread id only creates a repair preview.
    no_id = operations_ui.sync_topics(root, -100123, topic_key="supportreport", topic_state="unknown")
    assert no_id.kind == "error"
    repair = operations_ui.sync_topics(root, -100123, topic_key="supportreport", topic_state="unknown",
                                       thread_id=9912, permission_snapshot={"allowed": True})
    assert repair.kind == "preview" and "9912" in repair.text
    assert "ادعا نمی‌شود" in repair.text
    abuse_topic = operations_ui.sync_topics(root, -100123, topic_key="abusereport", topic_state="unknown", thread_id=9912)
    assert abuse_topic.kind == "error"
    assert operations_ui.show_operations({"actor_tg_id": 303, "persona": "rep"}, rows).kind == "error"
    assert attempts == []