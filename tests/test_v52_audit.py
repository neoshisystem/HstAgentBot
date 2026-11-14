import json
import sqlite3

import audit_service
import crypto_store


def test_redaction_nested_and_root_actions(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        crypto_store.base64.urlsafe_b64encode(b"a" * 32).decode("ascii").rstrip("="),
    )
    db_path = tmp_path / "audit.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE audit_logs (id INTEGER PRIMARY KEY, rep_id INTEGER, "
            "account_code TEXT, email TEXT, action TEXT NOT NULL, actor_id INTEGER NOT NULL, "
            "actor_name TEXT, details_json TEXT, result_status TEXT, details_cipher TEXT)"
        )
    secret_details = {
        "before": {
            "Authorization": "Bearer header-secret-marker",
            "url": "https://example.invalid/api?token=url-secret-marker",
            "subscription": "/sub/path-secret-marker",
        },
        "after": {"nested": [{"api_key": "nested-secret-marker", "label": "safe"}]},
    }
    audit_service.record_audit(
        "root_traffic",
        7,
        "Real Operator",
        secret_details,
        persona="root",
        resource="account:42",
        result_status="success",
        db_path=str(db_path),
    )
    audit_service.record_audit(
        "root_grant", 7, "Real Operator", {"before": {}, "after": {"grant": True}},
        persona="root", resource="representative:9", db_path=str(db_path)
    )
    audit_service.record_audit(
        "root_revoke", 7, "Real Operator", {"before": {"role": "admin"}, "after": {"role": "none"}},
        persona="root", resource="representative:9", db_path=str(db_path)
    )

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT action,actor_id,actor_name,details_json,details_cipher FROM audit_logs ORDER BY id"
        ).fetchall()
    assert [row[0] for row in rows] == ["root_traffic", "root_grant", "root_revoke"]
    assert all(row[1] == 7 and row[2] == "Real Operator" for row in rows)
    metadata = json.loads(rows[0][3])
    assert metadata == {
        "action": "root_traffic",
        "actor_persona": "root",
        "resource": "account:42",
        "result": "success",
    }
    assert "secret-marker" not in rows[0][3]
    assert rows[0][4]
    restored = json.loads(crypto_store.decrypt_secret(rows[0][4], "hstagent/columns/v1"))
    assert restored == secret_details

    cleaned = audit_service.redact_sensitive(
        {"outer": [{"Authorization": "Bearer abc123", "href": "/subscription/secret123"}]}
    )
    serialized = json.dumps(cleaned)
    assert "abc123" not in serialized
    assert "secret123" not in serialized
