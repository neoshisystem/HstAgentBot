import pytest

from services.renewal_calculator import RenewalCalculationError, calculate_addon, calculate_renewal

GIB = 1024 ** 3
NOW = 1_900_000_000


def client(*, used=12 * GIB, quota=10 * GIB, expiry=NOW - 86400,
           quota_mode="finite", time_mode="dated"):
    return {
        "used_bytes": used, "quota_bytes": quota, "expires_at": expiry,
        "quota_mode": quota_mode, "time_mode": time_mode, "enabled": True,
        "subscription_id": "sub-1",
    }


def plan(quota=5 * GIB, days=30, quota_mode="finite"):
    return {"quota_bytes": quota, "days": days, "quota_mode": quota_mode}


def test_reset_from_now_and_burn_replace_are_independent():
    current = client()
    for method in ("reset_from_now", "burn_and_replace"):
        target = calculate_renewal(current, plan(), method, NOW)
        assert target["used_bytes"] == 0
        assert target["quota_bytes"] == 5 * GIB
        assert target["expires_at"] == NOW + 30 * 86400


def test_add_to_expiry_preserves_usage_and_quota_and_uses_later_clock():
    current = client(used=3 * GIB, quota=10 * GIB, expiry=NOW + 100)
    target = calculate_renewal(current, plan(0, 10), "add_to_expiry", NOW)
    assert target["used_bytes"] == 3 * GIB
    assert target["quota_bytes"] == 10 * GIB
    assert target["expires_at"] == NOW + 100 + 10 * 86400


def test_rollover_adds_to_old_quota_not_remaining_quota():
    current = client(used=12 * GIB, quota=10 * GIB, expiry=NOW - 86400)
    target = calculate_renewal(current, plan(), "rollover_volume", NOW)
    assert target["used_bytes"] == 12 * GIB
    assert target["quota_bytes"] == 15 * GIB
    assert target["expires_at"] == NOW + 30 * 86400


def test_expired_and_on_hold_states_are_explicit():
    expired = client(expiry=NOW - 10 * 86400)
    target = calculate_renewal(expired, plan(1 * GIB, 7), "add_to_expiry", NOW)
    assert target["expires_at"] == NOW + 7 * 86400
    held = client(used=2 * GIB, quota=5 * GIB, expiry=None, time_mode="on_hold")
    target = calculate_renewal(held, plan(), "rollover_volume", NOW)
    assert target["time_mode"] == "on_hold" and target["expires_at"] is None
    with pytest.raises(RenewalCalculationError):
        calculate_addon(held, add_days=1, now=NOW)


def test_unlimited_states_never_become_finite():
    unlimited = client(used=None, quota=None, quota_mode="unlimited", expiry=None, time_mode="unlimited")
    reset = calculate_renewal(unlimited, plan(), "reset_from_now", NOW)
    assert reset["quota_mode"] == "unlimited" and reset["quota_bytes"] is None
    add = calculate_renewal(unlimited, plan(0, 30), "add_to_expiry", NOW)
    assert add["time_mode"] == "unlimited" and add["expires_at"] is None
