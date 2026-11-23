"""Read-only acceptance test for the v5.2 baseline probe."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tools import baseline_probe


def test_probe_is_read_only(tmp_path, monkeypatch, capsys):
    (tmp_path / "alpha.py").write_text(
        "def test_alpha():\n    return True\n\nasync def test_async_alpha():\n    return None\n",
        encoding="utf-8",
    )
    (tmp_path / "beta.py").write_text(
        "def helper():\n    return 7\n",
        encoding="utf-8",
    )
    (tmp_path / "schema.sql").write_text(
        "CREATE TABLE baseline_probe_fixture (id INTEGER PRIMARY KEY, label TEXT NOT NULL);\n",
        encoding="utf-8",
    )

    before = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.iterdir()
    }

    first = baseline_probe.collect_baseline(tmp_path)
    second = baseline_probe.collect_baseline(tmp_path)

    assert first["counts"] == {"files": 3, "python_files": 2, "test_functions": 2}
    assert first["syntax"]["status"] == "PASS"
    assert first["ddl"]["status"] == "PASS"
    assert first["ddl"]["standalone_sql_files"] == 1
    assert first["status"] == "PASS"
    assert {row["path"]: row["sha256"] for row in first["files"]} == {
        row["path"]: row["sha256"] for row in second["files"]
    }

    for row in first["files"]:
        data = (tmp_path / row["path"]).read_bytes()
        assert row["sha256"] == hashlib.sha256(data).hexdigest()

    def forbidden_subprocess(*args, **kwargs):
        raise AssertionError("subprocess must not run without --run-tests")

    monkeypatch.setattr(baseline_probe.subprocess, "run", forbidden_subprocess)
    assert baseline_probe.main(["--root", str(tmp_path)]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "PASS"

    after = {
        path.name: (path.read_bytes(), path.stat().st_mtime_ns)
        for path in tmp_path.iterdir()
    }
    assert after == before
    assert set(after) == {"alpha.py", "beta.py", "schema.sql"}
