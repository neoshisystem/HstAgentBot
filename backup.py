"""Verified local backup creation and safe disposable restore helpers."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import tarfile
import tempfile
from datetime import datetime, timezone

from config import DB_PATH, BACKUP_DIR, BACKUPS_TOPIC_ID

from server_integrations import load_integration
backup_and_report = load_integration("backup_and_report")

ARCHIVE_PREFIX = "agent_bot_backup_"
ARCHIVE_SUFFIX = ".tar.gz"
MANIFEST_NAME = "backup_manifest.json"
MANIFEST_VERSION = 1
DEFAULT_RETENTION = 30


def _sha256_file(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


def _sqlite_integrity_check(path: str) -> None:
    connection = sqlite3.connect(path, timeout=30)
    try:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
        if not rows or any(row[0] != "ok" for row in rows):
            raise ValueError(f"SQLite integrity_check failed for {path}: {rows[:5]}")
    finally:
        connection.close()


def _safe_member_name(name: str) -> PurePosixPath:
    path = PurePosixPath(name)
    if path.is_absolute() or not path.parts or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"Unsafe archive member path: {name!r}")
    if ":" in path.parts[0]:
        raise ValueError(f"Drive-qualified archive path is not allowed: {name!r}")
    return path


def _archive_members(archive: tarfile.TarFile):
    members = archive.getmembers()
    if not members:
        raise ValueError("Backup archive is empty")

    roots = set()
    names = set()
    for member in members:
        safe_path = _safe_member_name(member.name)
        roots.add(safe_path.parts[0])
        if member.name in names:
            raise ValueError(f"Duplicate archive member: {member.name}")
        names.add(member.name)
        if member.issym() or member.islnk():
            raise ValueError(f"Archive links are not allowed: {member.name}")
        if not (member.isdir() or member.isfile()):
            raise ValueError(f"Unsupported archive member type: {member.name}")
    if len(roots) != 1:
        raise ValueError("Backup archive must contain exactly one root directory")
    root = next(iter(roots))
    root_member = next((m for m in members if m.name == root), None)
    if not root_member or not root_member.isdir():
        raise ValueError("Backup archive root directory is missing")
    return members, root


def verify_backup_archive(archive_path: str) -> dict:
    """Verify archive checksum, manifest payloads and the SQLite snapshot."""
    archive_path = os.path.abspath(archive_path)
    if not os.path.isfile(archive_path):
        raise FileNotFoundError(f"Backup archive does not exist: {archive_path}")

    sidecar_path = archive_path + ".sha256"
    if not os.path.isfile(sidecar_path):
        raise ValueError("Backup SHA-256 sidecar is missing")
    sidecar = Path(sidecar_path).read_text(encoding="utf-8").strip().split()
    if not sidecar or len(sidecar[0]) != 64:
        raise ValueError("Backup SHA-256 sidecar is malformed")
    actual_archive_hash = _sha256_file(archive_path)
    if actual_archive_hash.lower() != sidecar[0].lower():
        raise ValueError("Backup archive SHA-256 does not match its sidecar")

    try:
        archive = tarfile.open(archive_path, "r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise ValueError(f"Backup archive cannot be opened: {exc}") from exc

    with archive:
        members, root = _archive_members(archive)
        by_name = {member.name: member for member in members}
        manifest_member = by_name.get(f"{root}/{MANIFEST_NAME}")
        if not manifest_member or not manifest_member.isfile():
            raise ValueError("Backup manifest is missing")
        manifest_stream = archive.extractfile(manifest_member)
        if manifest_stream is None:
            raise ValueError("Backup manifest cannot be read")
        try:
            manifest = json.load(manifest_stream)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("Backup manifest is invalid JSON") from exc

        if manifest.get("format_version") != MANIFEST_VERSION:
            raise ValueError("Unsupported backup manifest format")
        declared = manifest.get("files")
        if not isinstance(declared, list) or not declared:
            raise ValueError("Backup manifest has no payload file list")

        expected_names = {f"{root}/{MANIFEST_NAME}"}
        total_bytes = 0
        for item in declared:
            if not isinstance(item, dict):
                raise ValueError("Backup manifest entry is invalid")
            relative = _safe_member_name(str(item.get("path") or ""))
            member_name = f"{root}/{relative.as_posix()}"
            if member_name in expected_names:
                raise ValueError(f"Duplicate manifest path: {relative}")
            expected_names.add(member_name)
            member = by_name.get(member_name)
            if not member or not member.isfile():
                raise ValueError(f"Manifest payload is missing: {relative}")
            expected_size = int(item.get("size_bytes", -1))
            expected_hash = str(item.get("sha256") or "").lower()
            if expected_size < 0 or len(expected_hash) != 64:
                raise ValueError(f"Manifest metadata is invalid for {relative}")

            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f"Cannot read archive payload: {relative}")
            digest = hashlib.sha256()
            size = 0
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                digest.update(chunk)
            if size != expected_size or digest.hexdigest() != expected_hash:
                raise ValueError(f"Backup payload integrity mismatch: {relative}")
            total_bytes += size

        actual_file_names = {m.name for m in members if m.isfile()}
        if actual_file_names != expected_names:
            raise ValueError("Archive files do not match the manifest")

        db_member = by_name.get(f"{root}/agent_bot.db")
        if not db_member or not db_member.isfile():
            raise ValueError("Backup does not contain agent_bot.db")
        db_stream = archive.extractfile(db_member)
        if db_stream is None:
            raise ValueError("SQLite snapshot cannot be read")
        temp_db_fd, temp_db_path = tempfile.mkstemp(prefix="hst_backup_verify_", suffix=".sqlite3")
        try:
            with os.fdopen(temp_db_fd, "wb") as target:
                shutil.copyfileobj(db_stream, target)
            _sqlite_integrity_check(temp_db_path)
        finally:
            try:
                os.remove(temp_db_path)
            except FileNotFoundError:
                pass

        return {
            "archive": archive_path,
            "sha256": actual_archive_hash,
            "manifest": manifest,
            "file_count": len(declared),
            "payload_bytes": total_bytes,
            "sqlite_integrity": "ok",
        }


def _write_sha256_sidecar(archive_path: str, digest: str) -> str:
    sidecar_path = archive_path + ".sha256"
    fd, temporary_path = tempfile.mkstemp(
        prefix=".backup_sha256_", suffix=".tmp", dir=os.path.dirname(archive_path)
    )
    try:
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            stream.write(f"{digest}  {os.path.basename(archive_path)}\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, sidecar_path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)
    return sidecar_path


def _is_path_overlap(first: str, second: str) -> bool:
    first = os.path.realpath(os.path.abspath(first))
    second = os.path.realpath(os.path.abspath(second))
    try:
        return os.path.commonpath([first, second]) in (first, second)
    except ValueError:
        return False


def restore_backup_archive(archive_path: str, destination_dir: str) -> dict:
    """Extract only to a new/empty disposable destination outside configured live dirs."""
    verification = verify_backup_archive(archive_path)
    destination_dir = os.path.abspath(os.path.expanduser(destination_dir))
    production_db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    configured_backup_dir = os.path.abspath(BACKUP_DIR)

    if os.path.islink(destination_dir):
        raise ValueError("Restore destination cannot be a symlink")
    if _is_path_overlap(destination_dir, production_db_dir):
        raise ValueError("Restore destination overlaps the configured production database directory")
    if _is_path_overlap(destination_dir, configured_backup_dir):
        raise ValueError("Restore destination overlaps the configured backup directory")

    destination_existed = os.path.isdir(destination_dir)
    if os.path.exists(destination_dir):
        if not destination_existed or os.listdir(destination_dir):
            raise ValueError("Restore destination must be a new or empty directory")
    else:
        os.makedirs(destination_dir, exist_ok=False)

    restored_root = None
    try:
        with tarfile.open(archive_path, "r:gz") as archive:
            members, root = _archive_members(archive)
            for member in members:
                safe_path = _safe_member_name(member.name)
                target_path = os.path.realpath(
                    os.path.join(destination_dir, *safe_path.parts)
                )
                if not _is_path_overlap(target_path, destination_dir) or target_path == destination_dir:
                    raise ValueError(f"Archive path escapes restore destination: {member.name}")
                if member.isdir():
                    os.makedirs(target_path, exist_ok=True)
                    continue
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                source = archive.extractfile(member)
                if source is None:
                    raise ValueError(f"Cannot extract archive member: {member.name}")
                with source, open(target_path, "xb") as target:
                    shutil.copyfileobj(source, target)

        restored_root = os.path.join(destination_dir, root)
        restored_db = os.path.join(restored_root, "agent_bot.db")
        if not os.path.isfile(restored_db):
            raise ValueError("Restored SQLite database is missing")
        _sqlite_integrity_check(restored_db)
        return {
            "status": "restored",
            "destination": restored_root,
            "sha256": verification["sha256"],
            "sqlite_integrity": "ok",
            "file_count": verification["file_count"],
        }
    except Exception:
        # Remove only the extracted archive root. Preserve a caller-owned empty
        # destination directory; remove the destination itself only when this
        # function created it.
        if restored_root and os.path.isdir(restored_root):
            shutil.rmtree(restored_root, ignore_errors=True)
        if not destination_existed and os.path.isdir(destination_dir):
            try:
                os.rmdir(destination_dir)
            except OSError:
                pass
        raise


def prune_old_backups(backup_dir: str | None = None, keep: int = DEFAULT_RETENTION) -> list[str]:
    """Prune only verified archives and always keep at least one verified backup."""
    backup_dir = os.path.abspath(backup_dir or BACKUP_DIR)
    keep = max(1, int(keep))
    verified = []
    for filename in os.listdir(backup_dir):
        if not (filename.startswith(ARCHIVE_PREFIX) and filename.endswith(ARCHIVE_SUFFIX)):
            continue
        archive_path = os.path.join(backup_dir, filename)
        try:
            verify_backup_archive(archive_path)
        except Exception:
            # Unverified/legacy archives are preserved for manual inspection.
            continue
        verified.append((os.path.getmtime(archive_path), archive_path))

    verified.sort(key=lambda item: (item[0], item[1]), reverse=True)
    deleted = []
    for _, archive_path in verified[keep:]:
        # verified[0:keep] always includes the newest valid archive.
        os.remove(archive_path)
        sidecar_path = archive_path + ".sha256"
        if os.path.exists(sidecar_path):
            os.remove(sidecar_path)
        deleted.append(archive_path)
    return deleted


def _build_manifest(temp_dir: str, created_at: str) -> dict:
    entries = []
    for root, dirs, files in os.walk(temp_dir):
        dirs.sort()
        for filename in sorted(files):
            full_path = os.path.join(root, filename)
            relative = os.path.relpath(full_path, temp_dir).replace(os.sep, "/")
            if relative == MANIFEST_NAME:
                continue
            if os.path.islink(full_path) or not os.path.isfile(full_path):
                raise ValueError(f"Backup payload must be a regular file: {relative}")
            entries.append({
                "path": relative,
                "size_bytes": os.path.getsize(full_path),
                "sha256": _sha256_file(full_path),
            })
    entries.sort(key=lambda item: item["path"])
    return {
        "format_version": MANIFEST_VERSION,
        "created_at": created_at,
        "files": entries,
    }


def run_backup():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    created_at = datetime.now(timezone.utc).isoformat()
    temp_dir = tempfile.mkdtemp(prefix="hst_bot_backup_")

    try:
        # 1. Create a consistent SQLite snapshot; missing DB is a hard failure.
        temp_db = os.path.join(temp_dir, "agent_bot.db")
        if not os.path.isfile(DB_PATH):
            raise FileNotFoundError(f"Configured SQLite database does not exist: {DB_PATH}")
        src = sqlite3.connect(DB_PATH, timeout=30)
        dst = sqlite3.connect(temp_db)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
            src.close()
        _sqlite_integrity_check(temp_db)

        # 2. Copy source files and optional document registry.
        code_dir = os.path.dirname(os.path.abspath(__file__))
        for item in sorted(os.listdir(code_dir)):
            if item.endswith(".py"):
                source = os.path.join(code_dir, item)
                if os.path.isfile(source) and not os.path.islink(source):
                    shutil.copy2(source, temp_dir)

        registry_path = os.path.join(code_dir, "data", "docs_registry.json")
        if os.path.isfile(registry_path):
            registry_target = os.path.join(temp_dir, "docs_registry.json")
            shutil.copy2(registry_path, registry_target)

        # 3. Create manifest and a uniquely named archive.
        manifest = _build_manifest(temp_dir, created_at)
        manifest_path = os.path.join(temp_dir, MANIFEST_NAME)
        with open(manifest_path, "w", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")

        archive_path = os.path.join(BACKUP_DIR, f"{ARCHIVE_PREFIX}{timestamp}{ARCHIVE_SUFFIX}")
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(temp_dir, arcname=f"backup_{timestamp}")

        size_kb = round(os.path.getsize(archive_path) / 1024, 2)
        sha256_hash = _sha256_file(archive_path)
        _write_sha256_sidecar(archive_path, sha256_hash)

        # 4. Reopen and validate the exact artifact before upload or pruning.
        verification = verify_backup_archive(archive_path)

        caption = (
            f"📦 <b>بکاپ کامل دیتابیس و تنظیمات ربات (@HstAgent_bot)</b>\n\n"
            f"🏷 #Backup #HstAgentBot #3XUI #SQLite #D{datetime.now().strftime('%Y%m%d')}\n\n"
            f"📅 تاریخ: <code>{datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC</code>\n"
            f"📦 حجم آرشیو: <code>{size_kb} KB</code>\n"
            f"🔑 هش SHA256:\n<code>{sha256_hash}</code>\n\n"
            f"📁 <b>محتویات:</b> دیتابیس SQLite، فایل‌های منبع Python و رجیستری مستندات (در صورت وجود).\n"
            f"🔄 <b>راستی‌آزمایی:</b> manifest، SHA-256 و SQLite integrity_check موفق."
        )
        upload_status = "not_configured"
        upload_error = ""
        if BACKUPS_TOPIC_ID:
            try:
                upload_res = backup_and_report.send_telegram_document(
                    BACKUPS_TOPIC_ID, archive_path, caption, parse_mode="HTML"
                )
                if isinstance(upload_res, dict) and upload_res.get("ok"):
                    upload_status = "sent"
                else:
                    upload_status = "failed"
                    upload_error = (
                        str(upload_res.get("description") or upload_res)
                        if isinstance(upload_res, dict)
                        else f"Unexpected upload result type: {type(upload_res).__name__}"
                    )
            except Exception as exc:
                upload_status = "failed"
                upload_error = str(exc)
        else:
            upload_error = "BACKUPS_TOPIC_ID is not configured"

        # 5. Retain only verified archives. The current valid archive is included.
        pruned = prune_old_backups(BACKUP_DIR, keep=DEFAULT_RETENTION)

        return {
            "status": "ok",
            "archive": archive_path,
            "sha256_file": archive_path + ".sha256",
            "size_kb": size_kb,
            "sha256": sha256_hash,
            "verified": True,
            "verified_files": verification["file_count"],
            "sqlite_integrity": verification["sqlite_integrity"],
            "telegram_upload_status": upload_status,
            "telegram_upload_error": upload_error,
            "pruned_archives": pruned,
        }
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    result = run_backup()
    print("Backup completed:", result)
