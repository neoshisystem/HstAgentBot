"""Transport-neutral v5.2 custom-domain registration and proof pages."""
from __future__ import annotations

from collections.abc import Mapping
from contextlib import contextmanager, nullcontext

import config
import database
import domain_service
from ui_v52 import button, render_error, render_page


@contextmanager
def _domain_db():
    """Open a connection with the v5.2 domain columns ensured before any read."""
    with database.get_db() as conn:
        domain_service.ensure_domain_columns(conn)
        yield conn


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


# --- v5.2 domain-management surface (list, detail, wizard, lifecycle) ---

_ROLE_LABELS = {
    "subscription": "دامنه لینک اشتراک (Subscription Gateway)",
    "node": "دامنه اتصال نود/پنل",
}
_STATUS_LABELS = {
    "pending": "در انتظار تأیید",
    "verified": "تأییدشده",
    "active": "فعال",
    "drain": "تخلیه (Drain)",
    "disabled": "غیرفعال",
}
_LIVE_STATUSES = ("active", "drain", "disabled")


def _actor_tg_id(ctx) -> int:
    return _positive(_get(ctx, "actor_tg_id", _get(ctx, "telegram_id")), "actor_tg_id")


def _actor_persona(ctx) -> str:
    if _is_root(ctx):
        return "admin"
    return "rep" if str(_get(ctx, "persona", "")).lower() in {"rep", "representative"} else "user"


def _load_domain(conn, domain_id: int):
    return conn.execute(
        "SELECT d.*, r.name AS rep_name, p.name AS panel_name "
        "FROM sub_domains d "
        "LEFT JOIN representatives r ON r.id=d.rep_id "
        "LEFT JOIN panels p ON p.id=d.panel_id "
        "WHERE d.domain_id=?",
        (int(domain_id),),
    ).fetchone()


def _domain_line(row, *, with_rep: bool) -> str:
    role = _ROLE_LABELS.get(str(row["role"]), str(row["role"]))
    panel = row["panel_name"] or (f"#{int(row['panel_id'])}" if row["panel_id"] else "—")
    primary = "⭐ اصلی" if row["is_primary"] else "—"
    status = _STATUS_LABELS.get(str(row["status"]), str(row["status"]))
    head = str(row["hostname"])
    if with_rep:
        head += f" · نماینده {int(row['rep_id'])} ({row['rep_name'] or '—'})"
    return f"• {head}\n   نقش: {role} | پنل: {panel} | {primary} | وضعیت: {status}"


def _domain_button_label(row) -> str:
    host = str(row["hostname"])
    if len(host) > 40:
        host = host[:37] + "…"
    status = _STATUS_LABELS.get(str(row["status"]), str(row["status"]))
    return f"{host} · {status}"


def domains_page(ctx, scope: str = "admin"):
    """List every registered domain for the actor with a register entry point."""
    persona = _actor_persona(ctx)
    selected_scope = str(scope or "admin").strip().lower()
    if selected_scope == "admin" and not _is_root(ctx):
        selected_scope = "rep"
    try:
        with _domain_db() as conn:
            if selected_scope == "admin":
                rows = conn.execute(
                    "SELECT d.*, r.name AS rep_name, p.name AS panel_name "
                    "FROM sub_domains d "
                    "LEFT JOIN representatives r ON r.id=d.rep_id "
                    "LEFT JOIN panels p ON p.id=d.panel_id "
                    "ORDER BY d.is_primary DESC, d.rep_id, d.domain_id"
                ).fetchall()
            else:
                rep_id = _positive(_get(ctx, "rep_id"), "rep_id")
                rows = conn.execute(
                    "SELECT d.*, r.name AS rep_name, p.name AS panel_name "
                    "FROM sub_domains d "
                    "LEFT JOIN representatives r ON r.id=d.rep_id "
                    "LEFT JOIN panels p ON p.id=d.panel_id "
                    "WHERE d.rep_id=? "
                    "ORDER BY d.is_primary DESC, d.domain_id",
                    (rep_id,),
                ).fetchall()
        if not rows:
            body = ("هیچ دامنهٔ ثبت‌شده‌ای وجود ندارد. برای شروع، «➕ ثبت دامنه جدید» را بزنید.\n"
                    "دامنه‌ها به هویت نمایندهٔ مالک متصل می‌شوند و فقط پس از اثبات DNS/TLS فعال می‌شوند.")
        else:
            body = f"تعداد دامنه‌های ثبت‌شده: {len(rows)}\n\n" + "\n".join(
                _domain_line(row, with_rep=(selected_scope == "admin")) for row in rows
            )
        keyboard = [[button(_domain_button_label(row), f"v52:domaindetail:{int(row['domain_id'])}")]
                    for row in rows]
        keyboard.append([button("➕ ثبت دامنه جدید", "v52:domainnew")])
        return render_page("🌐 مدیریت دامنه‌ها", body, persona=persona, rows=keyboard,
                           parent_callback="v52:home:" + persona)
    except (TypeError, ValueError, PermissionError, domain_service.DomainError) as exc:
        return render_error(str(exc), persona=persona, parent_callback="v52:home:" + persona)


def domain_detail_page(domain_id, *, ctx=None, persona: str = "admin",
                       parent_callback: str | None = None):
    """Show one domain with its DNS/SSL, lifecycle and delete controls."""
    try:
        exact_id = _positive(domain_id, "domain_id")
        with _domain_db() as conn:
            row = _load_domain(conn, exact_id)
        if row is None:
            return render_error("دامنهٔ موردنظر ثبت نشده است.", persona=persona,
                                parent_callback=parent_callback or "v52:domains")
        if ctx is not None and not _authorized_rep(ctx, int(row["rep_id"])):
            raise PermissionError("only the domain owner or root may view this domain")
        role = _ROLE_LABELS.get(str(row["role"]), str(row["role"]))
        panel = row["panel_name"] or (f"#{int(row['panel_id'])}" if row["panel_id"] else "—")
        primary = "بله" if row["is_primary"] else "خیر"
        status = _STATUS_LABELS.get(str(row["status"]), str(row["status"]))
        body = (
            f"دامنه: {row['hostname']}\n"
            f"نمایندهٔ مالک: {int(row['rep_id'])} ({row['rep_name'] or '—'})\n"
            f"نقش: {role}\n"
            f"پنل متصل: {panel}\n"
            f"دامنهٔ اصلی: {primary}\n"
            f"وضعیت چرخه عمر: {status}\n"
            f"TXT name: _hst-sub.{row['hostname']}\n"
            f"TXT value: {row['challenge']}"
        )
        keyboard = [
            [button("🔍 بررسی وضعیت DNS و SSL دامنه", f"v52:domainverify:{exact_id}")],
            [button("🔄 تغییر وضعیت دامنه (Active / Drain / Disabled)", f"v52:domainlife:{exact_id}")],
            [button("🗑 حذف دامنه", f"v52:domaindel:{exact_id}")],
        ]
        return render_page("جزئیات دامنه", body, persona=persona, rows=keyboard,
                           parent_callback=parent_callback or "v52:domains")
    except (TypeError, ValueError, PermissionError, domain_service.DomainError) as exc:
        return render_error(str(exc), persona=persona, parent_callback=parent_callback or "v52:domains")


def rep_picker_page(ctx, *, persona: str = "admin", parent_callback: str | None = None):
    """Root-only step: choose the representative that will own the new domain."""
    try:
        reps = database.get_all_active_reps()
        if not reps:
            return render_error("هیچ نمایندهٔ فعالی برای ثبت دامنه وجود ندارد.", persona=persona,
                                parent_callback=parent_callback or "v52:domains")
        rows = [[button(f"نماینده {int(item['id'])} · {item.get('name') or ''}",
                        f"v52:domainnew:{int(item['id'])}")] for item in reps]
        return render_page(
            "ثبت دامنه جدید — انتخاب نماینده",
            "دامنه به هویت نمایندهٔ مالک متصل می‌شود؛ نماینده را انتخاب کنید.",
            persona=persona, page_kind="form", rows=rows,
            parent_callback=parent_callback or "v52:domains",
        )
    except (TypeError, ValueError, PermissionError, domain_service.DomainError) as exc:
        return render_error(str(exc), persona=persona, parent_callback=parent_callback or "v52:domains")


def register_hostname_prompt(ctx, rep_id, *, persona: str = "admin",
                             parent_callback: str | None = None):
    """Step 1 of the register wizard: collect the bare hostname as text."""
    try:
        exact_rep = _positive(rep_id, "rep_id")
        if not _authorized_rep(ctx, exact_rep):
            raise PermissionError("only the representative owner or root may register this domain")
        body = (
            f"نمایندهٔ مالک: {exact_rep}\n"
            "نام میزبان را بدون scheme، مسیر، port یا credential ارسال کنید.\n"
            "ثبت اولیه فقط وضعیت pending می‌سازد و به‌معنی تأیید DNS/TLS نیست."
        )
        return render_page(
            "ثبت دامنه — گام ۱ از ۴ (نام میزبان)", body,
            persona=persona, page_kind="form",
            rows=[[button("انصراف", parent_callback or "v52:domains")]],
            parent_callback=parent_callback or "v52:domains",
        )
    except (TypeError, ValueError, PermissionError, domain_service.DomainError) as exc:
        return render_error(str(exc), persona=persona, parent_callback=parent_callback or "v52:domains")


def register_role_step(ctx, *, persona: str = "admin", parent_callback: str | None = None):
    """Step 2 of the register wizard: choose the domain role."""
    body = "گام ۲ از ۴: نقش این دامنه را انتخاب کنید."
    rows = [
        [button("دامنه لینک اشتراک (Subscription Gateway)", "v52:domainrole:subscription")],
        [button("دامنه اتصال نود/پنل", "v52:domainrole:node")],
        [button("انصراف", parent_callback or "v52:domains")],
    ]
    return render_page("ثبت دامنه — نقش", body, persona=persona, page_kind="form", rows=rows,
                       parent_callback=parent_callback or "v52:domains")


def register_panel_step(ctx, *, persona: str = "admin", parent_callback: str | None = None):
    """Step 3 of the register wizard: attach an active panel (or none)."""
    try:
        with _domain_db() as conn:
            panels = conn.execute(
                "SELECT id,name,panel_type,lifecycle FROM panels "
                "WHERE lifecycle='active' ORDER BY is_default DESC,id"
            ).fetchall()
        rows = [[button(f"پنل {int(p['id'])} · {p['name']} · {p['panel_type']}",
                        f"v52:domainpanel:{int(p['id'])}")] for p in panels]
        rows.append([button("بدون پنل (فقط لینک)", "v52:domainpanel:none")])
        rows.append([button("انصراف", parent_callback or "v52:domains")])
        body = ("گام ۳ از ۴: پنل متصل به این دامنه را انتخاب کنید.\n"
                f"تعداد پنل‌های فعال: {len(panels)}")
        return render_page("ثبت دامنه — پنل متصل", body, persona=persona, page_kind="form",
                           rows=rows, parent_callback=parent_callback or "v52:domains")
    except (TypeError, ValueError, PermissionError, domain_service.DomainError) as exc:
        return render_error(str(exc), persona=persona, parent_callback=parent_callback or "v52:domains")


def register_primary_step(ctx, *, persona: str = "admin", parent_callback: str | None = None):
    """Step 4 of the register wizard: mark the domain as primary."""
    body = "گام ۴ از ۴: آیا این دامنه، دامنهٔ اصلی این نماینده باشد؟"
    rows = [
        [button("بله، دامنهٔ اصلی", "v52:domainprimary:1")],
        [button("خیر", "v52:domainprimary:0")],
        [button("انصراف", parent_callback or "v52:domains")],
    ]
    return render_page("ثبت دامنه — دامنهٔ اصلی", body, persona=persona, page_kind="form",
                       rows=rows, parent_callback=parent_callback or "v52:domains")


def preview_domain_lifecycle(ctx, domain_id, *, state: str | None = None,
                             persona: str = "admin", parent_callback: str | None = None):
    """Preview a lifecycle change; only the confirm button mutates the row."""
    try:
        exact_id = _positive(domain_id, "domain_id")
        with _domain_db() as conn:
            row = _load_domain(conn, exact_id)
        if row is None:
            return render_error("دامنهٔ موردنظر ثبت نشده است.", persona=persona,
                                parent_callback=parent_callback or "v52:domains")
        if not _authorized_rep(ctx, int(row["rep_id"])):
            raise PermissionError("only the domain owner or root may change this domain")
        current = str(row["status"])
        if current not in _LIVE_STATUSES:
            return render_error(
                "این دامنه هنوز فعال نشده است؛ ابتدا DNS/TLS را بررسی و دامنه را فعال کنید.",
                persona=persona, parent_callback=parent_callback or f"v52:domaindetail:{exact_id}")
        selected = str(state or "").strip().lower() if state is not None else None
        if selected is not None and selected not in _LIVE_STATUSES:
            raise ValueError("وضعیت انتخابی نامعتبر است.")
        lines = [f"دامنه: {row['hostname']}",
                 f"وضعیت فعلی: {_STATUS_LABELS.get(current, current)}"]
        if selected is None:
            lines.append("وضعیت جدید را انتخاب کنید؛ هیچ تغییری تا زدن «تأیید» اعمال نمی‌شود.")
            rows = [[button(_STATUS_LABELS[s], f"v52:domainlife:{exact_id}:{s}")]
                    for s in _LIVE_STATUSES if s != current]
        else:
            lines.append(f"وضعیت جدید: {_STATUS_LABELS.get(selected, selected)}")
            lines.append("با تأیید، وضعیت این دامنه تغییر می‌کند.")
            rows = [[button("تأیید تغییر وضعیت", f"v52:domainlifeok:{exact_id}:{selected}")],
                    [button("انصراف", f"v52:domaindetail:{exact_id}")]]
        return render_page("تغییر وضعیت دامنه", "\n".join(lines), persona=persona,
                           page_kind="preview", rows=rows,
                           parent_callback=parent_callback or f"v52:domaindetail:{exact_id}")
    except (TypeError, ValueError, PermissionError, domain_service.DomainError) as exc:
        return render_error(str(exc), persona=persona, parent_callback=parent_callback or "v52:domains")


def preview_domain_delete(ctx, domain_id, *, persona: str = "admin",
                          parent_callback: str | None = None):
    """First step of the two-step delete; only the confirm button removes the row."""
    try:
        exact_id = _positive(domain_id, "domain_id")
        with _domain_db() as conn:
            row = _load_domain(conn, exact_id)
        if row is None:
            return render_error("دامنهٔ موردنظر ثبت نشده است.", persona=persona,
                                parent_callback=parent_callback or "v52:domains")
        if not _authorized_rep(ctx, int(row["rep_id"])):
            raise PermissionError("only the domain owner or root may delete this domain")
        body = (
            f"دامنه: {row['hostname']}\n"
            f"وضعیت: {_STATUS_LABELS.get(str(row['status']), row['status'])}\n"
            "با تأیید، این دامنه برای همیشه حذف می‌شود و قابل بازگشت نیست."
        )
        rows = [[button("تأیید حذف نهایی", f"v52:domaindelok:{exact_id}")],
                [button("انصراف", f"v52:domaindetail:{exact_id}")]]
        return render_page("حذف دامنه", body, persona=persona, page_kind="preview", rows=rows,
                           parent_callback=parent_callback or f"v52:domaindetail:{exact_id}")
    except (TypeError, ValueError, PermissionError, domain_service.DomainError) as exc:
        return render_error(str(exc), persona=persona, parent_callback=parent_callback or "v52:domains")


__all__ = ["begin_register", "show_verification", "preview_activation", "domains_page",
           "domain_detail_page", "rep_picker_page", "register_hostname_prompt",
           "register_role_step", "register_panel_step", "register_primary_step",
           "preview_domain_lifecycle", "preview_domain_delete"]
