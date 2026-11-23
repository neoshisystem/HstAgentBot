"""W18.8: every reservation uses its canonical panel adapter and persistent lock pair."""
from contextlib import contextmanager
from types import SimpleNamespace
import asyncio

import rep_bot
import reservation_service
import scheduler_service
from panel_adapters.base import LookupResult, MutationResult


def _client(account_id, panel_id, email, now):
    return SimpleNamespace(
        ref=(account_id, panel_id, email),
        enabled=True,
        quota_mode="finite",
        quota_bytes=10 * 1024**3,
        used_bytes=10 * 1024**3,
        time_mode="dated",
        expires_at=now + 86400,
        subscription_id=None,
    )


def test_each_panel_and_activation_lock(monkeypatch):
    now = 1_794_500_000
    reservations = [
        {"id": 701, "account_id": 301, "account_code": "W18-P1",
         "email": "same@example.invalid", "gb": 20, "days": 30,
         "on_first_connect": 0, "plan_name": "Panel one", "rep_id": None},
        {"id": 702, "account_id": 302, "account_code": "W18-P2",
         "email": "same@example.invalid", "gb": 30, "days": 30,
         "on_first_connect": 0, "plan_name": "Panel two", "rep_id": None},
        {"id": 703, "account_id": 303, "account_code": "W18-MAINT",
         "email": "maintenance@example.invalid", "gb": 10, "days": 30,
         "on_first_connect": 0, "plan_name": "Maintenance", "rep_id": None},
    ]
    bindings = {
        301: {"id": 301, "panel_id": 1, "email": "same@example.invalid",
              "account_code": "W18-P1", "lifecycle": "active"},
        302: {"id": 302, "panel_id": 2, "email": "same@example.invalid",
              "account_code": "W18-P2", "lifecycle": "active"},
        303: {"id": 303, "panel_id": 3, "email": "maintenance@example.invalid",
              "account_code": "W18-MAINT", "lifecycle": "maintenance"},
    }
    adapter_calls = []
    prepared_tickets = []
    finalized = []

    class FakeAdapter:
        def __init__(self, account_id, panel_id):
            self.account_id, self.panel_id = account_id, panel_id

        async def get_client(self, email):
            return LookupResult(
                "found",
                _client(self.account_id, self.panel_id, email, now),
            )

        async def renew_client(self, email, target, operation_id):
            adapter_calls.append((self.account_id, self.panel_id, email, operation_id))
            return MutationResult("success", {"outcome": "success"}, target)

    adapters = {
        301: FakeAdapter(301, 1),
        302: FakeAdapter(302, 2),
        303: FakeAdapter(303, 3),
    }

    @contextmanager
    def no_transaction_connection():
        yield object()

    monkeypatch.setattr(rep_bot.database, "get_db", no_transaction_connection)
    monkeypatch.setattr(rep_bot.database, "get_all_pending_reservations", lambda: reservations)
    monkeypatch.setattr(
        rep_bot.database, "get_account_panel_binding",
        lambda **kwargs: bindings.get(int(kwargs.get("account_id") or 0)),
    )
    monkeypatch.setattr(
        rep_bot.database, "get_panel_adapter_for_account",
        lambda account_id: adapters[int(account_id)],
    )
    monkeypatch.setattr(rep_bot.database, "get_rep_by_id", lambda _rep_id: None)
    monkeypatch.setattr(rep_bot, "send_admin_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(rep_bot.database, "get_setting", lambda _key, default=None, **_kw: default)

    monkeypatch.setattr(
        scheduler_service, "claim_period_job",
        lambda _conn, **kwargs: {"job_key": kwargs["target"], "lease_token": "scheduler-ticket"},
    )

    def prepare(conn, account_id, reservation_id, *, now):
        ticket = {
            "account_id": account_id,
            "reservation_id": reservation_id,
            "locks": [
                {"key": f"account:{account_id}", "token": f"A{account_id:030d}"},
                {"key": f"reservation-activation:{reservation_id}", "token": f"R{reservation_id:030d}"},
            ],
        }
        prepared_tickets.append(ticket)
        return ticket

    def finish(_conn, ticket, *, outcome):
        finalized.append((ticket, outcome))
        return {"state": outcome, "locks_retained": outcome == "unknown"}

    monkeypatch.setattr(reservation_service, "prepare_activation", prepare)
    monkeypatch.setattr(reservation_service, "finish_activation", finish)

    application = SimpleNamespace(bot=SimpleNamespace())
    result = asyncio.run(rep_bot._watchdog_cycle(application, now=now))

    assert result["read"] == 3
    assert result["triggered"] == 3
    assert result["activated"] == 2
    assert result["maintenance_skipped"] == 1
    assert result["unknown"] == 0
    assert sorted((item[0], item[1]) for item in adapter_calls) == [(301, 1), (302, 2)]
    assert len(prepared_tickets) == 2
    assert all(
        ticket["locks"][1]["key"] == f"reservation-activation:{ticket['reservation_id']}"
        for ticket in prepared_tickets
    )
    assert [outcome for _ticket, outcome in finalized] == ["success", "success"]
