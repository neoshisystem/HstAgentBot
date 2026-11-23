"""Offline acceptance for explicit account-to-panel subscription bridging."""
from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path

import crypto_store
import sub_manager
from migrations_runner import apply_migrations


def _helpers():
    spec = importlib.util.spec_from_file_location(
        "_w18_sub_bridge_helpers", Path(__file__).with_name("v52_helpers.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sub_id_cipher_and_panel_specific_url(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(b"w18-sub-bridge-key-material-1234").decode("ascii").rstrip("="),
    )
    helpers = _helpers()
    root = Path(__file__).resolve().parents[1]
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w18-sub-bridge.sqlite3"):
        helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=1_800_000_000)) == 18
            conn.execute(
                "INSERT INTO panels(id,panel_code,name,panel_type,sub_base_url,lifecycle,is_default) "
                "VALUES(77,'W18-PANEL','W18 panel','manual','https://sub-panel-a.invalid/v1','active',0)"
            )
            conn.execute(
                "INSERT INTO accounts(id,rep_id,account_code,email,customer_name,sub_id,sub_id_cipher,"
                "enable,total_gb,expiry_time,owner_tg_id,panel_id) "
                "VALUES(501,202,'W18-ACC','w18@example.invalid','W18 customer','',?,1,10,0,101,77)",
                (crypto_store.encrypt_secret("ciphertext-sub-marker", "hstagent/columns/v1"),),
            )
        monkeypatch.setitem(sys.modules, "database", database)

        binding = database.get_account_panel_binding(account_id=501)
        assert binding and binding["panel_id"] == 77
        assert database.read_account_sub_id(501) == "ciphertext-sub-marker"
        assert sub_manager.get_subscription_url("new-token", account_ref=501) == (
            "https://sub-panel-a.invalid/v1/new-token"
        )
        assert sub_manager.get_subscription_url("new-token", account_ref=999999) == ""

        class LegacyPanel:
            def __init__(self):
                self.writes = []
                self.sub_url_calls = []

            def find_client_by_code(self, identifier):
                return {"found": True, "data": {"email": identifier, "raw": {"email": identifier}}}

            def update_client_sub_id(self, email, candidate):
                self.writes.append((email, candidate))
                return True, "rotated", candidate

            def get_sub_url(self, candidate):
                self.sub_url_calls.append(candidate)
                return "https://wrong-default.invalid/" + candidate

        panel = LegacyPanel()
        missing = sub_manager.reset_client_subscription(
            panel, "w18@example.invalid", account_ref=501,
        )
        assert missing["status"] == "error"
        assert panel.writes == [] and panel.sub_url_calls == []
        assert database.get_account_by_code_or_email("W18-ACC").get("sub_id") is None
        assert Path(sentinel) != Path(database.DB_PATH)
