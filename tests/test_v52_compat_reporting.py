"""W19.3: Internal reporting preserves consumer signatures and queues only."""
import internal_reporting as reporting


def test_backup_queue_not_external_import(monkeypatch):
    import backup

    queued = []
    monkeypatch.setattr(backup, "verify_backup_archive", lambda _path: {"sha256": "a" * 64})
    monkeypatch.setattr(
        reporting, "_queue",
        lambda logical_key, **kwargs: queued.append((logical_key, kwargs)) or {
            "ok": True, "queued": True, "state": "pending", "delivery_id": 5,
        },
    )
    from pathlib import Path
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        archive = Path(directory) / "synthetic.tar.gz.enc"
        archive.write_bytes(b"ciphertext-marker")
        result = reporting.send_telegram_document(
            -100123, str(archive), "verified backup", parse_mode="HTML",
        )
    assert result["queued"] is True
    assert queued[0][0] == "backupfile"
    assert queued[0][1]["resource_type"] == "encrypted_backup"
    import json
    resource = json.loads(queued[0][1]["resource_id"])
    assert resource == {"path": str(archive)}
    assert len(queued[0][1]["resource_id"]) <= 240
    assert not hasattr(reporting, "BOT_TOKEN")
