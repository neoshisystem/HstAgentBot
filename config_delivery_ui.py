"""ACL-first configuration display, exact-URI QR delivery and UTF-8 exports."""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from io import BytesIO
from html import escape
from typing import Any, Callable, Mapping

import access_service
import database
import qrcode
import re

from telegram import InlineKeyboardMarkup

from ui_v52 import button, render_page, render_uri

from account_card_service import _format_bytes, _format_epoch

TELEGRAM_TEXT_LIMIT = 3800


class ConfigDeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ConfigPage:
    account_id: int
    text: str
    configs: tuple[Any, ...]
    reply_markup: Any
    next_cursor: str | None = None
    status: str = "ok"


@dataclass(frozen=True)
class QRDelivery:
    account_id: int
    index: int
    uri: str | None
    qr_data: str | None
    image: bytes | None
    reply_markup: Any
    status: str


async def _await(value):
    return await value if inspect.isawaitable(value) else value


def _load_account(account_id: int) -> dict[str, Any] | None:
    with database.get_db() as conn:
        row = conn.execute(
            "SELECT * FROM accounts WHERE id=? AND deleted_at IS NULL", (account_id,)
        ).fetchone()
        return dict(row) if row is not None else None


def _actor_value(ctx: Any, key: str):
    return ctx.get(key) if isinstance(ctx, Mapping) else getattr(ctx, key, None)


def _uri(node: Any) -> str:
    value = node.get("uri") if isinstance(node, Mapping) else getattr(node, "uri", None)
    if not isinstance(value, str) or not value or "\n" in value or "\r" in value:
        raise ConfigDeliveryError("panel returned an invalid configuration URI")
    return value


def _label(node: Any, index: int) -> str:
    value = node.get("label") if isinstance(node, Mapping) else getattr(node, "label", None)
    return str(value or f"کانفیگ {index + 1}")


def _page_result(value: Any) -> tuple[list[Any], str | None]:
    if isinstance(value, Mapping):
        items = value.get("items", value.get("configs", []))
        cursor = value.get("next_cursor")
    else:
        items = getattr(value, "items", value)
        cursor = getattr(value, "next_cursor", None)
    if items is None:
        items = []
    if not isinstance(items, (list, tuple)):
        raise ConfigDeliveryError("panel configuration result has an invalid shape")
    return list(items), str(cursor) if cursor not in (None, "") else None


def _qr_bytes(uri: str, qr_factory: Callable[[str], Any] | None = None) -> bytes:
    result = qr_factory(uri) if qr_factory else qrcode.make(uri)
    if isinstance(result, bytes):
        return result
    output = BytesIO()
    if hasattr(result, "save"):
        result.save(output, format="PNG")
    elif hasattr(result, "read"):
        output.write(result.read())
    else:
        raise ConfigDeliveryError("QR renderer did not return image bytes")
    return output.getvalue()


def _subscription_fallback(account: Mapping[str, Any]) -> dict[str, Any] | None:
    """One config item built from the account's subscription link, or ``None``.

    A production account can answer the panel with no configs while still having a
    perfectly good subscription URL — the panel may simply not enumerate it. Falling
    back to that URL means the operator can always copy the link and get its QR code
    instead of being told there is nothing to deliver.
    """
    sub_id = str(account.get("sub_id") or "").strip()
    if not sub_id:
        return None
    url = ""
    try:
        import internal_panel_compat
        url = internal_panel_compat.get_sub_url(sub_id, account_ref=account)
    except Exception:
        url = ""
    if not url:
        try:
            import sub_manager
            url = sub_manager.get_subscription_url(sub_id, account_ref=account)
        except Exception:
            url = ""
    if not isinstance(url, str) or not url or "\n" in url or "\r" in url:
        return None
    return {"uri": url, "label": "لینک اشتراک"}


async def _fetch(
    account_id: int,
    *,
    adapter_factory: Any,
    account_loader: Callable[[int], Mapping[str, Any] | None] | None,
    authorizer: Callable[[Any, int, str], bool] | None,
    ctx: Any,
    cursor: str | None = None,
) -> tuple[dict[str, Any], list[Any], str | None]:
    authorize = authorizer or access_service.authorize_account
    if not authorize(ctx, account_id, "view_config"):
        raise PermissionError("actor cannot view this account configuration")
    account = (account_loader or _load_account)(account_id)
    if not account:
        raise ConfigDeliveryError("account is unavailable")
    adapter = await _await(adapter_factory.resolve_account_adapter(account_id))
    if cursor is not None:
        page_method = getattr(adapter, "get_client_configs_page", None)
        if not callable(page_method):
            raise ConfigDeliveryError("this adapter does not expose server-cursor pagination")
        result = await _await(page_method(str(account.get("email", "")), cursor=cursor))
    else:
        result = await _await(adapter.get_client_configs(str(account.get("email", ""))))
    items, next_cursor = _page_result(result)
    if not items and cursor is None:
        # Only the first page falls back: an empty later page is the real end of the
        # server cursor, not a panel that failed to enumerate the account.
        fallback = _subscription_fallback(account)
        if fallback is not None:
            items = [fallback]
    for item in items:
        _uri(item)
    return dict(account), items, next_cursor


async def show_configs(
    ctx: Any,
    account_id: int,
    *,
    adapter_factory: Any,
    account_loader: Callable[[int], Mapping[str, Any] | None] | None = None,
    authorizer: Callable[[Any, int, str], bool] | None = None,
    cursor: str | None = None,
    message: Any = None,
) -> ConfigPage:
    """Fetch configurations only after ACL; never place a URI in callback data."""
    if isinstance(account_id, bool) or int(account_id) <= 0:
        raise ValueError("account_id must be positive")
    account_id = int(account_id)
    account, items, next_cursor = await _fetch(
        account_id, adapter_factory=adapter_factory, account_loader=account_loader,
        authorizer=authorizer, ctx=ctx, cursor=cursor,
    )
    rows = []
    for index, item in enumerate(items):
        rows.append([button(f"QR · {_label(item, index)}", f"v52:qr:{account_id}:{index}")])
    if items:
        rows.append([button("دریافت همه کانفیگ‌ها", f"v52:allcfg:{account_id}")])
    if next_cursor:
        rows.append([button("صفحه بعد", f"v52:cfg:{account_id}:{next_cursor}")])
    body_html = "\n\n".join(
        f"{escape(_label(item, index), quote=True)}\n{render_uri(_uri(item))}"
        for index, item in enumerate(items)
    ) or "برای این حساب کانفیگی در دسترس نیست."
    page = render_page(
        f"کانفیگ‌های {account.get('account_code') or account_id}",
        persona=str(_actor_value(ctx, "persona") or "user"),
        parent_callback=f"v52:sub:{account_id}", rows=rows, body_html=body_html,
    )
    result = ConfigPage(account_id, page.text, tuple(items), page.reply_markup, next_cursor)
    if message is not None:
        await _await(message.reply_text(
            result.text, reply_markup=result.reply_markup, parse_mode="HTML"
        ))
    return result


def _escape_markdown_v2(text: str) -> str:
    """Escape MarkdownV2 special characters outside triple-backtick code blocks."""
    parts = re.split(r'(```)', text)
    escaped: list[str] = []
    inside_code = False
    special = r'_*[]()~`>#+-=|{}.!'
    for part in parts:
        if part == '```':
            escaped.append(part)
            inside_code = not inside_code
        elif inside_code:
            escaped.append(part)
        else:
            result = []
            for ch in part:
                if ch in special:
                    result.append('\\' + ch)
                else:
                    result.append(ch)
            escaped.append(''.join(result))
    return ''.join(escaped)


def _account_status_text(account: Mapping[str, Any]) -> str:
    enable = account.get("enable")
    return "فعال" if enable else "غیرفعال"


def _quota_text(account: Mapping[str, Any]) -> str:
    total_gb = account.get("total_gb")
    if total_gb is None:
        return "نامعلوم"
    return _format_bytes(int(float(total_gb) * 1024 ** 3))


def _expiry_text(account: Mapping[str, Any]) -> str:
    raw = account.get("expiry_time")
    if raw is None:
        return "نامعلوم"
    epoch = int(raw)
    if epoch < 1_000_000_000:
        return "نامحدود"
    formatted = _format_epoch(epoch)
    return formatted if formatted else "نامعلوم"


async def deliver_config_qr(
    ctx: Any,
    account_id: int,
    index: int,
    *,
    adapter_factory: Any,
    account_loader: Callable[[int], Mapping[str, Any] | None] | None = None,
    authorizer: Callable[[Any, int, str], bool] | None = None,
    qr_factory: Callable[[str], Any] | None = None,
    message: Any = None,
) -> QRDelivery:
    """Build a unified photo message with QR code, account info, config code block and self-service buttons."""
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        index = -1
    try:
        account, items, _ = await _fetch(
            int(account_id), adapter_factory=adapter_factory, account_loader=account_loader,
            authorizer=authorizer, ctx=ctx,
        )
    except PermissionError:
        raise
    aid = int(account_id)
    persona = str(_actor_value(ctx, "persona") or "user")
    page = render_page(
        "QR کانفیگ", "این شماره کانفیگ معتبر نیست؛ فهرست را دوباره باز کنید.",
        persona=persona,
        parent_callback=f"v52:cfg:{aid}",
    )
    if index < 0 or index >= len(items):
        return QRDelivery(aid, index, None, None, None, page.reply_markup, "invalid_index")

    uri = _uri(items[index])
    image = _qr_bytes(uri, qr_factory)

    # --- Caption (MarkdownV2) ---
    label = account.get("account_code") or f"#{aid}"
    status = _account_status_text(account)
    quota = _quota_text(account)
    expiry = _expiry_text(account)
    raw_caption = (
        f"*حساب:* {label}\n"
        f"*وضعیت:* {status}\n"
        f"*حجم:* {quota}\n"
        f"*انقضا:* {expiry}\n\n"
        f"کانفیگ {index + 1}:\n"
        f"```\n{uri}\n```"
    )
    caption = _escape_markdown_v2(raw_caption)

    # --- Inline buttons ---
    rows: list[list[Any]] = [
        [button("🔄 تغییر UUID", f"v52:rotate:cred:{aid}")],
        [button("🔄 بازنشانی لینک", f"v52:rotate:sub:{aid}")],
        [button("🔋 تمدید سرویس", f"v52:account:{aid}")],
        [button("⬅️ بازگشت", f"v52:cfg:{aid}"),
         button("🏠 خانه", f"v52:home:{persona}")],
    ]
    markup = InlineKeyboardMarkup(rows)
    result = QRDelivery(aid, index, uri, uri, image, markup, "ok")

    if message is not None:
        await _await(message.reply_photo(
            photo=BytesIO(image), caption=caption,
            reply_markup=markup, parse_mode="MarkdownV2",
        ))
    return result


async def deliver_all_configs(
    ctx: Any,
    account_id: int,
    *,
    adapter_factory: Any,
    account_loader: Callable[[int], Mapping[str, Any] | None] | None = None,
    authorizer: Callable[[Any, int, str], bool] | None = None,
    message: Any = None,
) -> dict[str, Any]:
    """Send a UTF-8 document for large payloads; never truncate configuration data."""
    account, items, _ = await _fetch(
        int(account_id), adapter_factory=adapter_factory, account_loader=account_loader,
        authorizer=authorizer, ctx=ctx,
    )
    payload = ("\n".join(_uri(item) for item in items)).encode("utf-8")
    if len(payload) > TELEGRAM_TEXT_LIMIT:
        document = BytesIO(payload)
        filename = f"account-{int(account_id)}-configs.txt"
        if message is not None:
            await _await(message.reply_document(
                document=document, filename=filename,
                caption=f"کانفیگ‌های {escape(str(account.get('account_code') or account_id))}",
            ))
        return {"status": "document", "payload": payload, "filename": filename}
    text = "\n\n".join(render_uri(_uri(item)) for item in items)
    if message is not None:
        await _await(message.reply_text(text or "کانفیگی در دسترس نیست.", parse_mode="HTML"))
    return {"status": "text", "payload": payload, "text": text}


__all__ = ["ConfigPage", "QRDelivery", "ConfigDeliveryError",
           "show_configs", "deliver_config_qr", "deliver_all_configs"]
