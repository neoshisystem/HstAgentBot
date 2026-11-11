"""Root-only private import wizards for manual panel pools and digital inventory."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, Callable

import config
from ui_v52 import button, render_error, render_page

MAX_BATCH_ITEMS = 100
MAX_ITEM_BYTES = 8192


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
            return render_page(
                "پیش‌نمایش ورود Pool دستی",
                f"پنل: {pid}\nشناسه خارجی: {identifier}\nتعداد URI معتبر: {len(validated)}\n"
                f"پروتکل‌ها: {', '.join(masked_schemes)}\n"
                "مقدار URI، credential یا متن کانفیگ نمایش داده نمی‌شود. هنوز هیچ رکوردی درج نشده است.",
                persona="admin", page_kind="preview", parent_callback=parent_callback,
                rows=[[button("تأیید ورود batch", f"v52:poolimport:{pid}")]],
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
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def begin_digital_import(
    ctx: Any,
    product_id: int,
    items: Sequence[str] | str,
    *,
    notes: str = "",
    confirm: bool = False,
    import_fn: Callable[..., Any] | None = None,
    now: int | None = None,
    parent_callback: str | None = None,
):
    """Preview a bounded digital-inventory batch; confirmed inserts go through goods_service."""
    try:
        if not _root(ctx):
            raise PermissionError("digital inventory import is root-only")
        if not _private(ctx):
            raise PermissionError("digital inventory secrets must be entered in private DM")
        pid = _positive(product_id, "product_id")
        values = _batch(items, "items")
        if len(str(notes).encode("utf-8")) > 1000:
            raise ValueError("notes exceeds the size limit")
        if not confirm:
            return render_page(
                "پیش‌نمایش ورود موجودی دیجیتال",
                f"محصول: {pid}\nتعداد اقلام: {len(values)}\n"
                "هیچ insert انجام نشده است. مقادیر محرمانه در این پیش‌نمایش و لاگ‌ها نشان داده نمی‌شوند.",
                persona="admin", page_kind="preview", parent_callback=parent_callback,
                rows=[[button("تأیید ورود موجودی", f"v52:inventory:{pid}")]],
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
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


__all__ = ["begin_manual_import", "begin_digital_import"]