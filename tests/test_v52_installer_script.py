"""Static contract tests for the HstAgentBot v1.1.0 installer."""
from pathlib import Path
import re
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "hstagentbot-installer.sh"
GUIDE = ROOT / "docs" / "INSTALLER_FA.md"
README = ROOT / "README.md"


def _installer_text() -> str:
    return INSTALLER.read_text(encoding="utf-8")


def test_installer_exists_and_passes_bash_syntax():
    assert INSTALLER.is_file()
    bash = shutil.which("bash")
    assert bash, "bash is required"
    result = subprocess.run(
        [bash, "-n", str(INSTALLER)], cwd=ROOT,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_installer_help_exits_successfully():
    result = subprocess.run(
        ["bash", str(INSTALLER), "--help"], cwd=ROOT,
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    for option in ("--check-only", "--skip-tests", "--update", "--ref REF"):
        assert option in result.stdout


def test_base_tests_and_migration_smoke_use_opposite_flags():
    source = _installer_text()
    assert re.search(r"env\s+V52_MIGRATIONS_ENABLED=0\s+DB_PATH=", source)
    smoke = re.search(
        r"run_migration_smoke_test\(\)(.*?)(?=^run_project_tests\(\))",
        source, flags=re.MULTILINE | re.DOTALL,
    )
    assert smoke, "run_migration_smoke_test is missing"
    for required in (
        "V52_MIGRATIONS_ENABLED=1", "database.init_db()",
        "database.upsert_account_record(", "PRAGMA integrity_check",
        "PRAGMA foreign_key_check",
    ):
        assert required in smoke.group(1)


def test_interactive_reads_use_dev_tty_when_available():
    source = _installer_text()
    assert "[[ -r /dev/tty ]]" in source
    assert re.search(r"read_input\(\).*?</dev/tty", source, re.DOTALL)
    assert re.search(r"read_secret\(\).*?</dev/tty", source, re.DOTALL)


def test_installer_docs_and_readme_contain_no_real_tokens_or_keys():
    token_pattern = re.compile(r"\b[0-9]{6,}:[A-Za-z0-9_-]{30,}\b")
    key_assignment = re.compile(
        r"(?im)^\s*BACKUP_ENCRYPTION_KEY\s*=\s*[A-Za-z0-9_+/=-]{40,}\s*$"
    )
    for path in (INSTALLER, GUIDE, README):
        assert path.is_file(), f"missing scan target: {path}"
        content = path.read_text(encoding="utf-8")
        assert not token_pattern.search(content), f"possible Telegram token in {path}"
        assert not key_assignment.search(content), f"possible live encryption key in {path}"
