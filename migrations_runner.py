"""Atomic, checksum-verified SQLite migration runner for HstAgentBot v5.2."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path
from typing import Callable


_REGISTRY_SQL = """CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    checksum TEXT NOT NULL,
    applied_at INTEGER NOT NULL
)"""
_TRANSACTION_KEYWORDS = {"BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT", "RELEASE"}


class MigrationError(RuntimeError):
    """Base error raised when a migration cannot be applied safely."""


class MigrationChecksumMismatch(MigrationError):
    """Raised when an already-applied migration's bytes have changed."""


def _numeric_version(path: Path) -> tuple[int, str]:
    """Return a stable numeric ordering key and reject unversioned filenames."""
    match = re.match(r"^(\d+)(?:[_-]|$)", path.stem)
    if match is None:
        raise MigrationError(
            f"migration filename must begin with a numeric version: {path.name}"
        )
    return int(match.group(1)), path.name


def _migration_files(directory: Path) -> list[Path]:
    if not directory.exists() or not directory.is_dir():
        raise MigrationError(f"migration directory does not exist: {directory}")

    files: list[Path] = []
    for path in directory.iterdir():
        if path.suffix.lower() != ".sql":
            continue
        if path.is_symlink() or not path.is_file():
            raise MigrationError(f"migration must be a regular file: {path.name}")
        files.append(path)

    files.sort(key=_numeric_version)
    numeric_versions = [_numeric_version(path)[0] for path in files]
    if len(numeric_versions) != len(set(numeric_versions)):
        raise MigrationError("duplicate numeric migration version")
    return files


def _first_keyword(statement: str) -> str | None:
    """Read the first SQL keyword while skipping leading whitespace/comments."""
    i = 0
    length = len(statement)
    while i < length:
        while i < length and statement[i].isspace():
            i += 1
        if statement.startswith("--", i):
            end = statement.find("\n", i + 2)
            if end < 0:
                return None
            i = end + 1
            continue
        if statement.startswith("/*", i):
            end = statement.find("*/", i + 2)
            if end < 0:
                raise MigrationError("unterminated SQL block comment")
            i = end + 2
            continue
        break

    match = re.match(r"([A-Za-z_][A-Za-z_0-9]*)", statement[i:])
    return match.group(1).upper() if match else None


def _split_statements(sql_text: str) -> list[str]:
    """Split SQL with SQLite's parser-aware completeness check.

    Checking at semicolons rather than lines handles several statements on one
    line, semicolons inside string literals, and multi-line CREATE TRIGGER bodies.
    """
    statements: list[str] = []
    buffer: list[str] = []

    for character in sql_text:
        buffer.append(character)
        if character == ";":
            candidate = "".join(buffer)
            if sqlite3.complete_statement(candidate):
                statement = candidate.strip()
                if statement:
                    keyword = _first_keyword(statement)
                    if keyword in _TRANSACTION_KEYWORDS:
                        raise MigrationError(
                            f"transaction control is forbidden inside migrations: {keyword}"
                        )
                    statements.append(statement)
                buffer.clear()

    tail = "".join(buffer)
    if tail.strip():
        # A final statement without a semicolon is still accepted only when
        # SQLite confirms it becomes complete after adding the terminator.
        candidate = tail + "\n;"
        if not sqlite3.complete_statement(candidate):
            raise MigrationError("incomplete SQL statement at end of migration")
        statement = candidate.strip()
        keyword = _first_keyword(statement)
        if keyword in _TRANSACTION_KEYWORDS:
            raise MigrationError(
                f"transaction control is forbidden inside migrations: {keyword}"
            )
        statements.append(statement)

    return statements


def _timestamp(now: int | Callable[[], int]) -> int:
    value = now() if callable(now) else now
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("now must be an integer epoch-seconds value or a callable returning one")
    return value


def apply_migrations(
    conn: sqlite3.Connection,
    migration_dir: str | Path,
    now: int | Callable[[], int],
) -> list[str]:
    """Apply unapplied SQL migrations atomically and return applied versions.

    Each migration and its registry row commit in the same BEGIN IMMEDIATE
    transaction. An already-applied version with identical bytes is a no-op;
    changed bytes raise MigrationChecksumMismatch.
    """
    if conn.in_transaction:
        raise MigrationError("connection already has an active transaction")

    directory = Path(migration_dir)
    files = _migration_files(directory)

    # Registry creation is deliberately outside per-migration transactions.
    conn.execute(_REGISTRY_SQL)

    applied: list[str] = []
    for path in files:
        version = path.stem
        content = path.read_bytes()
        checksum = hashlib.sha256(content).hexdigest()
        try:
            sql_text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise MigrationError(f"migration is not valid UTF-8: {path.name}") from exc
        statements = _split_statements(sql_text)

        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT checksum FROM schema_migrations WHERE version = ?",
                (version,),
            ).fetchone()
            if row is not None:
                if row[0] != checksum:
                    raise MigrationChecksumMismatch(
                        f"checksum mismatch for migration {version}: "
                        f"recorded={row[0]} current={checksum}"
                    )
                conn.rollback()
                continue

            for statement in statements:
                conn.execute(statement)
                if not conn.in_transaction:
                    raise MigrationError(
                        f"migration ended its transaction unexpectedly: {version}"
                    )

            conn.execute(
                "INSERT INTO schema_migrations(version, checksum, applied_at) "
                "VALUES (?, ?, ?)",
                (version, checksum, _timestamp(now)),
            )
            conn.commit()
            applied.append(version)
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise

    return applied
