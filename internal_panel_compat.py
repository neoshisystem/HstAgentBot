"""Internal scoped compatibility facade for the retired v5.2 server helper.

C13: this module never imports database at module initialization. Every
operation resolves a canonical account_id + panel_id binding before accessing
the panel adapter; ambiguous legacy identifiers fail closed.
"""
from __future__ import annotations

import asyncio
import html
import io
import re
import time
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from typing import Any

# Iran Standard Time (UTC+03:30, no DST since 2022). The legacy helper rendered
# expiry/last-online stamps in this zone and the callers' copy expects it.
_IRAN_TZ = timezone(timedelta(hours=3, minutes=30))

_FACTORY = None
_URI_MARKER = "://"
_DIGITS = str.maketrans({
    "۰": "0", "۱": "1", "۲": "2", "۳": "3", "۴": "4",
    "۵": "5", "۶": "6", "۷": "7", "۸": "8", "۹": "9",
    "٠": "0", "١": "1", "٢": "2", "٣": "3", "٤": "4",
    "٥": "5", "٦": "6", "٧": "7", "٨": "8", "٩": "9",
})


def _database():
    import database
    return database


def _factory():
    """Build the adapter factory only when a scoped operation is requested."""
    global _FACTORY
    if _FACTORY is not None:
        return _FACTORY
    import config
    import requests
    from crypto_store import decrypt_secret
    from panel_adapters.factory import PanelAdapterFactory
    from panel_adapters.manual_pool import ManualPoolRepository

    def panel_loader(panel_id: int):
        db = _database()
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT * FROM panels WHERE id=? AND lifecycle IN ('active','capacity','drain','maintenance')",
                (panel_id,),
            ).fetchone()
            return dict(row) if row else None

    def account_loader(account_id: int):
        db = _database()
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT id,panel_id,email,account_code,revision,deleted_at "
                "FROM accounts WHERE id=? AND deleted_at IS NULL",
                (account_id,),
            ).fetchone()
            return dict(row) if row else None

    def account_id_resolver(panel_id: int, identifier: str):
        db = _database()
        with db.get_db() as conn:
            rows = conn.execute(
                "SELECT id FROM accounts WHERE panel_id=? AND deleted_at IS NULL "
                "AND (LOWER(email)=LOWER(?) OR LOWER(account_code)=LOWER(?)) ORDER BY id",
                (panel_id, identifier, identifier),
            ).fetchall()
        return int(rows[0]["id"]) if len(rows) == 1 else None

    def operation_preflight(operation_id: str, candidate: Any, operation_kind: str) -> bool:
        """Permit dispatch only for a persisted, prepared, account-bound operation."""
        if not isinstance(operation_id, str) or not operation_id.strip():
            return False
        db = _database()
        with db.get_db() as conn:
            row = conn.execute(
                "SELECT account_id,panel_id,kind,state FROM panel_operations WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        if row is None or str(row["state"]) not in {"prepared", "dispatching"}:
            return False
        if isinstance(candidate, Mapping):
            account_id = candidate.get("account_id", candidate.get("id"))
            panel_id = candidate.get("panel_id")
            if account_id is not None and int(account_id) != int(row["account_id"] or 0):
                return False
            if panel_id is not None and int(panel_id) != int(row["panel_id"]):
                return False
        if operation_kind:
            actual = str(row["kind"]).casefold()
            requested = str(operation_kind).casefold()
            aliases = {
                # The adapter asks for "client_create" while the provisioning
                # service persists kind="create_client"; without this pair the
                # preflight refused every creation and the purchase ended in
                # definite_failure before any request left the process.
                "client_create": {"client_create", "create_client"},
                "create_client": {"create_client", "client_create"},
                "renew_client": {"renew", "renew_client", "vpn_renew"},
                "set_client_enabled": {"set_client_enabled", "enable", "suspend", "activate"},
                "rotate_subscription_url": {"rotate_subscription_url", "reset_sub", "subscription_reset"},
                "add_addon": {"add_addon", "vpn_addon"},
                "subscription_id": {"subscription_id", "subscription_reset", "reset_sub"},
            }
            if actual not in aliases.get(requested, {requested}):
                return False
        return True

    _FACTORY = PanelAdapterFactory(
        panel_loader=panel_loader,
        account_loader=account_loader,
        session_factory=lambda _panel: requests.Session(),
        manual_repository=ManualPoolRepository(config.DB_PATH),
        decryptor=decrypt_secret,
        manual_decryptor=lambda cipher: decrypt_secret(cipher, "hstagent/columns/v1"),
        transaction_probe=lambda: False,
        account_id_resolver=account_id_resolver,
        operation_preflight=operation_preflight,
    )
    return _FACTORY


def _positive(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{label} must be a positive integer") from None
    if result <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return result


def _resolve_account(identifier: str | None = None, *, account_ref: Any = None,
                     account_id: int | None = None, panel_id: int | None = None):
    """Resolve exact binding; identifier-only legacy lookup is allowed if unique."""
    db = _database()
    if isinstance(account_ref, Mapping):
        account_id = account_ref.get("account_id", account_ref.get("id", account_id))
        panel_id = account_ref.get("panel_id", panel_id)
        identifier = account_ref.get("email") or account_ref.get("account_code") or identifier
    elif account_ref is not None:
        account_id = account_ref
    if account_id is not None:
        binding = db.get_account_panel_binding(account_id=_positive(account_id, "account_id"))
        if not isinstance(binding, dict) or not binding.get("panel_id"):
            return None
        if panel_id is not None and _positive(panel_id, "panel_id") != int(binding["panel_id"]):
            return None
        if identifier:
            selected = str(identifier).strip().casefold()
            if selected not in {str(binding.get("email") or "").casefold(),
                                str(binding.get("account_code") or "").casefold()}:
                return None
        return binding
    if panel_id is not None and identifier:
        pid = _positive(panel_id, "panel_id")
        with db.get_db() as conn:
            rows = conn.execute(
                "SELECT id FROM accounts WHERE panel_id=? AND deleted_at IS NULL "
                "AND (LOWER(email)=LOWER(?) OR LOWER(account_code)=LOWER(?)) ORDER BY id",
                (pid, str(identifier).strip(), str(identifier).strip()),
            ).fetchall()
        return db.get_account_panel_binding(account_id=int(rows[0]["id"])) if len(rows) == 1 else None
    if identifier:
        try:
            legacy = db.get_account_by_code_or_email(str(identifier))
        except ValueError:
            return None
        if not isinstance(legacy, dict) or not legacy.get("id") or not legacy.get("panel_id"):
            return None
        return db.get_account_panel_binding(account_id=int(legacy["id"]))
    return None


def _run(coro):
    """Bridge sync legacy callers without nesting asyncio loops."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    if hasattr(coro, "close"):
        coro.close()
    raise RuntimeError("synchronous compatibility helper cannot nest an active event loop")


def _canonical_to_legacy(client, account=None):
    """Render a canonical client in the legacy ``parse_client_info`` shape.

    Every product call site (user_portal / rep_panel / rep_bot / admin_panel)
    was ported from the external ``xui_manager`` helper and reads that flat
    contract — ``sub_id``, ``total_gb``, ``used_gb``, ``remaining_gb``,
    ``days_left``, ``is_expired``, ``is_depleted``, ``enabled``, ``is_relative``,
    ``raw``. Emitting only the canonical spellings made every card render
    «اطلاعات این سرویس در دسترس نیست» while the panel was perfectly healthy.
    The canonical aliases (``state``/``enable``/``totalGB``/``subId``) are kept
    alongside so ``format_status_report`` and ``parse_client_info`` keep working.
    """
    if client is None:
        return {}
    quota = client.quota_bytes
    used = client.used_bytes
    email = str(client.ref[2])
    total_gb = round(quota / (1024 ** 3), 2) if quota is not None else 0.0
    used_gb = round((used or 0) / (1024 ** 3), 2)
    remaining_gb = max(0.0, round(total_gb - used_gb, 2)) if total_gb > 0 else "Unlimited"
    is_depleted = bool(quota) and used is not None and used >= quota

    now_ms = int(time.time() * 1000)
    time_mode = client.time_mode
    expires_at = client.expires_at
    expiry_ms = expires_at * 1000 if expires_at is not None else 0
    is_relative = time_mode == "on_hold"
    is_expired = False
    days_left = None
    if is_relative:
        expiry_str = "شمارش معکوس (پس از اولین اتصال)"
    elif time_mode == "dated" and expiry_ms > 0:
        expiry_str = datetime.fromtimestamp(expiry_ms / 1000, tz=_IRAN_TZ).strftime("%Y-%m-%d %H:%M")
        diff_secs = (expiry_ms - now_ms) / 1000
        days_left = round(diff_secs / 86400, 1)
        is_expired = diff_secs <= 0
    else:
        expiry_str = "نامحدود"

    last_connected = client.last_connected_at
    last_online = ("هرگز" if not last_connected
                   else datetime.fromtimestamp(last_connected, tz=_IRAN_TZ).strftime("%Y-%m-%d %H:%M"))

    traffic = {"up": 0, "down": int(used or 0), "total": int(used or 0)}
    raw = {
        "email": email,
        "enable": client.enabled,
        "comment": getattr(client, "comment", "") or "",
        "subId": client.subscription_id,
        "totalGB": quota if quota is not None else 0,
        "expiryTime": expiry_ms,
        "traffic": traffic,
    }
    legacy = {
        # --- legacy xui_manager.parse_client_info contract (authoritative) ---
        "email": email,
        "group": "Default",
        "enabled": client.enabled,
        "is_expired": is_expired,
        "is_depleted": is_depleted,
        "total_gb": total_gb,
        "used_gb": used_gb,
        "remaining_gb": remaining_gb,
        "expiry_str": expiry_str,
        "days_left": days_left,
        "is_relative": is_relative,
        "last_online": last_online,
        "inbound_ids": [],
        "sub_id": client.subscription_id or "",
        "raw": raw,
        # --- canonical aliases retained for the newer contract ---
        "state": "found",
        "enable": client.enabled,
        "totalGB": total_gb,
        "subId": client.subscription_id,
        "expiryTime": expiry_ms,
        "traffic": traffic,
        "quota_mode": client.quota_mode,
        "time_mode": time_mode,
        "expires_at": expires_at,
        "observed_at": client.observed_at,
        "account_id": int(account["id"]) if account and account.get("id") else int(client.ref[0]),
        "panel_id": int(account["panel_id"]) if account and account.get("panel_id") else int(client.ref[1]),
    }
    # NOTE: ``relative_days`` is deliberately omitted for on-hold clients — the
    # canonical model drops the countdown magnitude, and the legacy caller's own
    # ``.get("relative_days", 30)`` default is preferable to a fabricated value.
    return legacy


def normalize_persian_digits(value: Any) -> Any:
    """Translate human-input numerals but preserve any URI-bearing token exactly."""
    if not isinstance(value, str):
        return value
    parts = re.split(r"(\S+)", value)
    for index in range(1, len(parts), 2):
        if _URI_MARKER not in parts[index]:
            parts[index] = parts[index].translate(_DIGITS)
    return "".join(parts)


def extract_account_identifier(text: Any) -> str | None:
    if not isinstance(text, str) or not text.strip() or _URI_MARKER in text:
        return None
    value = text.strip()
    match = re.search(
        r"(?:شناسه\s*اکانت|کد\s*اکانت|اکانت|کد|email|client)\s*[:：#]?\s*"
        r"([A-Za-z0-9][A-Za-z0-9_.+-]{2,63}(?:@[A-Za-z0-9.-]+\.[A-Za-z]{2,})?)",
        value, re.IGNORECASE,
    )
    if match:
        return match.group(1)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.+-]{2,63}", value):
        return value
    if re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", value):
        return value
    return None


def extract_customer_name(text: Any) -> str | None:
    if not isinstance(text, str) or not text.strip():
        return None
    match = re.search(r"(?:نام\s*(?:مشتری)?|customer\s*name|name)\s*[:：]\s*([^\n\r]{2,80})", text, re.IGNORECASE)
    if not match:
        return None
    candidate = match.group(1).strip(" \t*_")
    if not candidate or _URI_MARKER in candidate or re.fullmatch(r"[\d۰-۹٠-٩\s._-]+", candidate):
        return None
    return candidate[:80]


def parse_package_terms(text: Any) -> tuple[float | None, int | None]:
    """Extract only explicit quota/day units; unsupported shorthand stays unknown."""
    if not isinstance(text, str) or not text.strip():
        return None, None
    human = normalize_persian_digits(text).casefold()
    quota = duration = None
    q = re.search(r"(?<![\w.])(\d+(?:[.,]\d+)?)\s*(?:gb|gib|گیگ(?:ابایت)?|گیگابایت)", human, re.IGNORECASE)
    if q:
        try:
            parsed = float(q.group(1).replace(",", "."))
            quota = parsed if 0 < parsed <= 100000 else None
        except ValueError:
            quota = None
    d = re.search(r"(?<!\w)(\d{1,5})\s*(?:روز|day|days)", human, re.IGNORECASE)
    if d:
        try:
            parsed = int(d.group(1))
            duration = parsed if 0 < parsed <= 36500 else None
        except ValueError:
            duration = None
    return quota, duration


def parse_client_info(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {"state": "unknown", "raw": None}
    email = value.get("email") or value.get("username") or value.get("id")
    if not isinstance(email, str) or not email.strip():
        return {"state": "unknown", "raw": None}
    traffic = value.get("traffic") if isinstance(value.get("traffic"), Mapping) else {}
    try:
        quota = int(value["totalGB"]) if value.get("totalGB") is not None else None
    except (TypeError, ValueError):
        quota = None
    try:
        expiry = int(value["expiryTime"]) if value.get("expiryTime") is not None else None
    except (TypeError, ValueError):
        expiry = None
    enabled = value.get("enable")
    enabled = bool(enabled) if enabled in (True, False, 0, 1) else None
    return {
        "state": "found", "email": email.strip(), "enable": enabled, "totalGB": quota,
        "expiryTime": expiry, "traffic": dict(traffic),
        "subId": value.get("subId") or value.get("sub_id"),
        "comment": value.get("comment") if isinstance(value.get("comment"), str) else "",
        "raw": dict(value),
    }


def format_status_report(client_info: Any, is_admin: bool = False) -> str:
    info = client_info if isinstance(client_info, Mapping) else {}
    if info.get("state") != "found" and not info.get("email"):
        return "وضعیت اکانت قابل تأیید نیست؛ نتیجه نامشخص است."
    # Accept both generations: the canonical ``enable``/``totalGB`` spellings and
    # the legacy ``enabled``/``total_gb`` ones produced by _canonical_to_legacy.
    state = info.get("enable") if "enable" in info else info.get("enabled")
    state_text = "فعال" if state is True else "غیرفعال" if state is False else "نامشخص"
    quota = info.get("totalGB") if "totalGB" in info else info.get("total_gb")
    try:
        quota_text = f"{float(quota):.2f} GB" if quota is not None else "نامشخص"
    except (TypeError, ValueError):
        quota_text = "نامشخص"
    remaining = info.get("remaining_gb")
    if isinstance(remaining, (int, float)):
        remaining_text = f"{float(remaining):.2f} GB"
    elif isinstance(remaining, str) and remaining.strip():
        remaining_text = html.escape(remaining.strip(), quote=True)
    else:
        remaining_text = "نامشخص"
    account = html.escape(str(info.get("email") or "نامشخص"), quote=True)
    lines = [f"📊 وضعیت اکانت: <code>{account}</code>", f"اتصال: <b>{state_text}</b>",
             f"حجم کل: <b>{quota_text}</b>", f"حجم باقی‌مانده: <b>{remaining_text}</b>"]
    expiry = info.get("expiry_str")
    if isinstance(expiry, str) and expiry.strip():
        lines.append("انقضا: <b>" + html.escape(expiry.strip(), quote=True) + "</b>")
    days_left = info.get("days_left")
    if isinstance(days_left, (int, float)):
        lines.append(f"روز باقی‌مانده: <b>{float(days_left):g}</b>")
    if is_admin and info.get("comment"):
        lines.append("توضیحات: " + html.escape(str(info["comment"]), quote=True))
    return "\n".join(lines)


def format_client_deliverable(client_info: Any, links: Any, is_admin: bool = False,
                              generate_qr_code: bool = False) -> str:
    info = client_info if isinstance(client_info, Mapping) else {}
    account = html.escape(str(info.get("email") or "اکانت"), quote=True)
    values = links.get("links", links.get("configs", [])) if isinstance(links, Mapping) else links
    rows = []
    if isinstance(values, (list, tuple)):
        for value in values:
            uri = value.get("uri") if isinstance(value, Mapping) else value
            label = value.get("label", "کانفیگ") if isinstance(value, Mapping) else "کانفیگ"
            if isinstance(uri, str) and uri.strip():
                rows.append(f"• {html.escape(str(label))}: <code>{html.escape(uri, quote=True)}</code>")
    if not rows:
        rows = ["لینک یا کانفیگ قابل تأیید در پنل موجود نیست."]
    return f"📦 اطلاعات اتصال اکانت <code>{account}</code>\n" + "\n".join(rows)


def format_audit_report(records: Any) -> str:
    if isinstance(records, Mapping):
        records = records.get("items", records.get("records", []))
    if not isinstance(records, (list, tuple)):
        return "گزارش ممیزی در دسترس نیست."
    result = []
    for record in records:
        if isinstance(record, Mapping):
            action = html.escape(str(record.get("action") or "unknown"), quote=True)
            state = html.escape(str(record.get("result_status") or record.get("state") or "unknown"), quote=True)
            result.append(f"• {action}: {state}")
    return "\n".join(result) if result else "موردی برای گزارش ممیزی ثبت نشده است."


def generate_qr(payload: str, output_path: str | None = None):
    if not isinstance(payload, str) or not payload.strip():
        raise ValueError("QR payload must be a non-empty string")
    try:
        import qrcode
    except ImportError:
        raise RuntimeError("QR generation dependency is missing; install qrcode[pil]") from None
    image = qrcode.make(payload)
    if output_path:
        image.save(output_path, format="PNG")
        return output_path
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    stream.seek(0)
    stream.name = "account-qr.png"
    return stream


def _lookup(identifier: str, *, account_ref=None, panel_id=None):
    binding = _resolve_account(identifier, account_ref=account_ref, panel_id=panel_id)
    if not binding:
        return None, {"state": "unknown", "reason": "account_binding_missing_or_ambiguous"}
    try:
        adapter = _factory().resolve_account_adapter(int(binding["id"]))
        result = _run(adapter.get_client(str(binding["email"])))
    except Exception as exc:
        return binding, {"state": "unknown", "reason": type(exc).__name__}
    if result.state == "not_found":
        return binding, {"state": "not_found"}
    if result.state != "found" or result.client is None:
        return binding, {"state": "unknown", "evidence": result.evidence}
    info = _canonical_to_legacy(result.client, binding)
    try:
        info["sub_id"] = _database().read_account_sub_id(int(binding["id"]))
    except Exception:
        info["sub_id"] = None
    if not info["sub_id"]:
        # The panel is authoritative for the live subscription ID; the local
        # column is empty for accounts created before the secret was stored.
        info["sub_id"] = result.client.subscription_id
    info["subId"] = info["sub_id"]
    return binding, {"state": "found", "client": result.client, "info": info}


def inspect_client(identifier: str, scope=None, by_code_only: bool = False, *, account_ref=None, panel_id=None):
    """Legacy-compatible single-client read for the portal/rep/admin callers.

    The historical ``xui_manager.inspect_client`` contract is
    ``{"status": "ok"|"not_found"|"multiple_matches"|"unknown", "data": <flat record>|None}``
    and every product caller (user_portal/rep_panel/rep_bot/admin_panel) is
    written against it. The canonical ``found``/``state`` keys are returned
    alongside so newer callers and the facade tests keep working.
    """
    binding, outcome = _lookup(identifier, account_ref=account_ref, panel_id=panel_id)
    state = outcome["state"]
    if state != "found":
        return {"status": state, "found": False, "state": state,
                "reason": outcome.get("reason"), "query": identifier, "data": None}
    return {"status": "ok", "found": True, "state": "found", "data": outcome["info"]}


def find_client_by_code(identifier: str, *, account_ref=None, panel_id=None):
    """Legacy contract: ``{"found": bool, "data": {"raw", "email", ...}}``."""
    return inspect_client(identifier, account_ref=account_ref, panel_id=panel_id)


def get_client_links(identifier: str, *, account_ref=None, panel_id=None):
    """Legacy contract: a plain list of config URIs (empty when unavailable).

    The historical ``xui_manager.get_client_links`` returned the raw
    ``clients/links/{email}`` payload — a list of share URI strings — and the
    delivery/QR callers index it directly (``links[0]``), so a mapping would
    break them.
    """
    binding, outcome = _lookup(identifier, account_ref=account_ref, panel_id=panel_id)
    if outcome["state"] != "found":
        return []
    try:
        adapter = _factory().resolve_account_adapter(int(binding["id"]))
        nodes = _run(adapter.get_client_configs(str(binding["email"])))
        return [item.uri for item in nodes if getattr(item, "uri", None)]
    except Exception:
        return []


def _account_for_sub_id(sub_id: str):
    """Reverse resolve only one exact stored, encrypted subscription secret."""
    if not isinstance(sub_id, str) or not sub_id.strip():
        return None
    db = _database()
    from crypto_store import decrypt_secret
    with db.get_db() as conn:
        rows = conn.execute(
            "SELECT id,panel_id,sub_id_cipher FROM accounts WHERE deleted_at IS NULL "
            "AND panel_id IS NOT NULL AND sub_id_cipher IS NOT NULL AND sub_id_cipher<>'' ORDER BY id"
        ).fetchall()
    matches = []
    for row in rows:
        try:
            current = decrypt_secret(str(row["sub_id_cipher"]), "hstagent/columns/v1")
        except Exception:
            continue
        if current == sub_id:
            binding = db.get_account_panel_binding(account_id=int(row["id"]))
            if binding and int(binding.get("panel_id") or 0) == int(row["panel_id"]):
                matches.append(binding)
    return matches[0] if len(matches) == 1 else None


def get_sub_url(sub_id: str, *, account_ref=None, panel_id=None) -> str:
    binding = _account_for_sub_id(sub_id) if account_ref is None and panel_id is None else (
        _resolve_account(None, account_ref=account_ref, panel_id=panel_id)
    )
    if not binding:
        return ""
    try:
        stored = _database().read_account_sub_id(int(binding["id"]))
        if stored != sub_id:
            return ""
        import sub_manager
        return sub_manager.get_subscription_url(stored, account_ref=binding)
    except Exception:
        return ""


def update_client_sub_id(identifier: str, new_sub_id: str, *, account_ref=None, panel_id=None,
                         operation_id=None, confirmation_nonce=None, actor_tg_id=None, bot_id=None):
    """Rotate only with a consumed, matching panel-operation confirmation."""
    binding = _resolve_account(identifier, account_ref=account_ref, panel_id=panel_id)
    if not binding or not all((operation_id, confirmation_nonce, actor_tg_id, bot_id)):
        return None, "EVIDENCE_REQUIRED: explicit account binding and confirmed operation required", ""
    try:
        db = _database()
        with db.get_db() as conn:
            proof = conn.execute(
                "SELECT actor_tg_id,bot_id,account_id,panel_id,kind,state,request_cipher FROM panel_operations WHERE operation_id=?",
                (str(operation_id),),
            ).fetchone()
            confirmation = conn.execute(
                "SELECT consumed_at,operation_id FROM action_confirmations WHERE nonce=?",
                (str(confirmation_nonce),),
            ).fetchone()
        if (proof is None or confirmation is None or confirmation["consumed_at"] is None
                or str(confirmation["operation_id"] or "") != str(operation_id)
                or int(proof["account_id"] or 0) != int(binding["id"])
                or int(proof["panel_id"]) != int(binding["panel_id"])
                or int(proof["actor_tg_id"]) != int(actor_tg_id)
                or int(proof["bot_id"]) != int(bot_id)
                or str(proof["kind"]).casefold() not in {"subscription_id", "subscription_reset", "reset_sub"}
                or str(proof["state"]) not in {"prepared", "dispatching"}):
            return None, "EVIDENCE_REQUIRED: operation confirmation does not match the account", ""
        from crypto_store import decrypt_secret
        sealed = json.loads(decrypt_secret(str(proof["request_cipher"]), "hstagent/columns/v1"))
        request = sealed.get("request") if isinstance(sealed, Mapping) else None
        params = request.get("params") if isinstance(request, Mapping) and isinstance(request.get("params"), Mapping) else request
        candidate = None
        if isinstance(params, Mapping):
            for field in ("new_sub_id", "candidate_sub_id", "subscription_id", "sub_id"):
                if isinstance(params.get(field), str):
                    candidate = params[field]
                    break
        if candidate != new_sub_id:
            return None, "EVIDENCE_REQUIRED: encrypted operation candidate does not match", ""
        adapter = _factory().resolve_account_adapter(int(binding["id"]))

        def dispatch_rotation(_request_payload, _expected_effect):
            return _run(adapter.rotate_subscription_url(
                binding["email"], str(new_sub_id), str(operation_id)
            ))

        dispatched = _dispatch_operation(binding, str(operation_id), dispatch_rotation)
        state = dispatched.get("dispatch_outcome", "unknown")
        if state == "success":
            return True, "Subscription ID updated and read back", str(new_sub_id)
        if state == "definite_failure":
            return False, "Subscription update definitively failed", ""
        return None, "Subscription outcome unknown; reconciliation required", str(new_sub_id)
    except Exception:
        return None, "Subscription outcome unknown; reconciliation required", str(new_sub_id)


def _operation_matches(binding: Mapping[str, Any], operation_id: str, *kinds: str) -> bool:
    """Require a persisted prepared operation bound to this exact account and panel."""
    if not isinstance(operation_id, str) or not operation_id.strip():
        return False
    db = _database()
    with db.get_db() as conn:
        row = conn.execute(
            "SELECT account_id,panel_id,kind,state FROM panel_operations WHERE operation_id=?",
            (operation_id,),
        ).fetchone()
    return bool(
        row and int(row["account_id"] or 0) == int(binding["id"])
        and int(row["panel_id"]) == int(binding["panel_id"])
        and str(row["state"]) in {"prepared", "dispatching"}
        and (not kinds or str(row["kind"]).casefold() in {item.casefold() for item in kinds})
    )


def _dispatch_operation(binding: Mapping[str, Any], operation_id: str, dispatcher):
    """Claim and record a prepared mutation through the durable operation worker."""
    import time as _time
    from operation_worker import claim_prepared, dispatch_claimed
    from v52_models import MutationResult as LedgerMutationResult

    db = _database()
    now = int(_time.time())
    with db.get_db() as conn:
        claim = claim_prepared(conn, operation_id=str(operation_id), now=now)
        if not isinstance(claim, Mapping) or claim.get("state") != "claimed":
            return {
                "dispatch_outcome": "unknown",
                "operation_state": str(claim.get("state", "unknown")) if isinstance(claim, Mapping) else "not_claimed",
            }

        def _safe_dispatch(request_payload):
            if (int(request_payload.get("account_id") or 0) != int(binding["id"])
                    or int(request_payload.get("panel_id") or 0) != int(binding["panel_id"])):
                return LedgerMutationResult(
                    "unknown", evidence={"reason": "canonical_scope_mismatch"}
                )
            try:
                adapter_result = dispatcher(request_payload, claim.get("expected_effect") or {})
                state = getattr(adapter_result, "state", "unknown")
                if state not in {"success", "definite_failure", "unknown"}:
                    state = "unknown"
                return LedgerMutationResult(
                    state,
                    evidence={"adapter_outcome": state, "stage": "canonical_adapter_dispatch"},
                )
            except Exception as exc:
                return LedgerMutationResult(
                    "unknown", evidence={"reason": "adapter_dispatch_exception",
                                         "exception_type": type(exc).__name__},
                )

        try:
            return dispatch_claimed(
                conn, claim=claim, dispatch=_safe_dispatch, now=now,
            )
        except Exception as exc:
            return {
                "dispatch_outcome": "unknown",
                "operation_state": "needs_reconciliation",
                "reason": type(exc).__name__,
            }


def _mutate(identifier: str, method: str, *, account_ref=None, panel_id=None, operation_id=None,
            target=None, enabled=None):
    binding = _resolve_account(identifier, account_ref=account_ref, panel_id=panel_id)
    if not binding:
        return {"status": "unknown", "state": "unknown", "dispatched": False,
                "reason": "account_binding_missing_or_ambiguous"}
    if not operation_id:
        return {"status": "unknown", "state": "unknown", "dispatched": False,
                "reason": "EVIDENCE_REQUIRED"}
    try:
        expected_kinds = (
            ("renew", "renew_client", "vpn_renew")
            if method == "renew_client"
            else ("set_client_enabled", "enable", "suspend", "activate")
        )
        if not _operation_matches(binding, str(operation_id), *expected_kinds):
            return {"status": "unknown", "state": "unknown", "dispatched": False,
                    "reason": "EVIDENCE_REQUIRED: operation is not prepared for this account"}
        adapter = _factory().resolve_account_adapter(int(binding["id"]))

        def dispatch_from_sealed_request(request_payload, expected_effect):
            request = request_payload.get("request")
            if not isinstance(request, Mapping):
                from panel_adapters.base import MutationResult
                return MutationResult("unknown", {"reason": "sealed_request_missing"})
            params = request.get("params") if isinstance(request.get("params"), Mapping) else request
            if method == "renew_client":
                target_effect = expected_effect.get("target") if isinstance(expected_effect, Mapping) else None
                snapshot = expected_effect.get("snapshot") if isinstance(expected_effect, Mapping) else None
                if not isinstance(target_effect, Mapping) or not isinstance(snapshot, Mapping):
                    from panel_adapters.base import MutationResult
                    return MutationResult("unknown", {"reason": "sealed_renewal_target_missing"})
                candidate_email = params.get("candidate_email") or request_payload.get("candidate_email")
                if candidate_email and str(candidate_email).casefold() != str(binding["email"]).casefold():
                    from panel_adapters.base import MutationResult
                    return MutationResult("unknown", {"reason": "sealed_account_identifier_mismatch"})
                target_value = {
                    "quota_mode": snapshot.get("quota_mode"),
                    "quota_bytes": target_effect.get("quota_bytes"),
                    "expires_at": target_effect.get("expires_at"),
                    "used_bytes": snapshot.get("used_bytes"),
                }
                return _run(adapter.renew_client(binding["email"], target_value, str(operation_id)))

            target_effect = expected_effect.get("target") if isinstance(expected_effect, Mapping) else None
            desired_enabled = (
                target_effect.get("enabled") if isinstance(target_effect, Mapping)
                else params.get("enabled", params.get("enable"))
            )
            if not isinstance(desired_enabled, bool):
                from panel_adapters.base import MutationResult
                return MutationResult("unknown", {"reason": "sealed_enable_target_missing"})
            return _run(adapter.set_client_enabled(binding["email"], desired_enabled, str(operation_id)))

        dispatched = _dispatch_operation(binding, str(operation_id), dispatch_from_sealed_request)
        state = dispatched.get("dispatch_outcome", "unknown")
        return {"status": "ok" if state == "success" else
                "failed" if state == "definite_failure" else "unknown",
                "state": state, "operation_state": dispatched.get("operation_state"),
                "evidence": dispatched.get("evidence")}
    except Exception:
        return {"status": "unknown", "state": "unknown", "reason": "adapter_or_readback_unavailable"}


def renew_client(identifier: str, gb=None, days=None, *args, account_ref=None, panel_id=None,
                 operation_id=None, **kwargs):
    if gb is None and days is None:
        return {"status": "unknown", "state": "unknown", "dispatched": False,
                "reason": "EVIDENCE_REQUIRED"}
    binding = _resolve_account(identifier, account_ref=account_ref, panel_id=panel_id)
    if not binding:
        return {"status": "unknown", "state": "unknown", "dispatched": False,
                "reason": "account_binding_missing_or_ambiguous"}
    try:
        with _database().get_db() as conn:
            panel = conn.execute(
                "SELECT renewal_method FROM panels WHERE id=?", (int(binding["panel_id"]),)
            ).fetchone()
        if panel is None:
            return {"status": "unknown", "state": "unknown", "dispatched": False,
                    "reason": "panel_binding_unavailable"}
        if str(panel["renewal_method"] or "").casefold() == "smart_rollover":
            return {"status": "unknown", "state": "unknown", "dispatched": False,
                    "reason": "EVIDENCE_REQUIRED: EV-02 smart_rollover characterization is incomplete"}
    except Exception:
        return {"status": "unknown", "state": "unknown", "dispatched": False,
                "reason": "EVIDENCE_REQUIRED"}
    target = {"gb": gb, "days": days, **kwargs}
    return _mutate(identifier, "renew_client", account_ref=binding, panel_id=panel_id,
                   operation_id=operation_id, target=target)


def set_client_enable(identifier: str, enable: bool, *args, account_ref=None, panel_id=None,
                      operation_id=None, **kwargs):
    return _mutate(identifier, "set_client_enabled", account_ref=account_ref, panel_id=panel_id,
                   operation_id=operation_id, enabled=enable)


def set_client_comment(identifier: str, comment: str, *args, account_ref=None, panel_id=None, **kwargs):
    return {"status": "unknown", "state": "unknown", "dispatched": False,
            "reason": "EVIDENCE_REQUIRED: comment mutation is not a verified adapter capability"}


def allocate_pool_client(identifier: str, gb=None, days=None, *args, account_ref=None, panel_id=None,
                         account_id=None, operation_id=None, **kwargs):
    binding = _resolve_account(identifier, account_ref=account_ref,
                               account_id=account_id, panel_id=panel_id)
    if not binding or not operation_id or not _operation_matches(
        binding, str(operation_id), "allocate_pool_client", "allocate", "account_create"
    ):
        return {"status": "unknown", "state": "unknown", "dispatched": False,
                "reason": "EVIDENCE_REQUIRED"}
    try:
        adapter = _factory().resolve_account_adapter(int(binding["id"]))

        def dispatch_allocation(_request_payload, _expected_effect):
            from panel_adapters.base import MutationResult
            claim = getattr(adapter, "claim_existing", None)
            if not callable(claim):
                return MutationResult("unknown", {"reason": "allocation_capability_unavailable"})
            allocation = claim(str(operation_id), int(binding["id"]))
            if allocation.state == "found" and allocation.client is not None:
                return MutationResult("success", {"stage": "manual_pool_claim_readback"})
            return MutationResult("unknown", {"stage": "manual_pool_claim",
                                              "lookup_state": str(allocation.state)})

        dispatched = _dispatch_operation(binding, str(operation_id), dispatch_allocation)
        state = dispatched.get("dispatch_outcome", "unknown")
        return {"status": "ok" if state == "success" else
                "failed" if state == "definite_failure" else "unknown",
                "state": state, "operation_state": dispatched.get("operation_state"),
                "account_id": int(binding["id"]), "panel_id": int(binding["panel_id"])}
    except Exception:
        return {"status": "unknown", "state": "unknown", "reason": "allocation_outcome_unknown"}


def get_panel_adapter(panel_id: int):
    """Resolve the adapter for one panel; used by the provisioning service."""
    return _factory().get_adapter(_positive(panel_id, "panel_id"))


def list_panel_inbounds(panel_id: int):
    """Read the panel's inbounds for the admin picker; never mutates anything."""
    try:
        pid = _positive(panel_id, "panel_id")
        inbounds = _run(_factory().get_adapter(pid).list_inbounds())
        return {"state": "found", "panel_id": pid, "inbounds": list(inbounds)}
    except Exception as exc:
        return {"state": "unknown", "panel_id": panel_id, "inbounds": [],
                "reason": type(exc).__name__}


def audit_clients_overview(panel_id: int):
    try:
        pid = _positive(panel_id, "panel_id")
        clients = _run(_factory().get_adapter(pid).list_clients())
        return {"state": "found", "panel_id": pid, "count": len(clients),
                "clients": [_canonical_to_legacy(client) for client in clients]}
    except Exception:
        return {"state": "unknown", "panel_id": panel_id, "count": None, "clients": []}


__all__ = [
    "extract_account_identifier", "parse_client_info", "format_status_report",
    "format_client_deliverable", "generate_qr", "normalize_persian_digits",
    "extract_customer_name", "parse_package_terms", "format_audit_report",
    "allocate_pool_client", "renew_client", "set_client_enable", "set_client_comment",
    "inspect_client", "get_client_links", "get_sub_url", "find_client_by_code",
    "update_client_sub_id", "audit_clients_overview", "list_panel_inbounds", "get_panel_adapter",
]
