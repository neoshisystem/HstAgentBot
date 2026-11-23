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
                    "_w17_action_helpers", monkeypatch)
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
    with helpers.make_db(database, monkeypatch, tmp_path, filename="w17-profile-actions.sqlite3"):
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


def test_token_actions_and_default_guard(tmp_path, monkeypatch):
    with _prepared(tmp_path, monkeypatch) as (ui, service, database, attempts, sentinel):
        ctx = {"actor_tg_id": 101, "persona": "user", "chat_type": "private"}
        created = ui.begin_create(ctx, 303, name="Work", now=1_800_000_001)
        old_token = created.token
        assert old_token

        link = ui.show_link(ctx, created.profile_id)
        qr = ui.show_qr(ctx, created.profile_id)
        assert link["link"] == qr["link"]
        assert qr["qr_payload"] == link["link"]
        assert qr["qr_data"] == link["link"].encode("utf-8")
        assert qr["image"].startswith(b"\x89PNG")
        assert "/s/" in link["link"]
        assert "<code>" in link["text"]

        # A group card carries no bearer URL or QR action; secret delivery is DM-only.
        group_card = ui.show_profile(ctx, created.profile_id, chat_type="group")
        group_callbacks = _callbacks(group_card.reply_markup)
        assert group_card.link is None
        assert not any(item.startswith(("v52:plink:", "v52:pqr:", "v52:prot:",
                                        "v52:ppause:", "v52:pclear:", "v52:pdel:"))
                       for item in group_callbacks)
        assert old_token not in group_card.text
        with pytest.raises(ui.ProfileUIAccessDenied):
            ui.show_link(ctx, created.profile_id, chat_type="group")

        rotate_preview = ui.preview_profile_action(
            ctx, created.profile_id, "rotate", now=1_800_000_002,
        )
        with database.get_db() as conn:
            rotated = service.rotate_profile_token(
                conn, 101, created.profile_id,
                nonce=rotate_preview.nonce, now=1_800_000_003,
            )
            assert rotated.token != old_token
            assert service.resolve_profile_by_token(conn, old_token) is None
            assert service.resolve_profile_by_token(conn, rotated.token)["profile_id"] == created.profile_id
        refreshed_link = ui.show_link(ctx, created.profile_id)
        assert rotated.token in refreshed_link["link"]

        default_view = ui.list_profiles(ctx)
        default_id = default_view.profiles[0]["profile_id"]
        default_card = ui.show_profile(ctx, default_id)
        assert not any(item == f"v52:pdel:{default_id}" for item in _callbacks(default_card.reply_markup))
        with pytest.raises(service.ProfileError):
            ui.preview_profile_action(ctx, default_id, "delete", now=1_800_000_005)
        with pytest.raises(service.ProfileError):
            ui.execute_profile_action(ctx, default_id, "delete", "forged-nonce",
                                      now=1_800_000_006)

        pause_preview = ui.preview_profile_action(
            ctx, created.profile_id, "pause", now=1_800_000_007,
        )
        nav = _callbacks(pause_preview.reply_markup)
        assert f"v52:profile:{created.profile_id}" in nav
        assert "v52:home:user" in nav
        with database.get_db() as conn:
            enabled = conn.execute(
                "SELECT enabled FROM subscription_profiles WHERE profile_id=?",
                (created.profile_id,),
            ).fetchone()[0]
        assert enabled == 1  # preview/cancel does not mutate the profile
        assert attempts == []
