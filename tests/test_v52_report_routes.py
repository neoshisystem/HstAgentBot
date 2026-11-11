"""W15.1 acceptance: report routing respects privacy and representative scope."""
from __future__ import annotations

import sqlite3

import pytest

from report_router import ReportRouteError, route_event


def _db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE system_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE bot_registry(
            bot_id INTEGER PRIMARY KEY,rep_id INTEGER,is_primary INTEGER NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE representatives(
            id INTEGER PRIMARY KEY,telegram_id INTEGER,group_id INTEGER,topic_id INTEGER,
            is_active INTEGER,deleted_at INTEGER
        );
        CREATE TABLE forum_topics(
            id INTEGER PRIMARY KEY,owner_key TEXT,bot_id INTEGER,group_chat_id INTEGER,
            generation INTEGER,topic_key TEXT,title_fa TEXT,thread_id INTEGER,state TEXT,
            updated_at INTEGER
        );
    """)
    conn.execute("INSERT INTO system_settings VALUES('admin_telegram_id','9090')")
    conn.executemany(
        "INSERT INTO bot_registry(bot_id,rep_id,is_primary,status) VALUES(?,?,?,?)",
        [(100,None,1,"active"),(201,11,0,"active"),(202,22,0,"active")],
    )
    conn.executemany(
        "INSERT INTO representatives VALUES(?,?,?,?,?,NULL)",
        [(11,1011,-10011,111,1),(22,1022,-10022,222,1)],
    )
    keys = (
        "buyreport", "otherservice", "pendingreceipts", "paymentreport",
        "reporttest", "supportreport", "errorreport", "reportcron",
        "reportnight", "backupfile",
    )
    conn.executemany(
        "INSERT INTO forum_topics(owner_key,bot_id,group_chat_id,generation,topic_key,"
        "title_fa,thread_id,state,updated_at) VALUES('root',100,-1000,1,?,?,?,'active',1)",
        [(key,key,300+index) for index,key in enumerate(keys)],
    )
    return conn


def test_abuse_private_and_rep_scope():
    conn = _db()
    try:
        abuse = route_event({
            "logical_key": "abusereport", "actor_tg_id": 1011,
            "body": "must not leak", "secret": "PRIVATE_SECRET_MARKER",
            "chat_id": -1000, "thread_id": 301,
        }, conn=conn)
        assert abuse["destination"] == "root_dm"
        assert abuse["bot_id"] == 100
        assert abuse["chat_id"] == 9090
        assert abuse["thread_id"] is None
        assert abuse["payload"] == {}

        group = route_event({
            "logical_key": "buyreport", "event_id": "evt-1",
            "amount_toman": 1200, "secret": "PRIVATE_SECRET_MARKER",
            "token": "PRIVATE_TOKEN_MARKER",
        }, conn=conn)
        assert group["destination"] == "group_topic"
        assert group["thread_id"] == 300
        assert "secret" not in group["payload"] and "token" not in group["payload"]
        assert group["payload"] == {"event_id": "evt-1", "amount_toman": 1200}

        own = route_event({
            "logical_key": "rep_buy", "rep_id": 11, "actor_tg_id": 1011,
            "amount_toman": 1200,
        }, conn=conn)
        assert own["destination"] == "representative_topic"
        assert (own["bot_id"], own["chat_id"], own["thread_id"]) == (201,-10011,111)

        with pytest.raises(PermissionError):
            route_event({
                "logical_key": "rep_buy", "rep_id": 22, "actor_tg_id": 1011,
                "chat_id": -10022, "thread_id": 222,
            }, conn=conn)
        with pytest.raises(ReportRouteError):
            route_event({"logical_key": "unknown", "actor_tg_id": 1011}, conn=conn)
    finally:
        conn.close()
