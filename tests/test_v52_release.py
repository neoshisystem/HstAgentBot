"""Release-gate regression for missing required evidence."""
from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_release_checker(root: Path):
    path = root / "tools" / "v52_release_check.py"
    spec = importlib.util.spec_from_file_location("_w20_release_check", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_fails_on_missing_evidence():
    root = Path(__file__).resolve().parents[1]
    release = _load_release_checker(root)
    complete_policies = set(release.REQUIRED_POLICY_IDS)
    complete_evidence = set(release.REQUIRED_EVIDENCE_IDS) - {"EV-04"}
    changed = {path: "A" for path in release.ALLOWED_CHANGED_FILES}

    result = release.check_release(
        root,
        changed_files=changed,
        policy_ids=complete_policies,
        evidence_ids=complete_evidence,
        run_tests=False,
    )
    assert result["status"] == "FAIL"
    assert result["checks"]["evidence_complete"] is False
    assert result["missing_evidence_ids"] == ["EV-04"]
