from __future__ import annotations

import socket
import urllib.request

import config
import support_ui


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


def test_abuse_entry_and_escalation(monkeypatch):
    attempts = _guard_network(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9901)
    user = {"actor_tg_id": 101, "bot_id": 9001, "chat_id": 101, "chat_type": "private", "persona": "user"}
    rep = {"actor_tg_id": 303, "bot_id": 9001, "chat_id": 303, "chat_type": "private", "persona": "rep", "rep_id": 55}
    created, abuses, replies, escalations = [], [], [], []

    class FakeSupport:
        def create_ticket(self, ctx, account_id, body, **kwargs):
            created.append((account_id, body, kwargs))
            return {"ticket_id": "ticket-101", "assigned_key": "rep:55", "status": "unseen",
                    "snapshot": {"state": "unavailable", "unavailable": True}}
        def reply_ticket(self, ctx, ticket_id, body, **kwargs):
            replies.append((ticket_id, body, kwargs))
            return {"message_id": 7, "visibility": kwargs["visibility"]}
        def escalate_ticket(self, ctx, ticket_id, **kwargs):
            escalations.append(ticket_id)
            return {"ticket_id": ticket_id, "status": "escalated", "assigned_key": "root"}

    class FakeAbuse:
        def create_abuse_report(self, ctx, body, **kwargs):
            abuses.append((ctx["actor_tg_id"], body, kwargs))
            return {"ticket_id": f"abuse-{ctx['actor_tg_id']}", "status": "unseen"}

    ticket = support_ui.begin_ticket(user, 703, body="VPN down", submit=True, service=FakeSupport())
    assert ticket.kind == "result" and "unavailable" in ticket.text
    assert created[0][0:2] == (703, "VPN down")
    assert created[0][2]["panel_reader"] is None

    delete_request = support_ui.begin_ticket(user, 703, ticket_type="delete_request", submit=True,
                                             service=FakeSupport())
    assert delete_request.kind == "result"

    for ctx in (user, rep):
        result = support_ui.begin_abuse(ctx, "Abuse details", service=FakeAbuse())
        assert result.kind == "result" and "root خصوصی" in result.text
    group = support_ui.begin_abuse({**user, "chat_type": "group"}, "Group abuse", service=FakeAbuse())
    assert group.kind == "redirect"
    assert len(abuses) == 2

    denied = support_ui.reply_or_escalate(rep, "ticket-101", action="transfer", reason="Transfer ownership", confirm=True)
    assert denied.kind == "error"
    transfer = support_ui.reply_or_escalate({**user, "actor_tg_id": 9901, "persona": "admin"},
                                            "ticket-101", action="transfer", reason="Transfer ownership", confirm=True)
    assert transfer.kind == "preview" and "فراخوانی نمی‌شود" in transfer.text
    account_transfer = support_ui.reply_or_escalate(
        {**user, "actor_tg_id": 9901, "persona": "admin"}, None, account_id=703,
        action="transfer", reason="Transfer ownership", confirm=True,
    )
    assert account_transfer.kind == "preview" and "703" in account_transfer.text

    replied = support_ui.reply_or_escalate(rep, "ticket-101", action="reply", body="I am checking",
                                           confirm=True, service=FakeSupport())
    assert replied.kind == "result" and replies[0][2]["visibility"] == "customer"
    escalated = support_ui.reply_or_escalate(rep, "ticket-101", action="escalate", confirm=True,
                                             service=FakeSupport())
    assert escalated.kind == "result" and escalations == ["ticket-101"]
    assert attempts == []