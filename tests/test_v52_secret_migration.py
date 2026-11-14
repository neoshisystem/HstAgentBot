import json
import os
import sqlite3

import backup
import crypto_store
import secret_migration


class _Uploader:
    def send_telegram_document(self, *args, **kwargs):
        raise AssertionError("network upload must not occur in offline tests")


def _key():
    return crypto_store.base64.urlsafe_b64encode(b"b" * 32).decode("ascii").rstrip("=")


def _make_legacy_db(path):
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE accounts (
                id INTEGER PRIMARY KEY, sub_id TEXT, sub_id_cipher TEXT
            );
            CREATE TABLE card_settings (
                id INTEGER PRIMARY KEY, card_number TEXT NOT NULL,
                card_holder TEXT, bank_name TEXT
            );
            CREATE TABLE audit_logs (
                id INTEGER PRIMARY KEY, action TEXT, actor_id INTEGER,
                actor_name TEXT, details_json TEXT, result_status TEXT,
                details_cipher TEXT
            );
            CREATE TABLE system_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE financial_ledger (id INTEGER PRIMARY KEY, marker TEXT);
            CREATE TABLE operation_locks (id INTEGER PRIMARY KEY, marker TEXT);
            CREATE TABLE outbox (id INTEGER PRIMARY KEY, marker TEXT);
            INSERT INTO accounts(id,sub_id) VALUES(1,'sub-secret-marker');
            INSERT INTO card_settings(id,card_number,card_holder,bank_name)
                VALUES(1,'6037991888887777','Account Holder','Test Bank');
            INSERT INTO audit_logs(id,action,actor_id,actor_name,details_json,result_status)
                VALUES(1,'update_account',9,'Operator',
                    '{"before":{"token":"audit-secret-marker"},"resource":"account:1"}','success');
            INSERT INTO system_settings(key,value) VALUES
                ('bot_token','bot-secret-marker'),('public_label','safe-value');
            INSERT INTO financial_ledger(id,marker) VALUES(1,'ledger-stable');
            INSERT INTO operation_locks(id,marker) VALUES(1,'lock-stable');
            INSERT INTO outbox(id,marker) VALUES(1,'outbox-stable');
            """
        )


def test_no_plaintext_and_preserved_access(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", _key())
    production_dir = tmp_path / "production"
    production_dir.mkdir()
    db_path = production_dir / "legacy.sqlite3"
    _make_legacy_db(db_path)

    backup_dir = tmp_path / "archives"
    monkeypatch.setattr(backup, "DB_PATH", str(db_path))
    monkeypatch.setattr(backup, "BACKUP_DIR", str(backup_dir))
    monkeypatch.setattr(backup, "BACKUPS_TOPIC_ID", None)
    monkeypatch.setattr(backup, "backup_and_report", _Uploader())
    archived = backup.run_backup()
    old_archive = archived["archive"]

    counts = secret_migration.migrate_legacy_secrets(str(db_path))
    assert counts == {"accounts": 1, "bank_cards": 1, "audit_logs": 1, "settings": 1}
    assert secret_migration.read_account_sub_id(1, str(db_path)) == "sub-secret-marker"
    assert secret_migration.read_bank_card(1, str(db_path))["card_number"] == "6037991888887777"
    assert secret_migration.read_system_setting("bot_token", str(db_path)) == "bot-secret-marker"
    assert secret_migration.read_system_setting("public_label", str(db_path)) == "safe-value"

    with sqlite3.connect(db_path) as conn:
        account = conn.execute("SELECT sub_id,sub_id_cipher FROM accounts WHERE id=1").fetchone()
        card = conn.execute("SELECT card_number FROM card_settings WHERE id=1").fetchone()
        audit = conn.execute("SELECT details_json,details_cipher FROM audit_logs WHERE id=1").fetchone()
        setting = conn.execute("SELECT value FROM system_settings WHERE key='bot_token'").fetchone()
        stable = conn.execute(
            "SELECT (SELECT COUNT(*) FROM financial_ledger),"
            "(SELECT COUNT(*) FROM operation_locks),(SELECT COUNT(*) FROM outbox)"
        ).fetchone()
    assert account[0] is None and account[1]
    assert card[0] == "[REDACTED]"
    assert "audit-secret-marker" not in audit[0] and audit[1]
    assert "bot-secret-marker" not in setting[0]
    assert stable == (1, 1, 1)

    with sqlite3.connect(db_path) as conn:
        before_cipher = conn.execute("SELECT sub_id_cipher FROM accounts WHERE id=1").fetchone()[0]
    rerun = secret_migration.migrate_legacy_secrets(str(db_path))
    with sqlite3.connect(db_path) as conn:
        after_cipher = conn.execute("SELECT sub_id_cipher FROM accounts WHERE id=1").fetchone()[0]
    assert rerun == {"accounts": 0, "bank_cards": 0, "audit_logs": 0, "settings": 0}
    assert before_cipher == after_cipher

    # A pre-migration encrypted snapshot remains independently restorable.
    restored = tmp_path / "old-backup-restore"
    result = backup.restore_backup_archive(old_archive, str(restored))
    with sqlite3.connect(os.path.join(result["destination"], "agent_bot.db")) as conn:
        assert conn.execute("SELECT sub_id FROM accounts WHERE id=1").fetchone()[0] == "sub-secret-marker"

    missing_key_db = tmp_path / "missing-key.sqlite3"
    _make_legacy_db(missing_key_db)
    before = missing_key_db.read_bytes()
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY")
    try:
        secret_migration.migrate_legacy_secrets(str(missing_key_db))
    except secret_migration.SecretMigrationError:
        pass
    else:
        raise AssertionError("missing key must fail closed")
    assert missing_key_db.read_bytes() == before
