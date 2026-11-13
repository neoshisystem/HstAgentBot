"""W20.4 safety test: rehearsals migrate and restore only a disposable copy."""
from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path

from crypto_store import encrypt_secret
from migrations_runner import apply_migrations


def _helpers():
    path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w20_rehearsal_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_rehearsal(root, monkeypatch):
    path = root / "tools" / "v52_rehearsal.py"
    spec = importlib.util.spec_from_file_location("_w20_rehearsal", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "_w20_rehearsal", module)
    spec.loader.exec_module(module)
    return module


def test_rehearsal_never_mutates_source(tmp_path, monkeypatch, capsys):
    root = Path(__file__).resolve().parents[1]
    helpers = _helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    config = sys.modules["config"]
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9090, raising=False)
    now = 1794500000

    with helpers.make_db(
        database, monkeypatch, tmp_path, filename="w20-rehearsal-source.sqlite3"
    ) as source_path:
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=now)) == 18
            helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
            conn.execute(
                "INSERT INTO bot_registry("
                "bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision"
                ") VALUES(9001,NULL,1,?,'w20_rehearsal','active',?,1)",
                (encrypt_secret("W20_REHEARSAL_BOT_TOKEN", "hstagent/columns/v1"), now),
            )
            conn.execute(
                "INSERT INTO panels(id,panel_code,name,panel_type,lifecycle,max_clients) "
                "VALUES(501,'W20-REHEARSAL','W20 rehearsal panel','3x-ui','active',100)"
            )
        helpers.seed_account(
            database, account_id=303, rep_id=202, owner_tg_id=101,
            account_code="W20-REHEARSAL-303", email="w20-rehearsal@example.invalid",
            sub_id="W20_REHEARSAL_SECRET_MUST_NOT_PRINT",
        )
        with database.get_db() as conn:
            conn.execute(
                "UPDATE accounts SET panel_id=501,seller_key='root' WHERE id=303"
            )
            conn.execute(
                "INSERT INTO panel_operations("
                "operation_id,order_id,actor_tg_id,bot_id,account_id,panel_id,kind,"
                "request_cipher,expected_effect_json,state,lock_key,lock_token,"
                "idempotency_key,generation,lease_until,evidence_json,created_at,updated_at"
                ") VALUES('w20-unknown-operation',NULL,101,9001,303,501,'renew',?,"
                "'{}','needs_reconciliation','account:303','W20_LOCK_OWNER_TOKEN',"
                "'w20-unknown-idempotency',1,NULL,'{}',?,?)",
                (encrypt_secret("{}", "hstagent/columns/v1"), now, now),
            )
            conn.execute(
                "INSERT INTO delivery_outbox("
                "event_key,bot_id,chat_id,thread_id,purpose,resource_type,resource_id,"
                "state,attempts,next_attempt_at,lease_token,lease_until,last_error_code,message_id"
                ") VALUES('w20-preserved-outbox',9001,101,NULL,'notice','account','303',"
                "'delivery_unknown',2,0,NULL,NULL,'send_timeout_effect_unknown',NULL)"
            )

        source_sha_before = __import__("hashlib").sha256(
            Path(source_path).read_bytes()
        ).hexdigest()
        rehearsal = _load_rehearsal(root, monkeypatch)
        output_dir = tmp_path / "w20-rehearsal-output"
        report = rehearsal.rehearse_migration_restore(
            source_path, output_dir, root / "migrations"
        )
        source_sha_after = __import__("hashlib").sha256(
            Path(source_path).read_bytes()
        ).hexdigest()

        assert report["status"] == "PASS"
        assert report["source_unchanged"] is True
        assert source_sha_before == source_sha_after == report["source_sha256_after"]
        assert report["migration_runner_checksum"] == "verified"
        assert report["migration_invariants_preserved"] is True
        assert report["pre_migration_snapshot"]["accounts"]["count"] == 1
        assert report["post_migration_snapshot"]["accounts"]["ownership_digest"]
        assert report["backup_encrypted"] is True
        assert report["copied_sqlite_integrity"] == "ok"
        assert report["restored_sqlite_integrity"] == "ok"
        assert report["copied_foreign_key_violations"] == 0
        assert report["restored_foreign_key_violations"] == 0
        assert report["unknown_operations_and_outbox_preserved"] is True
        assert report["snapshot"]["panel_operations"]["unresolved_count"] == 1
        assert report["snapshot"]["panel_operations"]["owned_lock_count"] == 1
        assert report["snapshot"]["delivery_outbox"]["pending_count"] == 1
        assert report["restored_snapshot"] == report["snapshot"]
        printed = capsys.readouterr().out
        assert "W20_REHEARSAL_SECRET_MUST_NOT_PRINT" not in printed
        assert "W20_LOCK_OWNER_TOKEN" not in printed
        assert sentinel != database.DB_PATH
    assert network_attempts == []
