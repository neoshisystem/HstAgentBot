import json
import os
import sqlite3
import tarfile
import time

import pytest

import backup
import database


class UploadResult:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def send_telegram_document(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def prepare_backup(tmp_path, monkeypatch, upload_result):
    production_dir = tmp_path / "production"
    production_dir.mkdir()
    db_path = production_dir / "source.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
    connection.execute("INSERT INTO sample(value) VALUES ('backup-data')")
    connection.commit()
    connection.close()

    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(backup, "DB_PATH", str(db_path))
    monkeypatch.setattr(backup, "BACKUP_DIR", str(backup_dir))
    monkeypatch.setattr(backup, "BACKUPS_TOPIC_ID", -1001234567890)
    uploader = UploadResult(upload_result)
    monkeypatch.setattr(backup, "backup_and_report", uploader)
    return backup_dir, uploader, db_path


def test_backup_reports_local_archive_success_and_telegram_upload_success(tmp_path, monkeypatch):
    backup_dir, uploader, _db_path = prepare_backup(
        tmp_path, monkeypatch, {"ok": True, "message_id": 42}
    )

    result = backup.run_backup()

    assert result["status"] == "ok"
    assert result["verified"] is True
    assert result["sqlite_integrity"] == "ok"
    assert result["telegram_upload_status"] == "sent"
    assert result["telegram_upload_error"] == ""
    assert result["archive"].startswith(str(backup_dir))
    assert result["size_kb"] > 0
    assert os.path.isfile(result["sha256_file"])
    assert len(uploader.calls) == 1


def test_backup_reports_telegram_api_failure_without_claiming_upload_success(tmp_path, monkeypatch):
    backup_dir, uploader, _db_path = prepare_backup(
        tmp_path, monkeypatch, {"ok": False, "description": "chat not found"}
    )

    result = backup.run_backup()

    assert result["status"] == "ok"
    assert result["telegram_upload_status"] == "failed"
    assert "chat not found" in result["telegram_upload_error"]
    assert result["archive"].startswith(str(backup_dir))
    assert os.path.isfile(result["archive"])
    assert os.path.isfile(result["sha256_file"])
    assert backup.verify_backup_archive(result["archive"])["sqlite_integrity"] == "ok"
    assert len(uploader.calls) == 1


def test_backup_reports_telegram_upload_exception_and_keeps_archive(tmp_path, monkeypatch):
    backup_dir, uploader, _db_path = prepare_backup(
        tmp_path, monkeypatch, RuntimeError("telegram network unavailable")
    )

    result = backup.run_backup()

    assert result["status"] == "ok"
    assert result["telegram_upload_status"] == "failed"
    assert "telegram network unavailable" in result["telegram_upload_error"]
    assert result["archive"].startswith(str(backup_dir))
    assert os.path.isfile(result["archive"])
    assert os.path.isfile(result["sha256_file"])
    assert backup.verify_backup_archive(result["archive"])["sqlite_integrity"] == "ok"
    assert len(uploader.calls) == 1


def test_backup_marks_missing_topic_as_not_configured(tmp_path, monkeypatch):
    backup_dir, uploader, _db_path = prepare_backup(tmp_path, monkeypatch, {"ok": True})
    monkeypatch.setattr(backup, "BACKUPS_TOPIC_ID", 0)

    result = backup.run_backup()

    assert result["status"] == "ok"
    assert result["telegram_upload_status"] == "not_configured"
    assert "not configured" in result["telegram_upload_error"]
    assert result["archive"].startswith(str(backup_dir))
    assert uploader.calls == []


def test_archive_manifest_and_sidecar_verify_all_payloads(tmp_path, monkeypatch):
    _backup_dir, _uploader, _db_path = prepare_backup(
        tmp_path, monkeypatch, {"ok": True}
    )
    result = backup.run_backup()

    verification = backup.verify_backup_archive(result["archive"])
    assert verification["sha256"] == result["sha256"]
    assert verification["sqlite_integrity"] == "ok"
    assert verification["file_count"] >= 1
    assert verification["manifest"]["format_version"] == backup.MANIFEST_VERSION
    assert any(item["path"] == "agent_bot.db" for item in verification["manifest"]["files"])


def test_restore_creates_isolated_database_with_expected_rows(tmp_path, monkeypatch):
    _backup_dir, _uploader, db_path = prepare_backup(
        tmp_path, monkeypatch, {"ok": True}
    )
    result = backup.run_backup()
    destination = tmp_path / "restore"

    restored = backup.restore_backup_archive(result["archive"], str(destination))
    restored_db = os.path.join(restored["destination"], "agent_bot.db")
    connection = sqlite3.connect(restored_db)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT value FROM sample").fetchone()[0] == "backup-data"
    finally:
        connection.close()

    # The configured source database must remain intact and separate.
    source = sqlite3.connect(db_path)
    try:
        assert source.execute("SELECT value FROM sample").fetchone()[0] == "backup-data"
    finally:
        source.close()


def test_restore_rejects_production_database_directory_and_nonempty_destination(tmp_path, monkeypatch):
    _backup_dir, _uploader, _db_path = prepare_backup(
        tmp_path, monkeypatch, {"ok": True}
    )
    result = backup.run_backup()

    with pytest.raises(ValueError, match="production database directory"):
        backup.restore_backup_archive(result["archive"], str(tmp_path / "production"))

    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "keep.txt").write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(ValueError, match="new or empty"):
        backup.restore_backup_archive(result["archive"], str(nonempty))
    assert (nonempty / "keep.txt").read_text(encoding="utf-8") == "do not overwrite"


def test_verification_rejects_archive_hash_mismatch(tmp_path, monkeypatch):
    _backup_dir, _uploader, _db_path = prepare_backup(
        tmp_path, monkeypatch, {"ok": True}
    )
    result = backup.run_backup()
    with open(result["archive"], "ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(ValueError, match="SHA-256"):
        backup.verify_backup_archive(result["archive"])


def test_verification_rejects_payload_change_even_with_recomputed_archive_sidecar(tmp_path, monkeypatch):
    _backup_dir, _uploader, _db_path = prepare_backup(
        tmp_path, monkeypatch, {"ok": True}
    )
    result = backup.run_backup()
    tampered = tmp_path / "tampered.tar.gz"
    with tarfile.open(result["archive"], "r:gz") as source, tarfile.open(tampered, "w:gz") as target:
        for member in source.getmembers():
            extracted = source.extractfile(member) if member.isfile() else None
            if member.isfile():
                payload = extracted.read()
                if member.name.endswith("/backup.py"):
                    payload += b"\n# injected payload change\n"
                info = tarfile.TarInfo(member.name)
                info.size = len(payload)
                target.addfile(info, __import__("io").BytesIO(payload))
            else:
                target.addfile(member)
    digest = backup._sha256_file(str(tampered))
    backup._write_sha256_sidecar(str(tampered), digest)
    with pytest.raises(ValueError, match="payload integrity mismatch"):
        backup.verify_backup_archive(str(tampered))


def test_restore_rejects_path_traversal_and_does_not_write_outside_destination(tmp_path, monkeypatch):
    _backup_dir, _uploader, _db_path = prepare_backup(
        tmp_path, monkeypatch, {"ok": True}
    )
    malicious = tmp_path / "malicious.tar.gz"
    outside = tmp_path / "escaped.txt"
    with tarfile.open(malicious, "w:gz") as archive:
        root = tarfile.TarInfo("backup_evil")
        root.type = tarfile.DIRTYPE
        archive.addfile(root)
        attack = tarfile.TarInfo("backup_evil/../../escaped.txt")
        payload = b"not allowed"
        attack.size = len(payload)
        archive.addfile(attack, __import__("io").BytesIO(payload))
    backup._write_sha256_sidecar(str(malicious), backup._sha256_file(str(malicious)))

    destination = tmp_path / "restore-evil"
    with pytest.raises(ValueError, match="Unsafe archive member path"):
        backup.restore_backup_archive(str(malicious), str(destination))
    assert not destination.exists()
    assert not outside.exists()


def test_retention_prunes_only_verified_archives_and_keeps_one_valid_backup(tmp_path, monkeypatch):
    backup_dir, _uploader, _db_path = prepare_backup(
        tmp_path, monkeypatch, {"ok": True}
    )
    archives = []
    for _ in range(3):
        result = backup.run_backup()
        archives.append(result["archive"])
        time.sleep(0.01)
    corrupt = backup_dir / "agent_bot_backup_corrupt.tar.gz"
    corrupt.write_bytes(b"not a valid archive")

    deleted = backup.prune_old_backups(str(backup_dir), keep=1)
    remaining_valid = [
        path for path in archives if os.path.isfile(path)
        and backup.verify_backup_archive(path)["sqlite_integrity"] == "ok"
    ]
    assert len(deleted) == 2
    assert len(remaining_valid) == 1
    assert os.path.isfile(remaining_valid[0] + ".sha256")
    assert corrupt.exists()


def test_restore_preserves_hstagentbot_database_schema_and_settings(tmp_path, monkeypatch):
    production_dir = tmp_path / "production"
    production_dir.mkdir()
    db_path = production_dir / "agent_bot.db"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    monkeypatch.setattr(backup, "DB_PATH", str(db_path))
    backup_dir = tmp_path / "backups"
    monkeypatch.setattr(backup, "BACKUP_DIR", str(backup_dir))
    monkeypatch.setattr(backup, "BACKUPS_TOPIC_ID", 0)
    database.init_db()
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO system_settings(key, value, label, description) "
            "VALUES ('backup_restore_probe', 'preserved', 'Probe', 'T10 restore test')"
        )

    result = backup.run_backup()
    restored = backup.restore_backup_archive(
        result["archive"], str(tmp_path / "isolated-application-restore")
    )
    restored_db = os.path.join(restored["destination"], "agent_bot.db")
    connection = sqlite3.connect(restored_db)
    try:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"accounts", "payment_receipts", "financial_ledger", "receipt_notification_outbox"} <= tables
        row = connection.execute(
            "SELECT value FROM system_settings WHERE key='backup_restore_probe'"
        ).fetchone()
        assert row[0] == "preserved"
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_backup_requires_existing_database(tmp_path, monkeypatch):
    _backup_dir, _uploader, _db_path = prepare_backup(
        tmp_path, monkeypatch, {"ok": True}
    )
    monkeypatch.setattr(backup, "DB_PATH", str(tmp_path / "missing.sqlite3"))
    with pytest.raises(FileNotFoundError, match="does not exist"):
        backup.run_backup()
