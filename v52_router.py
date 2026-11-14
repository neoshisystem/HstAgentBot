"""Strict, registered v5.2 callback/text/photo router and isolated draft FSM.

The router resolves callbacks only; business mutations remain inside the declared
service handlers. FSM identity includes bot, human actor and chat to prevent
cross-bot or cross-chat draft leakage.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable

from ui_v52 import render_error

MAX_CALLBACK_BYTES = 64
DEFAULT_TTL_SECONDS = 900
_ID = re.compile(r"^[1-9][0-9]{0,17}$")
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,40}$")


@dataclass(frozen=True)
class Route:
    name: str
    handler: str
    params: dict[str, Any]
    scope: str


@dataclass
class FSMEntry:
    bot_id: int
    telegram_id: int
    chat_id: int
    state: str
    parent_callback: str | None
    expires_at: int
    payload: dict[str, Any]


_CALLBACK_PATTERNS: tuple[tuple[re.Pattern[str], str, str, Callable[[re.Match[str]], dict[str, Any]]], ...] = (
    (re.compile(r"^v52:home:(admin|rep|user)$"), "route_home", "persona", lambda m: {"persona": m[1]}),
    (re.compile(r"^v52:sub:([1-9][0-9]{0,17})$"), "open_account_profiles", "view_config", lambda m: {"account_id": int(m[1])}),
    (re.compile(r"^v52:profiles$"), "list_profiles", "actor", lambda m: {}),
    (re.compile(r"^v52:profile:([1-9][0-9]{0,17})$"), "show_profile", "owner/root", lambda m: {"profile_id": int(m[1])}),
    (re.compile(r"^v52:padd:([1-9][0-9]{0,17}):([1-9][0-9]{0,17})$"), "add_member", "owner/root", lambda m: {"profile_id": int(m[1]), "account_id": int(m[2])}),
    (re.compile(r"^v52:premove:([1-9][0-9]{0,17}):([1-9][0-9]{0,17})$"), "preview_remove", "owner/root", lambda m: {"profile_id": int(m[1]), "account_id": int(m[2])}),
    (re.compile(r"^v52:pnew:([0-9]{1,18})$"), "begin_create", "actor", lambda m: {"account_id": int(m[1])}),
    (re.compile(r"^v52:plink:([1-9][0-9]{0,17})$"), "show_link", "owner/root", lambda m: {"profile_id": int(m[1])}),
    (re.compile(r"^v52:pqr:([1-9][0-9]{0,17})$"), "show_qr", "owner/root", lambda m: {"profile_id": int(m[1])}),
    (re.compile(r"^v52:prot:([1-9][0-9]{0,17})$"), "preview_rotate", "owner/root", lambda m: {"profile_id": int(m[1])}),
    (re.compile(r"^v52:ppause:([1-9][0-9]{0,17}):(0|1)$"), "preview_enabled", "owner/root", lambda m: {"profile_id": int(m[1]), "enabled": m[2] == "1"}),
    (re.compile(r"^v52:pdel:([1-9][0-9]{0,17})$"), "preview_delete", "owner/root", lambda m: {"profile_id": int(m[1])}),
    (re.compile(r"^v52:pclear:([1-9][0-9]{0,17})$"), "preview_clear", "owner/root", lambda m: {"profile_id": int(m[1])}),
    (re.compile(r"^v52:prename:([1-9][0-9]{0,17})$"), "begin_rename", "owner/root", lambda m: {"profile_id": int(m[1])}),
    (re.compile(r"^v52:rotate:(sub|cred):([1-9][0-9]{0,17})$"), "preview_rotation", "manager/owner/seller/root", lambda m: {"rotation_type": m[1], "account_id": int(m[2])}),
    (re.compile(r"^v52:traffic:([1-9][0-9]{0,17})$"), "begin_traffic", "root", lambda m: {"account_id": int(m[1])}),
    (re.compile(r"^v52:expiry:([1-9][0-9]{0,17})$"), "begin_expiry", "root", lambda m: {"account_id": int(m[1])}),
    (re.compile(r"^v52:grants:([1-9][0-9]{0,17})$"), "show_grants", "owner/seller/root", lambda m: {"account_id": int(m[1])}),
    (re.compile(r"^v52:gset:([1-9][0-9]{0,17}):(viewer|manager)$"), "begin_grant", "owner/seller/root", lambda m: {"account_id": int(m[1]), "level": m[2]}),
    (re.compile(r"^v52:grevoke:([1-9][0-9]{0,17}):([1-9][0-9]{0,17})$"), "preview_revoke", "owner/seller/root", lambda m: {"account_id": int(m[1]), "grantee_tg_id": int(m[2])}),
    (re.compile(r"^v52:bot:([1-9][0-9]{0,17})$"), "begin_register_bot", "root", lambda m: {"rep_id": int(m[1])}),
    (re.compile(r"^v52:domain:([1-9][0-9]{0,17})$"), "begin_register", "same rep/root", lambda m: {"rep_id": int(m[1])}),
    (re.compile(r"^v52:panel:([1-9][0-9]{0,17})$"), "show_panel", "root", lambda m: {"panel_id": int(m[1])}),
    (re.compile(r"^v52:panelconfig:([1-9][0-9]{0,17})$"), "begin_panel_config", "root", lambda m: {"panel_id": int(m[1])}),
    (re.compile(r"^v52:paneltest:([1-9][0-9]{0,17})$"), "test_panel_connection", "root", lambda m: {"panel_id": int(m[1])}),
    (re.compile(r"^v52:life:([1-9][0-9]{0,17}):(active|sold_out|hidden|drain|maintenance)$"), "preview_lifecycle", "root", lambda m: {"panel_id": int(m[1]), "lifecycle": m[2]}),
    (re.compile(r"^v52:life-confirm:([1-9][0-9]{0,17}):(active|sold_out|hidden|drain|maintenance)$"), "confirm_lifecycle", "root", lambda m: {"panel_id": int(m[1]), "lifecycle": m[2]}),
    (re.compile(r"^v52:buy:([A-Za-z0-9][A-Za-z0-9._-]{0,47})$"), "preview_buy", "seller route", lambda m: {"plan_code": m[1]}),
    (re.compile(r"^v52:renew:([1-9][0-9]{0,17}):([A-Za-z0-9][A-Za-z0-9._-]{0,47})$"), "preview_renew", "manager/owner/seller/root", lambda m: {"account_id": int(m[1]), "plan_code": m[2]}),
    (re.compile(r"^v52:addon:([1-9][0-9]{0,17}):(gib|day)$"), "begin_addon", "owner/seller/root", lambda m: {"account_id": int(m[1]), "addon_type": m[2]}),
    (re.compile(r"^v52:pay:([A-Za-z0-9._:-]{1,36}):(wallet|card)$"), "choose_rail", "payer", lambda m: {"order_id": m[1], "rail": m[2]}),
    (re.compile(r"^v52:receipt:(approve|reject):([A-Za-z0-9._:-]{1,36})$"), "preview_receipt", "reviewer/root", lambda m: {"decision": m[1], "receipt_id": m[2]}),
    (re.compile(r"^v52:shop$"), "show_catalog", "actor", lambda m: {}),
    (re.compile(r"^v52:goods:([1-9][0-9]{0,17})$"), "preview_product", "actor+seller", lambda m: {"product_id": int(m[1])}),
    (re.compile(r"^v52:fulfill:([A-Za-z0-9._:-]{1,36})$"), "prepare_manual_delivery", "assigned seller/root", lambda m: {"order_id": m[1]}),
    (re.compile(r"^v52:tickets$"), "list_tickets", "actor", lambda m: {}),
    (re.compile(r"^v52:support:([0-9]{1,18})$"), "begin_ticket", "view authorized", lambda m: {"account_id": int(m[1])}),
    (re.compile(r"^v52:abuse$"), "begin_abuse", "all actors; private root destination", lambda m: {}),
    (re.compile(r"^v52:ticket:([A-Za-z0-9_-]{1,32})$"), "reply_or_escalate", "ticket scope", lambda m: {"ticket_id": m[1]}),
    (re.compile(r"^v52:escalate:([A-Za-z0-9_-]{1,32})$"), "reply_or_escalate", "assigned seller/root", lambda m: {"ticket_id": m[1], "action": "escalate"}),
    (re.compile(r"^v52:transfer:([1-9][0-9]{0,17})$"), "reply_or_escalate", "root preview", lambda m: {"account_id": int(m[1]), "action": "transfer"}),
    (re.compile(r"^v52:ops$"), "show_operations", "root", lambda m: {}),
    (re.compile(r"^v52:reconcile:([A-Za-z0-9._:-]{1,36})$"), "show_operations", "root", lambda m: {"operation_id": m[1]}),
    (re.compile(r"^v52:refund:([A-Za-z0-9._:-]{1,36})$"), "preview_refund", "payer/root", lambda m: {"refund_id": m[1]}),
    (re.compile(r"^v52:forums:([A-Za-z0-9._:-]{1,36})$"), "sync_topics", "root", lambda m: {"owner_key": m[1]}),
    (re.compile(r"^v52:poolimport:([1-9][0-9]{0,17})$"), "begin_manual_import", "root DM", lambda m: {"panel_id": int(m[1])}),
    (re.compile(r"^v52:inventory:([1-9][0-9]{0,17})$"), "begin_digital_import", "root DM", lambda m: {"product_id": int(m[1])}),
    (re.compile(r"^v52:q:([A-Za-z0-9_-]{16,40})$"), "resolve_quote_preview", "actor+seller", lambda m: {"nonce": m[1]}),
    (re.compile(r"^v52:ok:([A-Za-z0-9_-]{16,40})$"), "consume_and_dispatch", "same actor/bot/action/revision", lambda m: {"nonce": m[1]}),
    (re.compile(r"^v52:no:([A-Za-z0-9_-]{16,40})$"), "cancel_confirmation", "same actor/bot", lambda m: {"nonce": m[1]}),
)


def _positive(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _identity(context: Any) -> tuple[int, int, int]:
    bot = _value(context, "bot")
    bot_id = _value(context, "bot_id", _value(bot, "id"))
    actor = _value(context, "actor_tg_id", _value(context, "telegram_id"))
    chat_id = _value(context, "chat_id", actor)
    return (_positive(bot_id, "bot_id"), _positive(actor, "telegram_id"), _positive(chat_id, "chat_id"))


def _value(obj: Any, key: str, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def route_callback(callback_data: str, *, context: Any = None):
    """Strictly resolve a registered callback. Unknown/forged input returns a page, not a crash."""
    if not isinstance(callback_data, str) or not callback_data:
        return render_error("callback خالی یا نامعتبر است.")
    if len(callback_data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        return render_error("callback از سقف ۶۴ بایت بیشتر است.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in callback_data):
        return render_error("callback دارای نویسه کنترلی است.")
    for pattern, handler, scope, decode in _CALLBACK_PATTERNS:
        match = pattern.fullmatch(callback_data)
        if match:
            return Route(callback_data.split(":", 2)[1], handler, decode(match), scope)
    return render_error("این callback در registry ثبت نشده است.")


class DraftFSM:
    """Small TTL-bound in-memory FSM; callers may persist it in bot/chat-scoped storage."""
    def __init__(self, *, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 86_400:
            raise ValueError("ttl_seconds must be 1..86400")
        self.ttl_seconds = ttl_seconds
        self._entries: dict[tuple[int, int, int], FSMEntry] = {}

    def set(self, context: Any, state: str, *, payload: dict[str, Any] | None = None,
            parent_callback: str | None = None, now: int) -> FSMEntry:
        bot_id, tg_id, chat_id = _identity(context)
        selected = str(state or "")
        if selected not in {"goods_manual_secret", "support_ticket_body", "support_reply",
                            "panel_connection_url", "panel_connection_token",
                            "inventory_manual_batch", "inventory_digital_batch"}:
            raise ValueError("unknown v5.2 FSM state")
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("now must be non-negative epoch seconds")
        entry = FSMEntry(bot_id, tg_id, chat_id, selected, parent_callback,
                         now + self.ttl_seconds, dict(payload or {}))
        self._entries[(bot_id, tg_id, chat_id)] = entry
        return entry

    def peek(self, context: Any, *, now: int) -> FSMEntry | None:
        key = _identity(context)
        entry = self._entries.get(key)
        if entry is None:
            return None
        if isinstance(now, bool) or not isinstance(now, int) or now >= entry.expires_at:
            self._entries.pop(key, None)
            return None
        return entry

    def consume(self, context: Any, *, event: str, now: int):
        entry = self.peek(context, now=now)
        if entry is None:
            return None
        accepted = {"text": {"goods_manual_secret", "support_ticket_body", "support_reply",
                             "panel_connection_url", "panel_connection_token",
                             "inventory_manual_batch", "inventory_digital_batch"},
                    "photo": {"support_ticket_body", "support_reply"}}
        if event not in accepted or entry.state not in accepted[event]:
            return None
        self._entries.pop((entry.bot_id, entry.telegram_id, entry.chat_id), None)
        return entry

    def clear(self, context: Any) -> bool:
        return self._entries.pop(_identity(context), None) is not None


DEFAULT_FSM = DraftFSM()


def route_text(context: Any, text: str, *, now: int, fsm: DraftFSM = DEFAULT_FSM):
    """Consume text only when an unexpired registered draft state owns this identity."""
    if not isinstance(text, str):
        return False
    entry = fsm.consume(context, event="text", now=now)
    if entry is None:
        return False
    return Route(entry.state, entry.state, {"text": text, "payload": dict(entry.payload),
                                           "parent_callback": entry.parent_callback}, "fsm")


def route_photo(context: Any, file_id: str, *, now: int, fsm: DraftFSM = DEFAULT_FSM):
    """Consume photo only for support states that explicitly accept attachments."""
    if not isinstance(file_id, str) or not file_id.strip():
        return False
    entry = fsm.consume(context, event="photo", now=now)
    if entry is None:
        return False
    return Route(entry.state, entry.state, {"file_id": file_id, "payload": dict(entry.payload),
                                           "parent_callback": entry.parent_callback}, "fsm")


__all__ = ["Route", "FSMEntry", "DraftFSM", "DEFAULT_FSM", "route_callback", "route_text", "route_photo"]