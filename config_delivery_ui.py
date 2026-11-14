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

from ui_v52 import button, render_page, render_uri

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
    """Build QR from exactly one freshly fetched URI; invalid indexes retain navigation."""
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        index = -1
    try:
        _, items, _ = await _fetch(
            int(account_id), adapter_factory=adapter_factory, account_loader=account_loader,
            authorizer=authorizer, ctx=ctx,
        )
    except PermissionError:
        raise
    page = render_page(
        "QR کانفیگ", "این شماره کانفیگ معتبر نیست؛ فهرست را دوباره باز کنید.",
        persona=str(_actor_value(ctx, "persona") or "user"),
        parent_callback=f"v52:cfg:{int(account_id)}",
    )
    if index < 0 or index >= len(items):
        return QRDelivery(int(account_id), index, None, None, None, page.reply_markup, "invalid_index")
    uri = _uri(items[index])
    image = _qr_bytes(uri, qr_factory)
    result = QRDelivery(int(account_id), index, uri, uri, image, page.reply_markup, "ok")
    if message is not None:
        await _await(message.reply_photo(
            photo=BytesIO(image), caption=f"QR کانفیگ {index + 1}",
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
