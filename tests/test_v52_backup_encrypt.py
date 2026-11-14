import base64
import io
import os
import sqlite3
import tarfile

import backup
import crypto_store


class _Uploader:
    def send_telegram_document(self, *args, **kwargs):
        raise AssertionError("network upload must not occur in offline tests")


def _key(byte):
    return crypto_store.base64.urlsafe_b64encode(bytes([byte]) * 32).decode("ascii").rstrip("=")


def _prepare(tmp_path, monkeypatch):
    production = tmp_path / "production"
    production.mkdir()
    db_path = production / "source.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO sample(value) VALUES ('backup-data')")
    backup_dir = tmp_path / "archives"
    monkeypatch.setattr(backup, "DB_PATH", str(db_path))
    monkeypatch.setattr(backup, "BACKUP_DIR", str(backup_dir))
    monkeypatch.setattr(backup, "BACKUPS_TOPIC_ID", None)
    monkeypatch.setattr(backup, "backup_and_report", _Uploader())
    return db_path, backup_dir


def test_encrypted_archive_integrity(tmp_path, monkeypatch):
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", _key(3))
    db_path, backup_dir = _prepare(tmp_path, monkeypatch)
    result = backup.run_backup()
    archive_path = result["archive"]
    assert result["encrypted"] is True
    assert archive_path.endswith(".tar.gz.enc")
    ciphertext = open(archive_path, "rb").read()
    assert not ciphertext.startswith(b"\x1f\x8b")
    sidecar = open(archive_path + ".sha256", encoding="ascii").read().split()[0]
    assert sidecar == result["sha256"]
    verified = backup.verify_backup_archive(archive_path)
    assert verified["sqlite_integrity"] == "ok"
    assert verified["sha256"] == sidecar

    envelope = ciphertext.decode("ascii")
    payload_b64 = crypto_store.decrypt_secret(envelope, "hstagent/archive/v1")
    raw_archive = base64.b64decode(payload_b64, validate=True)
    with tarfile.open(fileobj=io.BytesIO(raw_archive), mode="r:gz") as archive:
        names = archive.getnames()
        file_names = [name for name in names if archive.getmember(name).isfile()]
    assert any(name.endswith("/agent_bot.db") for name in file_names)
    assert not any(name.endswith(("-wal", "-shm")) for name in file_names)
    assert not any(".env" in name or "BACKUP_ENCRYPTION_KEY" in name for name in names)
    assert not any(str(db_path) in name for name in names)

    # Missing key rejects creation and verification; wrong key rejects ciphertext.
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY")
    try:
        backup.run_backup()
    except Exception:
        pass
    else:
        raise AssertionError("backup creation without key must fail closed")
    try:
        backup.verify_backup_archive(archive_path)
    except Exception:
        pass
    else:
        raise AssertionError("verification without key must fail closed")

    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", _key(4))
    try:
        backup.verify_backup_archive(archive_path)
    except Exception:
        pass
    else:
        raise AssertionError("wrong key must fail closed")

    # Updating the sidecar cannot make modified ciphertext authentic.
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", _key(3))
    changed = bytearray(ciphertext)
    changed[-2] ^= 1
    with open(archive_path, "wb") as stream:
        stream.write(changed)
    backup._write_sha256_sidecar(archive_path, backup._sha256_file(archive_path))
    try:
        backup.verify_backup_archive(archive_path)
    except Exception:
        pass
    else:
        raise AssertionError("tampered encrypted archive must fail")
