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


def plan(quota=5 * GIB, days=30):
    return {"quota_bytes": quota, "days": days, "quota_mode": "finite"}


def test_reset_volume_time_formula():
    target = calculate_renewal(client(), plan(), "reset_volume_time", NOW)
    assert (target["used_bytes"], target["quota_bytes"]) == (0, 5 * GIB)
    assert target["expires_at"] == NOW + 30 * 86400


def test_reset_time_add_volume_uses_remaining_plus_purchase():
    target = calculate_renewal(client(used=3 * GIB, quota=10 * GIB), plan(), "reset_time_add_volume", NOW)
    assert (target["used_bytes"], target["quota_bytes"]) == (0, 12 * GIB)
    assert target["expires_at"] == NOW + 30 * 86400


def test_reset_volume_add_time_resets_usage_and_extends_later_expiry():
    target = calculate_renewal(client(expiry=NOW + 100), plan(), "reset_volume_add_time", NOW)
    assert (target["used_bytes"], target["quota_bytes"]) == (0, 5 * GIB)
    assert target["expires_at"] == NOW + 100 + 30 * 86400


def test_add_both_retains_usage_and_adds_remaining_plus_purchase():
    target = calculate_renewal(client(), plan(), "add_both", NOW)
    assert target["used_bytes"] == 12 * GIB
    assert target["quota_bytes"] == 17 * GIB
    assert target["expires_at"] == NOW + 30 * 86400


def test_add_both_under_quota_adds_only_remaining_and_purchase():
    target = calculate_renewal(client(used=4 * GIB, quota=10 * GIB), plan(), "add_both", NOW)
    assert target["used_bytes"] == 4 * GIB
    assert target["quota_bytes"] == 15 * GIB


def test_legacy_method_aliases_are_explicit():
    assert calculate_renewal(client(), plan(), "rollover_volume", NOW)["quota_bytes"] == 17 * GIB
    assert calculate_renewal(client(), plan(), "reset_from_now", NOW)["quota_bytes"] == 5 * GIB


def test_expired_and_on_hold_guards():
    expired = calculate_renewal(client(expiry=NOW - 10 * 86400), plan(1 * GIB, 7), "add_both", NOW)
    assert expired["expires_at"] == NOW + 7 * 86400
    held = client(used=2 * GIB, quota=5 * GIB, expiry=None, time_mode="on_hold")
    with pytest.raises(RenewalCalculationError):
        calculate_renewal(held, plan(), "add_both", NOW)
    with pytest.raises(RenewalCalculationError):
        calculate_addon(held, add_days=1, now=NOW)


def test_unknown_and_unlimited_are_not_coerced_to_finite_zero():
    unknown = client(used=None, quota=None, quota_mode="unknown", expiry=None, time_mode="unknown")
    with pytest.raises(RenewalCalculationError):
        calculate_renewal(unknown, plan(), "add_both", NOW)
    unlimited = client(used=None, quota=None, quota_mode="unlimited", expiry=None, time_mode="unlimited")
    with pytest.raises(RenewalCalculationError):
        calculate_renewal(unlimited, plan(), "add_both", NOW)


def test_addon_adds_headroom_for_overused_client_without_resetting_usage():
    target = calculate_addon(client(), add_bytes=5 * GIB, now=NOW)
    assert target["used_bytes"] == 12 * GIB
    assert target["quota_bytes"] == 17 * GIB
    assert target["expires_at"] == NOW
