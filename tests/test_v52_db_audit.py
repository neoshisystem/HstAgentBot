"""Offline acceptance for encrypted legacy account secrets and audit redaction."""
from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path

import crypto_store
import pytest
from migrations_runner import apply_migrations


def _helpers():
    spec = importlib.util.spec_from_file_location(
        "_w18_audit_helpers", Path(__file__).with_name("v52_helpers.py")
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_secret_not_logged_and_ambiguous_lookup(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(b"w18-audit-key-material-123456789").decode("ascii").rstrip("="),
    )
    helpers = _helpers()
    root = Path(__file__).resolve().parents[1]
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w18-audit.sqlite3"):
        helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=1_800_000_000)) == 18
            cipher = crypto_store.encrypt_secret("legacy-sub-secret-marker", "hstagent/columns/v1")
            conn.execute(
                "INSERT INTO accounts(id,rep_id,account_code,email,customer_name,sub_id,sub_id_cipher,"
                "enable,total_gb,expiry_time,owner_tg_id) "
                "VALUES(501,202,'W18-ACC','w18-a@example.invalid','W18 A','legacy-plaintext',?,1,10,0,101)",
                (cipher,),
            )
            conn.execute(
                "INSERT INTO accounts(id,rep_id,account_code,email,customer_name,sub_id,enable,total_gb,"
                "expiry_time,owner_tg_id) "
                "VALUES(502,202,'W18-DUP','w18-b@example.invalid','W18 B','',1,10,0,101)"
            )
            conn.execute(
                "INSERT INTO accounts(id,rep_id,account_code,email,customer_name,sub_id,enable,total_gb,"
                "expiry_time,owner_tg_id) "
                "VALUES(503,202,'W18-DUP','w18-c@example.invalid','W18 C','',1,10,0,101)"
            )

        assert database.read_account_sub_id(501) == "legacy-sub-secret-marker"
        dto = database.get_account_by_code_or_email("W18-ACC")
        assert dto and dto["id"] == 501
        assert "sub_id" not in dto and "sub_id_cipher" not in dto
        with pytest.raises(ValueError, match="ambiguous"):
            database.get_account_by_code_or_email("W18-DUP")

        with database.get_db() as conn:
            conn.execute("UPDATE accounts SET sub_id_cipher=NULL,sub_id='plaintext-only-marker' WHERE id=501")
        with pytest.raises(ValueError, match="plaintext fallback"):
            database.read_account_sub_id(501)

        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            database._insert_audit(
                conn.cursor(),
                202,
                "W18-ACC",
                "w18-a@example.invalid",
                "reset_sub_id",
                101,
                "W18 actor",
                {"new_sub_id": "legacy-reset-secret-marker", "result": "success"},
                "success",
            )
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT details_json,details_cipher FROM audit_logs WHERE action='reset_sub_id'"
            ).fetchone()
            assert row and row["details_cipher"]
            assert "legacy-reset-secret-marker" not in row["details_json"]
            assert "new_sub_id" not in row["details_json"]
            restored = json.loads(
                crypto_store.decrypt_secret(row["details_cipher"], "hstagent/columns/v1")
            )
            assert restored["new_sub_id"] == "legacy-reset-secret-marker"

        assert Path(sentinel) != Path(database.DB_PATH)
