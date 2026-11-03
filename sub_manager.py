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
import json

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
    original_sub = None
    try:
        search = initial_search or xui_module.find_client_by_code(email)
        if not isinstance(search, dict) or not search.get("found"):
            return False, "اکانت موردنظر در پنل یافت نشد.", ""
        data = search.get("data") or {}
        client = data.get("raw")
        if not isinstance(client, dict):
            return False, "اطلاعات پایه اکانت از پنل کامل نیست.", ""
        client_id = client.get("id") or client.get("uuid")
        inbound_id = data.get("inbound_id")
        original_sub = client.get("subId") or client.get("sub_id")
        if not client_id or inbound_id is None:
            return False, "شناسه داخلی اکانت در پنل کامل نیست.", ""
        changed = dict(client)
        changed["subId"] = new_sub
        payload = {"id": inbound_id, "settings": json.dumps({"clients": [changed]})}
        response = xui_module.get_session().post(
            f"{xui_module.BASE_URL}/panel/api/inbounds/updateClient/{client_id}",
            json=payload, timeout=10
        )
        try:
            body = response.json()
        except Exception:
            body = None
        if response.ok and isinstance(body, dict) and body.get("success") is True:
            return True, "لینک سابسکریپشن با موفقیت بازنشانی شد.", new_sub
        reason = "پاسخ پنل موفقیت را تأیید نکرد"
    except Exception as exc:
        logger.exception("Subscription reset outcome is uncertain for %s", email)
        reason = f"نتیجه درخواست نامشخص است: {exc}"

    remote_sub = _read_remote_sub_id(xui_module, email)
    if remote_sub == new_sub:
        return True, "شناسه جدید در پنل تأیید شد.", new_sub
    if original_sub is not None and remote_sub == str(original_sub):
        return False, f"پنل شناسه قبلی را حفظ کرده است؛ {reason}.", ""
    return None, f"{reason} و وضعیت شناسه در پنل قابل تأیید نیست.", new_sub


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
    """Resolve an account identifier, rotate its subscription ID and return one stable result shape."""
    if not account_identifier:
        return {"status": "error", "message": "شناسه اکانت مشخص نشده است."}
    try:
        search = xui_module.find_client_by_code(account_identifier)
        if not search or not search.get("found"):
            return {"status": "error", "message": "اکانت موردنظر در پنل یافت نشد."}
        data = search.get("data") or {}
        raw = data.get("raw") or {}
        email = data.get("email") or raw.get("email") or account_identifier

        url_builder = getattr(xui_module, "get_sub_url", None)
        if not callable(url_builder) and not get_subscription_url("probe"):
            return {"status": "error", "message": "دامنه سابسکریپشن در تنظیمات سامانه تعریف نشده است."}

        ok, message, new_sub_id = reset_client_sub_id(xui_module, email)
        if not ok:
            return {"status": "error", "message": message}
        new_url = url_builder(new_sub_id) if callable(url_builder) else get_subscription_url(new_sub_id)
        if not new_url:
            return {
                "status": "partial",
                "message": "شناسه سابسکریپشن تغییر کرد، اما ساخت لینک جدید ممکن نشد؛ تنظیمات دامنه را بررسی کنید.",
                "new_sub_id": new_sub_id,
            }
        return {
            "status": "ok",
            "message": message,
            "new_sub_id": new_sub_id,
            "new_sub_url": new_url,
            "email": email,
        }
    except Exception as exc:
        logger.exception("Failed to reset subscription for %s", account_identifier)
        return {"status": "error", "message": str(exc)}
