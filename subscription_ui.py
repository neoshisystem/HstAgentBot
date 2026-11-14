"""DM-safe Telegram UI orchestration for subscription profiles."""
from __future__ import annotations

from dataclasses import dataclass
from io import BytesIO
from html import escape
from typing import Any, Mapping

import config
import database
import domain_service
import qrcode
from crypto_store import decrypt_secret
from confirmation_service import create_confirmation
import access_service
import subscription_service

from ui_v52 import button, render_error, render_page, render_uri


class ProfileUIError(RuntimeError):
    pass


class ProfileUIAccessDenied(ProfileUIError, PermissionError):
    pass


@dataclass(frozen=True)
class ProfileListView:
    text: str
    profiles: tuple[dict[str, Any], ...]
    reply_markup: Any
    visible_other_names: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfileView:
    profile_id: int
    text: str
    reply_markup: Any
    profile: Mapping[str, Any]
    member_count: int
    link: str | None = None


@dataclass(frozen=True)
class ProfilePreview:
    profile_id: int
    action: str
    nonce: str
    callback_ok: str
    callback_no: str
    text: str
    reply_markup: Any
    revision: int


def _actor_id(ctx: Any) -> int:
    value = ctx.get("actor_tg_id", ctx.get("telegram_id")) if isinstance(ctx, Mapping) else getattr(ctx, "actor_tg_id", getattr(ctx, "telegram_id", None))
    if isinstance(value, bool):
        raise ProfileUIAccessDenied("invalid actor")
    try:
        actor = int(value)
    except (TypeError, ValueError):
        raise ProfileUIAccessDenied("invalid actor") from None
    if actor <= 0:
        raise ProfileUIAccessDenied("invalid actor")
    return actor


def _root_id() -> int:
    try:
        value = int(getattr(config, "ADMIN_TELEGRAM_ID", 0) or 0)
    except (TypeError, ValueError):
        value = 0
    return value if value > 0 else 0


def _is_root(actor_id: int) -> bool:
    root = _root_id()
    return root > 0 and actor_id == root


def _profile_row(conn, profile_id: int):
    row = conn.execute(
        "SELECT * FROM subscription_profiles WHERE profile_id=? AND deleted_at IS NULL",
        (int(profile_id),),
    ).fetchone()
    if row is None:
        raise subscription_service.ProfileNotFoundError("profile is unavailable")
    return row


def _require_profile_owner(conn, actor_id: int, profile_id: int,
                           target_owner_tg_id: int | None = None):
    row = _profile_row(conn, profile_id)
    owner_id = int(row["owner_tg_id"])
    if _is_root(actor_id):
        if owner_id != actor_id and target_owner_tg_id != owner_id:
            raise ProfileUIAccessDenied("root must specify the target owner explicitly")
    elif owner_id != actor_id:
        raise ProfileUIAccessDenied("actor does not own this profile")
    if target_owner_tg_id is not None and int(target_owner_tg_id) != owner_id:
        raise ProfileUIAccessDenied("target owner does not match profile owner")
    return row


def _private(ctx: Any, chat_type: str | None) -> bool:
    selected = chat_type
    if selected is None:
        selected = ctx.get("chat_type") if isinstance(ctx, Mapping) else getattr(ctx, "chat_type", None)
    return str(selected or "private").lower() == "private"


def _canonical_host(canonical_host: str | None = None) -> str:
    raw = canonical_host if canonical_host is not None else getattr(config, "SUB_DOMAIN", "")
    return str(raw or "").strip().lower().rstrip(".")


def _profile_host(conn, profile, canonical_host: str | None = None) -> str | None:
    canonical = _canonical_host(canonical_host)
    if not canonical:
        return None
    try:
        if bool(profile["is_default"]):
            return canonical if domain_service.resolve_profile_host(conn, profile, canonical, canonical) else None
        brand_id = profile["brand_rep_id"]
        if brand_id is not None:
            row = conn.execute(
                "SELECT hostname FROM sub_domains WHERE rep_id=? AND status='active' "
                "ORDER BY verified_at DESC,domain_id DESC LIMIT 1",
                (int(brand_id),),
            ).fetchone()
            if row is not None:
                host = str(row["hostname"])
                if domain_service.resolve_profile_host(conn, profile, host, canonical):
                    return host
        return canonical if domain_service.resolve_profile_host(conn, profile, canonical, canonical) else None
    except Exception:
        return None


def _profile_link(conn, profile, canonical_host: str | None = None) -> str | None:
    if not bool(profile["enabled"]):
        return None
    host = _profile_host(conn, profile, canonical_host)
    if not host:
        return None
    try:
        token = decrypt_secret(str(profile["token_cipher"]), "hstagent/columns/v1")
    except Exception:
        return None
    if not token or any(ch in token for ch in "/?#"):
        return None
    return f"https://{host}/s/{token}"


def _persona(ctx: Any, actor: int) -> str:
    if _is_root(actor):
        return "admin"
    value = ctx.get("persona") if isinstance(ctx, Mapping) else getattr(ctx, "persona", None)
    return "rep" if str(value or "").lower() in {"rep", "representative"} else "user"


def _authorize_account(ctx: Any, account_id: int) -> None:
    if not access_service.authorize_account(ctx, int(account_id), "view_config"):
        raise ProfileUIAccessDenied("actor cannot view this account")


def list_profiles(
    ctx: Any,
    *,
    target_owner_tg_id: int | None = None,
    account_id: int | None = None,
    chat_type: str | None = None,
    parent_callback: str | None = None,
) -> ProfileListView:
    """List an owner's profiles; root cross-owner views require an explicit target."""
    actor = _actor_id(ctx)
    root = _is_root(actor)
    if target_owner_tg_id is not None:
        owner = int(target_owner_tg_id)
        if not root and owner != actor:
            raise ProfileUIAccessDenied("only root may select another profile owner")
    else:
        owner = actor
    if owner <= 0:
        raise ValueError("target owner must be positive")
    if account_id is not None:
        _authorize_account(ctx, int(account_id))
    with database.get_db() as conn:
        subscription_service.ensure_default_profile(conn, owner)
        rows = conn.execute(
            "SELECT p.*,COUNT(m.account_id) AS member_count,"
            "MAX(CASE WHEN m.account_id=? THEN 1 ELSE 0 END) AS contains_account "
            "FROM subscription_profiles p LEFT JOIN subscription_profile_accounts m "
            "ON m.profile_id=p.profile_id WHERE p.owner_tg_id=? AND p.deleted_at IS NULL "
            "GROUP BY p.profile_id ORDER BY p.is_default DESC,p.created_at,p.profile_id",
            (int(account_id or 0), owner),
        ).fetchall()
        profiles = tuple({
            "profile_id": int(row["profile_id"]), "owner_tg_id": int(row["owner_tg_id"]),
            "name": str(row["name"]), "is_default": bool(row["is_default"]),
            "enabled": bool(row["enabled"]), "member_count": int(row["member_count"]),
            "contains_account": bool(row["contains_account"]),
        } for row in rows)
        names = ()
        if account_id is not None:
            # The lookup is scoped to the selected owner's visible profiles only.
            names = tuple(str(row["name"]) for row in conn.execute(
                "SELECT p.name FROM subscription_profile_accounts m "
                "JOIN subscription_profiles p ON p.profile_id=m.profile_id "
                "WHERE m.account_id=? AND p.owner_tg_id=? AND p.deleted_at IS NULL "
                "ORDER BY p.is_default DESC,p.created_at,p.profile_id",
                (int(account_id), owner),
            ).fetchall())
        rows_markup = [
            [button(
                ("پیش‌فرض · " + item["name"].removeprefix("⭐ ")) if item["is_default"] else item["name"],
                f"v52:profile:{item['profile_id']}",
            )]
            for item in profiles
        ]
        rows_markup.append([button(
            "پروفایل جدید",
            f"v52:pnew:{int(account_id or 0)}",
        )])
        if account_id is not None:
            rows_markup.append([button("پروفایل‌های این حساب", f"v52:sub:{int(account_id)}")])
        persona = _persona(ctx, actor)
        legacy_parent = {"admin": "admin:main", "rep": "rep:main", "user": "user:main"}[persona]
        page = render_page(
            "پروفایل‌های اشتراک", f"تعداد پروفایل‌ها: {len(profiles)}",
            persona=persona, parent_callback=parent_callback or legacy_parent,
            rows=rows_markup,
        )
        return ProfileListView(page.text, profiles, page.reply_markup, names)


def show_profile(
    ctx: Any,
    profile_id: int,
    *,
    target_owner_tg_id: int | None = None,
    chat_type: str | None = None,
    canonical_host: str | None = None,
    candidate_account_id: int | None = None,
) -> ProfileView:
    """Show a profile card without leaking its token outside its owner's DM."""
    actor = _actor_id(ctx)
    with database.get_db() as conn:
        row = _require_profile_owner(conn, actor, int(profile_id), target_owner_tg_id)
        count = int(conn.execute(
            "SELECT COUNT(*) FROM subscription_profile_accounts WHERE profile_id=?",
            (int(profile_id),),
        ).fetchone()[0])
        if not _private(ctx, chat_type):
            page = render_page(
                "مدیریت پروفایل", "اطلاعات و عملیات پروفایل فقط در گفتگوی خصوصی نمایش داده می‌شوند.",
                persona=_persona(ctx, actor), parent_callback="v52:profiles",
            )
            return ProfileView(int(profile_id), page.text, page.reply_markup,
                               dict(row), count, None)
        link = _profile_link(conn, row, canonical_host)
        member_ids = [int(item["account_id"]) for item in conn.execute(
            "SELECT account_id FROM subscription_profile_accounts "
            "WHERE profile_id=? ORDER BY sort_order,account_id",
            (int(profile_id),),
        ).fetchall()]
        markup_rows = []
        if candidate_account_id is not None:
            candidate = int(candidate_account_id)
            if candidate > 0 and candidate not in member_ids:
                try:
                    can_view_candidate = access_service.authorize_account(ctx, candidate, "view_config")
                except Exception:
                    can_view_candidate = False
                if can_view_candidate:
                    markup_rows.append([button(
                        f"افزودن حساب {candidate}",
                        f"v52:padd:{int(profile_id)}:{candidate}",
                    )])
        for member_id in member_ids:
            markup_rows.append([button(
                f"پیش‌نمایش حذف حساب {member_id}",
                f"v52:premove:{int(profile_id)}:{member_id}",
            )])
        if _private(ctx, chat_type) and link:
            markup_rows.append([button("نمایش لینک", f"v52:plink:{int(profile_id)}"),
                                button("QR لینک", f"v52:pqr:{int(profile_id)}")])
        markup_rows.append([button("چرخش توکن", f"v52:prot:{int(profile_id)}"),
                            button("تغییر نام", f"v52:prename:{int(profile_id)}")])
        markup_rows.append([button(
            "توقف" if bool(row["enabled"]) else "فعال‌سازی",
            f"v52:ppause:{int(profile_id)}:{0 if bool(row['enabled']) else 1}",
        ), button("پاک‌سازی اعضا", f"v52:pclear:{int(profile_id)}")])
        if not bool(row["is_default"]):
            markup_rows.append([button("حذف پروفایل", f"v52:pdel:{int(profile_id)}")])
        link_line = "\n" + render_uri(link) if link else ""
        body = (
            f"وضعیت: {'فعال' if bool(row['enabled']) else 'متوقف'}\n"
            f"تعداد حساب‌های عضو: {count}"
        )
        body_html = body + (("\n" + render_uri(link)) if link else "")
        page = render_page(
            str(row["name"]), body, body_html=body_html, persona=_persona(ctx, actor),
            parent_callback="v52:profiles", rows=markup_rows,
        )
        public_profile = {
            key: row[key] for key in row.keys()
            if key not in {"token_hash", "token_cipher"}
        }
        return ProfileView(int(profile_id), page.text, page.reply_markup, public_profile, count, link)


def open_account_profiles(
    ctx: Any,
    account_id: int,
    *,
    target_owner_tg_id: int | None = None,
    chat_type: str = "private",
) -> ProfileListView:
    """Open the account's membership view using account_id, not its mutable email."""
    return list_profiles(
        ctx, target_owner_tg_id=target_owner_tg_id, account_id=int(account_id),
        chat_type=chat_type, parent_callback=f"v52:account:{int(account_id)}",
    )


def begin_create(
    ctx: Any,
    account_id: int = 0,
    *,
    name: str | None = None,
    target_owner_tg_id: int | None = None,
    now: int | None = None,
):
    """Start the name form or atomically create a profile and its first member."""
    actor = _actor_id(ctx)
    if isinstance(account_id, bool) or int(account_id) < 0:
        raise ValueError("account_id must be zero or positive")
    selected_account = int(account_id)
    if name is None:
        page = render_page(
            "پروفایل جدید", "نام پروفایل را به‌صورت متن ارسال کنید.",
            persona=_persona(ctx, actor), parent_callback="v52:profiles",
        )
        return {"status": "awaiting_name", "account_id": selected_account,
                "callback_data": f"v52:pnew:{selected_account}",
                "text": page.text, "reply_markup": page.reply_markup}
    owner = actor if target_owner_tg_id is None else int(target_owner_tg_id)
    if owner != actor and not _is_root(actor):
        raise ProfileUIAccessDenied("only root may create a profile for another owner")
    try:
        with database.get_db() as conn:
            profile = subscription_service.create_profile(
                conn, actor, name, owner_tg_id=owner,
                account_id=selected_account or None, now=now,
            )
        return profile
    except subscription_service.ProfileQuotaError:
        page = render_error(
            "سقف ساخت پروفایل تکمیل شده است؛ هیچ پروفایل خالی ایجاد نشد.",
            persona=_persona(ctx, actor), parent_callback="v52:profiles",
        )
        return {"status": "quota_exceeded", "account_id": selected_account,
                "text": page.text, "reply_markup": page.reply_markup}
    except ValueError as exc:
        page = render_error(
            str(exc), persona=_persona(ctx, actor), parent_callback="v52:profiles",
        )
        return {"status": "invalid_name", "account_id": selected_account,
                "text": page.text, "reply_markup": page.reply_markup}


def add_member(ctx: Any, profile_id: int, account_id: int, *, now: int | None = None):
    """Idempotent one-click membership addition; source account is never mutated."""
    actor = _actor_id(ctx)
    with database.get_db() as conn:
        result = subscription_service.add_profile_member(
            conn, actor, int(profile_id), int(account_id), now=now,
        )
    return result


def _make_preview(
    conn, actor: int, profile, action: str, now: int,
    *, target_type: str = "subscription_profile", target_id: str | int | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return create_confirmation(
        conn, actor_tg_id=actor, bot_id=int(profile["created_bot_id"]),
        action=action, target_type=target_type,
        target_id=int(profile["profile_id"]) if target_id is None else target_id,
        expected_revision=int(profile["revision"]),
        payload=payload or {"profile_id": int(profile["profile_id"]),
                            "revision": int(profile["revision"])},
        now=int(now),
    )


def preview_remove(
    ctx: Any,
    profile_id: int,
    account_id: int,
    *,
    now: int,
    target_owner_tg_id: int | None = None,
) -> ProfilePreview:
    """Create a revision-bound, one-use remove preview; no membership is changed."""
    actor = _actor_id(ctx)
    with database.get_db() as conn:
        profile = _require_profile_owner(conn, actor, int(profile_id), target_owner_tg_id)
        _authorize_account(ctx, int(account_id))
        conn.execute("BEGIN IMMEDIATE")
        try:
            confirmation = _make_preview(
                conn, actor, profile, "profile.member.remove", int(now),
                target_type="subscription_profile_account",
                target_id=f"{int(profile_id)}:{int(account_id)}",
                payload={"profile_id": int(profile_id), "account_id": int(account_id),
                         "revision": int(profile["revision"])},
            )
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    page = render_page(
        "پیش‌نمایش حذف عضویت", f"حساب {int(account_id)} فقط از این پروفایل جدا می‌شود.",
        persona=_persona(ctx, actor), parent_callback=f"v52:profile:{int(profile_id)}",
    )
    return ProfilePreview(int(profile_id), "member.remove", confirmation["nonce"],
                          confirmation["callback_ok"], confirmation["callback_no"],
                          page.text, page.reply_markup, int(profile["revision"]))


def preview_profile_action(
    ctx: Any,
    profile_id: int,
    action: str,
    *,
    now: int,
    chat_type: str | None = None,
    target_owner_tg_id: int | None = None,
) -> ProfilePreview:
    """Persist a one-use confirmation for rotate/pause/delete/clear operations."""
    actor = _actor_id(ctx)
    selected = str(action).strip().lower()
    actions = {
        "rotate": "profile.rotate", "pause": "profile.pause",
        "resume": "profile.resume", "delete": "profile.delete", "clear": "profile.clear",
    }
    if selected not in actions:
        raise ValueError("unsupported profile action")
    if not _private(ctx, chat_type):
        raise ProfileUIAccessDenied("profile actions are available only in a private chat")
    with database.get_db() as conn:
        profile = _require_profile_owner(conn, actor, int(profile_id), target_owner_tg_id)
        if selected == "delete" and bool(profile["is_default"]):
            raise subscription_service.ProfileError("the default profile cannot be deleted")
        conn.execute("BEGIN IMMEDIATE")
        try:
            confirmation = _make_preview(
                conn, actor, profile, actions[selected], int(now),
                payload={"profile_id": int(profile_id), "revision": int(profile["revision"]),
                         "action": selected},
            )
            conn.commit()
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    page = render_page(
        "تأیید عملیات پروفایل", f"عملیات «{selected}» برای پروفایل {int(profile_id)} آمادهٔ تأیید است.",
        persona=_persona(ctx, actor), parent_callback=f"v52:profile:{int(profile_id)}",
    )
    return ProfilePreview(int(profile_id), selected, confirmation["nonce"],
                          confirmation["callback_ok"], confirmation["callback_no"],
                          page.text, page.reply_markup, int(profile["revision"]))


def execute_profile_action(
    ctx: Any,
    profile_id: int,
    action: str,
    nonce: str,
    *,
    now: int,
    enabled: bool | None = None,
    chat_type: str | None = None,
):
    actor = _actor_id(ctx)
    if not _private(ctx, chat_type):
        raise ProfileUIAccessDenied("profile actions are available only in a private chat")
    selected = str(action).strip().lower()
    with database.get_db() as conn:
        if selected == "rotate":
            return subscription_service.rotate_profile_token(
                conn, actor, int(profile_id), nonce=nonce, now=int(now),
            )
        if selected in {"pause", "resume"}:
            return subscription_service.set_profile_enabled(
                conn, actor, int(profile_id), selected == "resume", nonce=nonce, now=int(now),
            )
        if selected == "delete":
            return subscription_service.delete_profile(
                conn, actor, int(profile_id), nonce=nonce, now=int(now),
            )
        if selected == "clear":
            return subscription_service.clear_profile(
                conn, actor, int(profile_id), nonce=nonce, now=int(now),
            )
    raise ValueError("unsupported profile action")


def rename_profile(
    ctx: Any, profile_id: int, name: str, *, expected_revision: int | None = None,
    chat_type: str | None = None,
):
    actor = _actor_id(ctx)
    if not _private(ctx, chat_type):
        raise ProfileUIAccessDenied("profile actions are available only in a private chat")
    with database.get_db() as conn:
        _require_profile_owner(conn, actor, int(profile_id))
        return subscription_service.rename_profile(
            conn, actor, int(profile_id), name, expected_revision=expected_revision,
        )



def begin_rename(ctx: Any, profile_id: int, *, chat_type: str | None = None):
    """Open a plain-text rename form; profile identifiers and tokens stay out of the form."""
    actor = _actor_id(ctx)
    if not _private(ctx, chat_type):
        raise ProfileUIAccessDenied("profile actions are available only in a private chat")
    with database.get_db() as conn:
        _require_profile_owner(conn, actor, int(profile_id))
    page = render_page(
        "تغییر نام پروفایل", "نام جدید را به‌صورت متن ارسال کنید.",
        persona=_persona(ctx, actor), parent_callback=f"v52:profile:{int(profile_id)}",
    )
    return {"profile_id": int(profile_id), "callback_data": f"v52:prename:{int(profile_id)}",
            "text": page.text, "reply_markup": page.reply_markup}


def show_link(
    ctx: Any,
    profile_id: int,
    *,
    target_owner_tg_id: int | None = None,
    chat_type: str | None = None,
    canonical_host: str | None = None,
) -> dict[str, Any]:
    """Return a canonical or verified-brand link only to the owner/root in DM."""
    actor = _actor_id(ctx)
    if not _private(ctx, chat_type):
        raise ProfileUIAccessDenied("profile links are available only in a private chat")
    with database.get_db() as conn:
        profile = _require_profile_owner(conn, actor, int(profile_id), target_owner_tg_id)
        link = _profile_link(conn, profile, canonical_host)
        if not link:
            raise ProfileUIError("a verified canonical subscription host is unavailable")
        page = render_page(
            "لینک پروفایل", body_html=render_uri(link),
            persona=_persona(ctx, actor), parent_callback=f"v52:profile:{int(profile_id)}",
        )
        return {"status": "ok", "profile_id": int(profile_id), "link": link,
                "text": page.text, "reply_markup": page.reply_markup}


def show_qr(
    ctx: Any,
    profile_id: int,
    *,
    target_owner_tg_id: int | None = None,
    chat_type: str | None = None,
    canonical_host: str | None = None,
) -> dict[str, Any]:
    """Generate a QR from byte-for-byte the same URL returned by show_link."""
    result = show_link(
        ctx, profile_id, target_owner_tg_id=target_owner_tg_id,
        chat_type=chat_type, canonical_host=canonical_host,
    )
    output = BytesIO()
    qrcode.make(result["link"]).save(output, format="PNG")
    result["qr_payload"] = result["link"]
    result["qr_data"] = result["link"].encode("utf-8")
    result["image"] = output.getvalue()
    return result


__all__ = [
    "ProfileListView", "ProfileView", "ProfilePreview", "ProfileUIError",
    "ProfileUIAccessDenied", "list_profiles", "show_profile", "open_account_profiles",
    "begin_create", "add_member", "preview_remove", "preview_profile_action",
    "execute_profile_action", "rename_profile", "begin_rename", "show_link", "show_qr",
]
