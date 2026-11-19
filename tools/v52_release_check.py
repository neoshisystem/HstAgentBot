"""W20 release gate for reproducible compile, test, evidence and scope checks."""
from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

BASE_SHA = "65a7af32eba9ffa52043ed84fe65927c47f800fc"
PRODUCT_VERSION = "5.2"
ALLOWED_CHANGED_FILES = (
    "readiness_service.py",
    "tools/v52_rehearsal.py",
    "tools/v52_release_check.py",
    "tests/test_v52_readiness.py",
    "tests/test_v52_acceptance_finance.py",
    "tests/test_v52_acceptance_access.py",
    "tests/test_v52_rehearsal.py",
    "tests/test_v52_release.py",
)
REQUIRED_POLICY_IDS = frozenset(
    [f"PA-{number:02d}" for number in range(1, 27)] + ["OPT-01", "OPT-07"]
)
REQUIRED_EVIDENCE_IDS = frozenset(f"EV-{number:02d}" for number in range(1, 6))
EVIDENCE_SOURCES = {
    "EV-01": "tests/test_v52_acceptance_finance.py",
    "EV-02": "tests/test_v52_acceptance_access.py",
    "EV-03": "tests/test_v52_dispatch.py",
    "EV-04": "tests/test_v52_bot_context.py",
    "EV-05": "tests/test_v52_gateway_http.py",
}
_SECRET_PATTERNS = (
    re.compile(r"\b[0-9]{8,12}:[A-Za-z0-9_-]{30,}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bsk-[A-Za-z0-9]{24,}\b"),
    re.compile(r"(?i)\baws_secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{30,}"),
)


def _git(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=root, check=True, capture_output=True,
            text=True, timeout=20,
        )
        return result.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _changed_file_status(root: Path, base_sha: str) -> dict[str, str]:
    output = _git(root, "diff", "--name-status", f"{base_sha}...HEAD")
    if output is None:
        return {}
    result: dict[str, str] = {}
    for line in output.splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            result[parts[1]] = parts[0]
    return result


def _load_policy_ids(root: Path) -> tuple[set[str], str | None]:
    path = root / "policy_registry.py"
    if not path.is_file():
        return set(), None
    spec = importlib.util.spec_from_file_location("_w20_policy_registry", path)
    if spec is None or spec.loader is None:
        return set(), None
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        return set(), None
    policies = getattr(module, "APPROVED_POLICIES", ())
    ids = {str(item[0]) for item in policies if isinstance(item, (tuple, list)) and item}
    if len(ids) != len(policies) or len(ids) != 28:
        return ids, None
    return ids, str(getattr(module, "POLICY_VERSION", ""))


def _evidence_digests(root: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for evidence_id, relative in EVIDENCE_SOURCES.items():
        path = root / relative
        if path.is_file():
            digests[evidence_id] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def _python_files(root: Path) -> Iterable[Path]:
    excluded = {".git", ".venv", "venv", "__pycache__", ".pytest_cache"}
    for path in root.rglob("*.py"):
        if any(part in excluded for part in path.relative_to(root).parts):
            continue
        yield path


def _compile_in_memory(root: Path) -> list[str]:
    failures: list[str] = []
    for path in _python_files(root):
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except (OSError, UnicodeError, SyntaxError, ValueError) as exc:
            failures.append(f"{path.relative_to(root)}:{type(exc).__name__}")
    return failures


def _callback_violations(root: Path) -> list[dict[str, Any]]:
    violations: list[dict[str, Any]] = []
    for path in _python_files(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, UnicodeError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "callback_data":
                    continue
                value = keyword.value
                literal = None
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    literal = value.value
                elif isinstance(value, ast.JoinedStr):
                    pieces = [
                        item.value for item in value.values
                        if isinstance(item, ast.Constant) and isinstance(item.value, str)
                    ]
                    literal = "".join(pieces)
                if literal is not None and len(literal.encode("utf-8")) > 64:
                    violations.append({
                        "path": str(path.relative_to(root)),
                        "line": getattr(node, "lineno", 0),
                        "bytes": len(literal.encode("utf-8")),
                    })
    return violations


def _secret_findings(root: Path) -> list[str]:
    findings: list[str] = []
    ignored = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", "tests"}
    for path in root.rglob("*"):
        if not path.is_file() or any(part in ignored for part in path.relative_to(root).parts):
            continue
        if path.suffix.lower() not in {".py", ".json", ".yml", ".yaml", ".toml", ".sql", ".ini", ".md", ".txt"} and not path.name.lower().startswith(".env"):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        if any(pattern.search(content) for pattern in _SECRET_PATTERNS):
            findings.append(str(path.relative_to(root)))
    return findings


def check_release(
    root: str | Path | None = None,
    *,
    base_sha: str = BASE_SHA,
    changed_files: Mapping[str, str] | Iterable[str] | None = None,
    policy_ids: Iterable[str] | None = None,
    evidence_ids: Iterable[str] | None = None,
    run_tests: bool = False,
    minimum_tests: int = 502,
) -> dict[str, Any]:
    """Return a machine-readable release decision; failures never get skipped."""
    root_path = Path(root or Path(__file__).resolve().parents[1]).resolve()
    if changed_files is None:
        changed = _changed_file_status(root_path, base_sha)
    elif isinstance(changed_files, Mapping):
        changed = {str(path): str(status) for path, status in changed_files.items()}
    else:
        changed = {str(path): "A" for path in changed_files}

    expected_paths = set(ALLOWED_CHANGED_FILES)
    actual_paths = set(changed)
    changed_scope_ok = actual_paths == expected_paths and all(
        changed[path] == "A" for path in expected_paths if path in changed
    )
    missing_paths = sorted(expected_paths - actual_paths)
    unexpected_paths = sorted(actual_paths - expected_paths)
    non_added_paths = sorted(path for path, status in changed.items() if status != "A")

    discovered_policies, policy_version = _load_policy_ids(root_path)
    selected_policies = set(policy_ids) if policy_ids is not None else discovered_policies
    missing_policies = sorted(REQUIRED_POLICY_IDS - selected_policies)
    unexpected_policies = sorted(selected_policies - REQUIRED_POLICY_IDS)

    evidence_hashes = _evidence_digests(root_path)
    selected_evidence = (
        set(evidence_ids) if evidence_ids is not None else set(evidence_hashes)
    )
    missing_evidence = sorted(REQUIRED_EVIDENCE_IDS - selected_evidence)
    missing_evidence_sources = sorted(
        item for item in REQUIRED_EVIDENCE_IDS if item not in evidence_hashes
    )

    missing_files = sorted(path for path in expected_paths if not (root_path / path).is_file())
    compile_failures = _compile_in_memory(root_path)
    callback_violations = _callback_violations(root_path)
    secret_findings = _secret_findings(root_path)

    commit_sha = _git(root_path, "rev-parse", "HEAD")
    tree_sha = _git(root_path, "rev-parse", "HEAD^{tree}")
    pytest_result: dict[str, Any] = {"status": "NOT_RUN"}
    if run_tests:
        try:
            process = subprocess.run(
                [sys.executable, "-m", "pytest", "-q"],
                cwd=root_path, capture_output=True, text=True, timeout=1200,
            )
            output = (process.stdout or "") + "\n" + (process.stderr or "")
            match = re.search(r"(?m)(\d+)\s+passed\b", output)
            passed = int(match.group(1)) if match else 0
            pytest_result = {
                "status": "PASS" if process.returncode == 0 and passed >= minimum_tests else "FAIL",
                "returncode": process.returncode,
                "passed": passed,
                "summary": output[-3000:],
            }
        except (OSError, subprocess.SubprocessError) as exc:
            pytest_result = {"status": "FAIL", "error": type(exc).__name__}

    checks = {
        "changed_file_budget": changed_scope_ok,
        "all_expected_files_present": not missing_files,
        "policy_registry_complete": not missing_policies and not unexpected_policies,
        "evidence_complete": not missing_evidence and not missing_evidence_sources,
        "compile": not compile_failures,
        "callback_bytes": not callback_violations,
        "raw_secrets": not secret_findings,
        "git_metadata": bool(commit_sha and tree_sha),
        "pytest": pytest_result["status"] in {"PASS", "NOT_RUN"},
    }
    failures = [name for name, passed in checks.items() if not passed]
    return {
        "status": "PASS" if not failures else "FAIL",
        "checks": checks,
        "failures": failures,
        "changed_files": changed,
        "missing_paths": missing_paths,
        "unexpected_paths": unexpected_paths,
        "non_added_paths": non_added_paths,
        "missing_files": missing_files,
        "missing_policy_ids": missing_policies,
        "unexpected_policy_ids": unexpected_policies,
        "missing_evidence_ids": missing_evidence,
        "missing_evidence_sources": missing_evidence_sources,
        "compile_failures": compile_failures,
        "callback_violations": callback_violations,
        "secret_findings": secret_findings,
        "evidence_sha256": evidence_hashes,
        "commit_sha": commit_sha,
        "tree_sha": tree_sha,
        "product_version": PRODUCT_VERSION,
        "policy_version": policy_version,
        "pytest": pytest_result,
    }


def main() -> int:
    result = check_release(run_tests=True)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
