"""W14.1 offline acceptance: unavailable panel snapshots still create private tickets."""
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


def _seed_user(conn, telegram_id, *, rep_id=None):
    conn.execute(
        "INSERT INTO bot_users(telegram_id,role,rep_id,username,is_blocked) "
        "VALUES(?,'end_user',?,?,0)",
        (telegram_id, rep_id, f"w14_{telegram_id}"),
    )
    conn.execute(
        "INSERT INTO user_settings(user_id,first_connect_mode,active_persona) "
        "VALUES(?,0,'end_user')",
        (telegram_id,),
    )
    conn.execute(
        "INSERT INTO user_wallets(user_id,balance_toman) VALUES(?,10000)",
        (telegram_id,),
    )


def test_unavailable_snapshot_still_creates_ticket(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _load(Path(__file__).with_name("v52_helpers.py"), "_w14_support_helpers", monkeypatch)
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

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w14-support.sqlite3"):
        with database.get_db() as conn:
            apply_migrations(conn, root / "migrations", now=1794400000)
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,parent_rep_id) "
                "VALUES(551,'W14-PARENT','W14 parent',199,'',NULL)"
            )
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,parent_rep_id) "
                "VALUES(550,'W14-SELLER','W14 seller',303,'',551)"
            )
            for telegram_id, rep_id in ((101, None), (199, 551), (303, 550), (9090, None), (404, None)):
                _seed_user(conn, telegram_id, rep_id=rep_id)
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,username,status,verified_at,revision) "
                "VALUES(9001,NULL,1,'w14_primary','active',1794400000,1)"
            )
        helpers.seed_account(
            database, account_id=703, rep_id=550, owner_tg_id=101,
            account_code="W14-SUPPORT-A", email="w14-support@example.invalid",
            sub_id="W14_PRIVATE_SUB_ID_MUST_NOT_LEAK",
        )
        with database.get_db() as conn:
            conn.execute("UPDATE accounts SET seller_key='rep:550' WHERE id=703")

        callback_state = []
        def unavailable_reader(public_ref):
            with database.get_db() as reader_conn:
                assert reader_conn.in_transaction is False
            callback_state.append(public_ref)
            raise TimeoutError("synthetic panel timeout")

        result = support.create_ticket(
            {"actor_tg_id": 101, "bot_id": 9001}, 703,
            "Connection stopped working",
            attachment_file_id="W14_FAKE_PHOTO_FILE_REFERENCE",
            attachment_bot_id=9001,
            panel_reader=unavailable_reader,
            now=1794400010,
        )
        assert result["assigned_key"] == "rep:550"
        assert result["status"] == "unseen"
        assert result["snapshot"]["state"] == "unavailable"
        assert result["snapshot"]["unavailable"] is True
        assert result["snapshot"]["observed_at"] == 1794400010
        assert result["snapshot"]["app"] is None
        assert result["snapshot"]["last_seen"] is None
        assert "sub_id" not in result["snapshot"]
        assert len(callback_state) == 1
        assert "sub_id" not in callback_state[0]
        assert "W14_PRIVATE_SUB_ID_MUST_NOT_LEAK" not in repr(result["snapshot"])

        with database.get_db() as conn:
            ticket = conn.execute(
                "SELECT assigned_key,rep_id,status,snapshot_cipher FROM support_tickets "
                "WHERE ticket_id=?",
                (result["ticket_id"],),
            ).fetchone()
            message = conn.execute(
                "SELECT body_cipher,attachment_file_id FROM support_messages WHERE ticket_id=?",
                (result["ticket_id"],),
            ).fetchone()
            assert tuple(ticket[:3]) == ("rep:550", 550, "unseen")
            assert "W14_PRIVATE_SUB_ID_MUST_NOT_LEAK" not in ticket["snapshot_cipher"]
            snapshot = __import__("json").loads(
                decrypt_secret(ticket["snapshot_cipher"], "hstagent/columns/v1")
            )
            assert snapshot["state"] == "unavailable"
            assert "Connection stopped working" not in message["body_cipher"]
            assert "W14_FAKE_PHOTO_FILE_REFERENCE" not in message["attachment_file_id"]

        try:
            support.create_ticket(
                {"actor_tg_id": 404, "bot_id": 9001}, 703, "wrong actor",
                panel_reader=unavailable_reader, now=1794400011,
            )
        except PermissionError:
            pass
        else:
            raise AssertionError("an unrelated actor must not open this account's ticket")

        assert attempts == []
        assert not Path(sentinel).exists()
