#!/usr/bin/env python3
"""Read-only baseline inventory and syntax probe for HstAgentBot v5.2.

The default path only reads the checkout. Pytest is never spawned unless the
caller supplies --run-tests and explicitly attests an isolated environment via
HSTAGENTBOT_TEST_ISOLATED=1.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

_IGNORED_DIRS = {
    ".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".tox", ".venv", "venv", "env",
}
_DDL_START = re.compile(r"^\s*(CREATE\s+(?:TEMP(?:ORARY)?\s+)?(?:TABLE|INDEX|UNIQUE\s+INDEX|TRIGGER|VIEW)|ALTER\s+TABLE)\b", re.I)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for current, dirs, names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        dirs[:] = sorted(
            name for name in dirs
            if name not in _IGNORED_DIRS and not (current_path / name).is_symlink()
        )
        for name in sorted(names):
            path = current_path / name
            if path.is_symlink() or not path.is_file():
                continue
            files.append(path)
    return sorted(files, key=lambda path: path.relative_to(root).as_posix())


def _check_ddl(root: Path, files: list[Path]) -> dict[str, Any]:
    """Validate standalone SQL scripts and embedded DDL on an in-memory DB.

    All statements are applied only to :memory:. Missing external prerequisites
    are reported separately from malformed SQL and never trigger a live DB read.
    """
    result: dict[str, Any] = {
        "standalone_sql_files": 0,
        "embedded_ddl_candidates": 0,
        "validated": 0,
        "unresolved_dependencies": [],
        "errors": [],
    }
    connection = sqlite3.connect(":memory:")
    try:
        for path in files:
            relative = path.relative_to(root).as_posix()
            try:
                text = path.read_text(encoding="utf-8") if path.suffix.lower() == ".sql" else None
            except (UnicodeError, OSError) as exc:
                result["errors"].append({"path": relative, "error": f"SQL read: {exc}"})
                continue
            if text is not None:
                result["standalone_sql_files"] += 1
                try:
                    connection.executescript(text)
                    result["validated"] += 1
                except sqlite3.Error as exc:
                    result["errors"].append({"path": relative, "error": str(exc)})
                continue

            if path.suffix.lower() != ".py":
                continue
            try:
                source = path.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=relative)
            except (UnicodeError, SyntaxError, OSError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                statement = node.value.strip()
                if not _DDL_START.match(statement):
                    continue
                result["embedded_ddl_candidates"] += 1
                if not sqlite3.complete_statement(statement if statement.endswith(";") else statement + ";"):
                    result["errors"].append({"path": relative, "line": getattr(node, "lineno", None),
                                             "error": "incomplete DDL statement"})
                    continue
                try:
                    # DDL is executed only in this in-memory probe database.
                    connection.execute(statement)
                    result["validated"] += 1
                except sqlite3.OperationalError as exc:
                    message = str(exc)
                    if "no such table" in message or "already exists" in message:
                        result["unresolved_dependencies"].append(
                            {"path": relative, "line": getattr(node, "lineno", None), "reason": message}
                        )
                    else:
                        result["errors"].append(
                            {"path": relative, "line": getattr(node, "lineno", None), "error": message}
                        )
                except sqlite3.Error as exc:
                    result["errors"].append(
                        {"path": relative, "line": getattr(node, "lineno", None), "error": str(exc)}
                    )
    finally:
        connection.close()
    result["status"] = "PASS" if not result["errors"] else "FAIL"
    return result


def collect_baseline(root: str | os.PathLike[str]) -> dict[str, Any]:
    """Collect a deterministic, read-only source baseline for the supplied root directory."""
    base = Path(root).expanduser().resolve(strict=True)
    if not base.is_dir():
        raise NotADirectoryError(str(base))

    paths = _iter_files(base)
    rows: list[dict[str, Any]] = []
    python_files = 0
    test_functions = 0
    syntax_errors: list[dict[str, Any]] = []

    for path in paths:
        relative = path.relative_to(base).as_posix()
        row = {
            "path": relative,
            "size_bytes": path.stat().st_size,
            "sha256": _sha256(path),
        }
        rows.append(row)
        if path.suffix.lower() != ".py":
            continue
        python_files += 1
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=relative)
            test_functions += sum(
                1 for node in ast.walk(tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name.startswith("test_")
            )
        except (UnicodeError, SyntaxError) as exc:
            syntax_errors.append({
                "path": relative,
                "error": str(exc),
                "line": getattr(exc, "lineno", None),
            })

    ddl = _check_ddl(base, paths)
    return {
        "schema_version": "1.0",
        "root": str(base),
        "read_only": True,
        "counts": {
            "files": len(rows),
            "python_files": python_files,
            "test_functions": test_functions,
        },
        "files": rows,
        "syntax": {
            "python_files_checked": python_files,
            "errors": syntax_errors,
            "status": "PASS" if not syntax_errors else "FAIL",
        },
        "ddl": ddl,
        "status": "PASS" if not syntax_errors and ddl["status"] == "PASS" else "FAIL",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=".", help="Product checkout root (read-only)")
    parser.add_argument("--run-tests", action="store_true",
                        help="explicitly run pytest; requires isolated test environment")
    parser.add_argument("--test-target", default="tests/",
                        help="pytest target used only together with --run-tests")
    args = parser.parse_args(argv)

    report = collect_baseline(args.root)
    if args.run_tests:
        if os.environ.get("HSTAGENTBOT_TEST_ISOLATED") != "1":
            report["pytest"] = {
                "status": "BLOCKED",
                "reason": "--run-tests requires HSTAGENTBOT_TEST_ISOLATED=1",
            }
            report["status"] = "FAIL"
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 2
        completed = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", args.test_target],
            cwd=Path(args.root).expanduser().resolve(),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            check=False,
            timeout=3600,
        )
        report["pytest"] = {
            "command": [sys.executable, "-m", "pytest", "-q", args.test_target],
            "returncode": completed.returncode,
            "output": completed.stdout,
            "status": "PASS" if completed.returncode == 0 else "FAIL",
        }
        if completed.returncode:
            report["status"] = "FAIL"

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
