"""Strict, transport-independent parser for configured panel origins."""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
from urllib.parse import urlsplit


@dataclass(frozen=True, slots=True)
class PanelEndpoint:
    scheme: str
    host: str
    port: int
    base_path: str

    @property
    def authority(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        default = 443 if self.scheme == "https" else 80
        return host if self.port == default else f"{host}:{self.port}"

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.authority}"

    @property
    def normalized_url(self) -> str:
        return f"{self.origin}{self.base_path}"

    @property
    def api_base_url(self) -> str:
        return f"{self.normalized_url.rstrip('/')}/panel/api"

    def url_for(self, api_path: str) -> str:
        path = str(api_path).lstrip("/")
        if not path or "?" in path or "#" in path:
            raise ValueError("API path must be a non-empty path without query or fragment")
        return f"{self.api_base_url}/{path}"


_KNOWN_SUFFIXES = (
    "/panel/api-docs",
    "/api-docs",
    "/swagger",
    "/docs",
    "/openapi.json",
)


def parse_panel_url(raw: str) -> PanelEndpoint:
    if not isinstance(raw, str) or not raw or raw.strip() != raw:
        raise ValueError("panel URL must be a non-empty trimmed string")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw) or "\\" in raw:
        raise ValueError("panel URL contains invalid characters")
    if "?" in raw or "#" in raw:
        raise ValueError("panel URL must not contain query or fragment")
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        if scheme not in {"http", "https"}:
            raise ValueError("panel URL scheme must be http or https")
        if not parsed.netloc or parsed.hostname is None:
            raise ValueError("panel URL must include a host")
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            raise ValueError("userinfo is forbidden in panel URLs")
        port = parsed.port
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("panel URL is invalid") from exc

    default_port = 443 if scheme == "https" else 80
    if port is None:
        port = default_port
    if isinstance(port, bool) or not 1 <= port <= 65535:
        raise ValueError("panel URL port must be between 1 and 65535")

    host = parsed.hostname
    try:
        address = ipaddress.ip_address(host)
        host = address.compressed.lower()
    except ValueError:
        host = host.rstrip(".").lower()
        if not host or len(host) > 253:
            raise ValueError("panel URL host is invalid")
        labels = host.split(".")
        if any(
            not label or len(label) > 63 or label.startswith("-") or label.endswith("-")
            or not all(c.isalnum() or c == "-" for c in label)
            for label in labels
        ):
            raise ValueError("panel URL host is invalid")

    path = parsed.path or ""
    for suffix in _KNOWN_SUFFIXES:
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    if path == "/":
        path = ""
    else:
        path = path.rstrip("/")
    if path and not path.startswith("/"):
        path = "/" + path

    return PanelEndpoint(scheme=scheme, host=host, port=port, base_path=path)
