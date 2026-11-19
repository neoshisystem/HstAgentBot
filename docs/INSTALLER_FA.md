# راهنمای نصب و راه‌اندازی HstAgentBot — Installer v1.2.0

این راهنما برای نصب پیش‌تولید HstAgentBot روی **Ubuntu 24.04 یا جدیدتر** و **Debian 12 یا جدیدتر** نوشته شده است. نصب‌کننده کد را از GitHub دریافت می‌کند، مجموعه مهاجرت‌های نسخه‌بندی‌شده را بررسی می‌کند، آزمون‌های پایه و Smoke Test مهاجرت را اجرا می‌کند و سرویس سخت‌سازی‌شده systemd را راه‌اندازی می‌کند.

> این نسخه پیش‌تولید است. ابتدا آن را روی سرور آزمایشی و با دسترسی پشتیبان معتبر اجرا کنید. نصب‌کننده جایگزین بررسی تنظیمات پنل، شبکه، توکن و سیاست‌های پشتیبان‌گیری سازمان شما نیست.

## ۱. پیش‌نیازها

- Ubuntu 24.04+ یا Debian 12+ با systemd و دسترسی sudo.
- اینترنت برای دریافت مخزن، بسته‌های سیستم و ارتباط با Telegram Bot API.
- دسترسی مدیریتی به توکن Bot تلگرام و شناسه عددی Super Admin.
- فضای کافی برای محیط مجازی Python، دیتابیس و نسخه پشتیبان.
- مخزن GitHub: neoshisystem/HstAgentBot.

نصب‌کننده در صورت نیاز بسته‌های git، curl، openssl، util-linux، python3-venv، python3-pip و sqlite3 را نصب می‌کند.

## ۲. نصب سریع با curl

دستور زیر اسکریپت نصب را از شاخه recovery/scope-1 دریافت کرده و با مجوز root اجرا می‌کند:

```
curl -fsSL https://raw.githubusercontent.com/neoshisystem/HstAgentBot/recovery/scope-1/scripts/hstagentbot-installer.sh | sudo bash
```

نصب‌کننده ورودی‌های تعاملی را در صورت وجود از /dev/tty می‌خواند؛ بنابراین اجرای curl | sudo bash مانع ورود پاسخ‌ها نمی‌شود.

برای انتخاب ref:

```
curl -fsSL https://raw.githubusercontent.com/neoshisystem/HstAgentBot/recovery/scope-1/scripts/hstagentbot-installer.sh | sudo bash -s -- --ref recovery/scope-1
```

## ۳. نصب از طریق Clone مستقیم

```
git clone https://github.com/neoshisystem/HstAgentBot.git
cd HstAgentBot
sudo bash scripts/hstagentbot-installer.sh --ref recovery/scope-1
```

نصب‌کننده به‌صورت پیش‌فرض از متغیر HSTAGENTBOT_GIT_REF یا recovery/scope-1 استفاده می‌کند. گزینه --ref مقدار محیطی را override می‌کند. SHA یا Tree ثابت در اسکریپت hardcode نشده و شناسه‌های واقعی پس از checkout با دستورهای زیر به دست می‌آیند:

```
git rev-parse HEAD
git rev-parse 'HEAD^{tree}'
```

## ۴. گزینه‌های خط فرمان

| گزینه | توضیح |
|---|---|
| --help | نمایش راهنما |
| --check-only | بررسی پیش‌نیازها و checkout موجود، بدون نصب یا تغییر فایل‌ها |
| --skip-tests | رد کردن compileall، pytest پایه و Smoke Test مهاجرت |
| --update | تأیید خودکار به‌روزرسانی checkout تمیز |
| --ref <git_ref> | انتخاب branch، tag یا commit |

در به‌روزرسانی، origin بررسی می‌شود و هرگونه تغییر در فایل‌های Tracked مانع عملیات خواهد شد. پس از تأیید، ref انتخابی fetch و به‌شکل detached روی FETCH_HEAD checkout می‌شود. فایل‌های Tracked تغییرکرده بازنویسی نخواهند شد.

## ۵. ترتیب عملیات نصب

نصب‌کننده ۱۰ مرحله دارد:

1. بررسی سیستم‌عامل و پیش‌نیازها.
2. دریافت یا به‌روزرسانی امن checkout و ثبت SHA واقعی commit و tree.
3. بررسی فایل‌های حیاتی و وجود دقیق ۱۸ فایل SQL در migrations/.
4. ساخت حساب سیستمی محدود hstagentbot و محیط مجازی Python.
5. ساخت یا حفظ .env، بررسی قالب توکن و اعتبارسنجی آن با Telegram Bot API.
6. آماده‌سازی تنظیمات runtime.
7. اجرای compileall و آزمون پایه با مهاجرت‌های نسخه‌بندی‌شده غیرفعال.
8. اجرای Smoke Test مهاجرت در دیتابیس موقت.
9. تهیه بکاپ آنلاین SQLite و اجرای database.init_db() روی دیتابیس اصلی.
10. اعمال مجوزها، نصب unit سخت‌سازی‌شده systemd و راه‌اندازی سرویس.

## ۶. آزمون پایه و Smoke Test مهاجرت

### آزمون پایه

مجموعه pytest با متغیرهای زیر اجرا می‌شود:

```
V52_MIGRATIONS_ENABLED=0
DB_PATH=<temporary-test-database>
BACKUP_DIR=<temporary-test-backups>
```

ابتدا دستور python -m compileall -q . اجرا می‌شود و سپس pytest با -p no:cacheprovider -q . اجرا خواهد شد تا cache آزمایش‌ها وارد checkout نشود. دیتابیس و پوشه backup این مرحله موقت و مستقل هستند.

### Smoke Test مهاجرت

Smoke Test با V52_MIGRATIONS_ENABLED=1 و دیتابیس موقت جداگانه انجام می‌شود و موارد زیر را می‌سنجد:

- اجرای database.init_db().
- ثبت دقیق ۱۸ migration در schema_migrations.
- وجود ستون panel_id در جدول accounts.
- درج و به‌روزرسانی حساب از طریق database.upsert_account_record در اسکیمای پنل‌محور پس از Migration 003.
- موفقیت PRAGMA integrity_check.
- خالی بودن خروجی PRAGMA foreign_key_check.

دیتابیس Smoke Test پس از موفقیت حذف می‌شود. گزینه --skip-tests تمام این آزمون‌های خودکار را رد می‌کند؛ برای نصب پیش‌تولید اجرای بدون این گزینه توصیه می‌شود.

## ۷. بکاپ و مهاجرت دیتابیس اصلی

اگر دیتابیس موجود باشد، پیش از مهاجرت اصلی با SQLite Backup API و از روی اتصال SQLite یک snapshot سازگار گرفته می‌شود. فایل بکاپ در backups/ ذخیره می‌شود.

سپس دیتابیس اصلی با V52_MIGRATIONS_ENABLED=1 و از طریق database.init_db() مقداردهی و مهاجرت می‌شود. در نصب تازه، نبود دیتابیس قبلی گزارش شده و دیتابیس ایجاد خواهد شد.

پیش از نصب روی محیط واقعی، فضای کافی و امکان بازیابی از نسخه پشتیبان را مستقلاً بررسی کنید.

## ۸. حساب سرویس و مجوز فایل‌ها

حساب سیستمی hstagentbot با shell برابر /usr/sbin/nologin ساخته می‌شود و ورود تعاملی برای آن فعال نیست.

- کد برنامه و محیط مجازی .venv متعلق به root:root هستند.
- سرویس با کاربر و گروه hstagentbot اجرا می‌شود.
- مسیرهای نوشتنی سرویس به data/، backups/ و logs/ محدود می‌شوند.
- فایل .env با مجوز 0640 و گروه سرویس نگهداری می‌شود.
- فایل INSTALL_REPORT.txt ref و شناسه‌های واقعی Git را ثبت می‌کند و شامل توکن یا کلید نیست.

## ۹. توکن تلگرام و فایل .env

نصب‌کننده قالب توکن را بررسی و سپس endpoint getMe را فراخوانی می‌کند. برای جلوگیری از نمایش توکن در فهرست آرگومان‌های process، URL حاوی توکن از طریق stdin به curl --config - داده می‌شود.

در نصب مجدد، مقادیر موجود .env حفظ می‌شوند و بی‌دلیل بازنویسی نخواهند شد. توکن نامعتبر یا placeholder نیازمند ورود مجدد است.

### نگهداری BACKUP_ENCRYPTION_KEY

در صورت نبود کلید، نصب‌کننده کلید ۲۵۶ بیتی را با دستور زیر تولید می‌کند:

```
openssl rand -base64 32 | tr '+/' '-_' | tr -d '='
```

کلید قبلی در نصب مجدد حفظ می‌شود. کلید را در مخزن Git، پیام‌رسان، گزارش عمومی یا محل قابل‌دسترسی برای کاربران غیرمجاز ذخیره نکنید. نسخه پشتیبان امن و جداگانه‌ای از آن نگهداری کنید؛ بدون کلید صحیح، بازیابی داده‌های رمزگذاری‌شده ممکن است ناممکن شود.

## ۱۰. مدیریت سرویس systemd

نام سرویس hstagentbot.service است:

```
sudo systemctl status hstagentbot.service
sudo systemctl restart hstagentbot.service
sudo systemctl stop hstagentbot.service
sudo systemctl start hstagentbot.service
sudo journalctl -u hstagentbot.service -n 100 --no-pager
sudo journalctl -u hstagentbot.service -f
```

Unit با ProtectSystem=strict، NoNewPrivileges=true و PrivateTmp=true اجرا می‌شود. مسیرهای نوشتنی آن فقط data/، backups/ و logs/ هستند.

## ۱۱. مسیرهای مهم و عیب‌یابی

| مورد | مسیر یا دستور |
|---|---|
| پوشه نصب پیش‌فرض | /opt/hstagentbot |
| تنظیمات | /opt/hstagentbot/.env |
| لاگ نصب‌کننده | /var/log/hstagentbot-installer.log |
| گزارش نصب | /opt/hstagentbot/INSTALL_REPORT.txt |
| فایل unit | /etc/systemd/system/hstagentbot.service |
| وضعیت سرویس | systemctl status hstagentbot.service |
| لاگ سرویس | journalctl -u hstagentbot.service |

در صورت ناموفق بودن راه‌اندازی، ابتدا لاگ نصب‌کننده و سپس journal سرویس را بررسی کنید. هنگام ارسال گزارش، توکن، کلید رمزگذاری و سایر اطلاعات محرمانه را حذف کنید.

## ۱۲. نکات امنیتی پیش‌تولید

- توکن بات و کلید رمزگذاری را در اختیار افراد غیرمجاز قرار ندهید.
- مجوز .env را روی 0640 یا محدودتر نگه دارید.
- کلید BACKUP_ENCRYPTION_KEY را جدا از سرور و بکاپ‌های رمزگذاری‌شده نگهداری کنید.
- بکاپ را به‌صورت دوره‌ای با بازیابی آزمایشی کنترل کنید.
- پیش از به‌روزرسانی، سلامت دیتابیس و checkout را بررسی کنید.
- از --skip-tests در محیط واقعی استفاده نکنید، مگر اینکه علت و پذیرش ریسک مستند شده باشد.
- نسخه پیش‌تولید را ابتدا با اطلاعات آزمایشی اعتبارسنجی کنید.


## نسخه Plug-and-Play 1.2.0 برای v5.2.0

در نصب تازه، ورودی‌های ضروری به چهار مورد محدود شده‌اند: مسیر نصب، توکن بات، شناسه عددی ادمین و شناسه گروه گزارش‌ها (پیش‌فرض `0`). مهاجرت‌ها و رابط کاربری v5.2 به‌صورت خودکار فعال می‌شوند. آیدی تاپیک‌های قدیمی و تنظیمات شخصی ساب در نصب تازه خنثی می‌شوند؛ برای تنظیم دستی آن‌ها از `--advanced` استفاده کنید.

گزینه‌های خط فرمان:

- `--service-name hstagentbot-v52`: نام واحد systemd را تغییر می‌دهد.
- `--advanced`: تنظیم آیدی تاپیک‌ها، sandbox و گزینه‌های دامنه اشتراک را نمایش می‌دهد.

نصب‌کننده در اولویت از `/usr/bin/python3` استفاده می‌کند و هر مفسر واقع در `/root` یا `/home` را رد می‌کند. پس از ایجاد محیط مجازی و نصب بسته‌ها، `chmod -R a+rX "$VENV_DIR"` اعمال می‌شود تا کاربر غیرروت سرویس بتواند فایل‌هایی مانند `pyvenv.cfg` را بخواند.

برای رفع وابستگی هدر نسخه به دسترسی پوشه `.git`، SHA واقعی commit در `.build_info` و متغیر `BUILD_COMMIT` در `.env` ثبت می‌شود. مجوز `safe.directory` نیز برای checkout نصب‌شده ثبت می‌شود.

آزمون پایه با `BACKUP_ENCRYPTION_KEY=""` و `HSTAGENTBOT_INTEGRATIONS_PATH=""` اجرا می‌شود تا مقادیر نصب‌شده از فایل `.env` وارد harness تست پایه نشوند. پس از تست‌ها، گام ۹ مهاجرت‌ها را اعمال و سپس از طریق Telegram `getMe` شناسه بات را تأیید می‌کند؛ seed سیاست‌ها، tierها، feature flags، ثبت بات اصلی، هویت ادمین، پروفایل‌ها، پنل پیش‌فرض و اتصال تعرفه‌ها به پنل در bootstrap تکرارپذیر انجام می‌شود.
