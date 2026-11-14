import importlib.util
import sys
from contextlib import contextmanager
from pathlib import Path

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
                    "_w17_list_helpers", monkeypatch)
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
    with helpers.make_db(database, monkeypatch, tmp_path, filename="w17-profile-list.sqlite3"):
        with database.get_db() as conn:
            apply_migrations(conn, root / "migrations", now=1_800_000_000)
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision) "
                "VALUES(9001,NULL,1,'synthetic-verified-primary','primary','active',1800000000,1)"
            )
        helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
        helpers.seed_account(database, account_id=303, rep_id=202, owner_tg_id=101)
        yield ui, service, database, attempts, sentinel


def _callbacks(markup):
    return [item.callback_data for row in markup.inline_keyboard for item in row]


def test_default_first_and_membership_privacy(tmp_path, monkeypatch):
    with _prepared(tmp_path, monkeypatch) as (ui, service, database, attempts, sentinel):
        ctx = {"actor_tg_id": 101, "persona": "user", "chat_type": "private"}
        with database.get_db() as conn:
            default = service.ensure_default_profile(conn, 101, now=1_800_000_001)
            own = service.create_profile(
                conn, 101, "Private Work", account_id=303, now=1_800_000_002,
            )
            foreign = service.create_profile(
                conn, 7001, "Secret Other Owner Profile",
                owner_tg_id=505, account_id=303, now=1_800_000_003,
            )
        view = ui.list_profiles(ctx, account_id=303)
        assert view.profiles[0]["profile_id"] == default.profile_id
        assert view.profiles[0]["is_default"] is True
        assert view.profiles[1]["profile_id"] == own.profile_id
        assert view.profiles[1]["contains_account"] is True
        assert view.visible_other_names == ("Private Work",)
        assert "Secret Other Owner Profile" not in view.text
        assert "Secret Other Owner Profile" not in view.visible_other_names
        labels = [item.text for row in view.reply_markup.inline_keyboard for item in row]
        assert labels[0].startswith("پیش‌فرض · ")
        assert "⭐" not in labels[0]

        # Membership is keyed by stable account_id and survives email changes.
        with database.get_db() as conn:
            conn.execute("UPDATE accounts SET email='changed@example.invalid' WHERE id=303")
        refreshed = ui.open_account_profiles(ctx, 303)
        assert any(item["profile_id"] == own.profile_id and item["contains_account"]
                   for item in refreshed.profiles)

        root = {"actor_tg_id": 7001, "persona": "admin", "chat_type": "private"}
        with_target = ui.list_profiles(root, target_owner_tg_id=505)
        assert any(item["profile_id"] == foreign.profile_id for item in with_target.profiles)
        target_labels = [item.text for row in with_target.reply_markup.inline_keyboard for item in row]
        assert "Secret Other Owner Profile" in target_labels
        assert "v52:home:admin" in _callbacks(with_target.reply_markup)
        assert attempts == []
