"""Canonical, immutable panel data models and explicit unit conversion."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, TypedDict

QuotaMode = Literal["finite", "unlimited", "unknown"]
TimeMode = Literal["dated", "unlimited", "on_hold", "unknown"]


@dataclass(frozen=True, slots=True)
class CanonicalClient:
    ref: tuple[int, int, str]
    enabled: bool | None
    quota_mode: QuotaMode
    quota_bytes: int | None
    used_bytes: int | None
    time_mode: TimeMode
    expires_at: int | None
    first_connected_at: int | None
    last_connected_at: int | None
    client_app: str | None
    subscription_id: str | None = field(repr=False)
    credential_revision: str | None = field(repr=False)
    observed_at: int = 0
    adapter_revision: str = ""


@dataclass(frozen=True, slots=True)
class NodeConfig:
    node_key: str
    label: str
    protocol: str
    uri: str = field(repr=False)


class ClientCreateSpec(TypedDict):
    operation_id: str
    email: str
    quota_mode: Literal["finite", "unlimited"]
    quota_bytes: int | None
    duration_seconds: int
    on_first_connect: bool
    inbound_ids: tuple[int, ...]
    comment: str
    candidate_credentials: dict[str, Any]


def normalize_volume(value: Any, *, zero_is_unlimited: bool = False) -> tuple[QuotaMode, int | None]:
    """Normalize a byte-valued 3X-UI totalGB wire field without guessing."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return "unknown", None
    if value == 0:
        return ("unlimited", None) if zero_is_unlimited else ("unknown", None)
    return "finite", value


def normalize_expiry(expiry_ms: Any, *, zero_is_unlimited: bool = False) -> tuple[TimeMode, int | None]:
    """Convert legacy panel milliseconds to UTC epoch seconds explicitly."""
    if isinstance(expiry_ms, bool) or not isinstance(expiry_ms, int):
        return "unknown", None
    if expiry_ms < 0:
        return "on_hold", None
    if expiry_ms == 0:
        return ("unlimited", None) if zero_is_unlimited else ("unknown", None)
    return "dated", expiry_ms // 1000


def expiry_seconds_to_panel_ms(expires_at: int | None) -> int:
    """Convert UTC epoch seconds to the panel's millisecond integer field."""
    if expires_at is None:
        return 0
    if isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at <= 0:
        raise ValueError("expires_at must be a positive UTC epoch-second integer or None")
    return expires_at * 1000
