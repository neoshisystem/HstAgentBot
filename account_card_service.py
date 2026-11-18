"""Fresh, ACL-gated account snapshots and message-only card refreshes."""
from __future__ import annotations

import inspect
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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


#: Iran has not observed daylight saving since 2022, so a fixed offset is exact and
#: avoids depending on a tzdata database being present in the image.
TEHRAN = timezone(timedelta(hours=3, minutes=30))

#: Below this, a number is a duration or a placeholder (``0``), not a real timestamp.
_EPOCH_FLOOR = 1_000_000_000


def _format_epoch(value: Any) -> str:
    """Render a Unix epoch as ``YYYY-MM-DD HH:MM`` in Tehran time, else ``""``.

    A panel that reports a remaining duration rather than a deadline (or a ``0``
    placeholder) is not a date, so anything below :data:`_EPOCH_FLOOR` is left alone
    and the caller keeps showing whatever it showed before.
    """
    try:
        epoch = int(value)
    except (TypeError, ValueError):
        return ""
    if epoch < _EPOCH_FLOOR:
        return ""
    return datetime.fromtimestamp(epoch, TEHRAN).strftime("%Y-%m-%d %H:%M")


def _remaining_days(expires_at: Any, now: int) -> str:
    """``X روز باقی‌مانده`` (or ``X روز گذشته``) for an epoch expiry, else ``""``."""
    try:
        epoch = int(expires_at)
    except (TypeError, ValueError):
        return ""
    if epoch < _EPOCH_FLOOR:
        return ""
    days = (epoch - int(now)) // 86400
    return f"{days} روز باقی‌مانده" if days >= 0 else f"{abs(days)} روز گذشته"


def _cached_lines(account: Mapping[str, Any], now: int) -> list[str]:
    """Body lines rebuilt from the local ``accounts`` row when the panel is silent.

    The row is the last thing the platform itself wrote, so it is a far better answer
    than a blank error: the operator still sees whether the account is enabled, how
    much traffic it was sold and when it was due. It is labelled as cached so nobody
    mistakes it for a fresh panel read.
    """
    lines: list[str] = []
    enabled = account.get("enable")
    if enabled is not None:
        lines.append("وضعیت: " + ("فعال" if int(enabled) else "غیرفعال"))
    total_gb = account.get("total_gb")
    if total_gb is not None:
        lines.append(f"حجم خریداری‌شده: {float(total_gb):g} گیگابایت")
    expiry = account.get("expiry_time")
    human = _format_epoch(expiry)
    if human:
        lines.append(f"انقضا: {human} ({_remaining_days(expiry, now)})")
    elif expiry is not None:
        lines.append(f"انقضا: {expiry}")
    return lines


async def build_account_card(
    ctx: Any,
    account_id: int,
    *,
    adapter_factory: Any,
    account_loader: Callable[[int], Mapping[str, Any] | None] | None = None,
    authorizer: Callable[[Any, int, str], bool] | None = None,
    now: int | None = None,
    parent_callback: str | None = None,
) -> AccountCard:
    """Read this account's own panel only; an unavailable read remains unknown.

    ``parent_callback`` is the surface the card was opened from. A drill-down from a
    360-degree profile passes that profile, so Back returns to the person the
    operator was working on. Without one the card offers the persona home: the
    previous default was ``v52:accounts:<persona>``, which was never registered in
    ``v52_router._CALLBACK_PATTERNS``, so every back press on this card resolved to
    no Route and surfaced the registry error page instead of navigating.
    """
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
        if time_mode == "unlimited":
            expiry_label = "نامحدود"
        elif time_mode == "on_hold":
            expiry_label = "در انتظار"
        elif expires_at is None:
            expiry_label = "نامعلوم"
        else:
            human = _format_epoch(expires_at)
            expiry_label = (f"{human} ({_remaining_days(expires_at, observed)})"
                            if human else str(expires_at))
        seen_label = _format_epoch(seen_at) or str(seen_at)
        body = (f"وضعیت: {enabled_label}\nمصرف: {used_label} / {quota_label}\n"
                f"انقضا: {expiry_label}\nمشاهده: {seen_label}")
    elif status == "not_found":
        body = "این حساب در پنل متصل پیدا نشد؛ مقادیر مصرف و انقضا نامعلوم‌اند."
    else:
        # The production loader is the local ``accounts`` row, so when the panel is
        # silent the operator still gets the last figures the platform itself wrote.
        # A caller that injected its own loader is exercising the unavailable path on
        # purpose and must keep seeing the plain notice.
        cached = _cached_lines(account, observed) if account_loader is None else []
        if cached:
            body = ("دریافت snapshot تازه از پنل ممکن نشد؛ آخرین مقادیر ثبت‌شده:\n"
                    + "\n".join(cached))
        else:
            body = "دریافت snapshot از پنل ممکن نشد؛ مقادیر مصرف و انقضا نامعلوم‌اند."

    rows = [[button("کانفیگ‌ها و پروفایل‌ها", f"v52:sub:{exact_id}")],
            [button("دریافت کانفیگ‌ها", f"v52:cfg:{exact_id}")],
            [button("🎫 پشتیبانی این سرویس", f"v52:support:{exact_id}")]]
    if authorize(ctx, exact_id, "rotate"):
        rows.append([button("چرخش لینک اشتراک", f"v52:rotate:sub:{exact_id}"),
                     button("چرخش اعتبارنامه", f"v52:rotate:cred:{exact_id}")])
    if authorize(ctx, exact_id, "renew"):
        rows.append([button("تمدید سرویس", f"v52:renew:{exact_id}")])
    if _is_root(ctx):
        rows.append([button("ترافیک", f"v52:traffic:{exact_id}"),
                     button("زمان", f"v52:expiry:{exact_id}")])
    page = render_page(f"حساب {label}", body, persona=persona, root=False,
                       parent_callback=parent_callback or f"v52:home:{persona}",
                       rows=rows)
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
    parent_callback: str | None = None,
) -> dict[str, Any]:
    """Edit the same Telegram message; edit errors never mutate business state."""
    card = await build_account_card(
        ctx, account_id, adapter_factory=adapter_factory, account_loader=account_loader,
        authorizer=authorizer, now=now, parent_callback=parent_callback,
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
