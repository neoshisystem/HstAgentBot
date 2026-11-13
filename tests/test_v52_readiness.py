"""Contract test for independent v5.2 feature gates."""
from __future__ import annotations

import hashlib

from readiness_service import check_feature_readiness


def _witness(label: str) -> dict[str, object]:
    return {
        "verified": True,
        "sha256": hashlib.sha256(label.encode("utf-8")).hexdigest(),
    }


def test_independent_feature_gates(tmp_path):
    result = check_feature_readiness(
        migration_versions=("013_profiles", "014_domains"),
        bootstrap={"profiles": True},
        evidence={"EV-05": _witness("subgateway-contract")},
        contract_tests={"subgateway_contract": {"passed": True}},
        flags={
            "subgateway": True,
            "uuid_disconnect": False,
            "crypto": True,
        },
        verifiers={"crypto": False},
        live_evidence={},
        production_db_path=tmp_path / "missing-production.sqlite3",
        unknown_payments=1,
        unknown_operations=1,
        pending_outbox=2,
    )

    # EV-03 is a multi-panel witness, not a subgateway prerequisite.
    assert result["features"]["subgateway"]["status"] == "PASS"
    assert "EV-03" not in result["features"]["subgateway"]["missing_evidence"]

    # A dormant capability remains dormant without live UUID evidence.
    assert result["features"]["uuid_disconnect"]["status"] == "DISABLED"
    assert result["features"]["uuid_disconnect"]["enabled_effective"] is False

    # Crypto cannot be enabled by a configuration flag without a verifier.
    assert result["rails"]["crypto"]["status"] == "DISABLED"
    assert result["rails"]["crypto"]["enabled_effective"] is False
    assert result["rails"]["crypto"]["reasons"] == ["verifier_missing"]

    # The absent binary production DB blocks only the production migration gate.
    assert result["production_migration_gate"]["status"] == "BLOCKED"
    assert result["features"]["subgateway"]["enabled_effective"] is True

    # Mapping inputs are interpreted as migration states, not just version names.
    pending_migration = check_feature_readiness(
        migration_versions={
            "013_profiles": "pending",
            "014_domains": {"status": "applied"},
        },
        bootstrap={"profiles": True},
        evidence={"EV-05": _witness("subgateway-contract")},
        contract_tests={"subgateway_contract": {"passed": True}},
        flags={"subgateway": True},
    )
    assert pending_migration["features"]["subgateway"]["status"] == "BLOCKED"
    assert pending_migration["features"]["subgateway"]["missing_migrations"] == ["013_profiles"]

    # Feature flags do not erase or pause reconciliation/outbox work.
    assert result["reconciliation"] == {
        "unknown_payments": 1,
        "unknown_operations": 1,
        "pending_outbox": 2,
        "resume_required": True,
        "feature_flags_may_suppress_reconciliation": False,
    }
