import os
import base64
import binascii
import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from pathlib import Path
from dotenv import load_dotenv

# Base Directory & Environment Loading
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(dotenv_path=ENV_PATH)

# Bot & Admin Settings
VERSION = "v5.2.0"
BUILD_BRANCH = "v5.2-release"
BUILD_COMMIT = os.getenv("BUILD_COMMIT", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", os.getenv("HST_AGENT_BOT_TOKEN", ""))
ADMIN_TELEGRAM_ID = int(os.getenv("ADMIN_TELEGRAM_ID", "0"))

# Destination Routing for Supergroups & Topics
MAIN_GROUP_ID = int(os.getenv("MAIN_GROUP_ID", "0"))
MY_PROJECT_GROUP_ID = MAIN_GROUP_ID  # Backward-compatible alias
SANDBOX_GROUP_ID = int(os.getenv("SANDBOX_GROUP_ID", "0"))

OPERATIONAL_LOGS_TOPIC_ID = int(os.getenv("OPERATIONAL_LOGS_TOPIC_ID", "10"))
BACKUPS_TOPIC_ID = int(os.getenv("BACKUPS_TOPIC_ID", "20"))
ADMIN_DM_LOGS_TOPIC_ID = int(os.getenv("ADMIN_DM_LOGS_TOPIC_ID", "0"))
REP_LOGS_TOPIC_ID = int(os.getenv("REP_LOGS_TOPIC_ID", "0"))

# Optional Bootstrap Representatives (Loaded dynamically from DB if empty)
REPRESENTATIVES = {}

# Paths
DB_PATH = os.getenv("DB_PATH", str(BASE_DIR / "data" / "agent_bot.db"))
BACKUP_DIR = os.getenv("BACKUP_DIR", str(BASE_DIR / "backups"))

# Public subscription URL configuration. No live-domain fallback is embedded in code.
SUB_DOMAIN = os.getenv("SUB_DOMAIN", "").strip()
def _read_subscription_port():
    try:
        value = int(os.getenv("SUB_PORT", "443"))
        return value if 1 <= value <= 65535 else 443
    except (TypeError, ValueError):
        return 443
SUB_PORT = _read_subscription_port()
SUB_PATH = os.getenv("SUB_PATH", "sub").strip("/")

# Ensure local directories exist
db_parent = os.path.dirname(DB_PATH)
if db_parent:
    os.makedirs(db_parent, exist_ok=True)
os.makedirs(BACKUP_DIR, exist_ok=True)


# v5.2 runtime settings.  These values are read on demand so a deployment can
# stage each capability independently without changing the legacy config API.
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})
_HOST_LABEL = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)"
    r"(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*\.?$"
)
_BASE64URL_KEY = re.compile(r"^[A-Za-z0-9_-]{43}=?$")


@dataclass(frozen=True)
class V52Settings:
    """Validated v5.2 settings; secret material is never rendered."""

    backup_encryption_key: str | None = field(default=None, repr=False)
    sub_gateway_bind: str = "127.0.0.1:8088"
    sub_gateway_host: str = "127.0.0.1"
    sub_gateway_port: int = 8088
    sub_gateway_base_url: str | None = None
    trusted_proxy_cidrs: tuple[str, ...] = ()
    migrations_enabled: bool = False
    sub_gateway_rollout_enabled: bool = False
    backup_encryption_enabled: bool = False
    dns_verification_enabled: bool = False
    sub_gateway_enabled: bool = False

    def __repr__(self) -> str:
        key_value = "'<redacted>'" if self.backup_encryption_key is not None else "None"
        return (
            "V52Settings("
            f"backup_encryption_key={key_value}, "
            f"sub_gateway_bind={self.sub_gateway_bind!r}, "
            f"sub_gateway_host={self.sub_gateway_host!r}, "
            f"sub_gateway_port={self.sub_gateway_port!r}, "
            f"sub_gateway_base_url={self.sub_gateway_base_url!r}, "
            f"trusted_proxy_cidrs={self.trusted_proxy_cidrs!r}, "
            f"migrations_enabled={self.migrations_enabled!r}, "
            f"sub_gateway_rollout_enabled={self.sub_gateway_rollout_enabled!r}, "
            f"backup_encryption_enabled={self.backup_encryption_enabled!r}, "
            f"dns_verification_enabled={self.dns_verification_enabled!r}, "
            f"sub_gateway_enabled={self.sub_gateway_enabled!r})"
        )

    __str__ = __repr__


def _read_v52_flag(environ, name: str, default: bool = False) -> bool:
    raw = environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    value = str(raw).strip().lower()
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be a boolean value")


def _validate_v52_host(host: str) -> None:
    if not isinstance(host, str) or not host or host.strip() != host:
        raise ValueError("host is invalid")
    try:
        ipaddress.ip_address(host)
        return
    except ValueError:
        if not _HOST_LABEL.fullmatch(host):
            raise ValueError("host is invalid") from None


def _read_v52_bind(environ) -> tuple[str, str, int]:
    raw = environ.get("SUB_GATEWAY_BIND", "127.0.0.1:8088")
    if not isinstance(raw, str) or not raw or raw.strip() != raw:
        raise ValueError("SUB_GATEWAY_BIND is invalid")
    # IPv6 literals must use URL-style brackets to make the port unambiguous.
    if not raw.startswith("[") and raw.count(":") != 1:
        raise ValueError("SUB_GATEWAY_BIND must be host:port")
    try:
        parsed = urlsplit("//" + raw)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("SUB_GATEWAY_BIND is invalid") from None
    if (
        host is None or port is None or not 1 <= port <= 65535
        or parsed.path or parsed.query or parsed.fragment
        or parsed.username is not None or parsed.password is not None
    ):
        raise ValueError("SUB_GATEWAY_BIND is invalid")
    _validate_v52_host(host)
    normalized_host = f"[{host}]" if ":" in host else host
    return f"{normalized_host}:{port}", host, port


def _read_v52_public_url(environ) -> str | None:
    raw = environ.get("SUB_GATEWAY_BASE_URL", "")
    if raw is None or str(raw).strip() == "":
        return None
    if not isinstance(raw, str) or raw.strip() != raw:
        raise ValueError("SUB_GATEWAY_BASE_URL is invalid")
    try:
        parsed = urlsplit(raw)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ValueError("SUB_GATEWAY_BASE_URL is invalid") from None
    if (
        parsed.scheme.lower() != "https" or host is None
        or parsed.username is not None or parsed.password is not None
        or parsed.query or parsed.fragment
        or (port is not None and not 1 <= port <= 65535)
    ):
        raise ValueError("SUB_GATEWAY_BASE_URL must be a public HTTPS URL")
    _validate_v52_host(host)
    return parsed._replace(scheme="https", path=parsed.path.rstrip("/")).geturl()


def _read_v52_encryption_key(environ) -> str | None:
    encoded = environ.get("BACKUP_ENCRYPTION_KEY")
    if encoded is None or str(encoded).strip() == "":
        return None
    if not isinstance(encoded, str) or not _BASE64URL_KEY.fullmatch(encoded):
        raise ValueError("BACKUP_ENCRYPTION_KEY is missing or invalid")
    try:
        padded = encoded.encode("ascii") + b"=" * (-len(encoded) % 4)
        key = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (UnicodeEncodeError, binascii.Error, ValueError):
        raise ValueError("BACKUP_ENCRYPTION_KEY is missing or invalid") from None
    canonical = base64.urlsafe_b64encode(key).rstrip(b"=").decode("ascii")
    if len(key) != 32 or canonical != encoded.rstrip("="):
        raise ValueError("BACKUP_ENCRYPTION_KEY is missing or invalid")
    return encoded


def _read_v52_trusted_proxies(environ) -> tuple[str, ...]:
    raw = environ.get("TRUSTED_PROXY_CIDRS", "")
    if raw is None or str(raw).strip() == "":
        return ()
    if not isinstance(raw, str):
        raise ValueError("TRUSTED_PROXY_CIDRS is invalid")
    entries = [item.strip() for item in raw.split(",")]
    if any(not item for item in entries):
        raise ValueError("TRUSTED_PROXY_CIDRS is invalid")
    try:
        return tuple(str(ipaddress.ip_network(item, strict=False)) for item in entries)
    except ValueError:
        raise ValueError("TRUSTED_PROXY_CIDRS contains an invalid network") from None


def read_v52_settings(environ=None) -> V52Settings:
    """Validate v5.2 environment settings; absent rollout flags stay disabled.

    The optional mapping exists for deterministic tests and callers that need
    to validate a staged environment before mutating process-global state.
    """
    env = os.environ if environ is None else environ
    key = _read_v52_encryption_key(env)
    bind, bind_host, bind_port = _read_v52_bind(env)
    public_url = _read_v52_public_url(env)
    trusted_proxies = _read_v52_trusted_proxies(env)

    migrations_enabled = _read_v52_flag(env, "V52_MIGRATIONS_ENABLED")
    gateway_rollout = _read_v52_flag(env, "V52_SUB_GATEWAY_ENABLED")
    backup_rollout = _read_v52_flag(env, "V52_BACKUP_ENCRYPTION_ENABLED", key is not None)
    dns_rollout = _read_v52_flag(env, "V52_DNS_VERIFICATION_ENABLED")

    return V52Settings(
        backup_encryption_key=key,
        sub_gateway_bind=bind,
        sub_gateway_host=bind_host,
        sub_gateway_port=bind_port,
        sub_gateway_base_url=public_url,
        trusted_proxy_cidrs=trusted_proxies,
        migrations_enabled=migrations_enabled,
        sub_gateway_rollout_enabled=gateway_rollout,
        backup_encryption_enabled=backup_rollout and key is not None,
        dns_verification_enabled=dns_rollout,
        # A public origin is mandatory; a rollout flag alone never activates
        # a bearer-token subscription endpoint.
        sub_gateway_enabled=gateway_rollout and public_url is not None,
    )
