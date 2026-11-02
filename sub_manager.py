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
    """Returns the absolute HTTPS subscription URL based on active domain."""
    sub_domain = getattr(config, "SUB_DOMAIN", "sub.example.com")
    sub_port = getattr(config, "SUB_PORT", 2096)
    sub_path = getattr(config, "SUB_PATH", "sub")

    if sub_port in (80, 443):
        port_part = ""
    else:
        port_part = f":{sub_port}"

    return f"https://{sub_domain}{port_part}/{sub_path}/{sub_id}"


def reset_client_sub_id(xui_module, email: str) -> tuple[bool, str, str]:
    """
    Resets client subId in 3X-UI panel to revoke old subscription link.
    Returns (success, message, new_sub_id).
    """
    try:
        new_sub = generate_secure_sub_id(16)
        session = xui_module.get_session()
        search_res = xui_module.find_client_by_code(email)
        if not search_res or not search_res.get("found"):
            return False, "اکانت موردنظر در پنل یافت نشد.", ""

        client = search_res["data"]["raw"]
        client_id = client.get("id") or client.get("uuid")
        inbound_id = search_res["data"]["inbound_id"]

        # Update client subId via 3X-UI updateClient API
        client["subId"] = new_sub
        payload = {
            "id": inbound_id,
            "settings": json.dumps({"clients": [client]})
        }
        res = session.post(f"{xui_module.BASE_URL}/panel/api/inbounds/updateClient/{client_id}", json=payload, timeout=10)
        if res.ok and res.json().get("success"):
            return True, "لینک سابسکریپشن با موفقیت بازنشانی شد و لینک قبلی باطل گردید.", new_sub
        else:
            return False, f"خطا در ارتباط با سرور پنل: {res.text}", ""
    except Exception as e:
        logger.error(f"Failed to reset subId for {email}: {e}")
        return False, str(e), ""
