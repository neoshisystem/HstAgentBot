"""W14.3 offline acceptance: abuse reports are root-DM-only and invisible to rep scopes."""
from __future__ import annotations

import base64
import importlib.util
from pathlib import Path
import sys

from crypto_store import decrypt_secret
from migrations_runner import apply_migrations


def _load(path, name, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _seed_user(conn, telegram_id, rep_id=None):
    conn.execute(
        "INSERT INTO bot_users(telegram_id,role,rep_id,username,is_blocked) "
        "VALUES(?,'end_user',?,?,0)",
        (telegram_id, rep_id, f"w14a_{telegram_id}"),
    )
    conn.execute(
        "INSERT INTO user_settings(user_id,first_connect_mode,active_persona) "
        "VALUES(?,0,'end_user')",
        (telegram_id,),
    )
    conn.execute(
        "INSERT INTO user_wallets(user_id,balance_toman) VALUES(?,5000)",
        (telegram_id,),
    )


def test_no_leak_via_count_search_outbox(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _load(Path(__file__).with_name("v52_helpers.py"), "_w14_abuse_helpers", monkeypatch)
    attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    config = sys.modules["config"]
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9090)
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    support = _load(root / "support_service.py", "support_service", monkeypatch)
    abuse = _load(root / "abuse_service.py", "abuse_service", monkeypatch)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w14-abuse.sqlite3"):
        with database.get_db() as conn:
            apply_migrations(conn, root / "migrations", now=1794400000)
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,parent_rep_id) "
                "VALUES(551,'W14A-PARENT','W14A parent',199,'',NULL)"
            )
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,parent_rep_id) "
                "VALUES(550,'W14A-SELLER','W14A seller',303,'',551)"
            )
            for telegram_id, rep_id in ((101, None), (202, None), (199, 551), (303, 550), (9090, None)):
                _seed_user(conn, telegram_id, rep_id)
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,username,status,verified_at,revision) "
                "VALUES(9001,NULL,1,'w14a_primary','active',1794400000,1)"
            )
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,username,status,verified_at,revision) "
                "VALUES(9002,550,0,'w14a_seller','active',1794400000,1)"
            )

        reporter_a = {"actor_tg_id": 101, "bot_id": 9002}
        reporter_b = {"actor_tg_id": 202, "bot_id": 9001}
        seller = {"actor_tg_id": 303, "bot_id": 9002, "rep_id": 550}
        parent = {"actor_tg_id": 199, "bot_id": 9001, "rep_id": 551}
        root_actor = {"actor_tg_id": 9090, "bot_id": 9001}
        first = abuse.create_abuse_report(
            reporter_a, "CONFIDENTIAL_REPORT_A",
            attachment_file_id="CONFIDENTIAL_PHOTO_A",
            attachment_bot_id=9002, now=1794400010,
        )
        second = abuse.create_abuse_report(
            reporter_b, "CONFIDENTIAL_REPORT_B", now=1794400020,
        )

        assert abuse.list_abuse_reports(seller) == []
        assert abuse.list_abuse_reports(parent) == []
        assert abuse.count_abuse_reports(seller) == 0
        assert abuse.count_abuse_reports(parent) == 0
        assert abuse.search_abuse_reports(seller, first["ticket_id"]) == []
        assert abuse.count_abuse_reports(reporter_a) == 1
        assert [r["ticket_id"] for r in abuse.list_abuse_reports(reporter_a)] == [first["ticket_id"]]
        assert abuse.count_abuse_reports(root_actor) == 2
        assert {r["ticket_id"] for r in abuse.list_abuse_reports(root_actor)} == {
            first["ticket_id"], second["ticket_id"]
        }
        assert support.list_tickets(seller) == []
        assert support.count_tickets(seller) == 0
        assert support.search_tickets(seller, first["ticket_id"]) == []

        own_copy = abuse.export_abuse_report(reporter_a, first["ticket_id"])
        assert len(own_copy["messages"]) == 1
        assert own_copy["messages"][0]["body"] == "CONFIDENTIAL_REPORT_A"
        try:
            abuse.export_abuse_report(reporter_b, first["ticket_id"])
        except PermissionError:
            pass
        else:
            raise AssertionError("a reporter must not export another reporter's report")

        root_copy = abuse.export_abuse_report(root_actor, first["ticket_id"])
        assert root_copy["messages"][0]["visibility"] == "root_only"
        assert root_copy["messages"][0]["body"] == "CONFIDENTIAL_REPORT_A"
        customer_reply = abuse.reply_abuse_report(
            root_actor, first["ticket_id"], "We received your private report",
            visibility="customer", now=1794400030,
        )
        reporter_export = abuse.export_abuse_report(reporter_a, first["ticket_id"])
        assert [item["visibility"] for item in reporter_export["messages"]] == [
            "root_only", "customer"
        ]
        assert reporter_export["messages"][1]["body"] == "We received your private report"

        with database.get_db() as conn:
            outbox = conn.execute(
                "SELECT bot_id,chat_id,thread_id,purpose,state FROM delivery_outbox "
                "ORDER BY id"
            ).fetchall()
            stored = conn.execute(
                "SELECT body_cipher,attachment_file_id FROM support_messages "
                "WHERE ticket_id=? ORDER BY id LIMIT 1",
                (first["ticket_id"],),
            ).fetchone()
        assert tuple(outbox[0]) == (9001, 9090, None, "abuse_report", "pending")
        assert tuple(outbox[1]) == (9001, 9090, None, "abuse_report", "pending")
        assert tuple(outbox[2]) == (9002, 101, None, "support_reply", "pending")
        assert "CONFIDENTIAL_REPORT_A" not in stored["body_cipher"]
        assert "CONFIDENTIAL_PHOTO_A" not in stored["attachment_file_id"]
        assert customer_reply["delivery"]["state"] == "pending"
        assert attempts == []
        assert not Path(sentinel).exists()
