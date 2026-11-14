"""Transport-neutral v5.2 custom-domain registration and proof pages."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import nullcontext

import config
import database
import domain_service
from ui_v52 import button, render_error, render_page


def _get(value, key, default=None):
    if isinstance(value, Mapping):
        return value.get(key, default)
    return getattr(value, key, default)


def _positive(value, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if result <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return result


def _is_root(ctx) -> bool:
    actor = _get(ctx, "actor_tg_id", _get(ctx, "telegram_id"))
    try:
        root_id = int(getattr(config, "ADMIN_TELEGRAM_ID", 0) or 0)
        return root_id > 0 and _positive(actor, "actor_tg_id") == root_id
    except (TypeError, ValueError):
        return False


def _authorized_rep(ctx, rep_id: int) -> bool:
    if _is_root(ctx):
        return True
    actor_rep_id = _get(ctx, "rep_id")
    try:
        return _positive(actor_rep_id, "rep_id") == rep_id
    except (TypeError, ValueError):
        return False


def begin_register(
    ctx,
    rep_id: int,
    hostname: str | None = None,
    *,
    now: int | None = None,
    conn=None,
    persona: str = "rep",
    parent_callback: str | None = None,
):
    """Begin a pending-domain draft, or register the supplied hostname as pending."""
    try:
        exact_rep = _positive(rep_id, "rep_id")
        if not _authorized_rep(ctx, exact_rep):
            raise PermissionError("only the representative owner or root may register this domain")
        if hostname is None:
            return render_page(
                "ثبت دامنهٔ اختصاصی",
                f"نماینده: {exact_rep}\nنام میزبان را بدون scheme، مسیر، port یا credential ارسال کنید. "
                "ثبت اولیه فقط وضعیت pending می‌سازد و به‌معنی تأیید DNS/TLS نیست.",
                persona="admin" if _is_root(ctx) else persona,
                page_kind="form", parent_callback=parent_callback,
                rows=[[button("راهنمای اثبات مالکیت", f"v52:domain:{exact_rep}")]],
            )
        actor_tg_id = _positive(_get(ctx, "actor_tg_id", _get(ctx, "telegram_id")), "actor_tg_id")
        if conn is not None:
            result = domain_service.register_domain(conn, actor_tg_id, exact_rep, hostname, now=now)
        else:
            with database.get_db() as db_conn:
                result = domain_service.register_domain(db_conn, actor_tg_id, exact_rep, hostname, now=now)
        return show_verification(result, persona="admin" if _is_root(ctx) else persona,
                                 parent_callback=parent_callback)
    except (TypeError, ValueError, PermissionError, domain_service.DomainError) as exc:
        return render_error(str(exc), persona="admin" if _is_root(ctx) else persona,
                            parent_callback=parent_callback)


def show_verification(
    domain,
    *,
    gateway_host: str | None = None,
    persona: str = "rep",
    parent_callback: str | None = None,
):
    """Render DNS proof instructions and preserve pending as pending."""
    try:
        domain_id = _positive(_get(domain, "domain_id", _get(domain, "id")), "domain_id")
        hostname = domain_service.normalize_hostname(str(_get(domain, "hostname", "")))
        challenge = str(_get(domain, "challenge", "") or "")
        status = str(_get(domain, "status", "pending") or "pending").lower()
        if status not in {"pending", "verified", "active", "disabled"}:
            raise ValueError("unknown domain state")
        if not challenge:
            raise ValueError("domain challenge is missing")
        records = [
            f"دامنه: {hostname}",
            f"وضعیت فعلی: {status}",
            f"TXT name: _hst-sub.{hostname}",
            f"TXT value: {challenge}",
        ]
        if gateway_host:
            records.append(f"CNAME target: {domain_service.normalize_hostname(gateway_host)}")
        else:
            records.append("CNAME target: میزبان درگاه مصوب پروژه (از پیکربندی معتبر)")
        if status == "pending":
            records.append("تا تطبیق TXT، مقصد DNS و TLS، دامنه فعال نیست.")
        elif status == "verified":
            records.append("اثبات DNS/TLS ثبت شده است؛ فعال‌سازی هنوز نیازمند تأیید جداگانه است.")
        elif status == "active":
            records.append("دامنه فعال است.")
        else:
            records.append("دامنه غیرفعال است.")
        return render_page(
            "راهنمای تأیید دامنه",
            "\n".join(records),
            persona=persona, page_kind="verification", parent_callback=parent_callback,
            rows=[[button("بازبینی وضعیت", f"v52:domainverify:{domain_id}")]],
        )
    except (TypeError, ValueError, domain_service.DomainError) as exc:
        return render_error(str(exc), persona=persona, parent_callback=parent_callback)


def preview_activation(
    ctx,
    domain,
    *,
    now: int,
    tls_max_age: int = 86400,
    persona: str = "rep",
    parent_callback: str | None = None,
):
    """Show activation confirmation only for a recently verified domain."""
    try:
        domain_id = _positive(_get(domain, "domain_id", _get(domain, "id")), "domain_id")
        rep_id = _positive(_get(domain, "rep_id"), "rep_id")
        if not _authorized_rep(ctx, rep_id):
            raise PermissionError("only the domain owner or root may activate this domain")
        status = str(_get(domain, "status", "") or "").lower()
        hostname = domain_service.normalize_hostname(str(_get(domain, "hostname", "")))
        if status == "active":
            return render_page("دامنه فعال است", f"{hostname} قبلاً فعال شده است.",
                               persona="admin" if _is_root(ctx) else persona,
                               page_kind="result", parent_callback=parent_callback)
        if status != "verified":
            return render_error(
                f"دامنه هنوز verified نیست (وضعیت: {status or 'نامشخص'}). فعال‌سازی مجاز نیست.",
                persona="admin" if _is_root(ctx) else persona, parent_callback=parent_callback,
            )
        checked_at = _get(domain, "tls_checked_at")
        if checked_at is None or isinstance(checked_at, bool):
            raise ValueError("TLS proof timestamp is missing")
        checked_at = int(checked_at)
        if isinstance(now, bool) or not isinstance(now, int) or now < checked_at:
            raise ValueError("now must be a valid epoch-seconds integer")
        if now - checked_at > int(tls_max_age):
            return render_error("اثبات TLS منقضی شده است؛ پیش از فعال‌سازی دوباره بررسی کنید.",
                                persona="admin" if _is_root(ctx) else persona,
                                parent_callback=parent_callback)
        return render_page(
            "پیش‌نمایش فعال‌سازی دامنه",
            f"دامنه: {hostname}\nوضعیت: verified\nاثبات TLS تازه است.\n"
            "با تأیید نهایی، وضعیت به active تغییر می‌کند. لینک canonical فقط در پیام خصوصی تحویل می‌شود.",
            persona="admin" if _is_root(ctx) else persona,
            page_kind="preview", parent_callback=parent_callback,
            rows=[[button("تأیید فعال‌سازی", f"v52:domainactivate:{domain_id}")]],
        )
    except (TypeError, ValueError, PermissionError, domain_service.DomainError) as exc:
        return render_error(str(exc), persona="admin" if _is_root(ctx) else persona,
                            parent_callback=parent_callback)


__all__ = ["begin_register", "show_verification", "preview_activation"]
