import sqlite3

import backup


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
    db_path = tmp_path / "source.sqlite3"
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
    return backup_dir, uploader


def test_backup_reports_local_archive_success_and_telegram_upload_success(tmp_path, monkeypatch):
    backup_dir, uploader = prepare_backup(tmp_path, monkeypatch, {"ok": True, "message_id": 42})

    result = backup.run_backup()

    assert result["status"] == "ok"
    assert result["telegram_upload_status"] == "sent"
    assert result["telegram_upload_error"] == ""
    assert result["archive"].startswith(str(backup_dir))
    assert result["size_kb"] > 0
    assert len(uploader.calls) == 1


def test_backup_reports_telegram_api_failure_without_claiming_upload_success(tmp_path, monkeypatch):
    backup_dir, uploader = prepare_backup(
        tmp_path, monkeypatch, {"ok": False, "description": "chat not found"}
    )

    result = backup.run_backup()

    assert result["status"] == "ok"
    assert result["telegram_upload_status"] == "failed"
    assert "chat not found" in result["telegram_upload_error"]
    assert result["archive"].startswith(str(backup_dir))
    assert len(uploader.calls) == 1


def test_backup_reports_telegram_upload_exception_and_keeps_archive(tmp_path, monkeypatch):
    backup_dir, uploader = prepare_backup(
        tmp_path, monkeypatch, RuntimeError("telegram network unavailable")
    )

    result = backup.run_backup()

    assert result["status"] == "ok"
    assert result["telegram_upload_status"] == "failed"
    assert "telegram network unavailable" in result["telegram_upload_error"]
    assert result["archive"].startswith(str(backup_dir))
    assert len(uploader.calls) == 1


def test_backup_marks_missing_topic_as_not_configured(tmp_path, monkeypatch):
    backup_dir, uploader = prepare_backup(tmp_path, monkeypatch, {"ok": True})
    monkeypatch.setattr(backup, "BACKUPS_TOPIC_ID", 0)

    result = backup.run_backup()

    assert result["status"] == "ok"
    assert result["telegram_upload_status"] == "not_configured"
    assert "not configured" in result["telegram_upload_error"]
    assert result["archive"].startswith(str(backup_dir))
    assert uploader.calls == []
