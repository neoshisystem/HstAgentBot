"""Registry for the 28 approved v5.2 authority decisions.

Decision text is fixed to the approved RFP. Conflicting rows are never overwritten.
"""
from __future__ import annotations

from typing import Any

AUTHORITY = "hisystem"
APPROVED_DATE = "2026-11-06"
POLICY_VERSION = "v1.0"
APPROVED_STATUS = "APPROVED"

APPROVED_POLICIES: tuple[tuple[str, str], ...] = (
    ("PA-01", "ابتدا 3X-UI فعلی و manual/pool؛ سپس Marzban و دیگر پنل‌ها با ترجمه پروتکل فقط داخل adapter."),
    ("PA-02", "دو عملیات مستقل تغییر subId و تغییر UUID/اعتبارنامه اتصال با هدف قطع دستگاه‌های قبلی."),
    ("PA-03", "پلن‌های قدیمی به پنل پیش‌فرض؛ پنل جدید فقط با عرضه صریح."),
    ("PA-04", "deny مقدم؛ سلب اجازه فروش جدید از نماینده حق تمدید سرویس قدیمی را به‌تنهایی حذف نمی‌کند؛ Drain/Maintenance مقدم بر اجازه تمدیدند."),
    ("PA-05", "حداکثر سه سطح نمایندگی، سقف صفر یعنی ممنوعیت ساخت فرزند؛ والد آمار می‌بیند نه کانفیگ خصوصی مشتری فرزند؛ root دسترسی کامل؛ حذف نماینده نرم."),
    ("PA-06", "credit و prepaid_wallet per-edge اتمیک؛ parent_shared غیرفعال."),
    ("PA-07", "قیمت مستقل هر edge، snapshot quote، تومان صحیح، هزینه کوپن بر سهم صادرکننده."),
    ("PA-08", "wallet صددرصد یا direct invoice صددرصد؛ mixed payment غیرفعال."),
    ("PA-09", "شکست قطعی بعد وصول مستقیم: اعتبار کیف‌پول همان مشتری در همان tenant؛ امکان درخواست عودت بانکی دستی."),
    ("PA-10", "quote سی دقیقه؛ submit فیش قبل انقضا رزرو را تا review نگه می‌دارد؛ تأیید فیش سفارش واقعاً منقضی فقط topup، بدون صدور."),
    ("PA-11", "چند کارت در فاز اول؛ crypto و gateway تا اتصال Verifier flag خاموش."),
    ("PA-12", "کارت root برای فروش root؛ کارت rep فقط تسویه زیرمجموعه و مشتری بات خودش؛ سقف روزانه مجموع pending و approved روز تهران."),
    ("PA-13", "smart_rollover فعلی و caps=2.0/2.0 حفظ؛ سایر فرمول‌های جدول ۷.۳ نسخه ۵.۱ مصوب؛ GiB=1024^3 بایت."),
    ("PA-14", "on-hold: تمدید و زمان اضافه ممنوع، حجم اضافه مجاز؛ unlimited از صفر جدا."),
    ("PA-15", "منطق فعلی reserved_packages و قفل reservation-activation حفظ."),
    ("PA-16", "sold_out/hidden فروش جدید بسته، تمدید باز؛ Drain/Maintenance هر دو بسته؛ ظرفیت active+on-hold+inflight، صفر نامحدود."),
    ("PA-17", "مصرف شخصی root معاف؛ مصرف شخصی rep به نرخ خرید از اعتبار/کیف‌پول خودش؛ /link فقط حساب بی‌مالک یا grant معتبر."),
    ("PA-18", "tier دستی توسط root؛ quota تست روزانه تهران؛ خرید اول یعنی نداشتن سفارش موفق غیرتستی."),
    ("PA-19", "abuse فقط پیوی root؛ هیچ رونوشت در گروه/تاپیک، حتی گروه مدیریت."),
    ("PA-20", "encryption ستون‌های حساس و آرشیو بکاپ تلگرام با کلید محیطی BACKUP_ENCRYPTION_KEY."),
    ("PA-21", "SQLite/WAL، busy_timeout=30000 و مرز گزارش 00:00 تهران."),
    ("PA-22", "auto_inventory/file_document/manual_fulfill فعال؛ کالای برگشتی پس از نمایش راز defective."),
    ("PA-23", "شش راهبرد تعریف‌شده نام‌گذاری؛ دو راهبرد تخیلی افزوده نشود."),
    ("PA-24", "هسته مشترک و چند بات اختصاصی نماینده؛ tenant فروش از هویت بات معتبر تعیین شود."),
    ("PA-25", "needs_reconciliation و قفل پایدار UNKNOWN حفظ."),
    ("PA-26", "financial_ledger موجود مرجع حقیقت؛ financial_holds اضافه شود؛ ledger موازی جایگزین ساخته نشود."),
    ("OPT-01", "پیش‌نمایش عددی حجم/زمان قبل و بعد تمدید داخل دامنه مصوب."),
    ("OPT-07", "plan_panels نرمال داخل دامنه مصوب."),
)
POLICY_BY_ID = dict(APPROVED_POLICIES)
if len(APPROVED_POLICIES) != 28 or len(POLICY_BY_ID) != 28:
    raise RuntimeError("approved policy registry must contain exactly 28 unique decisions")


class PolicyRegistryError(ValueError):
    """A policy row is missing or its approved metadata conflicts with v1.0."""


def require_policy(conn: Any, decision_id: str):
    """Return a row only if its identity and all approved metadata match."""
    if not isinstance(decision_id, str) or decision_id not in POLICY_BY_ID:
        raise PolicyRegistryError("unknown approved policy decision")
    row = conn.execute(
        "SELECT status, authority, approved_date, policy_version, decision_text "
        "FROM policy_decisions WHERE decision_id = ?",
        (decision_id,),
    ).fetchone()
    if row is None:
        raise PolicyRegistryError("approved policy decision is missing")
    status, authority, approved_date, version, decision_text = tuple(row)
    if (
        status != APPROVED_STATUS
        or authority != AUTHORITY
        or approved_date != APPROVED_DATE
        or version != POLICY_VERSION
        or decision_text != POLICY_BY_ID[decision_id]
    ):
        raise PolicyRegistryError("approved policy metadata does not match v1.0")
    return row


def seed_approved_policies(conn: Any) -> None:
    """Idempotently seed approved rows without overwriting conflicting values."""
    conn.execute("SAVEPOINT seed_approved_policies")
    try:
        for decision_id, decision_text in APPROVED_POLICIES:
            conn.execute(
                "INSERT INTO policy_decisions VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(decision_id) DO NOTHING",
                (
                    decision_id, APPROVED_STATUS, AUTHORITY,
                    APPROVED_DATE, POLICY_VERSION, decision_text,
                ),
            )
            require_policy(conn, decision_id)
        conn.execute("RELEASE SAVEPOINT seed_approved_policies")
    except Exception:
        conn.execute("ROLLBACK TO SAVEPOINT seed_approved_policies")
        conn.execute("RELEASE SAVEPOINT seed_approved_policies")
        raise
