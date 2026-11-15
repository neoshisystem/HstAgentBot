"""Transport-neutral v5.2 panel setup and panel-policy pages.

No credential is rendered, logged or placed in callback data. Network checks are
represented as a separate callback and are never executed while a DB transaction
is open by this UI module.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import config
from panel_adapters.url_parser import parse_panel_url
from ui_v52 import button, render_error, render_page


PANEL_TYPES = ("3x-ui", "manual")
PANEL_LIFECYCLES = ("active", "sold_out", "hidden", "drain", "maintenance")

# The four core families the operator can pick in step 1 of the add/edit wizard.
# The slug is what gets persisted in ``panels.panel_type``; the label is the exact
# inline-button text. ``manual`` stays a valid stored type (legacy rows and the
# pool adapter) but is no longer offered by the wizard.
PANEL_CORE_TYPES = (
    ("3x-ui", "🟢 3X-UI (Sanaei / Alireza)"),
    ("marzban", "🔵 Marzban"),
    ("hiddify", "🟣 Hiddify"),
    ("rebecca", "🟠 Rebecca / X-UI"),
)
PANEL_CORE_TYPE_SLUGS = tuple(slug for slug, _label in PANEL_CORE_TYPES)
PANEL_TYPE_LABELS = dict(PANEL_CORE_TYPES)
_PANEL_WIZARD_HOME = "v52:home:admin"


def _actor_id(ctx) -> int:
    if isinstance(ctx, Mapping):
        value = ctx.get("actor_tg_id", ctx.get("telegram_id"))
    else:
        value = getattr(ctx, "actor_tg_id", getattr(ctx, "telegram_id", None))
    if isinstance(value, bool):
        raise ValueError("actor Telegram ID must be positive")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("actor Telegram ID must be positive") from exc
    if result <= 0:
        raise ValueError("actor Telegram ID must be positive")
    return result


def _is_root(ctx) -> bool:
    try:
        root_id = int(getattr(config, "ADMIN_TELEGRAM_ID", 0) or 0)
        return root_id > 0 and _actor_id(ctx) == root_id
    except (TypeError, ValueError):
        return False


def _positive(value, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a positive integer")
    try:
        exact = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer") from exc
    if exact <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return exact


def _non_negative(value, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a non-negative integer")
    try:
        exact = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a non-negative integer") from exc
    if exact < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return exact


def _parse_inbounds(value) -> tuple[int, ...]:
    if value in (None, ""):
        return ()
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, ValueError) as exc:
        raise ValueError("inbound JSON is invalid") from exc
    if isinstance(parsed, Mapping):
        parsed = parsed.get("inbound_ids", parsed.get("inbounds", []))
    if not isinstance(parsed, Sequence) or isinstance(parsed, (str, bytes)):
        raise ValueError("inbound JSON must be a list or an object containing inbound_ids")
    identifiers = []
    for item in parsed:
        candidate = item.get("id") if isinstance(item, Mapping) else item
        identifiers.append(_positive(candidate, "inbound id"))
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("inbound ids must be unique")
    return tuple(identifiers)


def _wizard_nav(back_callback: str, card_callback: str | None = None) -> list[list]:
    """The mandatory navigation rows every wizard step carries.

    ``🔙 بازگشت به مرحله قبل`` returns to the previous step and
    ``🏠 بازگشت به منوی اصلی`` returns to the admin home, so no step is a dead end.
    An optional panel-card row keeps the operator one tap from the panel detail.
    """
    rows: list[list] = []
    if card_callback:
        rows.append([button("🖥 مشاهده کارت پنل", card_callback)])
    rows.append([button("🔙 بازگشت به مرحله قبل", back_callback)])
    rows.append([button("🏠 بازگشت به منوی اصلی", _PANEL_WIZARD_HOME)])
    return rows


def _core_type_label(slug: str) -> str:
    return PANEL_TYPE_LABELS.get(str(slug or "").strip().lower(), str(slug or ""))


def render_panel_type_step(ctx, *, panel_id=None, parent_callback: str = "v52:panels"):
    """Step 1 of 4 — pick the panel core type.

    In add mode (``panel_id is None``) the choice creates the draft row; in edit
    mode it updates the existing row. Both modes share this single builder so the
    add and edit flows cannot diverge.
    """
    if not _is_root(ctx):
        return render_error("افزودن/ویرایش پنل فقط در اختیار root است.", persona="admin",
                            parent_callback=parent_callback)
    rows = []
    for slug, label in PANEL_CORE_TYPES:
        if panel_id is None:
            callback = f"v52:panelnew:type:{slug}"
        else:
            callback = f"v52:panelsettype:{int(panel_id)}:{slug}"
        rows.append([button(label, callback)])
    back = f"v52:panel:{int(panel_id)}" if panel_id is not None else "v52:panels"
    rows.extend(_wizard_nav(back))
    body = (
        "نوع هستهٔ پنل را انتخاب کنید. نوع انتخابی روی رکورد پنل ذخیره می‌شود و مشخص "
        "می‌کند کدام درایور برای آزمون ورود استفاده شود. پنل جدید به‌صورت پیش‌فرض "
        "فروشی نیست و به هیچ تعرفه‌ای متصل نمی‌شود.\n"
        "مرحله ۱ از ۴: نوع هسته"
    )
    return render_page("افزودن/ویرایش پنل", body, persona="admin", page_kind="wizard",
                       root=True, rows=rows)


def render_panel_name_step(ctx, panel_id, *, error: str | None = None,
                           parent_callback: str = "v52:panels"):
    """Step 2 of 4 — the human-readable display name."""
    if not _is_root(ctx):
        return render_error("افزودن/ویرایش پنل فقط در اختیار root است.", persona="admin",
                            parent_callback=parent_callback)
    pid = int(panel_id)
    rows = _wizard_nav(f"v52:panelconfig:{pid}", f"v52:panel:{pid}")
    body = "نام نمایشی پنل را ارسال کنید (مثلاً «سرور آلمان ۱»). بین ۱ تا ۱۰۰ نویسه."
    if error:
        body = f"❌ {error}\n\n{body}"
    body += "\nمرحله ۲ از ۴: نام نمایشی"
    return render_page("افزودن/ویرایش پنل", body, persona="admin", page_kind="form",
                       root=True, rows=rows)


def render_panel_url_step(ctx, panel_id, *, error: str | None = None,
                          parent_callback: str = "v52:panels"):
    """Step 3 of 4 — the panel URL, validated as an http(s) origin."""
    if not _is_root(ctx):
        return render_error("افزودن/ویرایش پنل فقط در اختیار root است.", persona="admin",
                            parent_callback=parent_callback)
    pid = int(panel_id)
    rows = _wizard_nav(f"v52:panelname:{pid}", f"v52:panel:{pid}")
    body = ("نشانی کامل پنل را بدون نام کاربری/رمز، query یا fragment ارسال کنید. "
            "فقط نشانی معتبر http یا https پذیرفته می‌شود.")
    if error:
        body = f"❌ {error}\n\n{body}"
    body += "\nمرحله ۳ از ۴: نشانی پنل"
    return render_page("افزودن/ویرایش پنل", body, persona="admin", page_kind="form",
                       root=True, rows=rows)


def render_panel_credential_step(ctx, panel_id, *, error: str | None = None,
                                 parent_callback: str = "v52:panels"):
    """Step 4 of 4 — the credential, then the live login probe."""
    if not _is_root(ctx):
        return render_error("افزودن/ویرایش پنل فقط در اختیار root است.", persona="admin",
                            parent_callback=parent_callback)
    from panel_adapters.credentials import credential_help_text
    pid = int(panel_id)
    rows = _wizard_nav(f"v52:panelurl:{pid}", f"v52:panel:{pid}")
    body = credential_help_text()
    if error:
        body = f"❌ {error}\n\n{body}"
    body += "\nمرحله ۴ از ۴: اعتبارنامه"
    return render_page("افزودن/ویرایش پنل", body, persona="admin", page_kind="form",
                       root=True, rows=rows)


def render_panel_connection_result(ctx, panel_id, *, panel_type: str, reachable: bool,
                                   reason: str | None = None):
    """Render the outcome of the step-4 live login probe with the next action.

    On success the operator can pick the default inbound from the panel's real
    inbound list (``v52:panelinbound``); on failure every retry path is offered so
    the message is never a dead end.
    """
    if not _is_root(ctx):
        return render_error("افزودن/ویرایش پنل فقط در اختیار root است.", persona="admin",
                            parent_callback="v52:panels")
    pid = int(panel_id)
    if reachable:
        body = (f"✅ اتصال پنل {pid} ذخیره شد و آزمون ورود موفق بود. "
                "اکنون inbound پیش‌فرض ساخت اکانت را از فهرست واقعی پنل انتخاب کنید.")
        rows = [
            [button("🧩 انتخاب Inbound پیش‌فرض ساخت اکانت", f"v52:panelinbound:{pid}")],
            [button("⚡ تست اتصال و فعال‌سازی خودکار", f"v52:panelauto:{pid}")],
            [button("🧪 آزمایش اتصال پنل", f"v52:paneltest:{pid}")],
            [button("🟢 فعال‌سازی پس از آزمایش", f"v52:life:{pid}:active")],
            [button("🖥 مشاهده کارت پنل", f"v52:panel:{pid}")],
            [button("🔙 بازگشت به مرحله قبل", f"v52:panelcred:{pid}")],
            [button("🏠 بازگشت به منوی اصلی", _PANEL_WIZARD_HOME)],
        ]
        return render_page("نتیجهٔ اتصال پنل", body, persona="admin", page_kind="result",
                           root=True, rows=rows)
    detail = str(reason or "").strip() or "پاسخ قابل‌اتکا از پنل دریافت نشد"
    body = (f"✅ اتصال پنل {pid} ذخیره شد، اما آزمون ورود ناموفق بود: {detail}\n"
            "وضعیت پنل تغییر نکرد. اعتبارنامه را اصلاح کنید یا دوباره آزمایش کنید.")
    rows = [
        [button("✏️ اصلاح اعتبارنامه", f"v52:panelcred:{pid}")],
        [button("✏️ ویرایش اتصال", f"v52:panelconfig:{pid}")],
        [button("🔁 آزمایش مجدد اتصال", f"v52:paneltest:{pid}")],
        [button("⚡ تست اتصال و فعال‌سازی خودکار", f"v52:panelauto:{pid}")],
        [button("🟢 فعال‌سازی پس از آزمایش", f"v52:life:{pid}:active")],
        [button("🖥 مشاهده کارت پنل", f"v52:panel:{pid}")],
        [button("🏠 بازگشت به منوی اصلی", _PANEL_WIZARD_HOME)],
    ]
    return render_page("نتیجهٔ اتصال پنل", body, persona="admin", page_kind="result",
                       root=True, rows=rows)


def render_panel_connection_saved(ctx, panel_id, *, panel_type: str,
                                  parent_callback: str = "v52:panels"):
    """Credential saved for a non-3X-UI type: the live probe is not wired yet."""
    if not _is_root(ctx):
        return render_error("افزودن/ویرایش پنل فقط در اختیار root است.", persona="admin",
                            parent_callback=parent_callback)
    pid = int(panel_id)
    label = _core_type_label(panel_type)
    body = (f"✅ اتصال پنل {pid} ({label}) ذخیره شد. آزمون زندهٔ ورود در این نسخه فقط برای "
            "پنل 3X-UI پیاده‌سازی شده است؛ پس از تأیید درایور این نوع پنل، اتصال را آزمایش کنید.")
    rows = [
        [button("🧪 آزمایش اتصال پنل", f"v52:paneltest:{pid}")],
        [button("⚡ تست اتصال و فعال‌سازی خودکار", f"v52:panelauto:{pid}")],
        [button("🟢 فعال‌سازی پس از آزمایش", f"v52:life:{pid}:active")],
        [button("🖥 مشاهده کارت پنل", f"v52:panel:{pid}")],
        [button("🔙 بازگشت به مرحله قبل", f"v52:panelcred:{pid}")],
        [button("🏠 بازگشت به منوی اصلی", _PANEL_WIZARD_HOME)],
    ]
    return render_page("اتصال پنل ذخیره شد", body, persona="admin", page_kind="result",
                       root=True, rows=rows)


def begin_add_panel(ctx, *, parent_callback: str = "v52:panels"):
    """Start the root-only add-panel wizard at step 1 (core type)."""
    return render_panel_type_step(ctx, panel_id=None, parent_callback=parent_callback)


def preview_panel_config(
    ctx,
    panel_type: str,
    url: str,
    *,
    name: str = "",
    inbound_json=None,
    auth_secret: str | None = None,
    parent_callback: str = "v52:panelnew",
):
    """Validate a proposed endpoint/inbound set and render a redacted preview."""
    if not _is_root(ctx):
        return render_error("پیش‌نمایش پیکربندی پنل فقط در اختیار root است.",
                            persona="admin", parent_callback=parent_callback)
    selected_type = str(panel_type or "").strip().lower()
    if selected_type in {"3x-ui", "3xui", "xui"}:
        selected_type = "3x-ui"
    elif selected_type in {"manual", "manual-pool", "pool"}:
        selected_type = "manual"
    else:
        return render_error("نوع پنل باید 3x-ui یا manual باشد.",
                            persona="admin", parent_callback=parent_callback)
    title = str(name or "").strip()
    if not title or len(title) > 100:
        return render_error("نام پنل باید بین ۱ تا ۱۰۰ نویسه باشد.",
                            persona="admin", parent_callback=parent_callback)
    try:
        endpoint = parse_panel_url(url)
        inbounds = _parse_inbounds(inbound_json) if selected_type == "3x-ui" else ()
    except ValueError as exc:
        # Do not echo the raw URL, inbound payload or credentials in error pages.
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)
    lines = [
        f"نام: {title}",
        f"نوع: {selected_type}",
        f"نشانی نرمال‌شده: {endpoint.normalized_url}",
        f"مسیر API: {endpoint.api_base_url}",
        f"ورودی‌ها: {', '.join(map(str, inbounds)) if inbounds else 'تعریف نشده'}",
        f"اعتبارنامه: {'تنظیم شده (محرمانه)' if auth_secret else 'ثبت نشده'}",
        "پنل پیش‌فرض: خیر",
        "اتصال به پلن‌ها: هیچ‌کدام",
        "آزمون اتصال باید جدا از تراکنش DB اجرا شود؛ این پیش‌نمایش شبکه را لمس نمی‌کند.",
    ]
    return render_page(
        "پیش‌نمایش پیکربندی پنل",
        "\n".join(lines),
        persona="admin", page_kind="preview", parent_callback=parent_callback,
        rows=[[button("آزمون اتصال", "v52:panelnew:test"),
               button("ذخیره با تأیید", "v52:panelnew:confirm")]],
    )


def lifecycle_flags(lifecycle: str) -> dict[str, bool]:
    """Derive every display flag from the single canonical lifecycle enum."""
    state = str(lifecycle or "").strip().lower()
    if state not in PANEL_LIFECYCLES:
        raise ValueError("unknown panel lifecycle")
    return {
        "new_sales": state == "active",
        "renewals": state in {"active", "sold_out", "hidden"},
        "draining": state == "drain",
        "maintenance": state == "maintenance",
        "sellable": state == "active",
    }


def preview_lifecycle(
    ctx,
    panel_id,
    lifecycle: str | None = None,
    *,
    panel: Mapping | None = None,
    persona: str = "admin",
    parent_callback: str = "v52:panel",
):
    """Show lifecycle and flags derived from it; stale booleans are ignored."""
    if not _is_root(ctx):
        return render_error("تغییر چرخهٔ عمر پنل فقط در اختیار root است.",
                            persona=persona, parent_callback=parent_callback)
    selected_panel = panel
    if isinstance(panel_id, Mapping):
        selected_panel = panel_id
        try:
            exact_id = _positive(selected_panel.get("id", selected_panel.get("panel_id")), "panel_id")
        except ValueError as exc:
            return render_error(str(exc), persona=persona, parent_callback=parent_callback)
        if lifecycle is None:
            lifecycle = selected_panel.get("lifecycle")
    else:
        try:
            exact_id = _positive(panel_id, "panel_id")
        except ValueError as exc:
            return render_error(str(exc), persona=persona, parent_callback=parent_callback)
    if lifecycle is None and selected_panel is not None:
        lifecycle = selected_panel.get("lifecycle")
    try:
        state = str(lifecycle or "").strip().lower()
        flags = lifecycle_flags(state)
    except ValueError as exc:
        return render_error(str(exc), persona=persona, parent_callback=parent_callback)
    body = (
        f"پنل: {exact_id}\nLifecycle (منبع حقیقت): {state}\n"
        f"فروش جدید: {'مجاز' if flags['new_sales'] else 'مسدود'}\n"
        f"تمدید: {'مجاز' if flags['renewals'] else 'مسدود'}\n"
        f"Drain: {'بله' if flags['draining'] else 'خیر'}\n"
        f"Maintenance: {'بله' if flags['maintenance'] else 'خیر'}\n"
        "پرچم‌های نمایشی از lifecycle محاسبه شده‌اند؛ مقدار boolean مستقل ذخیره نمی‌شود."
    )
    rows = [[button(label, f"v52:life:{exact_id}:{value}")]
            for label, value in (("فعال", "active"), ("فروش‌متوقف", "sold_out"),
                                 ("پنهان", "hidden"), ("Drain", "drain"),
                                 ("تعمیرات", "maintenance"))]
    rows.append([button("تأیید تغییر وضعیت", f"v52:life-confirm:{exact_id}:{state}")])
    return render_page("پیش‌نمایش چرخهٔ عمر پنل", body, persona=persona,
                       page_kind="preview", parent_callback=parent_callback, rows=rows)


def _integer_price(value, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a finite non-negative integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a finite non-negative integer") from exc
    if result < 0 or str(value).strip() not in {str(result), f"{result}.0"}:
        raise ValueError(f"{label} must be a finite non-negative integer")
    return result


def edit_panel_policy(
    ctx,
    panel_id: int,
    policy: Mapping,
    *,
    current_revision: int,
    allowed_strategies: Sequence[str] = (),
    allowed_renew_methods: Sequence[str] = (),
    confirmed: bool = False,
    save=None,
    parent_callback: str = "v52:panel",
):
    """Validate policy changes and optionally hand a confirmed CAS write to a DB service.

    The injected save callback owns the transaction, revision CAS and unique-safe
    default-plan transition (unset current default, then set the new one).
    """
    if not _is_root(ctx):
        return render_error("ویرایش سیاست پنل فقط در اختیار root است.",
                            persona="admin", parent_callback=parent_callback)
    try:
        pid = _positive(panel_id, "panel_id")
        revision = _positive(current_revision, "current_revision")
        if not isinstance(policy, Mapping):
            raise ValueError("policy must be a mapping")
        maximum = _non_negative(policy.get("max_clients", 0), "max_clients")
        quota = _non_negative(policy.get("daily_test_quota", 0), "daily_test_quota")
        addon_gb = _integer_price(policy.get("addon_rate_gb", 0), "addon_rate_gb")
        addon_days = _non_negative(policy.get("addon_rate_days", 0), "addon_rate_days")
        finite_prices = policy.get("finite_prices", {})
        if not isinstance(finite_prices, Mapping):
            raise ValueError("finite_prices must be a mapping")
        prices = {str(key): _integer_price(value, f"finite_prices.{key}")
                  for key, value in finite_prices.items()}
        if not isinstance(policy.get("renew_guard", False), bool):
            raise ValueError("renew_guard must be boolean")
        strategy = str(policy.get("strategy") or "").strip()
        renew_method = str(policy.get("renew_method") or "").strip()
        if allowed_strategies:
            if len(set(allowed_strategies)) != 6 or len(allowed_strategies) != 6:
                raise ValueError("the approved strategy registry must contain exactly six unique names")
            if strategy not in allowed_strategies:
                raise ValueError("strategy is not in the approved six-name registry")
        elif not strategy:
            raise ValueError("strategy is required")
        if allowed_renew_methods:
            if len(set(allowed_renew_methods)) != 5 or len(allowed_renew_methods) != 5:
                raise ValueError("the approved renewal registry must contain exactly five unique methods")
            if renew_method not in allowed_renew_methods:
                raise ValueError("renew_method is not in the approved five-method registry")
        elif not renew_method:
            raise ValueError("renew_method is required")
        default_plan = policy.get("default_plan")
        normalized = {
            "max_clients": maximum,
            "capacity_unlimited": maximum == 0,
            "daily_test_quota": quota,
            "addon_rate_gb": addon_gb,
            "addon_rate_days": addon_days,
            "renew_guard": bool(policy.get("renew_guard", False)),
            "strategy": strategy,
            "renew_method": renew_method,
            "finite_prices": prices,
            "default_plan": default_plan,
        }
        next_revision = revision + 1
        if confirmed:
            if not callable(save):
                raise ValueError("confirmed policy requires a transactional save callback")
            outcome = save(
                panel_id=pid, expected_revision=revision, next_revision=next_revision,
                policy=normalized, switch_default=default_plan is not None,
            )
            return render_page(
                "سیاست پنل ثبت شد",
                f"پنل: {pid}\nRevision: {next_revision}\nظرفیت: "
                f"{'نامحدود' if maximum == 0 else maximum}\nنتیجهٔ ثبت: {outcome}",
                persona="admin", page_kind="result", parent_callback=parent_callback,
            )
        summary = (
            f"پنل: {pid}\nRevision فعلی: {revision} → پس از تأیید: {next_revision}\n"
            f"ظرفیت: {'نامحدود' if maximum == 0 else maximum}\n"
            f"استراتژی: {strategy}\nمتد تمدید: {renew_method}\n"
            f"Guard تمدید: {'روشن' if normalized['renew_guard'] else 'خاموش'}\n"
            f"نرخ افزونه: {addon_gb} GB / {addon_days} روز\n"
            f"سهمیهٔ تست روزانه: {quota}\n"
            f"تعداد قیمت‌های finite: {len(prices)}\n"
            f"پلن پیش‌فرض پیشنهادی: {default_plan if default_plan is not None else 'بدون تغییر'}\n"
            "قیمت‌های finite باید نامنفی باشند. تغییر پلن پیش‌فرض فقط در تراکنش یکتا-ایمن انجام می‌شود."
        )
        return render_page(
            "پیش‌نمایش سیاست پنل",
            summary, persona="admin", page_kind="preview", parent_callback=parent_callback,
            rows=[[button("تأیید سیاست", f"v52:panelpolicy:{pid}")]],
        )
    except (TypeError, ValueError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


__all__ = ["PANEL_TYPES", "PANEL_LIFECYCLES", "PANEL_CORE_TYPES", "PANEL_CORE_TYPE_SLUGS",
           "PANEL_TYPE_LABELS", "begin_add_panel", "render_panel_type_step",
           "render_panel_name_step", "render_panel_url_step", "render_panel_credential_step",
           "render_panel_connection_result", "render_panel_connection_saved",
           "preview_panel_config", "lifecycle_flags", "preview_lifecycle", "edit_panel_policy"]
