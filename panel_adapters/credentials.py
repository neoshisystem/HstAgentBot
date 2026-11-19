"""Panel credential classification for the 3X-UI registration wizard.

The wizard accepts three shapes and stores exactly one encrypted secret:

* a bare API token (``BdEzE6...``) — the scheme this deployment's live panel
  actually uses, sent as ``Authorization: Bearer <token>``;
* ``username:password`` — the panel's interactive login, exchanged for a
  session cookie before any client API call;
* a JSON object carrying either ``token`` or ``username``/``password``.

Classification is pure and transport-free so the wizard can validate a
credential before anything is persisted.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import re

BEARER = "bearer_token"
PASSWORD = "password"

# Characters that never appear in a panel username or password.
_FORBIDDEN = re.compile(r"[\s\x00-\x1f\x7f]")
# A JSON document is only treated as structured when it starts with a brace.
_JSON_PREFIX = "{"


@dataclass(frozen=True, slots=True)
class PanelCredential:
    """A classified panel credential.

    ``secret`` is the exact string that gets encrypted into ``panels.auth_cipher``.
    ``username``/``password`` are kept for the login handshake and are excluded
    from ``repr`` so a credential can never reach a log record.
    """

    kind: str
    secret: str = field(repr=False)
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)

    def __repr__(self) -> str:
        return f"PanelCredential(kind={self.kind!r}, secret=<redacted>)"

    @property
    def is_password(self) -> bool:
        return self.kind == PASSWORD

    @property
    def is_bearer(self) -> bool:
        return self.kind == BEARER


class CredentialFormatError(ValueError):
    """Raised when a submitted panel credential cannot be classified."""


def _clean(value) -> str:
    return str(value).strip() if value is not None else ""


def _from_json(raw: str) -> PanelCredential | None:
    """Return a credential from a JSON object, or ``None`` when it is not JSON."""
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        raise CredentialFormatError(
            "JSON اعتبارنامه باید یک شیء باشد، نه آرایه یا مقدار ساده."
        )
    token = _clean(parsed.get("token") or parsed.get("api_token"))
    if token:
        return PanelCredential(BEARER, token)
    username = _clean(parsed.get("username") or parsed.get("user"))
    password = _clean(parsed.get("password") or parsed.get("pass"))
    if username and password:
        return PanelCredential(PASSWORD, f"{username}:{password}", username, password)
    raise CredentialFormatError(
        "JSON اعتبارنامه باید کلید token یا هر دو کلید username و password را داشته باشد."
    )


def classify_panel_credential(raw) -> PanelCredential:
    """Classify a submitted panel credential, or raise ``CredentialFormatError``.

    A single colon with non-empty sides is read as ``username:password``; every
    other non-empty value is an opaque API token. Anything containing whitespace
    or control characters is rejected outright, because a pasted credential with
    a line break is a mis-paste rather than a token.
    """
    text = _clean(raw)
    if not text:
        raise CredentialFormatError("اعتبارنامه خالی است؛ توکن یا نام کاربری:رمز عبور را ارسال کنید.")
    if _FORBIDDEN.search(text):
        raise CredentialFormatError(
            "اعتبارنامه نباید فاصله، خط جدید یا نویسه کنترلی داشته باشد."
        )
    if text.startswith(_JSON_PREFIX):
        parsed = _from_json(text)
        if parsed is not None:
            return parsed
    if text.count(":") == 1:
        username, password = text.split(":", 1)
        if username and password:
            return PanelCredential(PASSWORD, text, username, password)
    if ":" in text:
        raise CredentialFormatError(
            "قالب نام کاربری:رمز عبور باید دقیقاً یک دونقطه داشته باشد."
        )
    return PanelCredential(BEARER, text)


def credential_help_text() -> str:
    """Wizard help that names every accepted shape and the real deployment scheme."""
    return (
        "توکن API پنل را ارسال کنید، یا اعتبارنامهٔ ورود را به شکل «نام کاربری:رمز عبور».\n\n"
        "• توکن API (روش این نصب): رشتهٔ یکپارچه، مثل `BdEzE6gjv69O79gRfMhI7TmBiDStDpmt...`\n"
        "  این توکن به‌صورت `Authorization: Bearer <token>` ارسال می‌شود.\n"
        "• ورود کاربری: `admin:MyPassw0rd` — بات ابتدا از مسیر `/login` نشست می‌گیرد و\n"
        "  سپس همان کوکی نشست را برای فراخوانی‌های API استفاده می‌کند.\n"
        "• قالب JSON: `{\"token\": \"...\"}` یا `{\"username\": \"admin\", \"password\": \"...\"}`\n\n"
        "پیام حاوی اعتبارنامه بلافاصله پس از ذخیره از گفت‌وگو حذف می‌شود."
    )


__all__ = [
    "BEARER", "PASSWORD", "PanelCredential", "CredentialFormatError",
    "classify_panel_credential", "credential_help_text",
]
