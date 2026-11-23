import base64
import hashlib
import io
import os
import socket
import sqlite3
import tarfile

import backup
import crypto_store


def _key(byte):
    return crypto_store.base64.urlsafe_b64encode(bytes([byte]) * 32).decode("ascii").rstrip("=")


class _Uploader:
    def send_telegram_document(self, *args, **kwargs):
        raise AssertionError("restore tests must remain offline")


def _prepare(tmp_path, monkeypatch):
    production = tmp_path / "production"
    production.mkdir()
    db_path = production / "source.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE financial_ledger(id INTEGER PRIMARY KEY, amount INTEGER);
            CREATE TABLE operation_locks(id INTEGER PRIMARY KEY, state TEXT);
            CREATE TABLE payment_receipts(id INTEGER PRIMARY KEY, state TEXT);
            CREATE TABLE orders(id INTEGER PRIMARY KEY, state TEXT);
            CREATE TABLE outbox(id INTEGER PRIMARY KEY, state TEXT);
            INSERT INTO financial_ledger VALUES(1,1250);
            INSERT INTO operation_locks VALUES(1,'held');
            INSERT INTO payment_receipts VALUES(1,'pending');
            INSERT INTO orders VALUES(1,'UNKNOWN');
            INSERT INTO outbox VALUES(1,'pending');
            """
        )
    backup_dir = tmp_path / "archives"
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", _key(8))
    monkeypatch.setattr(backup, "DB_PATH", str(db_path))
    monkeypatch.setattr(backup, "BACKUP_DIR", str(backup_dir))
    monkeypatch.setattr(backup, "BACKUPS_TOPIC_ID", None)
    monkeypatch.setattr(backup, "backup_and_report", _Uploader())
    return db_path, backup_dir, backup.run_backup()["archive"]


def _write_encrypted_archive(path, raw, key):
    encoded = base64.b64encode(raw).decode("ascii")
    ciphertext = crypto_store.encrypt_secret(encoded, "hstagent/archive/v1").encode("ascii")
    with open(path, "wb") as stream:
        stream.write(ciphertext)
    digest = hashlib.sha256(ciphertext).hexdigest()
    with open(path + ".sha256", "w", encoding="ascii") as stream:
        stream.write(f"{digest}  {os.path.basename(path)}\n")


def _network_forbidden(*args, **kwargs):
    raise AssertionError("restore must not start workers or perform network I/O")


def test_unknown_not_replayed_on_restore(tmp_path, monkeypatch):
    db_path, backup_dir, archive_path = _prepare(tmp_path, monkeypatch)
    monkeypatch.setattr(socket.socket, "connect", _network_forbidden)
    monkeypatch.setattr(socket, "create_connection", _network_forbidden)

    # Wrong key must fail before creating or changing the destination.
    wrong_destination = tmp_path / "wrong-key-destination"
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", _key(9))
    try:
        backup.restore_backup_archive(archive_path, str(wrong_destination))
    except Exception:
        pass
    else:
        raise AssertionError("wrong key must be rejected")
    assert not wrong_destination.exists()

    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", _key(8))
    destination = tmp_path / "restored"
    result = backup.restore_backup_archive(archive_path, str(destination))
    restored_db = os.path.join(result["destination"], "agent_bot.db")
    with sqlite3.connect(restored_db) as conn:
        counts = conn.execute(
            "SELECT (SELECT COUNT(*) FROM financial_ledger),"
            "(SELECT COUNT(*) FROM operation_locks),"
            "(SELECT COUNT(*) FROM payment_receipts),"
            "(SELECT COUNT(*) FROM orders WHERE state='UNKNOWN'),"
            "(SELECT COUNT(*) FROM outbox)"
        ).fetchone()
    assert counts == (1, 1, 1, 1, 1)
    assert result["sqlite_integrity"] == "ok"

    # A validly encrypted but path-traversing tarball is rejected before extract.
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as archive:
        import tarfile as _tarfile
        root = _tarfile.TarInfo("safe-root")
        root.type = _tarfile.DIRTYPE
        archive.addfile(root)
        bad = _tarfile.TarInfo("../escaped.txt")
        payload = b"not extracted"
        bad.size = len(payload)
        archive.addfile(bad, io.BytesIO(payload))
    malicious = str(backup_dir / "malicious.tar.gz.enc")
    _write_encrypted_archive(malicious, raw.getvalue(), _key(8))
    malicious_destination = tmp_path / "malicious-restore"
    try:
        backup.restore_backup_archive(malicious, str(malicious_destination))
    except Exception:
        pass
    else:
        raise AssertionError("path traversal must be rejected")
    assert not malicious_destination.exists()
    assert not (tmp_path / "escaped.txt").exists()
