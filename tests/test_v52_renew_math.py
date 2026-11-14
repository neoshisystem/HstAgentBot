import pytest
from renewal_calculator import RenewalCalculationError, calculate_renewal


GIB = 1024 ** 3
NOW = 1_900_000_000


def test_overused_expired_and_onhold():
    current = {
        "used_bytes": 12 * GIB, "quota_bytes": 10 * GIB,
        "expires_at": NOW - 86400, "quota_mode": "finite", "time_mode": "dated",
    }
    plan = {"quota_bytes": 5 * GIB, "days": 30, "quota_mode": "finite"}
    target = calculate_renewal(current, plan, "add_both", NOW)
    assert target["used_bytes"] == 12 * GIB
    assert target["quota_bytes"] == 17 * GIB
    assert target["expires_at"] == NOW + 30 * 86400

    held = {**current, "time_mode": "on_hold", "expires_at": None}
    with pytest.raises(RenewalCalculationError):
        calculate_renewal(held, plan, "add_both", NOW)
