"""Frozen canonical context and result models for the v5.2 execution layer."""

from dataclasses import dataclass, field


AccountRef = tuple[int, int, str]


@dataclass(frozen=True)
class ActorContext:
    """Authenticated Telegram actor and the bot through which they act."""

    tg_id: int
    bot_id: int
    persona: str
    is_root: bool


@dataclass(frozen=True)
class BotContext:
    """Resolved bot/merchant identity from the bot registry."""

    bot_id: int
    merchant_key: str = field(repr=False)
    rep_id: int | None = None
    is_primary: bool = False


@dataclass(frozen=True)
class LookupResult:
    """Three-way panel lookup result; unavailable is never not_found."""

    state: str
    client: dict | None = field(default=None, repr=False)
    evidence: dict | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.state not in {"found", "not_found", "unavailable"}:
            raise ValueError(f"invalid lookup state: {self.state!r}")
        if self.client is not None and not isinstance(self.client, dict):
            raise TypeError("client must be a dict or None")
        if self.evidence is not None and not isinstance(self.evidence, dict):
            raise TypeError("evidence must be a dict or None")
        if self.state == "found" and self.client is None:
            raise ValueError("found lookup result requires a client")
        if self.state != "found" and self.client is not None:
            raise ValueError(f"{self.state} lookup result cannot carry a client")


@dataclass(frozen=True)
class MutationResult:
    """Mutation outcome preserving uncertainty for explicit reconciliation."""

    state: str
    evidence: dict | None = field(default=None, repr=False)
    expected_effect: dict | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.state not in {"success", "definite_failure", "unknown"}:
            raise ValueError(f"invalid mutation state: {self.state!r}")
        if self.evidence is not None and not isinstance(self.evidence, dict):
            raise TypeError("evidence must be a dict or None")
        if self.expected_effect is not None and not isinstance(self.expected_effect, dict):
            raise TypeError("expected_effect must be a dict or None")


@dataclass(frozen=True)
class ReconcileResult:
    """Evidence-backed reconciliation outcome for an uncertain mutation."""

    state: str
    details: dict | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.state not in {"applied", "not_applied_proven", "still_unknown", "conflict"}:
            raise ValueError(f"invalid reconciliation state: {self.state!r}")
        if self.details is not None and not isinstance(self.details, dict):
            raise TypeError("details must be a dict or None")
