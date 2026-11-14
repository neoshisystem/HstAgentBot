import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from migrations_runner import apply_migrations


def _load(path, name, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


@contextmanager
def _prepared(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _load(Path(__file__).with_name("v52_helpers.py"),
                    "_w17_member_helpers", monkeypatch)
    attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    cfg = sys.modules["config"]
    monkeypatch.setattr(cfg, "ADMIN_TELEGRAM_ID", 7001, raising=False)
    monkeypatch.setattr(cfg, "SUB_DOMAIN", "sub.example.invalid", raising=False)
    import base64
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY",
                       base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="))
    service = _load(root / "subscription_service.py", "subscription_service", monkeypatch)
    _load(root / "access_service.py", "access_service", monkeypatch)
    _load(root / "domain_service.py", "domain_service", monkeypatch)
    ui = _load(root / "subscription_ui.py", "subscription_ui", monkeypatch)
    with helpers.make_db(database, monkeypatch, tmp_path, filename="w17-profile-member.sqlite3"):
        with database.get_db() as conn:
            apply_migrations(conn, root / "migrations", now=1_800_000_000)
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision) "
                "VALUES(9001,NULL,1,'synthetic-verified-primary','primary','active',1800000000,1)"
            )
            conn.execute(
                "INSERT INTO system_settings(key,value,label,description) "
                "VALUES('sub_profile_limit','2','synthetic quota','W17.7')"
            )
        helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
        helpers.seed_account(database, account_id=303, rep_id=202, owner_tg_id=101)
        yield ui, service, database, attempts, sentinel


def _member_count(database, profile_id):
    with database.get_db() as conn:
        return int(conn.execute(
            "SELECT COUNT(*) FROM subscription_profile_accounts WHERE profile_id=?",
            (profile_id,),
        ).fetchone()[0])


def test_create_add_remove_flow(tmp_path, monkeypatch):
    with _prepared(tmp_path, monkeypatch) as (ui, service, database, attempts, sentinel):
        ctx = {"actor_tg_id": 101, "persona": "user", "chat_type": "private"}
        prompt = ui.begin_create(ctx, 0)
        assert prompt["callback_data"] == "v52:pnew:0"
        assert "v52:home:user" in [
            item.callback_data for row in prompt["reply_markup"].inline_keyboard for item in row
        ]

        with database.get_db() as conn:
            before = int(conn.execute(
                "SELECT COUNT(*) FROM subscription_profiles WHERE owner_tg_id=101"
            ).fetchone()[0])
        invalid = ui.begin_create(ctx, 0, name="   ", now=1_800_000_010)
        assert invalid["status"] == "invalid_name"
        assert "v52:home:user" in [
            item.callback_data for row in invalid["reply_markup"].inline_keyboard for item in row
        ]
        with database.get_db() as conn:
            after_invalid = int(conn.execute(
                "SELECT COUNT(*) FROM subscription_profiles WHERE owner_tg_id=101"
            ).fetchone()[0])
        assert after_invalid == before

        first = ui.begin_create(ctx, 303, name="First", now=1_800_000_011)
        assert first.created is True
        assert _member_count(database, first.profile_id) == 1

        second = ui.begin_create(ctx, 0, name="Second", now=1_800_000_012)
        assert second.created is True
        assert _member_count(database, second.profile_id) == 0
        added = ui.add_member(ctx, second.profile_id, 303, now=1_800_000_013)
        duplicate = ui.add_member(ctx, second.profile_id, 303, now=1_800_000_014)
        assert added.added is True and duplicate.added is False
        assert _member_count(database, second.profile_id) == 1

        preview = ui.preview_remove(
            ctx, second.profile_id, 303, now=1_800_000_015,
        )
        assert preview.action == "member.remove"
        assert preview.nonce and preview.callback_ok.startswith("v52:ok:")
        assert _member_count(database, second.profile_id) == 1
        with database.get_db() as conn:
            removed = service.remove_profile_member(
                conn, 101, second.profile_id, 303,
                nonce=preview.nonce, now=1_800_000_016,
            )
            account_still_exists = conn.execute(
                "SELECT 1 FROM accounts WHERE id=303"
            ).fetchone() is not None
        assert removed is True and account_still_exists is True
        assert _member_count(database, second.profile_id) == 0

        quota_error = ui.begin_create(ctx, 0, name="Third", now=1_800_000_017)
        assert quota_error["status"] == "quota_exceeded"
        last_row = quota_error["reply_markup"].inline_keyboard[-1]
        assert [item.callback_data for item in last_row] == [
            "v52:profiles", "v52:home:user",
        ]
        assert attempts == []
