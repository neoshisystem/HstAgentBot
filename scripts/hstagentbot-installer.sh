#!/usr/bin/env bash
# HstAgentBot Plug-and-Play installer v1.2.0
set -Eeuo pipefail
umask 027

readonly INSTALLER_VERSION="1.2.0"
readonly REPO_URL="https://github.com/neoshisystem/HstAgentBot.git"
REPO_REF="${HSTAGENTBOT_GIT_REF:-recovery/scope-1}"
readonly LOCK_FILE="/run/lock/hstagentbot-installer.lock"
readonly LOG_FILE="/var/log/hstagentbot-installer.log"
SERVICE_NAME="${HSTAGENTBOT_SERVICE_NAME:-hstagentbot}"
SERVICE_USER="${HSTAGENTBOT_SERVICE_USER:-hstagentbot}"
SERVICE_GROUP="${HSTAGENTBOT_SERVICE_GROUP:-hstagentbot}"
readonly DEFAULT_INSTALL_DIR="/opt/hstagentbot"
INSTALL_DIR="${HSTAGENTBOT_INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"
VENV_DIR=""
SYSTEM_PYTHON=""
FRESH_INSTALL=0
ADVANCED=0
SKIP_TESTS=0
CHECK_ONLY=0
UPDATE_APPROVED=0
REPO_HEAD=""
REPO_TREE=""
BOT_TOKEN=""

red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
die() { red "ERROR: $*"; exit 1; }
log() { printf '[%s] %s\n' "$(date -Is)" "$*"; }
step() { printf '\n\033[1;36m[Step %s/10]\033[0m \033[1m%s\033[0m\n' "$1" "$2"; }

print_banner() {
    printf '\n'
    printf '╔══════════════════════════════════════════════════════════════╗\n'
    printf '║                                                              ║\n'
    printf '║            H S T A G E N T B O T   I N S T A L L E R         ║\n'
    printf '║                                                              ║\n'
    printf '║                 Pre-production release  v%s                ║\n' "$INSTALLER_VERSION"
    printf '║                                                              ║\n'
    printf '╚══════════════════════════════════════════════════════════════╝\n\n'
}

usage() {
    cat <<'USAGE'
HstAgentBot installer v1.2.0

Usage:
  sudo bash hstagentbot-installer.sh [options]

Options:
  --help                 Show help and exit.
  --check-only           Verify host prerequisites and an existing checkout without installing.
  --skip-tests           Skip compileall, base pytest, and migration smoke test.
  --update               Approve a safe update of an existing clean checkout without prompting.
  --ref REF              Install a branch, tag, or commit reference (default: recovery/scope-1).
  --service-name NAME    Install the systemd service under NAME.
  --service-user NAME    Run the service as NAME (default: hstagentbot).
  --advanced             Ask for optional legacy topic and subscription settings.

Environment:
  HSTAGENTBOT_INSTALL_DIR  Installation directory (default: /opt/hstagentbot)
  HSTAGENTBOT_GIT_REF      Default Git ref; overridden by --ref.
  HSTAGENTBOT_SERVICE_NAME Default service name; overridden by --service-name.
  HSTAGENTBOT_SERVICE_USER Default service user; overridden by --service-user.
  HSTAGENTBOT_SERVICE_GROUP Service group; defaults to the service user name.

Fresh installs ask only for install path, bot token, numeric admin ID and report group
ID (default 0). v5.2 migrations and UI are enabled automatically.

Examples:
  sudo bash scripts/hstagentbot-installer.sh
  sudo bash scripts/hstagentbot-installer.sh --service-name hstagentbot-v52
  sudo bash scripts/hstagentbot-installer.sh --advanced --ref recovery/scope-1
  curl -fsSL https://raw.githubusercontent.com/neoshisystem/HstAgentBot/recovery/scope-1/scripts/hstagentbot-installer.sh | sudo bash

Interactive input is read from /dev/tty where available, including curl | sudo bash.
USAGE
}



parse_args() {
    while (($#)); do
        case "$1" in
            --help|-h) usage; exit 0 ;;
            --check-only) CHECK_ONLY=1; shift ;;
            --skip-tests) SKIP_TESTS=1; shift ;;
            --update) UPDATE_APPROVED=1; shift ;;
            --advanced) ADVANCED=1; shift ;;
            --service-name)
                (($# >= 2)) || die "--service-name requires a name"
                [[ "$2" =~ ^[A-Za-z0-9][A-Za-z0-9_.@-]{0,62}$ ]] ||
                    die "Invalid service name: $2"
                SERVICE_NAME="${2%.service}"
                shift 2
                ;;
            --service-user)
                (($# >= 2)) || die "--service-user requires a name"
                [[ "$2" =~ ^[A-Za-z_][A-Za-z0-9_-]{0,31}$ ]] ||
                    die "Invalid service user: $2"
                SERVICE_USER="$2"
                # Keep the group aligned unless it was pinned explicitly.
                [[ -n "${HSTAGENTBOT_SERVICE_GROUP:-}" ]] || SERVICE_GROUP="$2"
                shift 2
                ;;
            --ref)
                (($# >= 2)) || die "--ref requires a Git reference"
                [[ -n "$2" ]] || die "--ref cannot be empty"
                REPO_REF="$2"
                shift 2
                ;;
            *) die "Unknown option: $1 (use --help)" ;;
        esac
    done
    [[ "$SERVICE_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_.@-]{0,62}$ ]] ||
        die "Invalid service name: $SERVICE_NAME"
    [[ "$SERVICE_USER" =~ ^[A-Za-z_][A-Za-z0-9_-]{0,31}$ ]] ||
        die "Invalid service user: $SERVICE_USER"
    [[ "$SERVICE_GROUP" =~ ^[A-Za-z_][A-Za-z0-9_-]{0,31}$ ]] ||
        die "Invalid service group: $SERVICE_GROUP"
}



read_input() {
    local target="$1" prompt="$2" value=""
    printf '%s' "$prompt"
    if [[ -r /dev/tty ]]; then IFS= read -r value </dev/tty || true
    else IFS= read -r value || true
    fi
    printf -v "$target" '%s' "$value"
}
read_secret() {
    local target="$1" prompt="$2" value=""
    printf '%s' "$prompt"
    if [[ -r /dev/tty ]]; then IFS= read -r -s value </dev/tty || true
    else IFS= read -r -s value || true
    fi
    printf '\n'
    printf -v "$target" '%s' "$value"
}
confirm() {
    local answer=""
    read_input answer "$1 [y/N]: "
    [[ "$answer" =~ ^([Yy]|[Yy][Ee][Ss])$ ]]
}
require_root() {
    [[ "${EUID:-$(id -u)}" -eq 0 ]] || die "Run the installer as root (sudo bash ...)"
}
setup_logging_and_lock() {
    install -d -m 0755 /run/lock
    exec 9>"$LOCK_FILE"
    flock -n 9 || die "Another installer is already running (lock: $LOCK_FILE)"
    touch "$LOG_FILE"
    chmod 0600 "$LOG_FILE"
    exec > >(tee -a "$LOG_FILE") 2>&1
    log "Starting HstAgentBot installer v$INSTALLER_VERSION; ref=$REPO_REF"
}
check_supported_os() {
    [[ -r /etc/os-release ]] || die "/etc/os-release is missing"
    # shellcheck disable=SC1091
    . /etc/os-release
    local version_id="${VERSION_ID:-0}"
    case "${ID:-}" in
        ubuntu) dpkg --compare-versions "$version_id" ge "24.04" || die "Ubuntu 24.04+ required" ;;
        debian) dpkg --compare-versions "$version_id" ge "12" || die "Debian 12+ required" ;;
        *) die "Unsupported OS: ${PRETTY_NAME:-unknown}; use Ubuntu 24.04+ or Debian 12+" ;;
    esac
    log "Supported OS: ${PRETTY_NAME:-$ID $version_id}"
}
check_required_commands() {
    local missing=() name
    for name in git curl flock openssl python3 systemctl useradd; do
        command -v "$name" >/dev/null 2>&1 || missing+=("$name")
    done
    if ((${#missing[@]} > 0)); then
        yellow "Missing host commands: ${missing[*]}"
        return 1
    fi
}
install_system_dependencies() {
    step 1 "Host prerequisites and operating-system packages"
    check_supported_os
    if ! check_required_commands; then
        export DEBIAN_FRONTEND=noninteractive
        apt-get update
        apt-get install -y --no-install-recommends \
            ca-certificates curl git openssl util-linux python3 python3-venv python3-pip sqlite3
    fi
    check_required_commands || die "Required commands remain unavailable"
    command -v apt-get >/dev/null 2>&1 || die "apt-get is required"
}
check_install_path() {
    [[ "$INSTALL_DIR" = /* ]] || die "Installation directory must be absolute"
    [[ "$INSTALL_DIR" != "/" && "$INSTALL_DIR" != "/root" && "$INSTALL_DIR" != "/home" ]] ||
        die "Refusing unsafe installation directory: $INSTALL_DIR"
}
prompt_install_paths() {
    local selected=""
    read_input selected "Installation directory [$INSTALL_DIR]: "
    [[ -n "$selected" ]] && INSTALL_DIR="$selected"
    check_install_path
    VENV_DIR="$INSTALL_DIR/.venv"
    log "Installation directory: $INSTALL_DIR"
}
verify_existing_checkout_is_safe() {
    [[ -d "$INSTALL_DIR/.git" ]] || die "Target exists but is not a Git checkout: $INSTALL_DIR"
    git config --system --add safe.directory "$INSTALL_DIR" || true
    local origin dirty
    origin="$(git -C "$INSTALL_DIR" remote get-url origin 2>/dev/null || true)"
    case "$origin" in
        "$REPO_URL"|"git@github.com:neoshisystem/HstAgentBot.git") ;;
        *) die "Unexpected Git origin: $origin" ;;
    esac
    dirty="$(git -C "$INSTALL_DIR" status --porcelain --untracked-files=no)"
    [[ -z "$dirty" ]] || die "Tracked files are modified; commit or restore them before updating"
}
checkout_source() {
    step 2 "Resolve source checkout and pin actual Git objects"
    check_install_path
    if [[ -e "$INSTALL_DIR" ]]; then
        verify_existing_checkout_is_safe
        log "Existing HEAD: $(git -C "$INSTALL_DIR" rev-parse HEAD)"
        if ((UPDATE_APPROVED == 0)); then
            confirm "Update clean checkout to '$REPO_REF'?" || die "Update was not approved"
        fi
        git -C "$INSTALL_DIR" fetch --prune origin "$REPO_REF"
        git -C "$INSTALL_DIR" checkout --detach --force FETCH_HEAD
        git -C "$INSTALL_DIR" reset --hard FETCH_HEAD
    else
        FRESH_INSTALL=1
        install -d -m 0755 "$(dirname "$INSTALL_DIR")"
        git clone --no-checkout "$REPO_URL" "$INSTALL_DIR"
        git -C "$INSTALL_DIR" fetch --depth 1 origin "$REPO_REF"
        git -C "$INSTALL_DIR" checkout --detach FETCH_HEAD
    fi
    git config --system --add safe.directory "$INSTALL_DIR" || true
    REPO_HEAD="$(git -C "$INSTALL_DIR" rev-parse HEAD)"
    printf '%s\n' "$REPO_HEAD" > "$INSTALL_DIR/.build_info"
    chmod 0644 "$INSTALL_DIR/.build_info"
    REPO_TREE="$(git -C "$INSTALL_DIR" rev-parse 'HEAD^{tree}')"
    log "Resolved HEAD=$REPO_HEAD"
    log "Resolved tree=$REPO_TREE"
    log "Resolved ref=$REPO_REF"
}
verify_source_contract() {
    step 3 "Verify required repository paths and exact migration set"
    local path file count=0
    for path in requirements.txt database.py rep_bot.py scripts/hstagentbot-installer.sh migrations; do
        [[ -e "$INSTALL_DIR/$path" ]] || die "Required path missing: $path"
    done
    for file in "$INSTALL_DIR"/migrations/*.sql; do
        [[ -f "$file" ]] || continue
        ((count += 1))
    done
    [[ "$count" -eq 18 ]] || die "Expected exactly 18 SQL migrations, found $count"
    log "Required paths PASS; migrations=$count/18"
}
select_python() {
    local candidate resolved
    if [[ -x /usr/bin/python3 ]]; then
        candidate="/usr/bin/python3"
    else
        candidate="$(command -v python3 2>/dev/null || true)"
    fi
    [[ -n "$candidate" && -x "$candidate" ]] || die "No usable Python 3 interpreter found"
    resolved="$(readlink -f "$candidate" 2>/dev/null || printf '%s' "$candidate")"
    case "$resolved" in
        /root/*|/home/*) die "Refusing Python interpreter inside a protected home directory: $resolved" ;;
    esac
    [[ "$resolved" = /* && -x "$resolved" ]] || die "Selected Python interpreter is not executable"
    SYSTEM_PYTHON="$resolved"
    log "Selected system Python: $SYSTEM_PYTHON"
}
ensure_service_account() {
    step 4 "Create restricted service account and Python environment"
    if ! id "$SERVICE_USER" >/dev/null 2>&1; then
        useradd --system --home-dir "$INSTALL_DIR" --no-create-home \
            --shell /usr/sbin/nologin --user-group "$SERVICE_USER"
    fi
    local service_shell
    service_shell="$(getent passwd "$SERVICE_USER" | cut -d: -f7)"
    [[ "$service_shell" == "/usr/sbin/nologin" ]] || die "$SERVICE_USER must use /usr/sbin/nologin"
    # A pre-existing service user may carry a different primary group name.
    if ! getent group "$SERVICE_GROUP" >/dev/null 2>&1; then
        if id -gn "$SERVICE_USER" >/dev/null 2>&1; then
            SERVICE_GROUP="$(id -gn "$SERVICE_USER")"
            log "Service group resolved to the existing primary group: $SERVICE_GROUP"
        else
            groupadd --system "$SERVICE_GROUP"
        fi
    fi
    select_python
    VENV_DIR="$INSTALL_DIR/.venv"
    [[ -x "$VENV_DIR/bin/python" ]] || "$SYSTEM_PYTHON" -m venv "$VENV_DIR"
    "$VENV_DIR/bin/python" -m pip install --upgrade pip
    "$VENV_DIR/bin/python" -m pip install -r "$INSTALL_DIR/requirements.txt" pytest
    chmod -R a+rX "$VENV_DIR"
}



get_env_value() {
    local key="$1" file="$2"
    [[ -f "$file" ]] || return 0
    awk -v prefix="$key=" 'index($0,prefix)==1 { value=substr($0,length(prefix)+1) } END { if(value!="") print value }' "$file"
}
set_env_value() {
    local key="$1" value="$2" file="$3" tmp
    tmp="$(mktemp)"
    if [[ -f "$file" ]] && grep -q "^${key}=" "$file"; then
        awk -v prefix="$key=" -v replacement="$key=$value" '
            BEGIN { written=0 }
            index($0,prefix)==1 { if(!written) print replacement; written=1; next }
            { print }
            END { if(!written) print replacement }
        ' "$file" > "$tmp"
    else
        [[ -f "$file" ]] && cat "$file" > "$tmp"
        printf '%s=%s\n' "$key" "$value" >> "$tmp"
    fi
    install -m 0640 "$tmp" "$file"
    rm -f "$tmp"
}
validate_token_format() {
    [[ "$1" =~ ^[0-9]{6,}:[A-Za-z0-9_-]{30,}$ ]]
}
validate_telegram_token() {
    local token="$1" response config
    response="$(mktemp)"
    config="$(printf 'url = "https://api.telegram.org/bot%s/getMe"\n' "$token")"
    # Token-bearing URL goes to curl stdin, never curl argv.
    if printf '%s' "$config" | curl --config - --silent --show-error --fail --connect-timeout 10 --max-time 30 --output "$response"; then
        if grep -q '"ok"[[:space:]]*:[[:space:]]*true' "$response"; then
            rm -f "$response"
            return 0
        fi
    fi
    rm -f "$response"
    return 1
}
configure_environment() {
    step 5 "Configure Telegram credentials and v5.2 Plug-and-Play defaults"
    local env_file="$INSTALL_DIR/.env" encryption_key admin_id main_group_id selected key current
    if [[ ! -f "$env_file" ]]; then
        if [[ -f "$INSTALL_DIR/.env.example" ]]; then install -m 0640 "$INSTALL_DIR/.env.example" "$env_file"
        else install -m 0640 /dev/null "$env_file"
        fi
    fi
    encryption_key="$(get_env_value BACKUP_ENCRYPTION_KEY "$env_file")"
    if [[ -z "$encryption_key" ]]; then
        encryption_key="$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=')"
        set_env_value BACKUP_ENCRYPTION_KEY "$encryption_key" "$env_file"
        log "Generated 256-bit BACKUP_ENCRYPTION_KEY"
    else
        log "Preserving existing BACKUP_ENCRYPTION_KEY"
    fi

    BOT_TOKEN="$(get_env_value BOT_TOKEN "$env_file")"
    if [[ -n "$BOT_TOKEN" ]] && validate_token_format "$BOT_TOKEN"; then
        validate_telegram_token "$BOT_TOKEN" || BOT_TOKEN=""
    else
        BOT_TOKEN=""
    fi
    while [[ -z "$BOT_TOKEN" ]]; do
        read_secret BOT_TOKEN "Telegram Bot Token (hidden input): "
        if ! validate_token_format "$BOT_TOKEN"; then
            yellow "Invalid token format; expected numeric-id:secret"
            BOT_TOKEN=""
            continue
        fi
        if ! validate_telegram_token "$BOT_TOKEN"; then
            yellow "Telegram rejected the token or the API is unreachable"
            BOT_TOKEN=""
        fi
    done
    set_env_value BOT_TOKEN "$BOT_TOKEN" "$env_file"

    admin_id="$(get_env_value ADMIN_TELEGRAM_ID "$env_file")"
    if [[ ! "$admin_id" =~ ^[1-9][0-9]{0,17}$ || "$admin_id" == "123456789" ]]; then
        read_input admin_id "Super Admin Telegram numeric ID: "
        [[ "$admin_id" =~ ^[1-9][0-9]{0,17}$ ]] || die "ADMIN_TELEGRAM_ID must be a positive numeric Telegram ID"
    fi
    set_env_value ADMIN_TELEGRAM_ID "$admin_id" "$env_file"

    main_group_id="$(get_env_value MAIN_GROUP_ID "$env_file")"
    [[ "$main_group_id" =~ ^-?[0-9]+$ ]] || main_group_id="0"
    selected=""
    read_input selected "Report supergroup ID [$main_group_id; 0 to disable]: "
    [[ -n "$selected" ]] && main_group_id="$selected"
    [[ "$main_group_id" =~ ^-?[0-9]+$ ]] || die "MAIN_GROUP_ID must be numeric"
    set_env_value MAIN_GROUP_ID "$main_group_id" "$env_file"

    if ((ADVANCED == 1)); then
        for key in OPERATIONAL_LOGS_TOPIC_ID BACKUPS_TOPIC_ID ADMIN_DM_LOGS_TOPIC_ID REP_LOGS_TOPIC_ID SANDBOX_GROUP_ID; do
            current="$(get_env_value "$key" "$env_file")"
            [[ "$current" =~ ^-?[0-9]+$ ]] || current="0"
            selected=""
            read_input selected "$key [$current]: "
            [[ -n "$selected" ]] && current="$selected"
            [[ "$current" =~ ^-?[0-9]+$ ]] || die "$key must be numeric"
            set_env_value "$key" "$current" "$env_file"
        done
        local sub_domain sub_port sub_path
        sub_domain="$(get_env_value SUB_DOMAIN "$env_file")"
        sub_port="$(get_env_value SUB_PORT "$env_file")"
        sub_path="$(get_env_value SUB_PATH "$env_file")"
        [[ -n "$sub_port" ]] || sub_port="443"
        [[ -n "$sub_path" ]] || sub_path="sub"
        selected=""
        read_input selected "SUB_DOMAIN [$sub_domain]: "
        [[ -n "$selected" ]] && sub_domain="$selected"
        set_env_value SUB_DOMAIN "$sub_domain" "$env_file"
        selected=""
        read_input selected "SUB_PORT [$sub_port]: "
        [[ -n "$selected" ]] && sub_port="$selected"
        [[ "$sub_port" =~ ^[0-9]+$ ]] || die "SUB_PORT must be numeric"
        set_env_value SUB_PORT "$sub_port" "$env_file"
        selected=""
        read_input selected "SUB_PATH [$sub_path]: "
        [[ -n "$selected" ]] && sub_path="$selected"
        set_env_value SUB_PATH "$sub_path" "$env_file"
    elif ((FRESH_INSTALL == 1)); then
        # Do not carry project-specific v4 IDs or subscription settings into a clean install.
        set_env_value OPERATIONAL_LOGS_TOPIC_ID "0" "$env_file"
        set_env_value BACKUPS_TOPIC_ID "0" "$env_file"
        set_env_value ADMIN_DM_LOGS_TOPIC_ID "0" "$env_file"
        set_env_value REP_LOGS_TOPIC_ID "0" "$env_file"
        set_env_value SANDBOX_GROUP_ID "0" "$env_file"
        set_env_value SUB_DOMAIN "" "$env_file"
        set_env_value SUB_PORT "443" "$env_file"
        set_env_value SUB_PATH "sub" "$env_file"
    fi

    set_env_value V52_MIGRATIONS_ENABLED "1" "$env_file"
    set_env_value V52_UI_ENABLED "1" "$env_file"
    set_env_value BUILD_COMMIT "$REPO_HEAD" "$env_file"
    chmod 0640 "$env_file"
    chown root:"$SERVICE_GROUP" "$env_file"
    log ".env mode=0640 owner=root:$SERVICE_GROUP; v5.2 migrations/UI enabled"
}



run_migration_smoke_test() {
    local smoke_dir smoke_db smoke_backups
    smoke_dir="$(mktemp -d /tmp/hstagentbot-migration-smoke.XXXXXX)"
    smoke_db="$smoke_dir/smoke.db"
    smoke_backups="$smoke_dir/backups"
    install -d -m 0700 "$smoke_backups"
    (
        cd "$INSTALL_DIR"
        env V52_MIGRATIONS_ENABLED=1 DB_PATH="$smoke_db" BACKUP_DIR="$smoke_backups" \
            "$VENV_DIR/bin/python" - <<'PY'
import database

database.init_db()
with database.get_db() as conn:
    applied = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    if len(applied) != 18:
        raise SystemExit(f"Expected 18 registered migrations, found {len(applied)}")
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(accounts)")}
    if "panel_id" not in columns:
        raise SystemExit("Migration 003 contract failed: accounts.panel_id is missing")

database.upsert_account_record(
    rep_id=None, email="installer-smoke@invalid.example",
    account_code="installer-smoke", customer_name="Installer Smoke",
    sub_id="smoke-subscription", enable=1, total_gb=1.5,
    expiry_time=0, owner_tg_id=None, panel_id=None,
)
created = database.get_account_by_code_or_email("installer-smoke@invalid.example")
if not created or "panel_id" not in created:
    raise SystemExit("Upsert insert failed on migrated schema")

database.upsert_account_record(
    rep_id=None, email="installer-smoke@invalid.example",
    account_code="installer-smoke", customer_name="Installer Smoke Updated",
    sub_id="smoke-subscription-updated", enable=1, total_gb=2.5,
    expiry_time=0, owner_tg_id=None, panel_id=created.get("panel_id"),
)
updated = database.get_account_by_code_or_email("installer-smoke@invalid.example")
if not updated or updated.get("customer_name") != "Installer Smoke Updated":
    raise SystemExit("Upsert update failed on migrated schema")
if float(updated.get("total_gb") or 0) != 2.5:
    raise SystemExit("Upsert did not persist total_gb")

with database.get_db() as conn:
    if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("SQLite integrity_check failed")
    errors = conn.execute("PRAGMA foreign_key_check").fetchall()
    if errors:
        raise SystemExit(f"SQLite foreign_key_check returned {len(errors)} row(s)")
print("MIGRATION_SMOKE_PASS: migrations=18 panel_id upsert=insert/update integrity=ok foreign_keys=ok")
PY
    )
    rm -rf "$smoke_dir"
    green "Migration smoke test PASS"
}
prepare_runtime_paths() {
    step 6 "Prepare isolated runtime directories and verify deploy layout"
    local dir
    for dir in data backups logs; do
        install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0750 "$INSTALL_DIR/$dir"
    done
    [[ -x "$VENV_DIR/bin/python" ]] || die "Python virtual environment is unavailable"
    [[ -s "$INSTALL_DIR/.env" ]] || die "Protected .env file is missing or empty"
    log "Runtime directories and protected environment are ready"
}

run_project_tests() {
    step 7 "Compile Python sources and run the base pytest suite"
    if ((SKIP_TESTS == 1)); then
        yellow "--skip-tests: compileall, base pytest, and migration smoke are skipped"
        return 0
    fi
    local test_dir
    test_dir="$(mktemp -d /tmp/hstagentbot-tests.XXXXXX)"
    install -d -m 0700 "$test_dir/backups"
    (
        cd "$INSTALL_DIR"
        "$VENV_DIR/bin/python" -m compileall -q .
        env V52_MIGRATIONS_ENABLED=0 V52_UI_ENABLED="" BACKUP_ENCRYPTION_KEY="" HSTAGENTBOT_INTEGRATIONS_PATH="" DB_PATH="$test_dir/test.db" BACKUP_DIR="$test_dir/backups" \
            "$VENV_DIR/bin/python" -m pytest -p no:cacheprovider -q .
    )
    rm -rf "$test_dir"
    green "compileall and base pytest PASS"
    step 8 "Run isolated v5.2 migration smoke test"
    run_migration_smoke_test
}
resolve_database_path() {
    (cd "$INSTALL_DIR" && "$VENV_DIR/bin/python" -c "import database; print(database.DB_PATH)")
}
backup_existing_database() {
    step 9 "Back up existing SQLite database, apply migrations and seed v5.2"
    local db_path backup_file
    db_path="$(resolve_database_path)"
    if [[ -f "$db_path" ]]; then
        install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0750 "$INSTALL_DIR/backups"
        backup_file="$INSTALL_DIR/backups/pre-migration-$(date -u +%Y%m%dT%H%M%SZ).sqlite3"
        (
            cd "$INSTALL_DIR"
            DB_PATH="$db_path" BACKUP_FILE="$backup_file" "$VENV_DIR/bin/python" - <<'PY'
import os
import sqlite3
import database

source = sqlite3.connect(database.DB_PATH, timeout=30)
target = sqlite3.connect(os.environ["BACKUP_FILE"])
try:
    source.backup(target)
    if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("Online backup integrity_check failed")
    target.commit()
finally:
    target.close()
    source.close()
os.chmod(os.environ["BACKUP_FILE"], 0o600)
print("Online SQLite Backup API snapshot created")
PY
        )
        chown "$SERVICE_USER:$SERVICE_GROUP" "$backup_file"
    else
        log "No existing DB; first-time initialization"
    fi
    install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0750 "$INSTALL_DIR/backups"
    (
        cd "$INSTALL_DIR"
        env V52_MIGRATIONS_ENABLED=1 V52_UI_ENABLED=1 "$VENV_DIR/bin/python" - <<'PY'
import asyncio
import config
import database
from telegram import Bot

async def bootstrap():
    bot = Bot(token=config.BOT_TOKEN)
    await bot.initialize()
    try:
        me = await bot.get_me()
        result = database.bootstrap_v52_installation(
            bot_id=int(me.id), username=str(getattr(me, "username", "") or ""),
            token=config.BOT_TOKEN,
        )
        print("v5.2 installation bootstrap PASS", result)
    finally:
        await bot.shutdown()

database.init_db()
asyncio.run(bootstrap())
PY
    )
}



write_systemd_unit() {
    local unit="/etc/systemd/system/$SERVICE_NAME.service"
    cat > "$unit" <<UNIT
[Unit]
Description=HstAgentBot Telegram Management Service
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_GROUP
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=$VENV_DIR/bin/python -u rep_bot.py
Restart=on-failure
RestartSec=5
TimeoutStartSec=90
TimeoutStopSec=30
KillSignal=SIGINT
UMask=0027
ProtectSystem=strict
NoNewPrivileges=true
PrivateTmp=true
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
CapabilityBoundingSet=
AmbientCapabilities=
ReadWritePaths=$INSTALL_DIR/data $INSTALL_DIR/backups $INSTALL_DIR/logs

[Install]
WantedBy=multi-user.target
UNIT
    chmod 0644 "$unit"
    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME.service"
}
apply_permissions() {
    # All application source remains root-owned. Do not chmod venv files to 0644:
    # pip console scripts and a copied Python interpreter must keep execute bits.
    chown -R root:root "$INSTALL_DIR"
    find "$INSTALL_DIR" -type d -exec chmod 0755 {} +
    find "$INSTALL_DIR" -path "$VENV_DIR" -prune -o -type f -exec chmod 0644 {} +
    chmod 0755 "$INSTALL_DIR/scripts/hstagentbot-installer.sh"
    chown -R root:root "$VENV_DIR"
    find "$VENV_DIR" -type d -exec chmod 0755 {} +
    find "$VENV_DIR" -type f -exec chmod go-w {} +
    chmod -R a+rX "$VENV_DIR"

    # Existing database, backup, and log files must remain writable by the
    # restricted service account, not merely their parent directories.
    local dir
    for dir in data backups logs; do
        install -d -o "$SERVICE_USER" -g "$SERVICE_GROUP" -m 0750 "$INSTALL_DIR/$dir"
        chown -R "$SERVICE_USER:$SERVICE_GROUP" "$INSTALL_DIR/$dir"
        find "$INSTALL_DIR/$dir" -type d -exec chmod 0750 {} +
        find "$INSTALL_DIR/$dir" -type f -exec chmod 0640 {} +
    done
    if [[ -f "$INSTALL_DIR/.env" ]]; then
        chown root:"$SERVICE_GROUP" "$INSTALL_DIR/.env"
        chmod 0640 "$INSTALL_DIR/.env"
    fi
    chmod 0755 "$INSTALL_DIR"
}
write_install_report() {
    cat > "$INSTALL_DIR/INSTALL_REPORT.txt" <<REPORT
HstAgentBot installer v$INSTALLER_VERSION
Installed: $(date -Is)
Requested ref: $REPO_REF
Resolved HEAD: $REPO_HEAD
Resolved tree: $REPO_TREE
Install dir: $INSTALL_DIR
Service: $SERVICE_NAME.service
Service user: $SERVICE_USER (/usr/sbin/nologin)
Base tests: $([[ "$SKIP_TESTS" -eq 1 ]] && printf 'SKIPPED' || printf 'PASS')
Main DB migrations: V52_MIGRATIONS_ENABLED=1
REPORT
    chmod 0644 "$INSTALL_DIR/INSTALL_REPORT.txt"
    chown root:root "$INSTALL_DIR/INSTALL_REPORT.txt"
}
print_summary() {
    local state
    state="$(systemctl is-active "$SERVICE_NAME.service" 2>/dev/null || true)"
    printf '\n╔════════════════════════════════════════════════════════════════════╗\n'
    printf '║                    HSTAGENTBOT INSTALL SUMMARY                    ║\n'
    printf '╠════════════════════════════════════════════════════════════════════╣\n'
    printf '║ Version       : %-49s ║\n' "$INSTALLER_VERSION"
    printf '║ Git ref       : %-49.49s ║\n' "$REPO_REF"
    printf '║ Commit        : %-49.49s ║\n' "$REPO_HEAD"
    printf '║ Tree          : %-49.49s ║\n' "$REPO_TREE"
    printf '║ Install path  : %-49.49s ║\n' "$INSTALL_DIR"
    printf '║ Service       : %-49s ║\n' "$state"
    printf '║ Service user  : %-49s ║\n' "$SERVICE_USER"
    printf '║ SQL migrations: %-49s ║\n' "18 / 18"
    printf '║ Log           : %-49.49s ║\n' "$LOG_FILE"
    printf '╚════════════════════════════════════════════════════════════════════╝\n'
}
main() {
    # Banner is the first action in main, before asking for the installation path.
    print_banner
    require_root
    setup_logging_and_lock
    check_supported_os
    if ((CHECK_ONLY == 1)); then
        check_required_commands || die "Host prerequisites are incomplete"
        check_install_path
        if [[ -d "$INSTALL_DIR/.git" ]]; then
            verify_existing_checkout_is_safe
            verify_source_contract
        else
            log "--check-only: no checkout found at $INSTALL_DIR; no files changed"
        fi
        green "Check-only completed without installation changes"
        return 0
    fi
    install_system_dependencies
    prompt_install_paths
    checkout_source
    verify_source_contract
    ensure_service_account
    configure_environment
    prepare_runtime_paths
    run_project_tests
    backup_existing_database
    step 10 "Apply ownership, install hardened systemd unit, and start service"
    apply_permissions
    write_systemd_unit
    write_install_report
    systemctl restart "$SERVICE_NAME.service"
    sleep 2
    systemctl is-active --quiet "$SERVICE_NAME.service" ||
        die "Service did not become active; inspect journalctl -u $SERVICE_NAME.service"
    print_summary
    green "HstAgentBot v$INSTALLER_VERSION installation completed successfully"
}
parse_args "$@"
main
