"""Transport-free adapter contract shared by panel implementations."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from .models import CanonicalClient, ClientCreateSpec, NodeConfig


class AdapterUnavailable(RuntimeError):
    """The panel cannot provide a trustworthy read/transport result."""


class UnsupportedCapability(RuntimeError):
    """Raised before any network mutation for an unsupported capability."""


@dataclass(frozen=True, slots=True)
class LookupResult:
    state: str  # found | not_found | unavailable
    client: CanonicalClient | None = None
    evidence: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        if self.state not in {"found", "not_found", "unavailable"}:
            raise ValueError("invalid lookup state")
        if (self.state == "found") != (self.client is not None):
            raise ValueError("found lookup results require exactly one client")


@dataclass(frozen=True, slots=True)
class MutationResult:
    state: str  # success | definite_failure | unknown
    evidence: dict[str, Any] = field(default_factory=dict, repr=False)
    expected_effect: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        if self.state not in {"success", "definite_failure", "unknown"}:
            raise ValueError("invalid mutation state")


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    state: str  # applied | not_applied_proven | still_unknown | conflict
    evidence: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        if self.state not in {"applied", "not_applied_proven", "still_unknown", "conflict"}:
            raise ValueError("invalid reconcile state")


@dataclass(frozen=True, slots=True)
class CanonicalServerStatus:
    reachable: bool
    panel_version: str | None = None
    core_running: bool | None = None
    cpu_percent: float | None = None
    ram_used_bytes: int | None = None
    ram_total_bytes: int | None = None
    observed_at: int = 0


@dataclass(frozen=True, slots=True)
class PanelCapabilities:
    methods: frozenset[str] = frozenset()

    def supports(self, method_name: str) -> bool:
        return method_name in self.methods

    def require(self, method_name: str) -> None:
        if not self.supports(method_name):
            raise UnsupportedCapability(f"panel capability is not verified: {method_name}")


class BasePanelAdapter(ABC):
    """C11 signatures only; deliberately contains no HTTP or vendor assumptions."""
    def __init__(self, capabilities: PanelCapabilities | None = None):
        self.capabilities = capabilities or PanelCapabilities()

    @abstractmethod
    async def verify_connection(self) -> CanonicalServerStatus: ...

    @abstractmethod
    async def get_client(self, identifier: str) -> LookupResult: ...

    @abstractmethod
    async def list_clients(self, scope_filter: str | None = None) -> list[CanonicalClient]: ...

    @abstractmethod
    async def get_client_configs(self, identifier: str) -> list[NodeConfig]: ...

    @abstractmethod
    async def create_client(self, spec: ClientCreateSpec) -> MutationResult: ...

    @abstractmethod
    async def renew_client(self, identifier: str, target: dict, operation_id: str) -> MutationResult: ...

    @abstractmethod
    async def add_addon(self, identifier: str, target: dict, operation_id: str) -> MutationResult: ...

    @abstractmethod
    async def rotate_subscription_url(self, identifier: str, candidate: str, operation_id: str) -> MutationResult: ...

    @abstractmethod
    async def rotate_connection_credentials(self, identifier: str, candidate: dict, operation_id: str) -> MutationResult: ...

    @abstractmethod
    async def disconnect_existing_sessions(self, identifier: str, operation_id: str) -> MutationResult: ...

    @abstractmethod
    async def set_client_enabled(self, identifier: str, enabled: bool, operation_id: str) -> MutationResult: ...

    @abstractmethod
    async def set_traffic(self, identifier: str, mode: str, value: int | None, operation_id: str) -> MutationResult: ...

    @abstractmethod
    async def set_expiry(self, identifier: str, expires_at: int | None, operation_id: str) -> MutationResult: ...

    @abstractmethod
    async def delete_client(self, identifier: str, operation_id: str) -> MutationResult: ...

    @abstractmethod
    async def reconcile(self, operation: dict) -> ReconcileResult: ...
