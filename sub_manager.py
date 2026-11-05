"""
sub_manager.py - Subscription Link & Identity Utilities for HstAgentBot
Handles:
- Subscription URL construction and formatting
- QR code payload creation
- SubId / UUID regeneration to invalidate compromised subscriptions
"""

import logging
import secrets
import string
import uuid
import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))
import config

logger = logging.getLogger(__name__)


def generate_secure_sub_id(length: int = 16) -> str:
    """Generates a cryptographically strong subscription token."""
    chars = string.ascii_letters + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def generate_new_uuid() -> str:
    """Generates a standard RFC 4122 version 4 UUID."""
    return str(uuid.uuid4())


def get_subscription_url(sub_id: str) -> str:
    """Build a URL only from a valid configured public host and path."""
    from urllib.parse import quote, urlsplit

    domain = str(getattr(config, "SUB_DOMAIN", "") or "").strip()
    if not domain or not isinstance(sub_id, str) or not sub_id.strip():
        return ""
    try:
        parsed = urlsplit("https://" + domain)
        if any(ord(ch) <= 32 or ord(ch) == 127 for ch in parsed.netloc):
            return ""
        if (
            not parsed.hostname or parsed.username is not None
            or parsed.password is not None or parsed.path
            or parsed.query or parsed.fragment or parsed.port is not None
        ):
            return ""
        port = int(getattr(config, "SUB_PORT", 443))
        if not 1 <= port <= 65535:
            return ""
    except (TypeError, ValueError):
        return ""

    raw_path = str(getattr(config, "SUB_PATH", "sub") or "").strip("/")
    path = "/".join(quote(part, safe="-._~") for part in raw_path.split("/") if part)
    token = quote(sub_id.strip(), safe="-._~")
    port_part = "" if port == 443 else f":{port}"
    path_part = f"/{path}" if path else ""
    return f"https://{parsed.netloc}{port_part}{path_part}/{token}"


def _read_remote_sub_id(xui_module, email: str):
    """Return the currently stored ID, or None when an exact read is unavailable."""
    try:
        found = xui_module.find_client_by_code(email)
        if not isinstance(found, dict) or not found.get("found"):
            return None
        raw = (found.get("data") or {}).get("raw") or {}
        value = raw.get("subId") or raw.get("sub_id")
        return str(value) if value else ""
    except Exception:
        logger.exception("Could not verify remote subscription ID for %s", email)
        return None


def reset_client_sub_id(xui_module, email: str, initial_search=None) -> tuple[bool | None, str, str]:
    """True=confirmed applied, False=verified not applied, None=unknown remote result."""
    if not email:
        return False, "شناسه اکانت مشخص نشده است.", ""
    new_sub = generate_secure_sub_id(16)

    # Confirm the client exists in the panel before rotating (pre-validation).
    if initial_search is None:
        try:
            initial_search = xui_module.find_client_by_code(email)
        except Exception:
            logger.exception("Could not pre-verify client %s before reset", email)
            initial_search = None
    if isinstance(initial_search, dict) and not initial_search.get("found"):
        return False, "اکانت موردنظر در پنل یافت نشد.", ""

    # Central client endpoint: persists subId in the central client table and
    # auto-syncs all inbounds (OpenAPI 3.x clients/update). Read-back is
    # performed inside the method against GET clients/get/{email}.
    updater = getattr(xui_module, "update_client_sub_id", None)
    if not callable(updater):
        logger.error("xui_manager does not expose update_client_sub_id; reset aborted for %s", email)
        return None, "متد به‌روزرسانی متمرکز در پنل در دسترس نیست.", new_sub

    try:
        result: tuple[bool | None, str, str] = updater(email, new_sub)  # type: ignore[assignment]
        applied, message, confirmed_sub = result
    except Exception as exc:
        logger.exception("Subscription reset outcome is uncertain for %s", email)
        return None, f"نتیجه درخواست نامشخص است: {exc}", new_sub

    if applied is True:
        return True, message, str(confirmed_sub or new_sub)
    if applied is False:
        return False, message, ""
    # Unknown: fall back to a fresh read before giving up.
    remote_sub = _read_remote_sub_id(xui_module, email)
    if remote_sub == new_sub:
        return True, "شناسه جدید در پنل تأیید شد.", new_sub
    return None, message, new_sub


def generate_qr_code(payload: str):
    """Return a Telegram-compatible in-memory PNG QR image."""
    if not isinstance(payload, str) or not payload.strip():
        raise ValueError("QR payload must be a non-empty string")
    try:
        import qrcode
        from io import BytesIO
    except ImportError as exc:
        raise RuntimeError("QR generation dependency is missing; install qrcode[pil]") from exc
    image = qrcode.make(payload)
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    buffer.seek(0)
    buffer.name = "subscription-qr.png"
    return buffer


def reset_client_subscription(xui_module, account_identifier: str, scope: str = "all") -> dict:
    """Return ok, partial, error or unknown without losing a possibly rotated token."""
    if not str(account_identifier or "").strip():
        return {"status": "error", "message": "شناسه اکانت مشخص نشده است."}
    try:
        search = xui_module.find_client_by_code(account_identifier)
        if not isinstance(search, dict) or not search.get("found"):
            return {"status": "error", "message": "اکانت موردنظر در پنل یافت نشد."}
        data = search.get("data") or {}
        raw = data.get("raw") or {}
        email = data.get("email") or raw.get("email") or account_identifier
        applied, message, new_sub_id = reset_client_sub_id(
            xui_module, email, initial_search=search
        )
        if applied is None:
            return {
                "status": "unknown", "message": message,
                "candidate_sub_id": new_sub_id, "email": email,
            }
        if not applied:
            return {"status": "error", "message": message, "email": email}

        # A missing public URL does not undo a confirmed remote rotation.
        new_url = ""
        builder = getattr(xui_module, "get_sub_url", None)
        try:
            if callable(builder):
                new_url = str(builder(new_sub_id) or "")
            if not new_url:
                new_url = get_subscription_url(new_sub_id)
        except Exception:
            logger.exception("Could not build the new public URL for %s", email)

        if not new_url:
            return {
                "status": "partial", "message": "شناسه جدید در پنل تأیید شد، اما لینک عمومی ساخته نشد.",
                "new_sub_id": new_sub_id, "email": email,
            }
        return {
            "status": "ok", "message": message, "new_sub_id": new_sub_id,
            "new_sub_url": new_url, "email": email,
        }
    except Exception as exc:
        logger.exception("Subscription reset outcome could not be established for %s", account_identifier)
        return {"status": "unknown", "message": str(exc)}
