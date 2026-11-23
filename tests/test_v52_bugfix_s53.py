"""S53 regression coverage for existing-panel discovery and forum idempotency.

All adapter/panel/Telegram interactions are synthetic and offline.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import sys
from pathlib import Path
import pytest

from forum_manager import sync_report_topics
from panel_adapters.xui_openapi import XUIOpenAPIAdapter

PRODUCT_ROOT = Path(__file__).resolve().parents[1]


class _Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def _wire(email):
    return {
        "email": email,
        "enable": True,
        "subId": "synthetic-sub",
        "totalGB": 10 * (1024 ** 3),
        "expiryTime": 1_900_000_000_000,
        "traffic": {"up": 5, "down": 7},
        "id": "synthetic-uuid",
        "inboundIds": [1],
    }


def _adapter(session, resolver=None):
    return XUIOpenAPIAdapter(
        {"id": 17, "raw_url": "https://panel.invalid/panel/api-docs",
         "auth_cipher": "synthetic-cipher", "auth_type": "bearer_token"},
        session,
        decryptor=lambda *_: "synthetic-token",
        transaction_probe=lambda: False,
        account_id_resolver=resolver,
    )


def _isolated_database(tmp_path, monkeypatch, filename):
    """Load the real Product database module against a fresh migrated temp DB."""
    helper_path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_s53_helpers", helper_path)
    assert spec and spec.loader
    helpers = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, helpers)
    spec.loader.exec_module(helpers)
    helpers.guard_network(monkeypatch)
    database, _sentinel = helpers.load_database_module(PRODUCT_ROOT, monkeypatch, tmp_path)
    return helpers, database


def _seed_panels(database, rows):
    from migrations_runner import apply_migrations
    with database.get_db() as conn:
        assert len(apply_migrations(conn, PRODUCT_ROOT / "migrations", now=1_794_800_000)) == 18
        for row in rows:
            conn.execute(
                "INSERT INTO panels(id,panel_code,name,panel_type) VALUES(?,?,?,?)", row
            )


def test_xui_list_clients_allows_unbound_discovery_but_get_client_stays_strict():
    listed = _adapter(_Session(_Response(200, {"success": True, "obj": [
        _wire("legacy-one@example.test"), _wire("legacy-two@example.test"),
    ]})), resolver=lambda *_: None)

    clients = asyncio.run(listed.list_clients())
    assert [client.ref for client in clients] == [
        (0, 17, "legacy-one@example.test"),
        (0, 17, "legacy-two@example.test"),
    ]

    single = _adapter(_Session(_Response(200, {
        "success": True, "obj": {"client": _wire("legacy-one@example.test"),
                                  "inboundIds": [1]},
    })), resolver=lambda *_: None)
    result = asyncio.run(single.get_client("legacy-one@example.test"))
    assert result.state == "unavailable"
    assert result.client is None


def test_database_sync_imports_unbound_panel_clients_with_autoincrement_ids(tmp_path, monkeypatch):
    """Existing clients on a just-added panel are discoverable and persisted once."""
    helpers, database = _isolated_database(tmp_path, monkeypatch, "s53-discovery.sqlite3")

    with helpers.make_db(database, monkeypatch, tmp_path, filename="s53-discovery.sqlite3"):
        _seed_panels(database, [(17, "S53-P17", "S53 panel", "3x-ui")])
        with database.get_db() as conn:
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,is_active) "
                "VALUES(27,'S53-R27','Scoped rep',2700,'legacy-one',1)"
            )

        discovered = [
            {"ref": (0, 17, "legacy-one@example.test"), "enabled": True,
             "quota_mode": "finite", "quota_bytes": 10 * (1024 ** 3), "used_bytes": 12,
             "time_mode": "dated", "expires_at": 1_900_000_000,
             "subscription_id": "sub-one"},
            {"ref": (0, 17, "pool-client@example.test"), "enabled": True,
             "quota_mode": "unlimited", "quota_bytes": None, "used_bytes": 0,
             "time_mode": "unlimited", "expires_at": None,
             "subscription_id": "sub-pool"},
        ]
        class _DiscoveryAdapter:
            def list_clients(self):
                return discovered

        monkeypatch.setattr(database, "_get_panel_adapter", lambda panel_id: _DiscoveryAdapter())
        result = database.sync_all_active_reps_detailed()
        assert result["status"] == "success"
        assert result["count"] == 2
        assert result["unmatched"] == 1

        with database.get_db() as conn:
            rows = conn.execute(
                "SELECT id,rep_id,email,panel_id FROM accounts ORDER BY email"
            ).fetchall()
        assert len(rows) == 2
        assert all(int(row["id"]) > 0 for row in rows)
        by_email = {row["email"]: dict(row) for row in rows}
        assert by_email["legacy-one@example.test"]["rep_id"] == 27
        assert by_email["pool-client@example.test"]["rep_id"] is None
        assert all(row["panel_id"] == 17 for row in by_email.values())
        pool_id = by_email["pool-client@example.test"]["id"]

        # Replaying discovery reconciles rows in place rather than duplicating them.
        replay = database.sync_all_active_reps_detailed()
        assert replay["status"] == "success"
        assert replay["count"] == 2
        with database.get_db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM accounts WHERE panel_id=17").fetchone()[0] == 2
            assert conn.execute(
                "SELECT id FROM accounts WHERE panel_id=17 AND email='pool-client@example.test'"
            ).fetchone()[0] == pool_id


def test_sync_skips_an_unreadable_panel_and_still_imports_the_others(tmp_path, monkeypatch):
    """A half-configured panel must never abort the import of every other panel."""
    helpers, database = _isolated_database(tmp_path, monkeypatch, "s53-skip.sqlite3")

    with helpers.make_db(database, monkeypatch, tmp_path, filename="s53-skip.sqlite3"):
        _seed_panels(database, [
            (1, "S53-P1", "Configured panel", "3x-ui"),
            (2, "S53-P2", "Half-configured panel", "3x-ui"),
        ])

        class _ReadableAdapter:
            def list_clients(self):
                return [{"ref": (0, 1, "imported@example.test"), "enabled": True,
                         "quota_mode": "unlimited", "quota_bytes": None, "used_bytes": 0,
                         "time_mode": "unlimited", "expires_at": None,
                         "subscription_id": "sub-1"}]

        def _factory(panel_id):
            if int(panel_id) == 2:
                raise RuntimeError("XUI adapter could not be constructed")
            return _ReadableAdapter()

        monkeypatch.setattr(database, "_get_panel_adapter", _factory)
        result = database.sync_all_active_reps_detailed()
        assert result["status"] == "success"     # never an ERROR line at startup
        assert result["count"] == 1
        assert result["skipped"] == [{"panel_id": 2, "reason": "RuntimeError"}]

        with database.get_db() as conn:
            rows = conn.execute("SELECT email,panel_id FROM accounts").fetchall()
        assert [(row["email"], row["panel_id"]) for row in rows] == [
            ("imported@example.test", 1)
        ]


def test_sync_reports_error_only_when_no_panel_can_be_read(tmp_path, monkeypatch):
    helpers, database = _isolated_database(tmp_path, monkeypatch, "s53-allfail.sqlite3")

    with helpers.make_db(database, monkeypatch, tmp_path, filename="s53-allfail.sqlite3"):
        _seed_panels(database, [(1, "S53-P1", "Broken panel", "3x-ui")])

        def _factory(panel_id):
            raise RuntimeError("XUI adapter could not be constructed")

        monkeypatch.setattr(database, "_get_panel_adapter", _factory)
        result = database.sync_all_active_reps_detailed()
        assert result["status"] == "error"
        assert result["count"] == 0
        assert result["skipped"] == [{"panel_id": 1, "reason": "RuntimeError"}]


def _forum_db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE forum_topics("
        "id INTEGER PRIMARY KEY,owner_key TEXT NOT NULL,bot_id INTEGER NOT NULL,"
        "group_chat_id INTEGER NOT NULL,generation INTEGER NOT NULL,topic_key TEXT NOT NULL,"
        "title_fa TEXT NOT NULL,thread_id INTEGER,state TEXT NOT NULL,updated_at INTEGER NOT NULL,"
        "UNIQUE(owner_key,bot_id,group_chat_id,generation,topic_key))"
    )
    return conn


class _Forum:
    def __init__(self, conn, *, edit_error=None):
        self.conn = conn
        self.edit_error = edit_error
        self.creates = []
        self.edits = []

    def get_chat(self, *, chat_id):
        return {"id": chat_id, "type": "supergroup", "is_forum": True}

    def get_me(self):
        return {"id": 700}

    def get_chat_member(self, *, chat_id, user_id):
        return {"status": "administrator", "can_manage_topics": True,
                "can_pin_messages": True}

    def create_forum_topic(self, *, chat_id, name):
        self.creates.append((chat_id, name))
        return {"message_thread_id": 900 + len(self.creates)}

    def edit_forum_topic(self, *, chat_id, message_thread_id, name):
        self.edits.append((chat_id, message_thread_id, name))
        if self.edit_error:
            raise self.edit_error
        return True


def test_forum_created_topic_is_not_edited_and_replay_not_modified_is_verified():
    conn = _forum_db()
    try:
        api = _Forum(conn)
        first = sync_report_topics(
            conn, api, bot_id=700, group_chat_id=-10001, now=100,
            topic_keys=("buyreport",),
        )
        assert first == [{"topic_key": "buyreport", "state": "active",
                          "thread_id": 901, "action": "created"}]
        assert len(api.creates) == 1
        assert api.edits == []  # createForumTopic already set the requested name.

        # Simulate a stale local title so this subsequent sync legitimately asks
        # Telegram to edit; TOPIC_NOT_MODIFIED confirms the remote title is already right.
        conn.execute("UPDATE forum_topics SET title_fa='older title'")
        conn.commit()
        api.edit_error = RuntimeError("Bad Request: TOPIC_NOT_MODIFIED")
        replay = sync_report_topics(
            conn, api, bot_id=700, group_chat_id=-10001, now=101,
            topic_keys=("buyreport",),
        )
        assert replay == [{"topic_key": "buyreport", "state": "active",
                           "thread_id": 901, "action": "unchanged"}]
        assert len(api.creates) == 1  # no duplicate topic
        assert len(api.edits) == 1

        conn.execute("UPDATE forum_topics SET title_fa='another stale title'")
        conn.commit()
        api.edit_error = RuntimeError("Bad Request: CHAT_ADMIN_REQUIRED")
        failed = sync_report_topics(
            conn, api, bot_id=700, group_chat_id=-10001, now=102,
            topic_keys=("buyreport",),
        )
        assert failed[0]["action"] == "edit_failed"
        assert failed[0]["error_type"] == "RuntimeError"
    finally:
        conn.close()


@pytest.mark.parametrize("message", [
    "Bad Request: TOPIC_NOT_MODIFIED",
    "TOPIC_NOT_MODIFIED",
    "Bad Request: topic not modified",
])
def test_forum_unchanged_detection_is_case_insensitive(message):
    from forum_manager import _is_topic_unchanged
    assert _is_topic_unchanged(RuntimeError(message)) is True


def test_forum_different_edit_errors_are_not_swallowed():
    from forum_manager import _is_topic_unchanged
    assert _is_topic_unchanged(RuntimeError("Bad Request: CHAT_ADMIN_REQUIRED")) is False
