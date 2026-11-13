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
from collections.abc import Mapping

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


def _panel_subscription_base(account_ref) -> str:
    """Resolve the public subscription base from one explicit account/panel binding."""
    account_id = account_ref.get("account_id", account_ref.get("id")) if isinstance(account_ref, Mapping) else account_ref
    if isinstance(account_id, bool):
        return ""
    try:
        account_id = int(account_id)
        if account_id <= 0:
            return ""
        import database
        binding = database.get_account_panel_binding(account_id=account_id)
        if not isinstance(binding, dict) or not binding.get("panel_id"):
            return ""
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT sub_base_url FROM panels WHERE id=? AND lifecycle='active'",
                (int(binding["panel_id"]),),
            ).fetchone()
        return str(row["sub_base_url"] or "").strip() if row else ""
    except Exception:
        logger.exception("Panel-scoped subscription URL resolution failed")
        return ""


def get_subscription_url(sub_id: str, *, account_ref=None) -> str:
    """Build a panel-scoped URL for explicit account refs; preserve legacy-only fallback."""
    from urllib.parse import quote, urlsplit

    if not isinstance(sub_id, str) or not sub_id.strip():
        return ""
    if account_ref is not None:
        base = _panel_subscription_base(account_ref)
        if not base:
            return ""
        try:
            parsed = urlsplit(base)
            if (
                parsed.scheme.lower() != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment
                or any(ord(ch) <= 32 or ord(ch) == 127 for ch in parsed.netloc)
            ):
                return ""
            # Preserve only the configured path for the account's own panel host.
            path = "/".join(quote(part, safe="-._~") for part in parsed.path.split("/") if part)
            token = quote(sub_id.strip(), safe="-._~")
            return f"https://{parsed.netloc}{'/' + path if path else ''}/{token}"
        except (TypeError, ValueError):
            return ""

    # Compatibility for pre-panel legacy intents only. New panel-bound call sites
    # must always pass account_ref; this branch must never be used as a panel fallback.
    domain = str(getattr(config, "SUB_DOMAIN", "") or "").strip()
    if not domain:
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


def reset_client_subscription(
    xui_module, account_identifier: str, scope: str = "all", *,
    account_ref=None, confirmation_nonce: str | None = None,
    operation_id: str | None = None, actor_tg_id: int | None = None,
    bot_id: int | None = None,
) -> dict:
    """Reset a legacy client or dispatch a nonce-consumed, prepared panel operation.

    Panel-bound calls never invoke the legacy update helper. The confirmation
    nonce must already be consumed by operation_service.prepare_operation and
    linked to the supplied operation_id. Missing or mismatched evidence fails closed.
    """
    if not str(account_identifier or "").strip():
        return {"status": "error", "message": "شناسه اکانت مشخص نشده است."}

    if account_ref is not None:
        account_id = account_ref.get("account_id", account_ref.get("id")) if isinstance(account_ref, Mapping) else account_ref
        if isinstance(account_id, bool):
            return {"status": "error", "message": "شناسه داخلی حساب معتبر نیست."}
        try:
            account_id = int(account_id)
            if account_id <= 0:
                raise ValueError
        except (TypeError, ValueError):
            return {"status": "error", "message": "شناسه داخلی حساب معتبر نیست."}
        if (
            not isinstance(confirmation_nonce, str) or not confirmation_nonce.strip()
            or not isinstance(operation_id, str) or not operation_id.strip()
            or isinstance(actor_tg_id, bool) or not isinstance(actor_tg_id, int) or actor_tg_id <= 0
            or isinstance(bot_id, bool) or not isinstance(bot_id, int) or bot_id <= 0
        ):
            return {"status": "error", "message": "بازنشانی پنل به مرجع حساب، هویت actor/bot، nonce و operation آماده‌شده نیاز دارد."}
        try:
            import asyncio
            import inspect
            import json
            import database
            import operation_service

            binding = database.get_account_panel_binding(account_id=account_id)
            if not isinstance(binding, dict) or not binding.get("panel_id"):
                return {"status": "error", "message": "اتصال یکتای حساب به پنل قابل اثبات نیست."}
            normalized = str(account_identifier).strip().casefold()
            if normalized not in {
                str(binding.get("email") or "").casefold(),
                str(binding.get("account_code") or "").casefold(),
            }:
                return {"status": "error", "message": "شناسه حساب با مرجع canonical مطابقت ندارد."}
            current_sub_id = database.read_account_sub_id(account_id)
            if not current_sub_id:
                return {"status": "error", "message": "شناسه اشتراک رمز‌شده حساب در دسترس نیست."}

            def _prepared_candidate(candidate: str, requested_kind: str) -> bool:
                try:
                    with database.get_db() as conn:
                        row = conn.execute(
                            "SELECT * FROM panel_operations WHERE operation_id=?",
                            (operation_id,),
                        ).fetchone()
                        if row is None:
                            return False
                        operation = operation_service._operation_result(
                            conn, row, replayed=False,
                        )
                        if (
                            operation.get("state") != "prepared"
                            or int(row["account_id"] or 0) != account_id
                            or int(row["panel_id"]) != int(binding["panel_id"])
                            or int(row["actor_tg_id"]) != actor_tg_id
                            or int(row["bot_id"]) != bot_id
                            or str(operation.get("kind")) not in {
                                "rotate_subscription_url", "subscription_id", "rotate_sub",
                            }
                        ):
                            return False
                        request = operation.get("request")
                        if not isinstance(request, Mapping):
                            return False
                        stored_candidate = next(
                            (request.get(key) for key in (
                                "candidate_sub_id", "new_sub_id", "sub_id", "subscription_id",
                            ) if request.get(key)),
                            None,
                        )
                        if not isinstance(stored_candidate, str) or stored_candidate != candidate:
                            return False
                        confirmation = conn.execute(
                            "SELECT actor_tg_id,bot_id,action,target_type,target_id,expires_at,"
                            "consumed_at,operation_id FROM action_confirmations WHERE nonce=?",
                            (confirmation_nonce,),
                        ).fetchone()
                        if confirmation is None:
                            return False
                        return bool(
                            int(confirmation[0]) == actor_tg_id
                            and int(confirmation[1]) == bot_id
                            and str(confirmation[2]) == "panel.operation.prepare"
                            and str(confirmation[3]) == "account"
                            and str(confirmation[4]) == str(account_id)
                            and confirmation[6] is not None
                            and str(confirmation[7] or "") == operation_id
                            and int(confirmation[5]) > int(time.time())
                            and requested_kind == "subscription_id"
                        )
                except Exception:
                    logger.exception("Prepared subscription operation preflight failed")
                    return False

            adapter = database.get_panel_adapter_for_account(account_id)
            mutator = getattr(adapter, "rotate_subscription_url", None)
            if not callable(mutator):
                return {"status": "error", "message": "پنل انتخاب‌شده قابلیت چرخش لینک را ارائه نمی‌کند."}
            adapter._operation_preflight = _prepared_candidate
            request_probe = database.get_account_panel_binding(account_id=account_id)
            if not request_probe or int(request_probe["panel_id"]) != int(binding["panel_id"]):
                return {"status": "error", "message": "اتصال پنل هنگام آماده‌سازی تغییر کرده است."}

            # The candidate is recovered from the authenticated, encrypted prepared request.
            # It is never regenerated after confirmation because the confirmation is
            # bound to that immutable payload.
            with database.get_db() as conn:
                prepared_row = conn.execute(
                    "SELECT * FROM panel_operations WHERE operation_id=?",
                    (operation_id,),
                ).fetchone()
                if prepared_row is None:
                    return {"status": "error", "message": "عملیات آماده‌شده پیدا نشد؛ هیچ تغییری ارسال نشد."}
                prepared = operation_service._operation_result(conn, prepared_row, replayed=False)
                prepared_request = prepared.get("request")
                if not isinstance(prepared_request, Mapping):
                    return {"status": "error", "message": "درخواست رمز‌شده عملیات معتبر نیست."}
                candidate = next(
                    (prepared_request.get(key) for key in (
                        "candidate", "new_id", "subscription", "subscription_id",
                        "new_subscription_id",
                    ) if prepared_request.get(key)),
                    None,
                )
            if not isinstance(candidate, str) or not candidate:
                return {"status": "error", "message": "candidate در درخواست رمز‌شده ثبت نشده است."}
            if not _prepared_candidate(candidate, "subscription_id"):
                return {"status": "error", "message": "nonce یا عملیات آماده‌شده معتبر نیست؛ هیچ تغییری ارسال نشد."}
            result = mutator(str(binding["email"]), candidate, operation_id)
            if inspect.isawaitable(result):
                result = asyncio.run(result)
            state = str(getattr(result, "state", "unknown"))
            if state == "unknown":
                return {
                    "status": "unknown",
                    "message": "نتیجه چرخش پنل نامشخص است؛ تطبیق لازم است.",
                    "candidate_sub_id": candidate,
                    "email": binding["email"],
                }
            if state != "success":
                return {"status": "error", "message": "پنل چرخش شناسه را تأیید نکرد.", "email": binding["email"]}
            new_url = get_subscription_url(candidate, account_ref=account_id)
            if not new_url:
                return {
                    "status": "partial",
                    "message": "شناسه جدید در پنل تأیید شد، اما لینک پنل در دسترس نیست.",
                    "new_sub_id": candidate,
                    "email": binding["email"],
                }
            return {
                "status": "ok",
                "message": "شناسه جدید در پنل تأیید شد.",
                "new_sub_id": candidate,
                "new_sub_url": new_url,
                "email": binding["email"],
            }
        except Exception:
            logger.exception("Panel-bound subscription reset failed closed for account %s", account_id)
            return {"status": "unknown", "message": "نتیجه عملیات پنل قابل اثبات نیست؛ نیازمند تطبیق است."}

    # Legacy compatibility is isolated to non-panel-bound callers. The v5.2
    # callback routers present a preview and confirmation before this path is reachable.
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
        new_url = get_subscription_url(new_sub_id)
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

