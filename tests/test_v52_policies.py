"""Acceptance coverage for the approved authority policy registry."""
import sqlite3

import pytest

from policy_registry import (
    APPROVED_DATE, APPROVED_POLICIES, AUTHORITY, POLICY_VERSION,
    PolicyRegistryError, require_policy, seed_approved_policies,
)


def test_all_authority_decisions_are_approved():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE policy_decisions ("
        "decision_id TEXT PRIMARY KEY, status TEXT NOT NULL, authority TEXT NOT NULL, "
        "approved_date TEXT NOT NULL, policy_version TEXT NOT NULL, decision_text TEXT NOT NULL)"
    )
    try:
        seed_approved_policies(conn)
        expected = dict(APPROVED_POLICIES)
        rows = conn.execute(
            "SELECT decision_id,status,authority,approved_date,policy_version,decision_text "
            "FROM policy_decisions ORDER BY decision_id"
        ).fetchall()
        assert len(expected) == 28
        assert len(rows) == 28
        assert {r[0] for r in rows} == set(expected)
        for decision_id, status, authority, approved_date, version, decision_text in rows:
            assert status == "APPROVED"
            assert authority == AUTHORITY == "hisystem"
            assert approved_date == APPROVED_DATE == "2026-11-06"
            assert version == POLICY_VERSION == "v1.0"
            assert decision_text == expected[decision_id]
            assert tuple(require_policy(conn, decision_id)) == (
                "APPROVED", "hisystem", "2026-11-06", "v1.0", expected[decision_id]
            )

        seed_approved_policies(conn)
        assert conn.execute("SELECT COUNT(*) FROM policy_decisions").fetchone()[0] == 28
        assert conn.execute(
            "SELECT COUNT(*) FROM policy_decisions WHERE status='OPEN'"
        ).fetchone()[0] == 0

        with pytest.raises(PolicyRegistryError):
            require_policy(conn, "PA-99")

        conn.execute(
            "UPDATE policy_decisions SET policy_version='v9.9' WHERE decision_id='PA-01'"
        )
        with pytest.raises(PolicyRegistryError, match="metadata"):
            seed_approved_policies(conn)
        assert conn.execute(
            "SELECT policy_version FROM policy_decisions WHERE decision_id='PA-01'"
        ).fetchone()[0] == "v9.9"
    finally:
        conn.close()
