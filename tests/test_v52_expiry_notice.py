"""W15.5 acceptance: only validated snapshots generate idempotent alerts."""
from __future__ import annotations

import base64
import sqlite3

from crypto_store import decrypt_secret
from notification_service import plan_expiry_notifications


def test_no_duplicate_or_unknown_alert(monkeypatch):
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE bot_registry(
            bot_id INTEGER PRIMARY KEY,rep_id INTEGER,is_primary INTEGER NOT NULL,status TEXT NOT NULL
        );
        CREATE TABLE accounts(
            id INTEGER PRIMARY KEY,owner_tg_id INTEGER,revision INTEGER,expiry_time INTEGER,deleted_at INTEGER
        );
        CREATE TABLE account_access_grants(
            account_id INTEGER,grantee_tg_id INTEGER,access_level TEXT,
            revoked_at INTEGER,PRIMARY KEY(account_id,grantee_tg_id)
        );
        CREATE TABLE delivery_outbox(
            id INTEGER PRIMARY KEY,event_key TEXT NOT NULL,bot_id INTEGER NOT NULL,
            chat_id INTEGER NOT NULL,thread_id INTEGER,purpose TEXT NOT NULL,
            resource_type TEXT NOT NULL,resource_id TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,next_attempt_at INTEGER NOT NULL DEFAULT 0,
            lease_token TEXT,lease_until INTEGER,last_error_code TEXT,message_id INTEGER,
            UNIQUE(event_key,bot_id,chat_id,purpose)
        );
    """)
    try:
        conn.executemany(
            "INSERT INTO system_settings VALUES(?,?)",
            [("critical_audit_days","3"),("critical_audit_gb","0.2")],
        )
        conn.execute("INSERT INTO bot_registry VALUES(500,NULL,1,'active')")
        conn.execute("INSERT INTO accounts VALUES(42,101,3,1500,NULL)")
        conn.executemany(
            "INSERT INTO account_access_grants VALUES(42,?,?,?)",
            [(202,"viewer",None),(303,"manager",900)],
        )
        conn.commit()  # enqueue_delivery owns its short transaction boundary.
        snapshot = {
            "account_id":42,"validated":True,"latest":True,"state":"available",
            "unavailable":False,"observed_at":1000,"expires_at":1500,
            "expiry_version":"v3","quota_mode":"finite",
            "quota_bytes":1024**3,"used_bytes":int(0.95*(1024**3)),
            "sub_id":"MUST_NOT_BE_QUEUED",
        }

        first = plan_expiry_notifications(conn,[snapshot],now=1000)
        assert len(first) == 4
        assert {item["kind"] for item in first} == {"expiry","usage"}
        assert {item["recipient_tg_id"] for item in first} == {101,202}
        rows = conn.execute(
            "SELECT event_key,bot_id,chat_id,thread_id,purpose,resource_type,resource_id "
            "FROM delivery_outbox ORDER BY purpose,chat_id"
        ).fetchall()
        assert len(rows) == 4
        assert all(row["bot_id"] == 500 and row["thread_id"] is None for row in rows)
        assert all(row["resource_type"] == "account" for row in rows)
        assert all(decrypt_secret(row["resource_id"],"hstagent/columns/v1") == "42" for row in rows)
        assert all("MUST_NOT_BE_QUEUED" not in repr(tuple(row)) for row in rows)

        second = plan_expiry_notifications(conn,[snapshot],now=1001)
        assert len(second) == 4
        assert conn.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0] == 4

        unknown = dict(snapshot,state="unknown",unavailable=True,latest=True)
        assert plan_expiry_notifications(conn,[unknown],now=1002) == []
        unvalidated = dict(snapshot,validated=False)
        assert plan_expiry_notifications(conn,[unvalidated],now=1002) == []
        assert conn.execute("SELECT COUNT(*) FROM delivery_outbox").fetchone()[0] == 4
    finally:
        conn.close()
