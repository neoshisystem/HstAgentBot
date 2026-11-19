"""Root-only private import wizards for manual panel pools and digital inventory.

Both wizards are two-step: a preview that validates and masks the batch, then a
confirmation that performs the insert. The parsed batch is handed between the two
steps through :mod:`inventory_import_service`, because the preview's local
variables do not survive the round trip through Telegram — the FSM entry that
carried the operator's text is consumed before the preview is even rendered, so a
confirmation callback wired back to the entry handler silently dropped the batch.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from html import escape as _html_escape
from typing import Any, Callable

import config
import inventory_import_service
from inventory_parser import (
    InventoryParseError,
    describe_batch,
    parse_batch,
    resolve_mode,
)
from ui_v52 import button, render_error, render_page

MAX_BATCH_ITEMS = 100
MAX_ITEM_BYTES = 8192

_MODE_FA = {
    "auto": "خودکار (بر اساس جداکننده)",
    "line": "هر خط یک قلم",
    "block": "هر بلوک بین --- یا ### یک قلم چندخطی",
}


def _value(obj: Any, key: str, default=None):
    return obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)


def _positive(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be positive")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be positive") from exc
    if result <= 0:
        raise ValueError(f"{label} must be positive")
    return result


def _root(ctx: Any) -> bool:
    try:
        actor = _positive(_value(ctx, "actor_tg_id", _value(ctx, "telegram_id")), "actor_tg_id")
        root = _positive(getattr(config, "ADMIN_TELEGRAM_ID", 0) or 0, "ADMIN_TELEGRAM_ID")
        return actor == root
    except (TypeError, ValueError):
        return False


def _private(ctx: Any) -> bool:
    return str(_value(ctx, "chat_type", "private")).lower() == "private"


def _actor(ctx: Any) -> int:
    return _positive(_value(ctx, "actor_tg_id", _value(ctx, "telegram_id")), "actor_tg_id")


def _bot(ctx: Any) -> int:
    return _positive(_value(ctx, "bot_id"), "bot_id")


def _db():
    import database
    return database.get_db()


def _default_stager(ctx: Any, *, kind: str, target_id: int, items: list[str],
                    meta: dict[str, Any], now: int | None) -> dict[str, Any]:
    """Persist a previewed batch in the real staging table.

    Injected as a callable so the UI layer stays unit-testable without a database,
    exactly like the existing ``repository=`` and ``import_fn=`` seams.
    """
    with _db() as conn:
        staged = inventory_import_service.stage(
            conn,
            actor_tg_id=_actor(ctx),
            bot_id=_bot(ctx),
            kind=kind,
            target_id=target_id,
            items=items,
            meta=meta,
            now=now,
        )
        conn.commit()
        return staged


def _batch(values: Any, label: str) -> list[str]:
    if isinstance(values, str):
        items = [line.strip() for line in values.splitlines() if line.strip()]
    elif isinstance(values, Sequence) and not isinstance(values, (bytes, bytearray)):
        items = list(values)
    else:
        raise ValueError(f"{label} must be a sequence or newline-delimited text")
    if not items or len(items) > MAX_BATCH_ITEMS:
        raise ValueError(f"{label} must contain 1..{MAX_BATCH_ITEMS} items")
    out = []
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{label} contains an empty/non-text item")
        if len(item.encode("utf-8")) > MAX_ITEM_BYTES:
            raise ValueError(f"{label} item exceeds the size limit")
        out.append(item.strip())
    return out


def _staged_target(conn, token: str) -> int:
    """Read a staged row's own target so the compare-and-swap can match it exactly.

    The token is unguessable and already bound to actor, bot and kind, so reading
    its own target cannot widen anyone's access. It exists because the product or
    panel id does not fit in Telegram's 64-byte callback data alongside the token.
    """
    inventory_import_service.ensure_staging_table(conn)
    row = conn.execute(
        "SELECT target_id FROM inventory_import_staging WHERE token=?", (token,)
    ).fetchone()
    if row is None:
        raise ValueError("پیش‌نمایش ورود موجودی یافت نشد یا منقضی شده است.")
    return int(row[0])


# --------------------------------------------------------------------------- #
# Manual panel pool
# --------------------------------------------------------------------------- #
def begin_manual_import(
    ctx: Any,
    panel_id: int,
    external_identifier: str,
    config_uris: Sequence[str] | str,
    *,
    quota_mode: str = "unknown",
    quota_bytes: int | None = None,
    used_bytes: int | None = None,
    time_mode: str = "unknown",
    expires_at: int | None = None,
    confirm: bool = False,
    repository=None,
    encryptor: Callable[[str, str], str] | None = None,
    stager: Callable[..., dict[str, Any]] | None = None,
    now: int | None = None,
    parent_callback: str | None = None,
):
    """Validate/mask a batch first; only root DM confirmation invokes the repository."""
    try:
        if not _root(ctx):
            raise PermissionError("manual pool import is root-only")
        if not _private(ctx):
            raise PermissionError("manual pool import and URI entry are private-DM only")
        pid = _positive(panel_id, "panel_id")
        identifier = str(external_identifier or "").strip()
        if not identifier or len(identifier) > 120:
            raise ValueError("external_identifier is invalid")
        items = _batch(config_uris, "config_uris")
        from panel_adapters.manual_pool import validate_manual_configs
        validated = validate_manual_configs(items)
        if quota_mode not in {"finite", "unlimited", "unknown"}:
            raise ValueError("quota_mode is invalid")
        if time_mode not in {"dated", "unlimited", "on_hold", "unknown"}:
            raise ValueError("time_mode is invalid")
        if quota_mode == "finite":
            if isinstance(quota_bytes, bool) or not isinstance(quota_bytes, int) or quota_bytes <= 0:
                raise ValueError("finite quota requires a positive byte count")
        elif quota_bytes is not None:
            raise ValueError("quota_bytes must be empty unless quota_mode=finite")
        if used_bytes is not None and (isinstance(used_bytes, bool) or not isinstance(used_bytes, int) or used_bytes < 0):
            raise ValueError("used_bytes must be a non-negative integer")
        if time_mode == "dated":
            if isinstance(expires_at, bool) or not isinstance(expires_at, int) or expires_at <= 0:
                raise ValueError("dated expiry requires a positive UTC epoch")
        elif expires_at is not None:
            raise ValueError("expires_at must be empty unless time_mode=dated")
        masked_schemes = sorted({item.split(":", 1)[0].lower() for item in validated})
        if not confirm:
            staged = (stager or _default_stager)(
                ctx,
                kind="manual_pool",
                target_id=pid,
                items=list(validated),
                meta={
                    "external_identifier": identifier,
                    "quota_mode": quota_mode,
                    "quota_bytes": quota_bytes,
                    "used_bytes": used_bytes,
                    "time_mode": time_mode,
                    "expires_at": expires_at,
                },
                now=now,
            )
            return render_page(
                "پیش‌نمایش ورود Pool دستی",
                f"پنل: {pid}\nشناسه خارجی: {identifier}\nتعداد URI معتبر: {len(validated)}\n"
                f"پروتکل‌ها: {', '.join(masked_schemes)}\n\n"
                "مقدار URI، credential یا متن کانفیگ نمایش داده نمی‌شود. "
                "هنوز هیچ رکوردی درج نشده است.",
                persona="admin", page_kind="preview", parent_callback=parent_callback,
                rows=[[button("✅ تأیید ورود batch", staged["callback_confirm"]),
                       button("✖️ لغو", staged["callback_cancel"])]],
            )
        if repository is None:
            raise ValueError("ManualPoolRepository injection is required for confirmed import")
        if encryptor is None:
            from crypto_store import encrypt_secret as encryptor
        cipher = encryptor(json.dumps(list(validated), ensure_ascii=False, separators=(",", ":")),
                           "hstagent/columns/v1")
        result = repository.import_client(
            panel_id=pid, external_identifier=identifier, config_cipher=cipher,
            config_uris=list(validated), actor_is_root=True, quota_mode=quota_mode,
            quota_bytes=quota_bytes, used_bytes=used_bytes, time_mode=time_mode,
            expires_at=expires_at, created_at=now,
        )
        return render_page(
            "ورود Pool ثبت شد",
            f"شناسه رکورد: {result.get('id', 'ثبت‌شده')}\nپنل: {pid}\nتعداد کانفیگ‌ها: {len(validated)}\n"
            "مقادیر حساس ذخیره رمزنگاری شده‌اند و در این صفحه نشان داده نمی‌شوند.",
            persona="admin", page_kind="result", parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError, InventoryParseError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def confirm_manual_import(
    ctx: Any,
    token: str,
    *,
    repository=None,
    encryptor: Callable[[str, str], str] | None = None,
    now: int | None = None,
    parent_callback: str | None = None,
):
    """Consume a staged manual-pool batch and perform the real insert."""
    try:
        if not _root(ctx):
            raise PermissionError("manual pool import is root-only")
        if not _private(ctx):
            raise PermissionError("manual pool import is private-DM only")
        with _db() as conn:
            staged = inventory_import_service.consume(
                conn, token=token, actor_tg_id=_actor(ctx), bot_id=_bot(ctx),
                kind="manual_pool", target_id=_staged_target(conn, token), now=now,
            )
            conn.commit()
        meta = staged["meta"]
        return begin_manual_import(
            ctx,
            int(staged["target_id"]),
            str(meta.get("external_identifier") or ""),
            staged["items"],
            quota_mode=str(meta.get("quota_mode") or "unknown"),
            quota_bytes=meta.get("quota_bytes"),
            used_bytes=meta.get("used_bytes"),
            time_mode=str(meta.get("time_mode") or "unknown"),
            expires_at=meta.get("expires_at"),
            confirm=True,
            repository=repository,
            encryptor=encryptor,
            now=now,
            parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError, InventoryParseError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def cancel_manual_import(ctx: Any, token: str, *, now: int | None = None,
                         parent_callback: str | None = None):
    """Drop a staged manual-pool batch without inserting anything."""
    try:
        if not _root(ctx):
            raise PermissionError("manual pool import is root-only")
        with _db() as conn:
            inventory_import_service.cancel(
                conn, token=token, actor_tg_id=_actor(ctx), bot_id=_bot(ctx), now=now)
            conn.commit()
        return render_page(
            "ورود Pool لغو شد",
            "هیچ رکوردی درج نشد و پیش‌نمایش ذخیره‌شده پاک شد.",
            persona="admin", page_kind="result",
            parent_callback=parent_callback or "v52:panels",
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


# --------------------------------------------------------------------------- #
# Digital inventory
# --------------------------------------------------------------------------- #
def begin_digital_import(
    ctx: Any,
    product_id: int,
    items: Sequence[str] | str,
    *,
    notes: str = "",
    mode: str = "auto",
    confirm: bool = False,
    import_fn: Callable[..., Any] | None = None,
    stager: Callable[..., dict[str, Any]] | None = None,
    now: int | None = None,
    parent_callback: str | None = None,
):
    """Preview a bounded digital-inventory batch; confirmed inserts go through goods_service.

    ``mode`` selects how the pasted text is split: ``auto`` uses the ``---`` /
    ``###`` block separators when they are present and otherwise one item per
    line, ``line`` forces one item per line, and ``block`` forces every block
    between separators to be a single multi-line item.
    """
    try:
        if not _root(ctx):
            raise PermissionError("digital inventory import is root-only")
        if not _private(ctx):
            raise PermissionError("digital inventory secrets must be entered in private DM")
        pid = _positive(product_id, "product_id")
        if isinstance(items, str):
            effective_mode = resolve_mode(items, mode)
            values = parse_batch(items, mode=mode)
        else:
            effective_mode = "line"
            values = _batch(items, "items")
        if len(str(notes).encode("utf-8")) > 1000:
            raise ValueError("notes exceeds the size limit")
        if not confirm:
            staged = (stager or _default_stager)(
                ctx,
                kind="digital_inventory",
                target_id=pid,
                items=values,
                meta={"notes": str(notes or ""), "mode": effective_mode},
                now=now,
            )
            return render_page(
                "پیش‌نمایش ورود موجودی دیجیتال",
                f"محصول: {pid}\n{describe_batch(values)}\n"
                f"حالت تقسیم‌بندی: {_MODE_FA.get(effective_mode, effective_mode)}\n\n"
                "هیچ insert انجام نشده است. مقادیر محرمانه در این پیش‌نمایش و لاگ‌ها "
                "نشان داده نمی‌شوند.",
                persona="admin", page_kind="preview", parent_callback=parent_callback,
                rows=[[button("✅ تأیید ورود موجودی", staged["callback_confirm"]),
                       button("✖️ لغو", staged["callback_cancel"])]],
            )
        if import_fn is None:
            from goods_service import import_inventory as import_fn
        inserted = import_fn(pid, values, notes=str(notes or ""), now=now)
        if not isinstance(inserted, Sequence) or len(inserted) != len(values):
            raise ValueError("inventory import did not return one receipt per input item")
        return render_page(
            "ورود موجودی انجام شد",
            f"محصول: {pid}\nتعداد رکوردهای رمزنگاری‌شده: {len(inserted)}\n"
            "فروش فقط پس از commit سرویس inventory امکان‌پذیر است.",
            persona="admin", page_kind="result", parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError, InventoryParseError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def confirm_digital_import(
    ctx: Any,
    token: str,
    *,
    import_fn: Callable[..., Any] | None = None,
    now: int | None = None,
    parent_callback: str | None = None,
):
    """Consume a staged digital batch and insert it for real.

    This is the step that was missing: the confirmation callback used to be wired
    to the *entry* handler, so the parsed batch was dropped and nothing was ever
    inserted.
    """
    try:
        if not _root(ctx):
            raise PermissionError("digital inventory import is root-only")
        if not _private(ctx):
            raise PermissionError("digital inventory secrets must be entered in private DM")
        with _db() as conn:
            staged = inventory_import_service.consume(
                conn, token=token, actor_tg_id=_actor(ctx), bot_id=_bot(ctx),
                kind="digital_inventory", target_id=_staged_target(conn, token), now=now,
            )
            conn.commit()
        meta = staged["meta"]
        return begin_digital_import(
            ctx,
            int(staged["target_id"]),
            staged["items"],
            notes=str(meta.get("notes") or ""),
            mode=str(meta.get("mode") or "line"),
            confirm=True,
            import_fn=import_fn,
            now=now,
            parent_callback=parent_callback,
        )
    except (TypeError, ValueError, PermissionError, InventoryParseError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def cancel_digital_import(ctx: Any, token: str, *, now: int | None = None,
                          parent_callback: str | None = None):
    """Drop a staged digital batch without inserting anything."""
    try:
        if not _root(ctx):
            raise PermissionError("digital inventory import is root-only")
        with _db() as conn:
            inventory_import_service.cancel(
                conn, token=token, actor_tg_id=_actor(ctx), bot_id=_bot(ctx), now=now)
            conn.commit()
        return render_page(
            "ورود موجودی لغو شد",
            "هیچ رکوردی درج نشد و پیش‌نمایش ذخیره‌شده پاک شد.",
            persona="admin", page_kind="result",
            parent_callback=parent_callback or "v52:shop",
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


# ---------------------------------------------------------------------------
# Structured item wizard (S60)
# ---------------------------------------------------------------------------

STRUCTURED_ITEM_WIZARD_KEY = "inv_structured_item"

#: The labels the assembled card uses. They are the same ones
#: :func:`inventory_parser.looks_like_credential_card` recognises, so a card built
#: here and a card pasted by hand are read back identically.
_CARD_LABELS = (
    ("username", "نام کاربری"),
    ("password", "رمز عبور"),
    ("secret", "کلید"),
    ("guide", "توضیحات"),
)


def _wz_optional(raw) -> str:
    """Keep an optional answer as typed and trimmed; an empty answer stays empty."""
    return str(raw or "").strip()


def _structured_card(values: Mapping[str, Any]) -> str:
    """Assemble one multi-line credential card from the wizard's answers.

    An empty answer is dropped rather than written as an empty label, so a card with
    only a key is a one-line card and not four lines of noise.
    """
    return "\n".join(
        f"{label}: {str(values.get(key) or '').strip()}"
        for key, label in _CARD_LABELS
        if str(values.get(key) or "").strip()
    )


def _preview_structured_item(values) -> str:
    """The confirmation card: the exact multi-line item that will be stored."""
    card = _structured_card(values)
    if not card:
        return "⚠️ هیچ فیلدی پر نشده است؛ برای ساخت آیتم حداقل یک مقدار لازم است."
    return ("کارت ساخته‌شده (یک قلم کامل):\n"
            f"<pre>{_html_escape(card)}</pre>\n\n"
            "این کارت با تمام خطوطش به‌عنوان یک آیتم ذخیره می‌شود.")


def _commit_structured_item(values, actor):
    product_id = int(values.get("product_id") or 0)
    card = _structured_card(values)
    if not card:
        raise ValueError("هیچ فیلدی پر نشده است؛ برای ساخت آیتم حداقل یک مقدار لازم است.")
    from goods_service import import_inventory
    inserted = import_inventory(product_id, [card])
    if not inserted:
        raise ValueError("درج آیتم در انبار انجام نشد.")
    return (f"آیتم ساختاریافته در انبار محصول {product_id} ثبت شد.\n"
            f"تعداد رکورد رمزنگاری‌شده: {len(inserted)}")


def begin_structured_item(context, query, product_id: int, *,
                          parent_callback: str | None = None):
    """Open the structured-item wizard on the live conversation store.

    The legacy text draft is cleared on purpose: the wizard supersedes it, and
    leaving both armed would let the FSM text route swallow the answer the wizard is
    waiting for.
    """
    import ui_v52
    import v52_router
    pid = _positive(product_id, "product_id")
    message = getattr(query, "message", None)
    chat = getattr(message, "chat", None)
    state = ui_v52.start_wizard(
        context.user_data, STRUCTURED_ITEM_WIZARD_KEY,
        return_callback=parent_callback or f"v52:shop:product:{pid}",
        seed={"product_id": pid},
        message_id=getattr(message, "message_id", None),
        chat_id=getattr(chat, "id", None),
    )
    try:
        v52_router.DEFAULT_FSM.clear(context)
    except Exception:
        pass
    return state


def _register_inventory_wizards() -> None:
    """Publish the structured-item flow; called once at import."""
    import ui_v52
    ui_v52.register_flow(ui_v52.WizardFlow(
        STRUCTURED_ITEM_WIZARD_KEY, "آیتم ساختاریافته انبار",
        intro=("هر گام را می‌توانید با «⏭ رد کردن» نادیده بگیرید. کارت نهایی با تمام "
               "خطوطش یک قلم کامل در انبار ذخیره می‌شود."),
        steps=(
            ui_v52.WizardStep("username", "نام کاربری / ایمیل",
                              hint="اختیاری؛ اگر ندارد رد کنید.",
                              parse=_wz_optional, skippable=True),
            ui_v52.WizardStep("password", "رمز عبور",
                              hint="اختیاری؛ اگر ندارد رد کنید.",
                              parse=_wz_optional, skippable=True),
            ui_v52.WizardStep("secret", "کلید / لینک / کد",
                              hint="اختیاری؛ اگر ندارد رد کنید.",
                              parse=_wz_optional, skippable=True),
            ui_v52.WizardStep("guide", "توضیحات راه‌اندازی",
                              hint="اختیاری؛ اگر ندارد رد کنید.",
                              parse=_wz_optional, skippable=True),
        ),
        preview=_preview_structured_item,
        commit=_commit_structured_item,
    ))


_register_inventory_wizards()


__all__ = [
    "STRUCTURED_ITEM_WIZARD_KEY",
    "begin_digital_import",
    "begin_manual_import",
    "begin_structured_item",
    "cancel_digital_import",
    "cancel_manual_import",
    "confirm_digital_import",
    "confirm_manual_import",
]
