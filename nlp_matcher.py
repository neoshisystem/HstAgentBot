import re
import sys
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

import database

sys.path.append("/root/.hermes/scripts")
import xui_manager

def normalize_text(text: str) -> str:
    if not text:
        return ""
    t = xui_manager.normalize_persian_digits(text)
    t = t.replace("ي", "ی").replace("ك", "ک").strip()
    return t

RESERVED_COMMAND_WORDS = {
    "start", "admin", "cancel", "status", "renew", "suspend", "activate",
    "deliv", "deliv_sub", "deliv_qr", "deliv_conf_qr", "qrenew", "more_plans", "main_menu",
    "link", "pricing", "setprice", "billing", "balance", "factor", "pay", "sync", "stats",
    "vless", "vmess", "trojan", "shadowsocks", "config", "configs", "sub", "subscription",
    "gig", "gigs", "gb", "day", "days", "month", "months", "pool", "yes", "no"
}

def extract_account_code(reply_text: str, msg_text: str) -> str:
    """
    Extracts account code prioritizing the replied message first,
    then falling back to the message text itself.
    Supports standard 4-digit codes, labeled IDs, and free alphanumeric identifiers (e.g. username123).
    """
    code = None
    if reply_text:
        code = xui_manager.extract_account_identifier(reply_text)
        if not code:
            m = re.search(r"(?:گزارش\s*وضعیت\s*اکانت|وضعیت\s*(?:محفوظ\s*)?اکانت|شناسه\s*اکانت|اکانت(?:\s*جدید)?|کد|email|client)\s*[:*`]*\s*([a-zA-Z0-9_\-\.]{3,32})", reply_text, re.IGNORECASE)
            if m and m.group(1).lower() not in RESERVED_COMMAND_WORDS:
                code = m.group(1)
            else:
                tokens = re.findall(r"\b([a-zA-Z0-9][a-zA-Z0-9_\-\.]{2,31})\b", reply_text)
                for tok in tokens:
                    if tok.lower() not in RESERVED_COMMAND_WORDS and not re.fullmatch(r"\d{1,2}(?:gb|g)?", tok.lower()):
                        code = tok
                        break

    if not code and msg_text:
        code = xui_manager.extract_account_identifier(msg_text)
        if not code:
            tokens = re.findall(r"\b([a-zA-Z0-9][a-zA-Z0-9_\-\.]{2,31})\b", msg_text)
            for tok in tokens:
                if tok.lower() not in RESERVED_COMMAND_WORDS and not re.fullmatch(r"\d{1,2}(?:gb|g)?", tok.lower()):
                    code = tok
                    break
            
            if not code:
                clean_t = normalize_text(msg_text).strip()
                stripped = re.sub(r"\b(استعلام|وضعیت|تمدید|وصل|قطع|اکانت|حجم|چقدر|چقدش|مونده|لینک|ساب|کیوآر|qr)\b", "", clean_t, flags=re.IGNORECASE).strip()
                if stripped and len(stripped) <= 32 and " " not in stripped and len(stripped) >= 3:
                    code = stripped

    return code

def clean_customer_name(text: str) -> str:
    if not text:
        return None
    name = xui_manager.extract_customer_name(text)
    if name:
        name_clean = normalize_text(name).lower()
        if re.search(r"\b(گیگ|روز|ماه|اکانت|تمدید|وصل|قطع|استعلام|وضعیت|شارژ|فاکتور|قیمت|پلن)\b", name_clean):
            return None
        if re.match(r"^[\d\s]+$", name_clean) or len(name_clean) < 3:
            return None
        return name.strip()
    return None

def detect_intent(text: str) -> tuple[str, dict]:
    """
    Analyzes Persian colloquial message to identify action.
    Returns (intent, params)
    """
    t = normalize_text(text).lower()
    
    # 1. New Account
    if any(k in t for k in ["اکانت جدید", "مشتری جدید", "اکانت بده", "یه اکانت", "یک اکانت"]):
        gb, days = xui_manager.parse_package_terms(t)
        name = clean_customer_name(text)
        return "new_account", {"gb": gb, "days": days, "comment": name}
        
    # 2. Suspend (Turn OFF)
    suspend_patterns = [
        r"قطع.*کن", r"قطعش.*کن", r"اتصال.*قطع", r"غیرفعال", r"ببند", r"خاموش.*کن", r"قطع.*ارتباط", r"suspend", r"disable"
    ]
    if any(re.search(p, t) for p in suspend_patterns):
        return "suspend", {}

    # 3. Activate (Turn ON)
    activate_patterns = [
        r"وصل.*کن", r"وصلش.*کن", r"اتصال.*وصل", r"فعال.*کن", r"فعالش.*کن", r"روشن.*کن", r"اکتیو", r"باز.*کن", r"resume", r"activate"
    ]
    if any(re.search(p, t) for p in activate_patterns):
        return "activate", {}

    # 4. Renewal (Add quota / time)
    renew_keywords = ["تمدید", "بزن تنگش", "شارژ", "اضافه کن", "گیگ تمدید", "گیگ اضافه", "تمدیدش کن"]
    if any(k in t for k in renew_keywords):
        gb, days = xui_manager.parse_package_terms(t)
        name = clean_customer_name(text)
        first_connect = any(k in t for k in ["اولین اتصال", "اول اتصال", "با اتصال", "کانکت"])
        return "renew", {"gb": gb, "days": days, "comment": name, "first_connect": first_connect}

    # 5. Deliverable (Configs / Subscription / QR)
    deliverable_keywords = ["کانفیگ", "کانفیگاش", "لینک", "ساب", "سابسکریپشن", "کیوآر", "qr", "کد کیوآر"]
    if any(k in t for k in deliverable_keywords):
        qr_needed = any(k in t for k in ["کیوآر", "qr", "کد"])
        conf_qr = any(k in t for k in ["کانفیگ", "کانفیگاش"]) and qr_needed
        return "deliverable", {"qr": qr_needed, "conf_qr": conf_qr}

    # 6. Status check
    status_keywords = ["چقدش مونده", "چقدرش مونده", "چقدش موندخ", "چقدر مونده", "چقد مونده", "وضعیت", "استعلام", "حجم", "چقدر حجم"]
    if (
        any(k in t for k in status_keywords)
        or not t
        or re.fullmatch(r"\d{3,4}", t)
        or re.fullmatch(r"[a-zA-Z0-9_\-]{3,32}", t)
    ):
        if t not in RESERVED_COMMAND_WORDS:
            return "status", {}

    return "unknown", {}

def build_account_keyboard(account_code: str, is_enabled: bool = True, on_first_connect: bool = False, role: str = "rep", pending_res: dict = None) -> InlineKeyboardMarkup:
    """
    Dynamic interactive account card:
    - Pending reservation status buttons (if any in queue)
    - Quick plans (is_quick=1) loaded dynamically from database
    - Status refresh, mode toggle, and deliverables
    """
    toggle_conn_text = "🛑 قطع موقت" if is_enabled else "✅ فعال‌سازی مجدد"
    toggle_conn_action = "suspend" if is_enabled else "activate"
    mode_text = "⏱ شروع: با اولین اتصال" if on_first_connect else "⏱ شروع: استاندارد (رول‌اوور)"
    
    keyboard = [
        [
            InlineKeyboardButton("📊 استعلام وضعیت لحظه‌ای", callback_data=f"status:{account_code}")
        ]
    ]

    # Check for active pending reservation
    if pending_res is None:
        try:
            pending_res = database.get_pending_reservation(account_code)
        except Exception:
            pending_res = None

    if pending_res and role in ("super_admin", "rep"):
        res_id = pending_res["id"]
        keyboard.append([
            InlineKeyboardButton("🚀 فعال‌سازی فوری رزرو (سوزاندن فعلی)", callback_data=f"force_res:{res_id}:{account_code}"),
            InlineKeyboardButton("🗑 لغو رزرو و عودت وجه", callback_data=f"cancel_res:{res_id}:{account_code}")
        ])
    
    if role in ("super_admin", "rep"):
        quick_plans = database.get_pricing_plans(quick_only=True)
        # Pair quick plans dynamically
        row = []
        for p in quick_plans:
            row.append(InlineKeyboardButton(f"⚡️ {p['name']}", callback_data=f"qrenew:{account_code}:{p['plan_code']}"))
            if len(row) == 2:
                keyboard.append(row)
                row = []
        if row:
            row.append(InlineKeyboardButton("📦 سایر تعرفه‌ها / پلن‌ها", callback_data=f"more_plans:{account_code}"))
            keyboard.append(row)
        else:
            keyboard.append([InlineKeyboardButton("📦 سایر تعرفه‌ها / پلن‌ها", callback_data=f"more_plans:{account_code}")])
            
        # Controls Row
        keyboard.append([
            InlineKeyboardButton(toggle_conn_text, callback_data=f"{toggle_conn_action}:{account_code}"),
            InlineKeyboardButton(mode_text, callback_data=f"toggle_mode:{account_code}")
        ])
    
    # Fixed Deliverables Row
    keyboard.append([
        InlineKeyboardButton("🔗 لینک ساب", callback_data=f"deliv_sub:{account_code}"),
        InlineKeyboardButton("📱 QR ساب", callback_data=f"deliv_qr:{account_code}"),
        InlineKeyboardButton("🔀 QR کانفیگ‌ها", callback_data=f"deliv_conf_qr:{account_code}")
    ])
    keyboard.append([
        InlineKeyboardButton("🔄 بازنشانی لینک ساب (ابطال قبلی)", callback_data=f"reset_sub:{account_code}")
    ])
    
    return InlineKeyboardMarkup(keyboard)

def build_more_plans_keyboard(account_code: str) -> InlineKeyboardMarkup:
    """
    Submenu for expanded pricing plans loaded dynamically from database (is_quick=0).
    """
    all_plans = database.get_pricing_plans(quick_only=False)
    more_plans = [p for p in all_plans if not p.get("is_quick")]
    
    keyboard = []
    row = []
    for p in more_plans:
        row.append(InlineKeyboardButton(f"⚡️ {p['name']}", callback_data=f"qrenew:{account_code}:{p['plan_code']}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
        
    keyboard.append([
        InlineKeyboardButton("↩️ بازگشت به منوی اصلی", callback_data=f"main_menu:{account_code}")
    ])
    return InlineKeyboardMarkup(keyboard)
