"""Deterministic renewal calculator for v5.2 adapter operations."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

DAY_SECONDS = 86400


class RenewalCalculationError(ValueError):
    """Raised when a renewal target cannot be calculated safely."""


def _value(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if isinstance(obj, Mapping) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _positive_int(value: Any, field: str, allow_zero: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RenewalCalculationError(field + " must be an integer")
    if value < 0 or (not allow_zero and value == 0):
        raise RenewalCalculationError(field + " is out of range")
    return value


def _now_seconds(now: Any) -> int:
    if isinstance(now, bool):
        raise RenewalCalculationError("now is invalid")
    if isinstance(now, int):
        return _positive_int(now, "now")
    if isinstance(now, datetime):
        if now.tzinfo is None:
            raise RenewalCalculationError("now must be timezone-aware")
        return int(now.astimezone(timezone.utc).timestamp())
    raise RenewalCalculationError("now must be UTC epoch seconds or an aware datetime")


def _current(client: Any) -> dict[str, Any]:
    quota_mode = _value(client, "quota_mode", default="finite")
    time_mode = _value(client, "time_mode", default="dated")
    if quota_mode not in {"finite", "unlimited", "unknown"}:
        raise RenewalCalculationError("invalid quota_mode")
    if time_mode not in {"dated", "unlimited", "on_hold", "unknown"}:
        raise RenewalCalculationError("invalid time_mode")
    if quota_mode == "unknown" or time_mode == "unknown":
        raise RenewalCalculationError("unknown client state cannot be treated as zero")
    quota = None if quota_mode == "unlimited" else _positive_int(_value(client, "quota_bytes"), "quota_bytes")
    used = _value(client, "used_bytes")
    if used is not None:
        used = _positive_int(used, "used_bytes")
    expiry = _value(client, "expires_at", "expiry_time")
    if time_mode == "dated":
        expiry = _positive_int(expiry, "expires_at", allow_zero=False)
    else:
        expiry = None
    return {
        "quota_mode": quota_mode, "quota_bytes": quota, "used_bytes": used,
        "time_mode": time_mode, "expires_at": expiry,
        "enabled": _value(client, "enabled"), "subscription_id": _value(client, "subscription_id"),
    }


def _plan(plan: Any) -> tuple[str, int | None, int]:
    mode = _value(plan, "quota_mode", default="finite")
    days = _positive_int(_value(plan, "days", "duration_days"), "plan.days")
    if mode == "unlimited":
        return mode, None, days
    if mode != "finite":
        raise RenewalCalculationError("invalid plan quota_mode")
    return mode, _positive_int(_value(plan, "quota_bytes", "volume_bytes"), "plan.quota_bytes"), days


def calculate_renewal(current_client: Any, plan: Any, method: str, now: Any) -> dict[str, Any]:
    """Calculate one of the four approved commercial renewal methods.

    reset_from_now: reset usage, replace quota, expiry from now.
    add_to_expiry: preserve usage/quota, extend expiry from max(now, expiry).
    rollover_volume: preserve usage, add plan quota to the old quota, extend expiry.
    burn_and_replace: replace quota and expiry from now, reset usage.
    """
    now_s = _now_seconds(now)
    current = _current(current_client)
    plan_mode, plan_quota, days = _plan(plan)
    if method not in {"reset_from_now", "add_to_expiry", "rollover_volume", "burn_and_replace"}:
        raise RenewalCalculationError("unsupported renewal method")
    if method in {"reset_from_now", "rollover_volume", "burn_and_replace"} and plan_mode == "unlimited":
        # Unlimited is explicit and must not accidentally become finite or be billed as bytes.
        target_quota_mode = "unlimited"
    else:
        target_quota_mode = current["quota_mode"]

    if method == "add_to_expiry":
        if current["time_mode"] == "unlimited":
            target_expiry, target_time_mode = None, "unlimited"
        elif current["time_mode"] == "on_hold":
            target_expiry, target_time_mode = None, "on_hold"
        else:
            target_expiry = max(now_s, current["expires_at"]) + days * DAY_SECONDS
            target_time_mode = "dated"
        return {
            **current, "method": method, "observed_at": now_s,
            "quota_bytes": current["quota_bytes"], "used_bytes": current["used_bytes"],
            "expires_at": target_expiry, "time_mode": target_time_mode,
            "chargeable_bytes": 0, "days": days,
        }

    if method == "rollover_volume":
        if current["quota_mode"] == "unlimited" or plan_mode == "unlimited":
            target_quota_mode, target_quota = "unlimited", None
        else:
            target_quota_mode = "finite"
            target_quota = current["quota_bytes"] + plan_quota
        if current["time_mode"] == "on_hold":
            target_expiry, target_time_mode = None, "on_hold"
        elif current["time_mode"] == "unlimited":
            target_expiry, target_time_mode = None, "unlimited"
        else:
            target_expiry = max(now_s, current["expires_at"]) + days * DAY_SECONDS
            target_time_mode = "dated"
        return {
            **current, "method": method, "observed_at": now_s,
            "quota_mode": target_quota_mode, "quota_bytes": target_quota,
            "used_bytes": current["used_bytes"], "expires_at": target_expiry,
            "time_mode": target_time_mode, "chargeable_bytes": plan_quota or 0,
            "days": days,
        }

    if current["quota_mode"] == "unlimited":
        target_quota_mode, target_quota = "unlimited", None
    elif plan_mode == "unlimited":
        target_quota_mode, target_quota = "unlimited", None
    else:
        target_quota_mode, target_quota = "finite", plan_quota
    return {
        **current, "method": method, "observed_at": now_s,
        "quota_mode": target_quota_mode, "quota_bytes": target_quota,
        "used_bytes": 0,
        "time_mode": "dated", "expires_at": now_s + days * DAY_SECONDS,
        "chargeable_bytes": plan_quota or 0, "days": days,
    }


def calculate_addon(current_client: Any, *, add_bytes: int = 0, add_days: int = 0, now: Any) -> dict[str, Any]:
    """Calculate an additive traffic/day adjustment without mutating usage."""
    now_s = _now_seconds(now)
    add_bytes = _positive_int(add_bytes, "add_bytes")
    add_days = _positive_int(add_days, "add_days")
    if add_bytes == 0 and add_days == 0:
        raise RenewalCalculationError("empty adjustment")
    current = _current(current_client)
    if current["time_mode"] == "on_hold" and add_days:
        raise RenewalCalculationError("on_hold client cannot receive days")
    if current["quota_mode"] == "unlimited":
        quota = None
    else:
        quota = current["quota_bytes"] + add_bytes
    if current["time_mode"] == "dated":
        expiry = max(now_s, current["expires_at"]) + add_days * DAY_SECONDS
        time_mode = "dated"
    else:
        expiry, time_mode = current["expires_at"], current["time_mode"]
    return {
        **current, "quota_bytes": quota, "used_bytes": current["used_bytes"],
        "expires_at": expiry, "time_mode": time_mode,
        "observed_at": now_s, "add_bytes": add_bytes, "add_days": add_days,
    }
