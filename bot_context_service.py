"""Seller routing derived only from the active Telegram bot registry."""
from __future__ import annotations
from dataclasses import dataclass
import sqlite3
from typing import Any
from identity_service import resolve_actor


class BotContextError(RuntimeError):
    """The Telegram bot context is absent or not registered as active."""
class CheckoutRouteDenied(PermissionError):
    """The requested actor/account route is not valid for this bot."""


@dataclass(frozen=True)
class BotContext:
    bot_id: int
    is_primary: bool
    rep_id: int | None
    seller_key: str
    merchant_key: str


def _positive(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise BotContextError(f"{label} must be a positive integer")
    try:
        value = int(value)
    except (TypeError, ValueError):
        raise BotContextError(f"{label} must be a positive integer") from None
    if value <= 0:
        raise BotContextError(f"{label} must be a positive integer")
    return value


def _rep_active(conn: sqlite3.Connection, rep_id: int) -> bool:
    return conn.execute(
        "SELECT 1 FROM representatives WHERE id=? AND is_active=1 AND deleted_at IS NULL",
        (rep_id,),
    ).fetchone() is not None


def resolve_bot_context(conn: sqlite3.Connection, context: Any) -> BotContext:
    """Resolve trusted bot identity from context.bot.id, never start/callback data."""
    bot = getattr(context, "bot", None)
    bot_id = _positive(getattr(bot, "id", None), "context.bot.id")
    row = conn.execute(
        "SELECT bot_id,rep_id,is_primary FROM bot_registry "
        "WHERE bot_id=? AND status='active'",
        (bot_id,),
    ).fetchone()
    if row is None:
        raise BotContextError("Telegram bot is not registered and active")
    is_primary = bool(row["is_primary"])
    rep_id = None if row["rep_id"] is None else int(row["rep_id"])
    if is_primary:
        if rep_id is not None:
            raise BotContextError("primary bot has an invalid representative binding")
        seller = "root"
    else:
        if rep_id is None or not _rep_active(conn, rep_id):
            raise BotContextError("dedicated bot representative is inactive or unavailable")
        seller = f"rep:{rep_id}"
    return BotContext(bot_id, is_primary, rep_id, seller, seller)


def _account_seller(conn: sqlite3.Connection, account_id: int) -> str:
    row = conn.execute(
        "SELECT seller_key,deleted_at,enable FROM accounts WHERE id=?", (account_id,)
    ).fetchone()
    if row is None or row["deleted_at"] is not None or not bool(row["enable"]):
        raise CheckoutRouteDenied("account is not active")
    seller = str(row["seller_key"] or "root")
    if seller != "root":
        if not seller.startswith("rep:"):
            raise CheckoutRouteDenied("account seller binding is invalid")
        try:
            rep_id = _positive(seller[4:], "account seller representative")
        except BotContextError:
            raise CheckoutRouteDenied("account seller binding is invalid") from None
        if not _rep_active(conn, rep_id):
            raise CheckoutRouteDenied("account seller representative is inactive")
    return seller


def authorize_checkout_route(
    conn: sqlite3.Connection, context: Any, *, actor_tg_id: int, action: str,
    account_id: int | None = None, start_parameter: str | None = None,
    callback_data: str | None = None,
) -> dict[str, Any]:
    """Return a trusted merchant binding after actor, bot and account checks.

    start_parameter and callback_data are accepted for callers that already have
    them, but are never parsed or used to select a seller.
    """
    bot = resolve_bot_context(conn, context)
    try:
        actor = resolve_actor(conn, actor_tg_id)
    except Exception as exc:
        raise CheckoutRouteDenied("actor identity could not be resolved") from exc
    if actor.is_blocked:
        raise CheckoutRouteDenied("blocked actors cannot enter checkout routes")
    selected = str(action or "").strip().lower()
    route_mode = "consumer"

    if selected == "buy":
        if actor.persona == "rep" and not actor.is_root:
            raise CheckoutRouteDenied("representatives must use the manual issue route")
        seller = bot.seller_key

    elif selected == "rep_issue":
        if actor.is_root:
            seller, route_mode = "root", "root_desk"
        elif actor.persona == "rep" and actor.rep_id is not None:
            if not _rep_active(conn, actor.rep_id):
                raise CheckoutRouteDenied("representative is inactive")
            if not bot.is_primary and bot.rep_id != actor.rep_id:
                raise CheckoutRouteDenied("representative does not own this dedicated bot")
            seller, route_mode = f"rep:{actor.rep_id}", "chain_price"
        else:
            raise CheckoutRouteDenied("only a representative or root can issue manually")

    elif selected in {"renew", "rep_renew"}:
        if account_id is None:
            raise CheckoutRouteDenied("renewal requires an account ID")
        selected_account = _positive(account_id, "account_id")
        account_seller = _account_seller(conn, selected_account)
        if selected == "rep_renew":
            if actor.is_root:
                seller, route_mode = "root", "root_desk"
            elif actor.persona == "rep" and actor.rep_id is not None:
                if not _rep_active(conn, actor.rep_id):
                    raise CheckoutRouteDenied("representative is inactive")
                seller, route_mode = f"rep:{actor.rep_id}", "chain_price"
            else:
                raise CheckoutRouteDenied("only the owning representative or root can renew manually")
        else:
            seller = bot.seller_key
        if account_seller != seller:
            raise CheckoutRouteDenied("account seller does not match this checkout route")

    elif selected == "view":
        seller, route_mode = bot.seller_key, "view_only"
    else:
        raise CheckoutRouteDenied("unsupported checkout route action")

    merchant_rep_id = None
    if seller.startswith("rep:"):
        merchant_rep_id = _positive(seller[4:], "merchant representative ID")
    return {
        "bot_id": bot.bot_id, "is_primary": bot.is_primary,
        "bot_rep_id": bot.rep_id, "actor_tg_id": actor.actor_tg_id,
        "actor_rep_id": actor.rep_id, "seller_key": seller,
        "merchant_key": seller, "merchant_rep_id": merchant_rep_id,
        "action": selected, "route_mode": route_mode,
    }


__all__ = [
    "BotContextError", "CheckoutRouteDenied", "BotContext",
    "resolve_bot_context", "authorize_checkout_route",
]
