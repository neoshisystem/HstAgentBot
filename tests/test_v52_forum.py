"""W15.2 acceptance: forum permissions and timeout-safe topic creation."""
from __future__ import annotations

import sqlite3

from forum_manager import check_forum_permissions, sync_report_topics


class FakeForum:
    def __init__(self, conn, *, timeout=False, chat_type="supergroup", can_post=None):
        self.conn = conn
        self.timeout = timeout
        self.chat_type = chat_type
        self.can_post = can_post
        self.creates = 0
        self.edits = 0
        self.intent_seen_before_create = False

    def get_chat(self, *, chat_id):
        return {"id": chat_id, "type": self.chat_type, "is_forum": True}

    def get_me(self):
        return {"id": 700}

    def get_chat_member(self, *, chat_id, user_id):
        member = {
            "status": "administrator",
            "can_manage_topics": True,
            "can_pin_messages": True,
        }
        if self.can_post is not None:
            member["can_post_messages"] = self.can_post
        return member

    def create_forum_topic(self, *, chat_id, name):
        self.creates += 1
        row = self.conn.execute(
            "SELECT state FROM forum_topics WHERE group_chat_id=? AND topic_key='buyreport'",
            (chat_id,),
        ).fetchone()
        self.intent_seen_before_create = row is not None and row["state"] == "creating"
        assert self.conn.in_transaction is False
        if self.timeout:
            raise TimeoutError("synthetic Telegram timeout after request")
        return {"message_thread_id": 710 + self.creates}

    def edit_forum_topic(self, *, chat_id, message_thread_id, name):
        self.edits += 1
        return True


def _db():
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


def test_permission_and_create_timeout():
    conn = _db()
    try:
        supergroup = FakeForum(conn, can_post=None)
        permission = check_forum_permissions(supergroup, -10001, bot_id=700)
        assert permission["allowed"] is True  # can_post_messages is not a supergroup right.

        created = sync_report_topics(
            conn, supergroup, bot_id=700, group_chat_id=-10001,
            now=100, topic_keys=("buyreport",),
        )
        assert created[0]["state"] == "active"
        assert supergroup.intent_seen_before_create is True
        assert supergroup.creates == 1

        replay = sync_report_topics(
            conn, supergroup, bot_id=700, group_chat_id=-10001,
            now=101, topic_keys=("buyreport",),
        )
        assert replay[0]["action"] == "edited"
        assert supergroup.creates == 1 and supergroup.edits == 1

        timeout = FakeForum(conn, timeout=True)
        first = sync_report_topics(
            conn, timeout, bot_id=700, group_chat_id=-10002,
            now=200, topic_keys=("buyreport",),
        )
        assert first[0]["state"] == "unknown"
        assert first[0]["action"] == "manual_repair_required"
        assert timeout.intent_seen_before_create is True
        assert timeout.creates == 1

        second = sync_report_topics(
            conn, timeout, bot_id=700, group_chat_id=-10002,
            now=201, topic_keys=("buyreport",),
        )
        assert second[0]["state"] == "unknown"
        assert second[0]["action"] == "manual_repair_required"
        assert timeout.creates == 1  # no blind second create after an ambiguous timeout.

        channel_without_post = FakeForum(conn, chat_type="channel", can_post=False)
        denied = check_forum_permissions(channel_without_post, -10003, bot_id=700)
        assert denied["allowed"] is False
        assert denied["reason"] == "can_post_messages_required_for_channel"
    finally:
        conn.close()
