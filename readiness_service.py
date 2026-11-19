"""Independent, evidence-backed v5.2 feature readiness gates.

Read-only by design: never alter flags, financial state, UNKNOWN operations,
reconciliation state, or durable outbox rows.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

FEATURES = (
    "corefinance", "multipanel", "multibot", "subgateway",
    "digital", "support", "forums", "uuid_disconnect",
)

_FEATURE_CONTRACT = {
    "corefinance": {
        "migrations": ("008_orders", "009_finance", "010_payments", "011_operations"),
        "bootstrap": ("financial_edges",),
        "evidence": ("EV-01", "EV-02"),
        "contracts": ("finance_acceptance",),
    },
    "multipanel": {
        "migrations": ("002_panels", "003_accounts"),
        "bootstrap": ("panels",),
        "evidence": ("EV-03",),
        "contracts": ("multipanel_contract",),
    },
    "multibot": {
        "migrations": ("006_bots", "007_access"),
        "bootstrap": ("bots",),
        "evidence": ("EV-04",),
        "contracts": ("multibot_contract",),
    },
    "subgateway": {
        "migrations": ("013_profiles", "014_domains"),
        "bootstrap": ("profiles",),
        "evidence": ("EV-05",),
        "contracts": ("subgateway_contract",),
    },
    "digital": {
        "migrations": ("012_goods", "016_refunds_promos"),
        "bootstrap": ("digital_catalog",),
        "evidence": ("EV-02",),
        "contracts": ("digital_contract",),
    },
    "support": {
        "migrations": ("015_support",),
        "bootstrap": ("support",),
        "evidence": ("EV-04",),
        "contracts": ("support_contract",),
    },
    "forums": {
        "migrations": (),
        "bootstrap": ("forums",),
        "evidence": ("EV-05",),
        "contracts": ("forums_contract",),
    },
    "uuid_disconnect": {
        "migrations": ("011_operations",),
        "bootstrap": ("uuid_disconnect",),
        "evidence": ("EV-03",),
        "contracts": ("uuid_disconnect_contract", "uuid_disconnect_live"),
    },
}

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _evidence_is_valid(value: Any) -> bool:
    """Require an explicit verification assertion and a content digest."""
    if not isinstance(value, Mapping):
        return False
    digest = str(value.get("sha256") or "").lower()
    return value.get("verified") is True and bool(_SHA256.fullmatch(digest))


def _contract_passed(value: Any) -> bool:
    if value is True:
        return True
    return isinstance(value, Mapping) and value.get("passed") is True


def _normalise_migrations(values: Iterable[str] | Mapping[str, Any]) -> set[str]:
    if isinstance(values, Mapping):
        source = []
        for version, state in values.items():
            applied = state is True
            if isinstance(state, str):
                applied = state.strip().casefold() in {"applied", "complete", "completed"}
            elif isinstance(state, Mapping):
                applied = (
                    state.get("applied") is True
                    or str(state.get("status") or "").casefold()
                    in {"applied", "complete", "completed"}
                )
            if applied:
                source.append(version)
    else:
        source = values
    return {Path(str(value)).stem for value in source}


def check_feature_readiness(
    *,
    migration_versions: Iterable[str] | Mapping[str, Any] = (),
    bootstrap: Mapping[str, Any] | None = None,
    evidence: Mapping[str, Any] | None = None,
    contract_tests: Mapping[str, Any] | None = None,
    flags: Mapping[str, Any] | None = None,
    verifiers: Mapping[str, Any] | None = None,
    live_evidence: Mapping[str, Any] | None = None,
    production_db_path: str | Path | None = None,
    unknown_payments: int = 0,
    unknown_operations: int = 0,
    pending_outbox: int = 0,
) -> dict[str, Any]:
    """Evaluate every feature independently and fail closed without valid evidence.

    A missing production database affects only the separate production migration
    gate. Reconciliation and outbox work remain actionable regardless of flags.
    """
    migrations = _normalise_migrations(migration_versions)
    bootstrap = bootstrap or {}
    evidence = evidence or {}
    contract_tests = contract_tests or {}
    flags = flags or {}
    verifiers = verifiers or {}
    live_evidence = live_evidence or {}

    feature_results: dict[str, dict[str, Any]] = {}
    for feature in FEATURES:
        requirements = _FEATURE_CONTRACT[feature]
        requested = flags.get(feature) is True
        missing_migrations = sorted(set(requirements["migrations"]) - migrations)
        missing_bootstrap = [
            name for name in requirements["bootstrap"] if bootstrap.get(name) is not True
        ]
        missing_evidence = [
            name for name in requirements["evidence"]
            if not _evidence_is_valid(evidence.get(name))
        ]
        missing_contracts = [
            name for name in requirements["contracts"]
            if not _contract_passed(contract_tests.get(name))
        ]
        if feature == "uuid_disconnect" and requested:
            if not _evidence_is_valid(live_evidence.get("uuid_disconnect")):
                missing_evidence.append("uuid_disconnect_live_evidence")

        if not requested:
            status, reasons = "DISABLED", ["feature_flag_disabled"]
        else:
            reasons = []
            if missing_migrations:
                reasons.append("missing_migrations")
            if missing_bootstrap:
                reasons.append("missing_bootstrap")
            if missing_evidence:
                reasons.append("missing_or_invalid_evidence")
            if missing_contracts:
                reasons.append("contract_tests_not_passed")
            status = "PASS" if not reasons else "BLOCKED"

        feature_results[feature] = {
            "status": status,
            "enabled_requested": requested,
            "enabled_effective": status == "PASS",
            "missing_migrations": missing_migrations,
            "missing_bootstrap": missing_bootstrap,
            "missing_evidence": sorted(set(missing_evidence)),
            "missing_contract_tests": missing_contracts,
            "reasons": reasons,
        }

    crypto_requested = flags.get("crypto") is True
    crypto_verified = verifiers.get("crypto") is True
    crypto_status = "PASS" if crypto_requested and crypto_verified else "DISABLED"
    crypto_reasons = [] if crypto_status == "PASS" else [
        "feature_flag_disabled" if not crypto_requested else "verifier_missing"
    ]

    if production_db_path is None:
        production_gate = {"status": "NOT_ASSESSED", "reason": "no_production_db_path"}
    else:
        exists = Path(production_db_path).is_file()
        production_gate = {
            "status": "PASS" if exists else "BLOCKED",
            "reason": "database_present" if exists else "production_database_missing",
        }

    payment_count = max(0, int(unknown_payments))
    unknown_count = max(0, int(unknown_operations))
    outbox_count = max(0, int(pending_outbox))
    return {
        "features": feature_results,
        "rails": {
            "crypto": {
                "status": crypto_status,
                "enabled_requested": crypto_requested,
                "enabled_effective": crypto_status == "PASS",
                "reasons": crypto_reasons,
            }
        },
        "production_migration_gate": production_gate,
        "reconciliation": {
            "unknown_payments": payment_count,
            "unknown_operations": unknown_count,
            "pending_outbox": outbox_count,
            "resume_required": bool(payment_count or unknown_count or outbox_count),
            "feature_flags_may_suppress_reconciliation": False,
        },
        "overall_status": (
            "BLOCKED"
            if any(item["status"] == "BLOCKED" for item in feature_results.values())
            else "READY"
        ),
    }


__all__ = ["FEATURES", "check_feature_readiness"]
