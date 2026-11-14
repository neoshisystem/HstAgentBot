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
    quota = None
    used = _value(client, "used_bytes")
    if quota_mode == "finite":
        quota = _positive_int(_value(client, "quota_bytes"), "quota_bytes", allow_zero=False)
        if used is None:
            raise RenewalCalculationError("used_bytes is required for finite-volume calculations")
        used = _positive_int(used, "used_bytes")
    elif used is not None:
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


def _plan(plan: Any) -> tuple[str, int, int]:
    mode = _value(plan, "quota_mode", default="finite")
    days = _positive_int(_value(plan, "days", "duration_days"), "plan.days", allow_zero=False)
    if mode != "finite":
        raise RenewalCalculationError("renewal formulas require an explicit finite plan")
    quota = _positive_int(_value(plan, "quota_bytes", "volume_bytes"), "plan.quota_bytes", allow_zero=False)
    return mode, quota, days


def calculate_renewal(current_client: Any, plan: Any, method: str, now: Any) -> dict[str, Any]:
    """Apply the four finite/started renewal formulas approved in RFP §6.1.

    GiB values are bytes. R=max(Q-U,0), B=max(expiry,N), D=days*86400.
    Legacy names remain aliases, but every result uses the canonical method.
    """
    now_s = _now_seconds(now)
    current = _current(current_client)
    plan_mode, volume, days = _plan(plan)
    if current["quota_mode"] != "finite":
        raise RenewalCalculationError("the four renewal formulas are defined for finite quota clients")
    if current["time_mode"] == "on_hold":
        raise RenewalCalculationError("on-hold clients cannot be renewed")
    if current["time_mode"] != "dated":
        raise RenewalCalculationError("the four renewal formulas require a dated client")

    aliases = {
        "reset_from_now": "reset_volume_time",
        "burn_and_replace": "reset_volume_time",
        "add_to_expiry": "reset_volume_add_time",
        "rollover_volume": "add_both",
    }
    canonical = aliases.get(method, method)
    if canonical not in {
        "reset_volume_time", "reset_time_add_volume",
        "reset_volume_add_time", "add_both",
    }:
        raise RenewalCalculationError("unsupported renewal method")

    used = current["used_bytes"]
    quota = current["quota_bytes"]
    remaining = max(quota - used, 0)
    base_expiry = max(current["expires_at"], now_s)
    duration = days * DAY_SECONDS

    if canonical == "reset_volume_time":
        target_used, target_quota, target_expiry = 0, volume, now_s + duration
    elif canonical == "reset_time_add_volume":
        target_used, target_quota, target_expiry = 0, remaining + volume, now_s + duration
    elif canonical == "reset_volume_add_time":
        target_used, target_quota, target_expiry = 0, volume, base_expiry + duration
    else:  # add_both
        target_used = used
        target_quota = used + remaining + volume
        target_expiry = base_expiry + duration

    return {
        **current,
        "method": canonical,
        "requested_method": method,
        "observed_at": now_s,
        "quota_mode": "finite",
        "quota_bytes": target_quota,
        "used_bytes": target_used,
        "time_mode": "dated",
        "expires_at": target_expiry,
        "remaining_bytes_before": remaining,
        "chargeable_bytes": volume,
        "days": days,
    }


def calculate_addon(current_client: Any, *, add_bytes: int = 0, add_days: int = 0, now: Any) -> dict[str, Any]:
    """Add quota/time without resetting usage or starting an on-hold countdown."""
    now_s = _now_seconds(now)
    add_bytes = _positive_int(add_bytes, "add_bytes")
    add_days = _positive_int(add_days, "add_days")
    if add_bytes == 0 and add_days == 0:
        raise RenewalCalculationError("empty adjustment")
    current = _current(current_client)
    if current["time_mode"] == "on_hold" and add_days:
        raise RenewalCalculationError("on-hold clients cannot receive additional days")

    if current["quota_mode"] == "finite":
        base_quota = max(current["quota_bytes"], current["used_bytes"])
        quota = base_quota + add_bytes if add_bytes else current["quota_bytes"]
    else:
        quota = None

    if current["time_mode"] == "dated" and add_days:
        expiry, time_mode = max(now_s, current["expires_at"]) + add_days * DAY_SECONDS, "dated"
    else:
        expiry, time_mode = current["expires_at"], current["time_mode"]

    return {
        **current,
        "quota_bytes": quota,
        "used_bytes": current["used_bytes"],
        "expires_at": expiry,
        "time_mode": time_mode,
        "observed_at": now_s,
        "add_bytes": add_bytes,
        "add_days": add_days,
    }
