"""W18.7: new renewals use immutable checkout while root exemption stays personal-only."""
from types import SimpleNamespace

import pytest

import rep_bot


@pytest.mark.parametrize(
    "account,expected",
    [
        ({"owner_tg_id": 7001, "seller_key": "root", "is_personal_use": 1}, True),
        ({"owner_tg_id": 7002, "seller_key": "root", "is_personal_use": 1}, False),
        ({"owner_tg_id": 7001, "seller_key": "root", "is_personal_use": 0}, False),
        ({"owner_tg_id": None, "seller_key": "root", "is_personal_use": 0}, False),
        ({"owner_tg_id": 7001, "seller_key": "rep:202", "is_personal_use": 1}, False),
    ],
)
def _root_exemption_matrix(account, expected):
    # Kept as a helper so the acceptance test can exercise every exemption edge.
    assert rep_bot._is_root_personal_renewal(
        SimpleNamespace(id=7001), "super_admin", account
    ) is expected


async def _renewal_result(account, monkeypatch, bridge_calls, legacy_calls, *,
                          role="super_admin", rep=None, user_id=7001,
                          confirmation_id=None, expired=False):
    monkeypatch.setattr(
        rep_bot.database, "get_account_renewal_modes",
        lambda *_: {"first_connect_mode": 0, "rollover_enabled": True},
    )
    monkeypatch.setattr(
        rep_bot.database, "get_plan",
        lambda _code: {"plan_code": "w18-20gb-30d", "name": "W18 plan",
                       "gb": 20.0, "days": 30, "rep_price": 450000, "is_active": 1},
    )
    monkeypatch.setattr(rep_bot.database, "get_pending_reservation", lambda *_: None)
    monkeypatch.setattr(rep_bot.database, "get_setting", lambda _key, default=None, **_kw: default)
    monkeypatch.setattr(rep_bot, "_build_account_keyboard", lambda *_a, **_kw: "legacy-keyboard")
    monkeypatch.setattr(rep_bot, "_runtime_flag", lambda name: name == "v52_renewal_bridge_enabled")

    async def checkout(*args, **kwargs):
        bridge_calls.append(kwargs.get("root_personal"))
        return "immutable checkout preview", "checkout-keyboard"

    monkeypatch.setattr(rep_bot, "_create_v52_renewal_checkout", checkout)

    def old_debit(**kwargs):
        legacy_calls.append(kwargs)
        return {"status": "busy"}

    monkeypatch.setattr(rep_bot.database, "begin_rep_renewal_intent", old_debit)
    monkeypatch.setattr(rep_bot.database, "begin_admin_renewal_confirmation", old_debit)

    monkeypatch.setattr(rep_bot.database, "check_rep_credit_limit", lambda *_: (True, 0, 999999))
    monkeypatch.setattr(rep_bot.database, "acquire_renewal_lock", lambda *_a, **_kw: True)
    user = SimpleNamespace(id=user_id, full_name="W18 actor", username="w18")
    return await rep_bot.process_account_renewal(
        user=user, role=role, rep=rep,
        acc_record={"id": 303, "account_code": "W18-A", "email": "w18@example.invalid", **account},
        client_info={"remaining_gb": 18.0, "days_left": 12, "enabled": True,
                     "is_expired": expired},
        gb=20.0, days=30.0, plan_code="w18-20gb-30d",
        effective_first=False, curr_comment="W18",
        confirmation_id=confirmation_id,
    )


def test_no_double_charge_and_root_exemption(monkeypatch):
    _root_exemption_matrix(
        {"owner_tg_id": 7001, "seller_key": "root", "is_personal_use": 1}, True
    )
    _root_exemption_matrix(
        {"owner_tg_id": None, "seller_key": "root", "is_personal_use": 0}, False
    )
    _root_exemption_matrix(
        {"owner_tg_id": 7001, "seller_key": "root", "is_personal_use": 0}, False
    )

    bridge_calls, legacy_calls = [], []
    commercial = {
        "owner_tg_id": 7001, "seller_key": "root", "is_personal_use": 0,
    }
    ok, message, keyboard = __import__("asyncio").run(
        _renewal_result(commercial, monkeypatch, bridge_calls, legacy_calls)
    )
    assert ok is True
    assert message == "immutable checkout preview"
    assert keyboard == "checkout-keyboard"
    assert bridge_calls == [False]
    assert legacy_calls == []

    personal = {
        "owner_tg_id": 7001, "seller_key": "root", "is_personal_use": 1,
    }
    ok, _message, _keyboard = __import__("asyncio").run(
        _renewal_result(personal, monkeypatch, bridge_calls, legacy_calls)
    )
    assert ok is True
    assert bridge_calls == [False, True]
    assert legacy_calls == []

    # A pre-existing representative confirmation still enters its already-debited
    # legacy intent path; it is not replayed as a second quote or charge.
    legacy_account = {
        "rep_id": 202, "owner_tg_id": 7101, "seller_key": "rep:202",
        "is_personal_use": 0,
    }
    before_bridge = list(bridge_calls)
    ok, message, _keyboard = __import__("asyncio").run(
        _renewal_result(
            legacy_account, monkeypatch, bridge_calls, legacy_calls,
            role="rep", rep={"id": 202, "scope_filter": "all"},
            user_id=7202, confirmation_id="legacy-intent", expired=True,
        )
    )
    assert ok is False
    assert "در حال پردازش" in message
    assert bridge_calls == before_bridge
    assert len(legacy_calls) == 1
    assert legacy_calls[0]["confirmation_id"] == "legacy-intent"
