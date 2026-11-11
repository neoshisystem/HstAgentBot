"""W14.2 offline acceptance: visibility separation, assigned scope and escalation."""
from __future__ import annotations

import base64
import importlib.util
from pathlib import Path
import sys

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
        (telegram_id, rep_id, f"w14r_{telegram_id}"),
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


def test_internal_hidden_and_escalation(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _load(Path(__file__).with_name("v52_helpers.py"), "_w14_reply_helpers", monkeypatch)
    attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    _load(root / "access_service.py", "access_service", monkeypatch)
    config = sys.modules["config"]
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9090)
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    support = _load(root / "support_service.py", "support_service", monkeypatch)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w14-support-reply.sqlite3"):
        with database.get_db() as conn:
            apply_migrations(conn, root / "migrations", now=1794400000)
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,parent_rep_id) "
                "VALUES(551,'W14R-PARENT','W14R parent',199,'',NULL)"
            )
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,parent_rep_id) "
                "VALUES(550,'W14R-SELLER','W14R seller',303,'',551)"
            )
            for telegram_id, rep_id in ((101, None), (199, 551), (303, 550), (9090, None)):
                _seed_user(conn, telegram_id, rep_id)
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,username,status,verified_at,revision) "
                "VALUES(9001,NULL,1,'w14r_primary','active',1794400000,1)"
            )
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,username,status,verified_at,revision) "
                "VALUES(9002,550,0,'w14r_seller','active',1794400000,1)"
            )
        helpers.seed_account(
            database, account_id=704, rep_id=550, owner_tg_id=101,
            account_code="W14R-SUPPORT-A", email="w14r-support@example.invalid",
        )
        with database.get_db() as conn:
            conn.execute("UPDATE accounts SET seller_key='rep:550' WHERE id=704")

        customer = {"actor_tg_id": 101, "bot_id": 9001}
        seller = {"actor_tg_id": 303, "bot_id": 9002, "rep_id": 550}
        parent = {"actor_tg_id": 199, "bot_id": 9001, "rep_id": 551}
        ticket = support.create_ticket(
            customer, 704, "Initial customer report",
            attachment_file_id="FILE_OWNED_BY_OTHER_BOT",
            attachment_bot_id=9002,
            now=1794400010,
        )
        internal = support.reply_ticket(
            seller, ticket["ticket_id"], "Private diagnostic note",
            visibility="internal", now=1794400020,
        )
        public = support.reply_ticket(
            seller, ticket["ticket_id"], "Please retry after configuration sync",
            visibility="customer", now=1794400030,
        )

        export = support.export_ticket(customer, ticket["ticket_id"])
        messages = export["messages"]
        assert [item["visibility"] for item in messages] == ["customer", "customer"]
        assert all("Private diagnostic note" not in item["body"] for item in messages)
        assert messages[0]["attachment_file_id"] is None
        assert messages[0]["attachment_reupload_required"] is True
        assert len(support.list_ticket_messages(seller, ticket["ticket_id"])) == 3
        assert support.list_tickets(parent) == []
        try:
            support.reply_ticket(parent, ticket["ticket_id"], "Parent must not access private ticket")
        except PermissionError:
            pass
        else:
            raise AssertionError("parent representative must not inherit private ticket access")
        try:
            support.reply_ticket(
                seller, ticket["ticket_id"], "root-only note",
                visibility="root_only", now=1794400031,
            )
        except PermissionError:
            pass
        else:
            raise AssertionError("only root may add root-only notes")

        with database.get_db() as conn:
            outbox = conn.execute(
                "SELECT bot_id,chat_id,thread_id,purpose,state FROM delivery_outbox"
            ).fetchall()
            before = conn.execute(
                "SELECT COUNT(*) FROM support_messages WHERE ticket_id=?",
                (ticket["ticket_id"],),
            ).fetchone()[0]
        assert public["delivery"]["state"] == "pending"
        assert len(outbox) == 1
        assert tuple(outbox[0]) == (9001, 101, None, "support_reply", "pending")
        assert before == 3
        escalated = support.escalate_ticket(seller, ticket["ticket_id"], now=1794400040)
        assert escalated == {
            "ticket_id": ticket["ticket_id"],
            "assigned_key": "root",
            "status": "escalated",
        }
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT assigned_key,status FROM support_tickets WHERE ticket_id=?",
                (ticket["ticket_id"],),
            ).fetchone()
            after = conn.execute(
                "SELECT COUNT(*) FROM support_messages WHERE ticket_id=?",
                (ticket["ticket_id"],),
            ).fetchone()[0]
        assert tuple(row) == ("root", "escalated")
        assert before == after
        assert attempts == []
        assert not Path(sentinel).exists()
