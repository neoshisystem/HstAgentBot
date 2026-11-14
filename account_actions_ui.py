"""Network-free account rotation and root adjustment pages for v5.2."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import access_service
import config
from ui_v52 import button, render_error, render_page


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, Mapping) else getattr(value, key, default)


def _positive(value, label):
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if result <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return result


def _root(ctx) -> bool:
    try:
        root_id = int(getattr(config, "ADMIN_TELEGRAM_ID", 0) or 0)
        actor = _positive(_get(ctx, "actor_tg_id", _get(ctx, "telegram_id")), "actor_tg_id")
        return root_id > 0 and actor == root_id
    except (TypeError, ValueError):
        return False


def _persona(ctx):
    return "admin" if _root(ctx) else ("rep" if str(_get(ctx, "persona", "")).lower() in {"rep", "representative"} else "user")


def _authorized(ctx, account_id, action):
    if _root(ctx):
        return True
    try:
        return bool(access_service.authorize_account(ctx, account_id, action))
    except (TypeError, ValueError, PermissionError):
        return False


def preview_rotation(
    ctx,
    account_id: int,
    rotation_type: str = "sub",
    *,
    confirmation: Mapping | None = None,
    disconnect_evidence: Mapping | None = None,
    parent_callback: str | None = None,
    persona: str | None = None,
):
    """Preview subscription-link or connection-credential rotation, never dispatch it."""
    try:
        aid = _positive(account_id, "account_id")
        kind = str(rotation_type or "").strip().lower()
        if kind not in {"sub", "cred"}:
            raise ValueError("rotation_type must be sub or cred")
        if not _authorized(ctx, aid, "rotate"):
            raise PermissionError("actor cannot rotate this account")
        if kind == "sub":
            title = "پیش‌نمایش چرخش لینک اشتراک"
            body = (
                f"حساب: {aid}\nنوع عملیات: rotate_subscription_url\n"
                "هشدار: لینک قبلی برای دریافت‌های آینده به‌روزرسانی نخواهد شد؛ "
                "کانفیگی که قبلاً دانلود شده است همچنان باقی می‌ماند.\n"
                "این صفحه هیچ درخواست پنلی ارسال نمی‌کند."
            )
        else:
            title = "پیش‌نمایش چرخش اعتبارنامه"
            body = (
                f"حساب: {aid}\nنوع عملیات: rotate_connection_credentials\n"
                "هشدار: اعتبارنامهٔ اتصال تغییر می‌کند و ممکن است نیاز به دریافت دوبارهٔ کانفیگ باشد.\n"
            )
            verified_ev03 = (
                isinstance(disconnect_evidence, Mapping)
                and disconnect_evidence.get("evidence_id") == "EV-03"
                and disconnect_evidence.get("status") == "verified"
            )
            if verified_ev03:
                body += "شاهد EV-03 قطع نشست‌های قبلی را تأیید کرده است.\n"
            else:
                body += "وضعیت قطع اتصال همهٔ دستگاه‌ها/مشارکت‌کنندگان اثبات نشده است؛ قطع قطعی ادعا نمی‌شود.\n"
            body += "این صفحه هیچ درخواست پنلی ارسال نمی‌کند."
        rows = []
        if isinstance(confirmation, Mapping):
            ok = str(confirmation.get("callback_ok") or "")
            no = str(confirmation.get("callback_no") or "")
            if ok:
                rows.append([button("تأیید و اجرای عملیات", ok)])
            if no:
                rows.append([button("لغو", no)])
        else:
            rows.append([button("بازگشت", f"v52:account:{aid}")])
        return render_page(title, body, persona=persona or _persona(ctx),
                           page_kind="preview", parent_callback=parent_callback, rows=rows)
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona=persona or _persona(ctx), parent_callback=parent_callback)


def begin_traffic(
    ctx,
    account_id: int,
    mode: str | None = None,
    value: int | float | None = None,
    *,
    unit: str = "GiB",
    unlimited: bool = False,
    parent_callback: str | None = None,
):
    """Root-only traffic form with explicit reset, finite-byte and unlimited modes."""
    try:
        aid = _positive(account_id, "account_id")
        if not _root(ctx):
            raise PermissionError("تغییر حجم و بازنشانی مصرف فقط در اختیار root است.")
        if not isinstance(unlimited, bool):
            raise ValueError("unlimited must be boolean")
        selected = str(mode or "").strip().lower()
        if not selected:
            return render_page(
                "تنظیم ترافیک حساب",
                f"حساب: {aid}\nیکی از دو عملیات را انتخاب کنید: reset_usage یا set_quota_bytes.\n"
                "حجم محدود با واحد GiB/MiB/bytes دریافت می‌شود؛ نامحدود باید صریح انتخاب شود.",
                persona="admin", page_kind="form", parent_callback=parent_callback,
                rows=[[button("بازنشانی مصرف", f"v52:traffic:{aid}:reset_usage")],
                      [button("تنظیم سقف حجم", f"v52:traffic:{aid}:set_quota_bytes")]],
            )
        if selected == "reset_usage":
            if value is not None or unlimited:
                raise ValueError("reset_usage does not accept quota value or unlimited flag")
            detail = "مصرف فعلی بازنشانی می‌شود؛ سقف حجم و زمان انقضا باید حفظ شوند."
        elif selected == "set_quota_bytes":
            if unlimited:
                if value is not None:
                    raise ValueError("unlimited must not be combined with a finite value")
                detail = "سقف حجم: نامحدود (explicit unlimited)"
            else:
                if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                    raise ValueError("حجم محدود باید مثبت باشد؛ صفر به معنی نامحدود نیست.")
                units = {"B": 1, "bytes": 1, "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3}
                if unit not in units:
                    raise ValueError("unit must be B, KiB, MiB or GiB")
                raw = float(value) * units[unit]
                if not raw.is_integer():
                    raise ValueError("quota must resolve to an integer byte count")
                detail = f"سقف محدود: {int(raw)} bytes ({value} {unit})"
        else:
            raise ValueError("mode must be reset_usage or set_quota_bytes")
        return render_page(
            "پیش‌نمایش تنظیم ترافیک",
            f"حساب: {aid}\nحالت: {selected}\n{detail}\nتغییر فقط پس از تأیید و اجرای سرویس پنل انجام می‌شود.",
            persona="admin", page_kind="preview", parent_callback=parent_callback,
            rows=[[button("بازگشت", f"v52:account:{aid}")]],
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def begin_expiry(
    ctx,
    account_id: int,
    mode: str | None = None,
    value=None,
    *,
    timezone_name: str = "Asia/Tehran",
    unlimited: bool = False,
    parent_callback: str | None = None,
):
    """Root-only expiry form; Tehran wall time is converted to UTC epoch seconds."""
    try:
        aid = _positive(account_id, "account_id")
        if not _root(ctx):
            raise PermissionError("تغییر زمان انقضا فقط در اختیار root است.")
        selected = str(mode or "").strip().lower()
        if not selected:
            return render_page(
                "تنظیم زمان حساب",
                f"حساب: {aid}\nیکی از دو حالت set_expiry_utc یا set_remaining_days را انتخاب کنید.\n"
                "تاریخ/زمان محلی تهران به UTC تبدیل می‌شود؛ نامحدود گزینه‌ای صریح است.",
                persona="admin", page_kind="form", parent_callback=parent_callback,
                rows=[[button("تاریخ و زمان", f"v52:expiry:{aid}:set_expiry_utc")],
                      [button("تعداد روز باقی‌مانده", f"v52:expiry:{aid}:set_remaining_days")]],
            )
        if selected == "set_remaining_days":
            if unlimited or isinstance(value, bool):
                raise ValueError("set_remaining_days requires a positive finite integer")
            try:
                days = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("remaining days must be a positive integer") from exc
            if days <= 0 or str(value).strip() not in {str(days), f"{days}.0"}:
                raise ValueError("remaining days must be a positive integer")
            detail = f"زمان باقی‌مانده: {days} روز"
        elif selected == "set_expiry_utc":
            if unlimited:
                if value is not None:
                    raise ValueError("unlimited expiry cannot include a date")
                detail = "زمان انقضا: نامحدود (explicit)"
            else:
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("expiry datetime is required")
                parsed = datetime.fromisoformat(value.strip())
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))
                epoch = int(parsed.astimezone(timezone.utc).timestamp())
                detail = f"زمان محلی: {parsed.isoformat()}\nUTC epoch seconds: {epoch}"
        else:
            raise ValueError("mode must be set_expiry_utc or set_remaining_days")
        return render_page(
            "پیش‌نمایش زمان انقضا",
            f"حساب: {aid}\nحالت: {selected}\n{detail}\nتغییر فقط پس از تأیید و readback پنل معتبر است.",
            persona="admin", page_kind="preview", parent_callback=parent_callback,
            rows=[[button("بازگشت", f"v52:account:{aid}")]],
        )
    except (TypeError, ValueError, PermissionError, KeyError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


__all__ = ["preview_rotation", "begin_traffic", "begin_expiry"]
