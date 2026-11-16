"""Persian rendering of service-layer error messages.

Service modules raise with precise English messages so logs, tracebacks and the
audit trail stay unambiguous. Those messages must never reach a customer or a
representative: the UI layer translates them here, and any message without a
translation degrades to a generic Persian sentence rather than leaking English
into a Telegram page.

The mapping is keyed by the literal strings raised by :mod:`access_service` and
:mod:`support_service`. A message that already contains Persian is returned
unchanged, so a service that raises a user-facing Persian message keeps it.
"""
from __future__ import annotations

# Exact service message -> operator-facing Persian.
_MESSAGES = {
    # access_service
    "actor cannot grant access to this account":
        "شما مجاز به اعطای دسترسی برای این حساب نیستید.",
    "actor cannot revoke access to this account":
        "شما مجاز به لغو دسترسی این حساب نیستید.",
    "self-grants are not supported":
        "امکان اعطای دسترسی به خودتان وجود ندارد.",
    "grantee must be an existing, unblocked Telegram user":
        "کاربری که می‌خواهید به او دسترسی بدهید باید در سامانه ثبت شده و مسدود نباشد.",
    "access_level must be viewer or manager":
        "سطح دسترسی انتخابی معتبر نیست؛ فقط «بازدیدکننده» یا «مدیر» مجاز است.",
    # Raised by access_ui itself when the grant it is about to act on is gone or
    # no longer matches the button that was pressed.
    "active grant not found":
        "این دسترسی دیگر فعال نیست؛ فهرست دسترسی‌ها را تازه کنید.",
    "grant target does not match":
        "این دسترسی با کاربر انتخاب‌شده مطابقت ندارد؛ صفحه را تازه کنید.",
    "grant access_level is invalid":
        "سطح دسترسی ثبت‌شده برای این حساب معتبر نیست؛ با مدیر کل هماهنگ کنید.",
    "account and grantee IDs must be positive integers":
        "شناسه حساب یا کاربر نامعتبر است.",
    "unsupported account action":
        "این عملیات روی حساب پشتیبانی نمی‌شود.",
    "account_id must be a positive integer":
        "شناسه حساب نامعتبر است.",
    "actor Telegram ID must be a positive integer":
        "شناسه تلگرام شما معتبر نیست؛ لطفاً دوباره وارد شوید.",
    # support_service
    "support ticket does not exist":
        "تیکت پشتیبانی مورد نظر یافت نشد.",
    "actor cannot view this support ticket":
        "شما مجاز به مشاهده این تیکت نیستید.",
    "actor cannot reply to this support ticket":
        "شما مجاز به پاسخ دادن به این تیکت نیستید.",
    "actor cannot export this support ticket":
        "شما مجاز به گرفتن خروجی از این تیکت نیستید.",
    "actor cannot close this support ticket":
        "شما مجاز به بستن این تیکت نیستید.",
    "actor cannot reopen this support ticket":
        "شما مجاز به بازگشایی این تیکت نیستید.",
    "actor cannot open a ticket for this account":
        "شما مجاز به ثبت تیکت برای این حساب نیستید.",
    "actor cannot view diagnostics for this account":
        "شما مجاز به مشاهده عیب‌یابی این حساب نیستید.",
    "support ticket is already closed":
        "این تیکت پیش‌تر بسته شده است.",
    "only the assigned seller or root may escalate this ticket":
        "فقط فروشندهٔ مسئول این تیکت یا مدیر کل می‌تواند آن را ارجاع دهد.",
    "customers may only add customer-visible messages":
        "کاربر عادی فقط می‌تواند پیام قابل مشاهده برای خودش ثبت کند.",
    "root-only notes require the configured root":
        "یادداشت داخلی فقط توسط مدیر کل قابل ثبت است.",
    "account does not exist or is deleted":
        "حساب مورد نظر وجود ندارد یا حذف شده است.",
    "a message must contain text or an attachment":
        "پیام باید متن یا فایل پیوست داشته باشد.",
    "a ticket must contain text or an attachment":
        "تیکت باید متن یا فایل پیوست داشته باشد.",
    "attachment reference is too long":
        "فایل پیوست بزرگ‌تر از حد مجاز است.",
    "unsupported message visibility":
        "سطح نمایش پیام معتبر نیست.",
    "unsupported support ticket type":
        "نوع تیکت انتخابی معتبر نیست.",
    "the selected Telegram bot is not registered and active":
        "بات انتخاب‌شده ثبت نشده یا فعال نیست.",
    "panel unavailable":
        "پنل در دسترس نیست؛ لطفاً چند لحظه بعد دوباره تلاش کنید.",
    "stored support message failed authentication":
        "محتوای ذخیره‌شدهٔ پیام قابل اعتبارسنجی نبود.",
    "diagnostic reader returned an invalid result":
        "نتیجهٔ عیب‌یابی معتبر نبود.",
    "ticket close lost a concurrent update":
        "هم‌زمانی در بستن تیکت رخ داد؛ صفحه را تازه کنید و دوباره تلاش کنید.",
    "ticket reopen lost a concurrent update":
        "هم‌زمانی در بازگشایی تیکت رخ داد؛ صفحه را تازه کنید و دوباره تلاش کنید.",
    "ticket escalation lost a concurrent update":
        "هم‌زمانی در ارجاع تیکت رخ داد؛ صفحه را تازه کنید و دوباره تلاش کنید.",
}

DEFAULT_FALLBACK = "این عملیات انجام نشد. اگر تکرار شد با مدیریت هماهنگ کنید."


def is_persian(text: str) -> bool:
    """True when the text contains at least one Arabic-script (Persian) letter."""
    return any("\u0600" <= char <= "\u06ff" for char in str(text or ""))


def translate(exc_or_message, *, fallback: str = DEFAULT_FALLBACK) -> str:
    """Persian text for a service error; never returns the English original.

    Accepts an exception or a message string. An exact match wins, then a
    containment match (a service may append detail, e.g.
    ``unsupported account action: issue``), then the caller's fallback.
    """
    message = str(exc_or_message or "").strip()
    if not message:
        return fallback
    if is_persian(message):
        return message
    exact = _MESSAGES.get(message)
    if exact:
        return exact
    lowered = message.casefold()
    for english, persian in _MESSAGES.items():
        if english in lowered:
            return persian
    return fallback


__all__ = ["DEFAULT_FALLBACK", "is_persian", "translate"]
