"""Static contract tests for the HstAgentBot v1.2.0 installer."""
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
    for option in ("--check-only", "--skip-tests", "--update", "--ref REF",
                   "--service-name NAME", "--advanced"):
        assert option in result.stdout


def test_base_tests_and_migration_smoke_use_opposite_flags():
    source = _installer_text()
    assert re.search(
        r'env\s+V52_MIGRATIONS_ENABLED=0\s+BACKUP_ENCRYPTION_KEY=""'
        r'\s+HSTAGENTBOT_INTEGRATIONS_PATH=""\s+DB_PATH=', source
    )
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



def test_v12_selects_system_python_and_readable_virtualenv():
    source = _installer_text()
    assert 'if [[ -x /usr/bin/python3 ]]; then' in source
    assert '/root/*|/home/*)' in source
    assert 'chmod -R a+rX "$VENV_DIR"' in source
    assert 'SYSTEM_PYTHON="$resolved"' in source


def test_v12_build_metadata_and_service_name_contract():
    source = _installer_text()
    assert 'git config --system --add safe.directory "$INSTALL_DIR" || true' in source
    assert 'BUILD_COMMIT "$REPO_HEAD" "$env_file"' in source
    assert '>"$INSTALL_DIR/.build_info"' in source or '> "$INSTALL_DIR/.build_info"' in source
    assert 'SERVICE_NAME="' + chr(36) + '{HSTAGENTBOT_SERVICE_NAME:-hstagentbot}"' in source


def test_v12_minimal_inputs_and_v52_bootstrap_contract():
    source = _installer_text()
    assert 'Report supergroup ID [$main_group_id; 0 to disable]' in source
    assert 'V52_MIGRATIONS_ENABLED "1"' in source
    assert 'V52_UI_ENABLED "1"' in source
    assert 'database.bootstrap_v52_installation(' in source
    assert 'await bot.get_me()' in source
    assert '--service-name NAME' in source and '--advanced' in source
