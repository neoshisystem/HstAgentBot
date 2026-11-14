"""Fresh, ACL-gated account snapshots and message-only card refreshes."""
from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from html import escape
from typing import Any, Callable, Mapping

import access_service
import config
import database
from telegram import InlineKeyboardMarkup

from ui_v52 import button, render_page


class AccountCardError(RuntimeError):
    pass


class AccountCardDenied(AccountCardError, PermissionError):
    pass


@dataclass(frozen=True)
class AccountCard:
    account_id: int
    text: str
    reply_markup: InlineKeyboardMarkup | None
    snapshot: Mapping[str, Any]
    status: str
    observed_at: int

    def as_dict(self) -> dict[str, Any]:
        return {"account_id": self.account_id, "text": self.text,
                "reply_markup": self.reply_markup, "snapshot": dict(self.snapshot),
                "status": self.status, "observed_at": self.observed_at}


def _actor_id(ctx: Any) -> int:
    value = ctx.get("actor_tg_id", ctx.get("telegram_id")) if isinstance(ctx, Mapping) else getattr(ctx, "actor_tg_id", getattr(ctx, "telegram_id", None))
    if isinstance(value, bool):
        raise AccountCardDenied("invalid actor")
    try:
        actor = int(value)
    except (TypeError, ValueError):
        raise AccountCardDenied("invalid actor") from None
    if actor <= 0:
        raise AccountCardDenied("invalid actor")
    return actor


def _is_root(ctx: Any) -> bool:
    try:
        root_id = int(getattr(config, "ADMIN_TELEGRAM_ID", 0) or 0)
    except (TypeError, ValueError):
        root_id = 0
    return root_id > 0 and _actor_id(ctx) == root_id


def _load_account(account_id: int) -> dict[str, Any] | None:
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE id=? AND deleted_at IS NULL",
            (account_id,),
        ).fetchone()
        return dict(row) if row is not None else None


async def _await(value):
    return await value if inspect.isawaitable(value) else value


def _format_bytes(value: int | None) -> str:
    if value is None:
        return "نامعلوم"
    amount = max(0, int(value))
    if amount >= 1024 ** 3:
        return f"{amount / (1024 ** 3):.2f} GiB"
    if amount >= 1024 ** 2:
        return f"{amount / (1024 ** 2):.1f} MiB"
    return f"{amount} B"


async def build_account_card(
    ctx: Any,
    account_id: int,
    *,
    adapter_factory: Any,
    account_loader: Callable[[int], Mapping[str, Any] | None] | None = None,
    authorizer: Callable[[Any, int, str], bool] | None = None,
    now: int | None = None,
) -> AccountCard:
    """Read this account's own panel only; an unavailable read remains unknown."""
    if isinstance(account_id, bool) or int(account_id) <= 0:
        raise ValueError("account_id must be positive")
    exact_id = int(account_id)
    authorize = authorizer or access_service.authorize_account
    if not authorize(ctx, exact_id, "view_config"):
        raise AccountCardDenied("actor cannot view this account")
    loader = account_loader or _load_account
    account = loader(exact_id)
    if not account:
        raise AccountCardError("account is unavailable")
    observed = int(time.time()) if now is None else int(now)
    if observed < 0:
        raise ValueError("now must be non-negative")

    status = "unavailable"
    client = None
    try:
        adapter = await _await(adapter_factory.resolve_account_adapter(exact_id))
        lookup = await _await(adapter.get_client(str(account.get("email", ""))))
        state = getattr(lookup, "state", "found" if lookup is not None else "unavailable")
        if state == "found":
            client = getattr(lookup, "client", lookup)
            status = "fresh"
        elif state == "not_found":
            status = "not_found"
        else:
            status = "unavailable"
    except Exception:
        # A failed panel lookup is not zero traffic, zero expiry or a financial failure.
        status = "unavailable"
        client = None

    if client is not None:
        seen_at = int(getattr(client, "observed_at", 0) or observed)
        enabled = getattr(client, "enabled", None)
        quota_mode = getattr(client, "quota_mode", "unknown")
        quota_bytes = getattr(client, "quota_bytes", None)
        used_bytes = getattr(client, "used_bytes", None)
        time_mode = getattr(client, "time_mode", "unknown")
        expires_at = getattr(client, "expires_at", None)
        subscription_id = getattr(client, "subscription_id", None)
        adapter_revision = getattr(client, "adapter_revision", "")
    else:
        seen_at, enabled, quota_mode, quota_bytes, used_bytes = observed, None, "unknown", None, None
        time_mode, expires_at, subscription_id, adapter_revision = "unknown", None, None, ""

    snapshot = {
        "account_id": exact_id, "status": status, "observed_at": seen_at,
        "enabled": enabled, "quota_mode": quota_mode, "quota_bytes": quota_bytes,
        "used_bytes": used_bytes, "time_mode": time_mode, "expires_at": expires_at,
        "subscription_present": bool(subscription_id),
        "adapter_revision": adapter_revision,
    }
    persona = "admin" if _is_root(ctx) else "rep" if isinstance(ctx, Mapping) and ctx.get("persona") == "rep" else "user"
    label = escape(str(account.get("account_code") or f"#{exact_id}"), quote=True)
    if status == "fresh":
        enabled_label = "فعال" if enabled is True else "غیرفعال" if enabled is False else "نامعلوم"
        quota_label = "نامحدود" if quota_mode == "unlimited" else _format_bytes(quota_bytes)
        used_label = _format_bytes(used_bytes)
        expiry_label = "نامحدود" if time_mode == "unlimited" else "در انتظار" if time_mode == "on_hold" else str(expires_at) if expires_at is not None else "نامعلوم"
        body = (f"وضعیت: {enabled_label}\nمصرف: {used_label} / {quota_label}\n"
                f"انقضا: {expiry_label}\nمشاهده: {seen_at}")
    elif status == "not_found":
        body = "این حساب در پنل متصل پیدا نشد؛ مقادیر مصرف و انقضا نامعلوم‌اند."
    else:
        body = "دریافت snapshot از پنل ممکن نشد؛ مقادیر مصرف و انقضا نامعلوم‌اند."

    rows = [[button("کانفیگ‌ها و پروفایل‌ها", f"v52:sub:{exact_id}")],
            [button("دریافت کانفیگ‌ها", f"v52:cfg:{exact_id}")]]
    if authorize(ctx, exact_id, "rotate"):
        rows.append([button("چرخش لینک اشتراک", f"v52:rotate:sub:{exact_id}"),
                     button("چرخش اعتبارنامه", f"v52:rotate:cred:{exact_id}")])
    if authorize(ctx, exact_id, "renew"):
        rows.append([button("تمدید سرویس", f"v52:renew:{exact_id}")])
    if _is_root(ctx):
        rows.append([button("ترافیک", f"v52:traffic:{exact_id}"),
                     button("زمان", f"v52:expiry:{exact_id}")])
    page = render_page(f"حساب {label}", body, persona=persona, root=False,
                       parent_callback=f"v52:accounts:{persona}", rows=rows)
    return AccountCard(exact_id, page.text, page.reply_markup, snapshot, status, seen_at)


async def refresh_account_card(
    ctx: Any,
    account_id: int,
    *,
    message: Any,
    adapter_factory: Any,
    account_loader: Callable[[int], Mapping[str, Any] | None] | None = None,
    authorizer: Callable[[Any, int, str], bool] | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Edit the same Telegram message; edit errors never mutate business state."""
    card = await build_account_card(
        ctx, account_id, adapter_factory=adapter_factory, account_loader=account_loader,
        authorizer=authorizer, now=now,
    )
    try:
        await _await(message.edit_text(
            card.text, reply_markup=card.reply_markup, parse_mode="HTML",
        ))
        return {**card.as_dict(), "edited": True, "render_error": None}
    except Exception:
        return {**card.as_dict(), "edited": False, "render_error": "telegram_edit_failed"}


__all__ = ["AccountCard", "AccountCardError", "AccountCardDenied",
           "build_account_card", "refresh_account_card"]
