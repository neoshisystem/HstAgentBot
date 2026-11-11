"""Transport-neutral v5.2 access-grant and account-link pages.

The module deliberately performs no Telegram/network I/O. Mutations remain in
access_service/account_link_service and must be called by the confirmed router.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence

import access_service
import account_link_service
from ui_v52 import button, render_error, render_page


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


def _authorized(ctx, account_id: int) -> bool:
    try:
        return bool(access_service.authorize_account(ctx, account_id, "grant"))
    except (TypeError, ValueError, PermissionError):
        return False


def _persona(ctx, fallback: str) -> str:
    if isinstance(ctx, Mapping):
        rep_id = ctx.get("rep_id")
    else:
        rep_id = getattr(ctx, "rep_id", None)
    return "rep" if rep_id is not None else fallback


def _grant_line(item: Mapping) -> str:
    grantee = _positive(item.get("grantee_tg_id"), "grantee_tg_id")
    level = str(item.get("access_level") or "").strip().lower()
    if level not in {"viewer", "manager"}:
        raise ValueError("grant access_level must be viewer or manager")
    meaning = "مشاهدهٔ حساب و آمار" if level == "viewer" else "مشاهده، تمدید و چرخش مجاز"
    return f"کاربر {grantee} — {level}: {meaning}"


def _load_grants(account_id: int):
    import database
    with database.get_db() as conn:
        account = conn.execute(
            "SELECT owner_tg_id FROM accounts WHERE id=? AND deleted_at IS NULL",
            (account_id,),
        ).fetchone()
        if account is None:
            raise ValueError("account not found")
        rows = conn.execute(
            "SELECT grantee_tg_id,access_level,granted_by_tg_id,created_at,revision "
            "FROM account_access_grants WHERE account_id=? AND revoked_at IS NULL "
            "ORDER BY grantee_tg_id",
            (account_id,),
        ).fetchall()
    return (None if account["owner_tg_id"] is None else int(account["owner_tg_id"]),
            [dict(row) for row in rows])


def show_grants(
    ctx,
    account_id: int,
    grants: Sequence[Mapping] | None = None,
    *,
    owner_tg_id: int | None = None,
    persona: str = "admin",
    parent_callback: str | None = None,
):
    """List owner separately from active viewer/manager grants; never expose secrets."""
    try:
        exact_id = _positive(account_id, "account_id")
    except ValueError as exc:
        return render_error(str(exc), persona=persona, parent_callback=parent_callback)
    if not _authorized(ctx, exact_id):
        return render_error("فقط مالک حساب یا فروشندهٔ مجاز می‌تواند دسترسی‌ها را مدیریت کند.",
                            persona=_persona(ctx, persona), parent_callback=parent_callback)
    try:
        if grants is None or owner_tg_id is None:
            loaded_owner, loaded_grants = _load_grants(exact_id)
            if owner_tg_id is None:
                owner_tg_id = loaded_owner
            if grants is None:
                grants = loaded_grants
        safe_grants = []
        for item in grants or ():
            if not isinstance(item, Mapping):
                continue
            if item.get("revoked_at") is not None:
                continue
            safe_grants.append(item)
        lines = [f"شناسهٔ حساب: {exact_id}",
                 f"مالک اصلی: {owner_tg_id if owner_tg_id is not None else 'ثبت نشده'}",
                 "مالک اصلی با grant اشتباه گرفته نمی‌شود."]
        rows = [
            [button("افزودن دسترسی مشاهده", f"v52:gset:{exact_id}:viewer"),
             button("افزودن دسترسی مدیر", f"v52:gset:{exact_id}:manager")]
        ]
        for item in safe_grants:
            lines.append(_grant_line(item))
            grantee = _positive(item.get("grantee_tg_id"), "grantee_tg_id")
            rows.append([button(f"پیش‌نمایش لغو {grantee}", f"v52:grevoke:{exact_id}:{grantee}")])
        if not safe_grants:
            lines.append("هیچ grant فعالی ثبت نشده است.")
        return render_page("مدیریت دسترسی حساب", "\n".join(lines),
                           persona=_persona(ctx, persona), page_kind="access",
                           parent_callback=parent_callback, rows=rows)
    except (ValueError, TypeError) as exc:
        return render_error(str(exc), persona=_persona(ctx, persona),
                            parent_callback=parent_callback)


def begin_grant(
    ctx,
    account_id: int,
    grantee_tg_id: int | None = None,
    access_level: str | None = None,
    *,
    persona: str = "admin",
    parent_callback: str | None = None,
):
    """Start a grant draft; this page never writes a grant."""
    try:
        exact_id = _positive(account_id, "account_id")
        if not _authorized(ctx, exact_id):
            raise PermissionError("actor cannot grant access to this account")
        selected_level = None if access_level is None else str(access_level).strip().lower()
        if selected_level is not None and selected_level not in {"viewer", "manager"}:
            raise ValueError("access_level must be viewer or manager")
        selected_grantee = None if grantee_tg_id is None else _positive(grantee_tg_id, "grantee_tg_id")
        if selected_grantee is None:
            rows = []
            if selected_level is None:
                rows = [[button("viewer", f"v52:gset:{exact_id}:viewer"),
                         button("manager", f"v52:gset:{exact_id}:manager")]]
            text = (f"حساب {exact_id}\nشناسهٔ عددی و مثبت تلگرامِ دریافت‌کننده را ارسال کنید."
                    + (f" سطح انتخاب‌شده: {selected_level}." if selected_level else " ابتدا سطح viewer یا manager را انتخاب کنید."))
            return render_page("شروع اعطای دسترسی", text, persona=_persona(ctx, persona),
                               page_kind="form", parent_callback=parent_callback, rows=rows)
        if selected_level is None:
            return render_page("انتخاب سطح دسترسی",
                               f"کاربر {selected_grantee} برای حساب {exact_id}. سطح viewer یا manager را انتخاب کنید.",
                               persona=_persona(ctx, persona), page_kind="form",
                               parent_callback=parent_callback,
                               rows=[[button("viewer", f"v52:gset:{exact_id}:viewer"),
                                      button("manager", f"v52:gset:{exact_id}:manager")]])
        return preview_grant(ctx, exact_id, selected_grantee, selected_level,
                             persona=persona, parent_callback=parent_callback)
    except (ValueError, TypeError, PermissionError) as exc:
        return render_error(str(exc), persona=_persona(ctx, persona),
                            parent_callback=parent_callback)


def preview_grant(
    ctx, account_id: int, grantee_tg_id: int, access_level: str,
    *, persona: str = "admin", parent_callback: str | None = None,
):
    """Render a non-secret grant preview; the caller still requires confirmation."""
    try:
        aid = _positive(account_id, "account_id")
        grantee = _positive(grantee_tg_id, "grantee_tg_id")
        level = str(access_level).strip().lower()
        if level not in {"viewer", "manager"}:
            raise ValueError("access_level must be viewer or manager")
        if not _authorized(ctx, aid):
            raise PermissionError("actor cannot grant access to this account")
        if isinstance(ctx, Mapping):
            actor_id = ctx.get("actor_tg_id", ctx.get("telegram_id"))
        else:
            actor_id = getattr(ctx, "actor_tg_id", getattr(ctx, "telegram_id", None))
        if actor_id is not None and _positive(actor_id, "actor_tg_id") == grantee:
            raise ValueError("self-grants are not supported")
        meaning = "فقط مشاهده" if level == "viewer" else "مشاهده، تمدید و چرخش مجاز"
        return render_page(
            "پیش‌نمایش اعطای دسترسی",
            f"حساب: {aid}\nمالک اصلی تغییر نمی‌کند.\nدریافت‌کننده: {grantee}\nسطح: {level} — {meaning}\n"
            "مانده، مالکیت و اطلاعات محرمانه تغییر نمی‌کنند. ثبت grant فقط پس از تأیید نهایی انجام می‌شود.",
            persona=_persona(ctx, persona), page_kind="preview",
            parent_callback=parent_callback,
            rows=[[button("بازگشت به دسترسی‌ها", f"v52:grants:{aid}")]],
        )
    except (ValueError, TypeError, PermissionError) as exc:
        return render_error(str(exc), persona=_persona(ctx, persona),
                            parent_callback=parent_callback)


def preview_revoke(
    ctx,
    account_id: int,
    grantee_tg_id: int,
    *,
    grant: Mapping | None = None,
    persona: str = "admin",
    parent_callback: str | None = None,
):
    """Preview revocation of one active grant; no write occurs on page rendering."""
    try:
        aid = _positive(account_id, "account_id")
        grantee = _positive(grantee_tg_id, "grantee_tg_id")
        if not _authorized(ctx, aid):
            raise PermissionError("actor cannot revoke access to this account")
        selected = grant
        if selected is None:
            import database
            with database.get_db() as conn:
                row = conn.execute(
                    "SELECT grantee_tg_id,access_level,revoked_at FROM account_access_grants "
                    "WHERE account_id=? AND grantee_tg_id=?",
                    (aid, grantee),
                ).fetchone()
            selected = None if row is None else dict(row)
        if not isinstance(selected, Mapping) or selected.get("revoked_at") is not None:
            raise ValueError("active grant not found")
        if _positive(selected.get("grantee_tg_id"), "grantee_tg_id") != grantee:
            raise ValueError("grant target does not match")
        level = str(selected.get("access_level") or "").lower()
        if level not in {"viewer", "manager"}:
            raise ValueError("grant access_level is invalid")
        return render_page(
            "پیش‌نمایش لغو دسترسی",
            f"حساب: {aid}\nدریافت‌کننده: {grantee}\nسطح فعلی: {level}\n"
            "لغو فقط دسترسی همین دریافت‌کننده را می‌بندد؛ مالک اصلی و سایر grantها دست‌نخورده می‌مانند.\n"
            "این صفحه هنوز هیچ تغییری در پایگاه داده ایجاد نکرده است.",
            persona=_persona(ctx, persona), page_kind="preview",
            parent_callback=parent_callback,
            rows=[[button("تأیید لغو", f"v52:grevoke:{aid}:{grantee}")]],
        )
    except (ValueError, TypeError, PermissionError) as exc:
        return render_error(str(exc), persona=_persona(ctx, persona),
                            parent_callback=parent_callback)


def show_link_candidates(
    ctx, identifier: str, *, persona: str = "user", parent_callback: str | None = None,
):
    """Display only candidates returned by the authorization-filtered link service."""
    try:
        result = account_link_service.resolve_link_candidates(ctx, identifier)
        candidates = result.get("candidates", []) if isinstance(result, Mapping) else []
        status = result.get("status") if isinstance(result, Mapping) else "not_found"
        if status == "not_found" or not candidates:
            return render_error("حساب مجاز و منطبق پیدا نشد.", persona=persona,
                                parent_callback=parent_callback)
        rows = []
        lines = []
        for candidate in candidates:
            account_id = _positive(candidate.get("account_id"), "account_id")
            code = str(candidate.get("account_code") or "—")
            email = str(candidate.get("email") or "—")
            lines.append(f"حساب {account_id}: {code} — {email}")
            rows.append([button(f"انتخاب حساب {account_id}", f"v52:link:{account_id}")])
        heading = "چند نتیجهٔ مجاز پیدا شد؛ انتخاب صریح لازم است." if status == "ambiguous" else "یک حساب مجاز پیدا شد."
        return render_page("نتایج اتصال حساب", heading + "\n" + "\n".join(lines),
                           persona=persona, page_kind="selection",
                           parent_callback=parent_callback, rows=rows)
    except (ValueError, TypeError) as exc:
        return render_error(str(exc), persona=persona, parent_callback=parent_callback)


__all__ = ["show_grants", "begin_grant", "preview_grant", "preview_revoke",
           "show_link_candidates"]
