"""Focused acceptance coverage for W01.2 result-state and repr contracts."""

from dataclasses import FrozenInstanceError

import pytest

from v52_models import BotContext, LookupResult, MutationResult, ReconcileResult


def test_result_types_keep_unknown_distinct():
    secret_marker = "V52_ENCRYPTED_SECRET_MARKER"

    with pytest.raises(ValueError, match="requires a client"):
        LookupResult(state="found")

    unavailable = LookupResult(
        state="unavailable",
        evidence={"transport": "timeout", "credential_blob": secret_marker},
    )
    assert unavailable.state == "unavailable"
    assert unavailable.client is None
    assert unavailable.evidence["transport"] == "timeout"

    found = LookupResult(
        state="found",
        client={"ref": (17, 3, "client@example.invalid"), "credential": secret_marker},
    )
    assert found.state == "found"
    assert found.client["ref"] == (17, 3, "client@example.invalid")

    unknown = MutationResult(
        state="unknown",
        evidence={"response": "connection_lost", "sealed": secret_marker},
        expected_effect={"subscription_secret": secret_marker},
    )
    definite_failure = MutationResult(
        state="definite_failure", evidence={"response": "validation_rejected"}
    )
    assert unknown.state == "unknown"
    assert definite_failure.state == "definite_failure"
    assert unknown.state != definite_failure.state

    reconciled = ReconcileResult(
        state="still_unknown", details={"encrypted_payload": secret_marker}
    )
    bot = BotContext(
        bot_id=9, merchant_key=secret_marker, rep_id=21, is_primary=False
    )

    rendered = "\n".join(
        repr(value)
        for value in (unavailable, found, unknown, definite_failure, reconciled, bot)
    )
    assert secret_marker not in rendered

    with pytest.raises(FrozenInstanceError):
        unknown.state = "success"
