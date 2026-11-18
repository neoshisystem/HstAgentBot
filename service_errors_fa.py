"""Persian rendering of service-layer error messages.

Service modules raise with precise English messages so logs, tracebacks and the
audit trail stay unambiguous. Those messages must never reach a customer or a
representative: the UI layer translates them here, and any message without a
translation degrades to a generic Persian sentence rather than leaking English
into a Telegram page.

The mapping is keyed by the literal strings raised by :mod:`access_service`,
:mod:`support_service` and the S58 operator surfaces (:mod:`checkout_ui`,
:mod:`account_actions_ui`, :mod:`access_ui`, :mod:`bot_admin_ui`). A message that
already contains Persian is returned unchanged, so a service that raises a
user-facing Persian message keeps it.

Two shapes of key are used. A *literal* key is the exact message a module raises.
A *template* key carries a parametrised message whose field name varies
(``{label} must be a positive integer``) or a standard-library message whose tail
varies (``invalid isoformat string``); those are matched by the containment pass,
so the field name or parser detail never reaches the operator.
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
    # --- S58: the checkout, account-action, access and bot-admin surfaces ---- #
    # Parametrised validators. ``_positive``/``_money``/``_positive_id`` build the
    # message from the field name, so only the suffix carries meaning; the field
    # name itself must never be echoed to the operator.
    "{label} must be a positive integer":
        "شناسهٔ واردشده معتبر نیست؛ باید یک عدد صحیح و مثبت باشد.",
    "must be a positive integer":
        "شناسهٔ واردشده معتبر نیست؛ باید یک عدد صحیح و مثبت باشد.",
    "{label} must be a non-negative integer toman amount":
        "مبلغ واردشده معتبر نیست؛ باید یک عدد صحیح و نامنفی به تومان باشد.",
    "must be a non-negative integer toman amount":
        "مبلغ واردشده معتبر نیست؛ باید یک عدد صحیح و نامنفی به تومان باشد.",
    # checkout_ui
    "an immutable price quote is required":
        "برای این صفحه به یک برآورد قیمت قفل‌شده نیاز است؛ آن را از مسیر خرید باز کنید.",
    "plan_code is required":
        "پلن انتخاب نشده است؛ پلن را از فهرست تعرفه‌ها انتخاب کنید.",
    "quote snapshot is malformed":
        "اطلاعات برآورد قیمت ناقص است؛ خرید را از ابتدا آغاز کنید.",
    "quote price edge is malformed":
        "زنجیرهٔ قیمت این برآورد ناقص است؛ خرید را از ابتدا آغاز کنید.",
    "quote final price edge does not match the frozen amount":
        "مبلغ نهایی با برآورد قفل‌شده یکسان نیست؛ برای جلوگیری از اختلاف، خرید متوقف شد.",
    "quote purpose is not a purchase":
        "این برآورد قیمت برای خرید صادر نشده است.",
    "quote purpose is not a renewal":
        "این برآورد قیمت برای تمدید صادر نشده است.",
    "paid purchase quote must be positive":
        "مبلغ خرید باید بزرگ‌تر از صفر باشد.",
    "plan_code and immutable renewal quote are required":
        "برای تمدید، انتخاب پلن و برآورد قیمت قفل‌شده لازم است.",
    "renewal quote must contain before and after snapshots":
        "برآورد تمدید باید وضعیت پیش و پس از تمدید را همراه داشته باشد؛ صفحه را دوباره باز کنید.",
    "before snapshot differs from the frozen quote":
        "وضعیت پیش از تمدید با برآورد قفل‌شده یکسان نیست؛ صفحه را تازه کنید.",
    "after snapshot differs from the frozen quote":
        "وضعیت پس از تمدید با برآورد قفل‌شده یکسان نیست؛ صفحه را تازه کنید.",
    "addon_type must be gib or day":
        "نوع افزونه معتبر نیست؛ فقط افزایش حجم یا افزایش زمان مجاز است.",
    "order snapshot is required":
        "اطلاعات سفارش در دسترس نیست؛ سفارش را از فهرست سفارش‌ها باز کنید.",
    "order_id is required":
        "شناسهٔ سفارش مشخص نیست؛ سفارش را از فهرست سفارش‌ها باز کنید.",
    "order belongs to another actor":
        "این سفارش به شما تعلق ندارد.",
    "order is not awaiting payment":
        "این سفارش در وضعیت انتظار پرداخت نیست.",
    "payment attempt and order snapshots are required":
        "اطلاعات پرداخت یا سفارش در دسترس نیست؛ صفحه را دوباره باز کنید.",
    "payment instructions are private to the payer":
        "اطلاعات پرداخت فقط برای پرداخت‌کنندهٔ همین سفارش نمایش داده می‌شود.",
    "partial or mixed receipt amounts are not accepted":
        "مبلغ رسید باید دقیقاً برابر مبلغ سفارش باشد؛ پرداخت جزئی یا ترکیبی پذیرفته نمی‌شود.",
    "assigned bank card snapshot is unavailable":
        "کارت مقصد برای این سفارش ثبت نشده است؛ با پشتیبانی تماس بگیرید.",
    "payer card details are incomplete":
        "اطلاعات کارت مقصد کامل نیست؛ با پشتیبانی تماس بگیرید.",
    # account_actions_ui
    "rotation_type must be sub or cred":
        "نوع چرخش معتبر نیست؛ فقط چرخش لینک اشتراک یا چرخش اعتبارنامهٔ اتصال مجاز است.",
    "actor cannot rotate this account":
        "شما مجاز به چرخش این حساب نیستید.",
    "unlimited must be boolean":
        "انتخاب حالت نامحدود معتبر نیست؛ صفحه را دوباره باز کنید.",
    "reset_usage does not accept quota value or unlimited flag":
        "در حالت بازنشانی مصرف نه مقدار حجم و نه حالت نامحدود قابل ثبت نیست.",
    "unlimited must not be combined with a finite value":
        "حالت نامحدود را نمی‌توان همراه با مقدار حجم ثبت کرد.",
    "unit must be B, KiB, MiB or GiB":
        "واحد حجم معتبر نیست؛ واحدهای مجاز بایت، کیلوبایت، مگابایت یا گیگابایت است.",
    "quota must resolve to an integer byte count":
        "مقدار حجم باید به تعداد صحیح بایت برسد؛ مقدار اعشاری مجاز نیست.",
    "mode must be reset_usage or set_quota_bytes":
        "حالت ترافیک انتخاب‌شده معتبر نیست؛ یکی از دو گزینهٔ همین صفحه را انتخاب کنید.",
    "mode must be set_expiry_utc or set_remaining_days":
        "حالت زمان حساب انتخاب‌شده معتبر نیست؛ یکی از دو گزینهٔ همین صفحه را انتخاب کنید.",
    "set_remaining_days requires a positive finite integer":
        "تعداد روز باقی‌مانده باید یک عدد صحیح و مثبت باشد.",
    "remaining days must be a positive integer":
        "تعداد روز باقی‌مانده باید یک عدد صحیح و مثبت باشد.",
    "unlimited expiry cannot include a date":
        "برای انقضای نامحدود نمی‌توان تاریخ ثبت کرد.",
    "expiry datetime is required":
        "تاریخ و زمان انقضا ارسال نشده است.",
    # access_ui. The rest of this module's messages are mapped above.
    "account not found":
        "حساب مورد نظر یافت نشد یا حذف شده است.",
    "grant access_level must be viewer or manager":
        "سطح دسترسی این واگذاری معتبر نیست؛ فقط «مشاهده‌گر» یا «مدیر حساب» مجاز است.",
    # account_link_service, surfaced by the access page's candidate list.
    "identifier must be text":
        "شناسهٔ ارسالی معتبر نیست؛ کد حساب را به‌صورت متن بفرستید.",
    # bot_admin_ui
    "is_primary must be boolean":
        "انتخاب بات اصلی بودن معتبر نیست؛ صفحه را دوباره باز کنید.",
    "primary bot cannot be assigned to a representative":
        "بات اصلی را نمی‌توان به یک نماینده نسبت داد.",
    "bot token is required":
        "توکن بات ارسال نشده است.",
    "bot token must be in <bot_id>:<secret> form":
        "قالب توکن بات معتبر نیست؛ توکن را به شکل «شناسه:کلید» بفرستید.",
    # bot_registry_service, surfaced by the bot-registration wizard. Every refusal an
    # operator can hit while registering, activating or disabling a bot gets its own
    # sentence instead of the generic fallback.
    "only the configured root may manage bot registrations":
        "ثبت و مدیریت بات فقط در اختیار مدیر کل است.",
    "this Telegram bot token is already registered":
        "این توکن بات پیش‌تر ثبت شده است؛ برای هر بات فقط یک توکن معتبر پذیرفته می‌شود.",
    "bot ID is already registered":
        "این شناسهٔ بات پیش‌تر ثبت شده است؛ اگر بات را جابه‌جا کرده‌اید ابتدا ثبت قبلی را غیرفعال کنید.",
    "representative is inactive or unavailable":
        "نمایندهٔ انتخاب‌شده فعال نیست؛ نمایندهٔ دیگری را انتخاب کنید.",
    "declared bot ID does not match Telegram getMe":
        "شناسهٔ داخل توکن با پاسخ تأیید تلگرام یکسان نیست؛ توکن را دوباره بررسی کنید.",
    "Telegram getMe verification failed":
        "تلگرام این توکن را تأیید نکرد؛ توکن را دوباره بررسی کنید.",
    "Telegram getMe returned no valid bot ID":
        "پاسخ تأیید تلگرام شناسهٔ بات معتبری نداشت؛ توکن را دوباره بررسی کنید.",
    "an existing bot credential cannot be verified safely":
        "این توکن پیش‌تر در سامانه ثبت شده و بازبینی مجدد آن ایمن نیست.",
    "only a pending bot can be activated":
        "فقط باتی که در انتظار تأیید است قابل فعال‌سازی است.",
    "only an active bot can be disabled":
        "فقط بات فعال قابل غیرفعال‌سازی است.",
    "bot registration does not exist":
        "این ثبت بات در سامانه وجود ندارد.",
    "bot token and get_me callable are required":
        "توکن بات ارسال نشده است.",
    "bot credential encryption failed":
        "ذخیرهٔ امن توکن بات انجام نشد؛ با مدیر کل هماهنگ کنید.",
    "activation preview could not be created":
        "ساخت پیش‌نمایش فعال‌سازی بات انجام نشد؛ دوباره تلاش کنید.",
    "activation confirmation is invalid, stale or expired":
        "این تأیید فعال‌سازی معتبر نیست یا منقضی شده است؛ ثبت را از ابتدا آغاز کنید.",
    "bot status is not recognised":
        "وضعیت این بات شناخته نشد؛ صفحهٔ بات‌ها را دوباره باز کنید.",
    # The enable / reassign / delete half of the bot lifecycle, so an operator never
    # sees the generic fallback while managing a bot.
    "only a disabled bot can be enabled":
        "فقط بات غیرفعال قابل فعال‌سازی مجدد است.",
    "enable preview could not be created":
        "ساخت پیش‌نمایش فعال‌سازی مجدد بات انجام نشد؛ دوباره تلاش کنید.",
    "enable confirmation is invalid, stale or expired":
        "این تأیید فعال‌سازی مجدد معتبر نیست یا منقضی شده است؛ از ابتدا آغاز کنید.",
    "another primary bot is already registered":
        "یک بات اصلی دیگر در سامانه ثبت شده است؛ ابتدا آن را غیرفعال کنید.",
    "this representative already has an active bot; disable or reassign it first":
        "این نماینده از قبل یک بات فعال دارد؛ ابتدا آن بات را غیرفعال یا منتقل کنید.",
    "the primary bot cannot be reassigned":
        "بات اصلی سامانه قابل انتقال یا مصادره نیست.",
    "reassign preview could not be created":
        "ساخت پیش‌نمایش تغییر مالکیت بات انجام نشد؛ دوباره تلاش کنید.",
    "reassign confirmation is invalid, stale or expired":
        "این تأیید تغییر مالکیت معتبر نیست یا منقضی شده است؛ از ابتدا آغاز کنید.",
    "this representative already has an active bot; disable it first":
        "این نماینده از قبل یک بات فعال دارد؛ ابتدا آن بات را غیرفعال کنید.",
    "an active bot must be disabled before deletion":
        "بات فعال را نمی‌توان حذف کرد؛ ابتدا آن را غیرفعال کنید.",
    "the primary bot cannot be deleted":
        "بات اصلی سامانه قابل حذف نیست.",
    "this bot has operational history; disable it instead of deleting it":
        "این بات سابقهٔ عملیاتی دارد و حذف نمی‌شود؛ به‌جای حذف آن را غیرفعال کنید.",
    "delete preview could not be created":
        "ساخت پیش‌نمایش حذف بات انجام نشد؛ دوباره تلاش کنید.",
    "delete confirmation is invalid, stale or expired":
        "این تأیید حذف معتبر نیست یا منقضی شده است؛ از ابتدا آغاز کنید.",
    "prepared bot action is required":
        "پیش‌نمایش عملیات بات در دسترس نیست؛ کارت بات را دوباره باز کنید.",
    "bot action result is required":
        "نتیجهٔ عملیات بات در دسترس نیست؛ فهرست بات‌ها را دوباره باز کنید.",
    "unknown bot action":
        "این عملیات بات شناخته نشد؛ کارت بات را دوباره باز کنید.",
    "verified registration snapshot is required":
        "اطلاعات ثبت‌شدهٔ بات در دسترس نیست؛ ثبت را از ابتدا آغاز کنید.",
    "only a pending verified bot can be activated":
        "فقط بات تأییدشده‌ای که در وضعیت انتظار است قابل فعال‌سازی است.",
    "verified bot username is required":
        "نام کاربری بات تأییدشده ثبت نشده است.",
    # Standard-library messages these pages can surface. The operator gets the
    # Persian sentence instead of the raw parser text.
    "invalid isoformat string":
        "قالب تاریخ و زمان معتبر نیست؛ نمونهٔ درست: 2026-12-01T12:00:00",
    "invalid date string":
        "قالب تاریخ معتبر نیست؛ نمونهٔ درست: 2026-12-01T12:00:00",
    "no time zone found":
        "منطقهٔ زمانی انتخاب‌شده شناخته نشد؛ منطقهٔ پیش‌فرض تهران استفاده می‌شود.",
    "invalid literal for int()":
        "مقدار عددی واردشده معتبر نیست.",
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
