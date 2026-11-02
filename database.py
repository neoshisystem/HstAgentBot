import os
import sys
import sqlite3
import json
from datetime import datetime, timezone

from config import (
    DB_PATH,
    REPRESENTATIVES,
    ADMIN_TELEGRAM_ID,
    MAIN_GROUP_ID,
    OPERATIONAL_LOGS_TOPIC_ID,
    BACKUPS_TOPIC_ID,
    ADMIN_DM_LOGS_TOPIC_ID
)
MY_PROJECT_GROUP_ID = int(MAIN_GROUP_ID)

# Add scripts directory to path to access xui_manager
sys.path.append("/root/.hermes/scripts")
import xui_manager

DEFAULT_PLANS = [
    ("20gb_30d", "۲۰ گیگ ۱ ماهه", 20.0, 30, 300000, 450000, 1, 1),
    ("30gb_30d", "۳۰ گیگ ۱ ماهه", 30.0, 30, 350000, 550000, 1, 2),
    ("50gb_60d", "۵۰ گیگ ۲ ماهه", 50.0, 60, 650000, 1000000, 1, 3),
    ("10gb_30d", "۱۰ گیگ ۱ ماهه", 10.0, 30, 45000, 80000, 0, 4),
    ("40gb_30d", "۴۰ گیگ ۱ ماهه", 40.0, 30, 120000, 200000, 0, 5),
    ("60gb_60d", "۶۰ گیگ ۲ ماهه", 60.0, 60, 185000, 310000, 0, 6),
    ("80gb_60d", "۸۰ گیگ ۲ ماهه", 80.0, 60, 235000, 390000, 0, 7),
    ("100gb_90d", "۱۰۰ گیگ ۳ ماهه", 100.0, 90, 290000, 480000, 0, 8),
    ("150gb_90d", "۱۵۰ گیگ ۳ ماهه", 150.0, 90, 410000, 680000, 0, 9),
]

DEFAULT_SETTINGS = [
    ("rollover_max_gb", "2.0", "سقف رول‌اوور حجم (GB)", "سقف حجم مجاز باقیمانده برای تمدید مستقیم تجمیعی به گیگابایت"),
    ("rollover_max_days", "2.0", "سقف رول‌اوور زمان (روز)", "سقف زمان مجاز باقیمانده برای تمدید مستقیم تجمیعی به روز (معادل ۴۸ ساعت)"),
    ("reserve_trigger_gb", "0.15", "آستانه فعال‌سازی رزرو (GB)", "آستانه اتمام حجم جهت فعال‌سازی خودکار بسته رزرو به گیگابایت"),
    ("watchdog_interval_sec", "300", "فاصله بررسی دیده‌بان (ثانیه)", "فاصله زمانی اجرای دیده‌بان بسته رزرو به ثانیه (پیش‌فرض ۳۰۰ ثانیه)"),
    ("renewal_lock_ttl_sec", "20", "قفل ضد دابل‌کلیک (ثانیه)", "زمان قفل ضد کلیک تکراری به ثانیه"),
    ("pool_warning_threshold", "10", "آستانه هشدار استخر آزاد", "حداقل تعداد اکانت‌های آماده استخر آزاد قبل از هشدار"),
    ("critical_audit_gb", "2.0", "آستانه حجم اکانت بحرانی (GB)", "حداکثر حجم باقیمانده برای قرارگیری در گزارش بحرانی"),
    ("critical_audit_days", "3.0", "آستانه زمان اکانت بحرانی (روز)", "حداکثر روز باقیمانده برای قرارگیری در گزارش بحرانی"),
    ("main_group_id", str(MAIN_GROUP_ID), "شناسه گروه اصلی", "شناسه عددی سوپرگروه اصلی سامانه"),
    ("operational_logs_topic_id", str(OPERATIONAL_LOGS_TOPIC_ID), "تاپیک لاگ عمومی", "شناسه تاپیک لاگ‌های عمومی سامانه"),
    ("rep_logs_topic_id", "0", "تاپیک لاگ نمایندگی", "شناسه تاپیک اختصاصی لاگ‌های نماینده"),
    ("backups_topic_id", str(BACKUPS_TOPIC_ID), "تاپیک بکاپ‌ها", "شناسه تاپیک آپلود خودکار فایل‌های پشتیبان"),
    ("admin_dm_topic_id", str(ADMIN_DM_LOGS_TOPIC_ID), "تاپیک پیوی ادمین", "شناسه تاپیک میرور گزارشات در پیوی ادمین"),
    ("admin_telegram_id", str(ADMIN_TELEGRAM_ID), "شناسه تلگرام ادمین کل", "شناسه عددی ادمین اصلی سیستم")
]

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with get_db() as conn:
        cur = conn.cursor()
        
        # 1. Representatives table (Multi-Rep architecture)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS representatives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rep_code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            telegram_id INTEGER,
            group_id INTEGER,
            topic_id INTEGER,
            scope_filter TEXT NOT NULL,
            balance_toman INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        
        # 2. Scoped Accounts table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rep_id INTEGER DEFAULT NULL,
            account_code TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            customer_name TEXT,
            sub_id TEXT,
            enable INTEGER DEFAULT 1,
            total_gb REAL DEFAULT 0,
            expiry_time INTEGER DEFAULT 0,
            owner_tg_id INTEGER DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (rep_id) REFERENCES representatives(id) ON DELETE SET NULL
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_accounts_rep_code ON accounts(rep_id, account_code)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_accounts_email ON accounts(email)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_accounts_owner ON accounts(owner_tg_id)")
        
        # 3. Audit Logs table (compliance and history)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rep_id INTEGER,
            account_code TEXT,
            email TEXT,
            action TEXT NOT NULL,
            actor_id INTEGER NOT NULL,
            actor_name TEXT,
            details_json TEXT,
            result_status TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (rep_id) REFERENCES representatives(id)
        )
        """)
        
        # 4. Bot Users table (End-Users & RBAC)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS bot_users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER UNIQUE NOT NULL,
            role TEXT NOT NULL DEFAULT 'end_user', -- 'admin', 'rep', 'end_user'
            rep_id INTEGER,
            username TEXT,
            full_name TEXT,
            is_blocked INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (rep_id) REFERENCES representatives(id)
        )
        """)
        
        # 5. Pricing Plans table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS pricing_plans (
            plan_code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            gb REAL NOT NULL,
            days INTEGER NOT NULL,
            rep_price INTEGER NOT NULL,
            user_price INTEGER NOT NULL,
            is_quick INTEGER DEFAULT 0,
            sort_order INTEGER DEFAULT 0
        )
        """)
        
        # 6. Financial Ledger table
        cur.execute("""
        CREATE TABLE IF NOT EXISTS financial_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rep_id INTEGER NOT NULL,
            account_code TEXT,
            email TEXT,
            plan_code TEXT,
            transaction_type TEXT NOT NULL, -- 'debit', 'credit', 'adjust'
            amount INTEGER NOT NULL,
            balance_after INTEGER NOT NULL,
            description TEXT NOT NULL,
            actor_id INTEGER NOT NULL,
            actor_name TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (rep_id) REFERENCES representatives(id)
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ledger_rep ON financial_ledger(rep_id)")
        
        # 7. User Settings (e.g. On First Connect toggle per user/session)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            first_connect_mode INTEGER DEFAULT 0
        )
        """)

        # 8. Dynamic System Settings (hot-reloadable system config)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS system_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            label TEXT,
            description TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # 9. Reserved Packages Queue (anti-debt early renewal buffer)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS reserved_packages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rep_id INTEGER,
            account_code TEXT NOT NULL,
            email TEXT NOT NULL,
            plan_code TEXT NOT NULL,
            plan_name TEXT NOT NULL,
            gb REAL NOT NULL,
            days INTEGER NOT NULL,
            on_first_connect INTEGER DEFAULT 0,
            charge_amount INTEGER NOT NULL,
            actor_id INTEGER NOT NULL,
            actor_name TEXT,
            status TEXT DEFAULT 'pending', -- 'pending', 'activated', 'cancelled'
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            activated_at TIMESTAMP,
            FOREIGN KEY (rep_id) REFERENCES representatives(id)
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_reserved_status_acc ON reserved_packages(status, account_code)")

        # Migrations check
        cols_acc = [c[1] for c in cur.execute("PRAGMA table_info(accounts)").fetchall()]
        if "owner_tg_id" not in cols_acc:
            cur.execute("ALTER TABLE accounts ADD COLUMN owner_tg_id INTEGER DEFAULT NULL")
            
        cols_rep = [c[1] for c in cur.execute("PRAGMA table_info(representatives)").fetchall()]
        if "balance_toman" not in cols_rep:
            cur.execute("ALTER TABLE representatives ADD COLUMN balance_toman INTEGER DEFAULT 0")

        # Seed default settings
        for s in DEFAULT_SETTINGS:
            cur.execute("""
            INSERT INTO system_settings (key, value, label, description)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                label = excluded.label,
                description = excluded.description
            """, s)

        # Seed default representative(s) if configured
        if REPRESENTATIVES:
            for rep_key, rep_info in REPRESENTATIVES.items():
                cur.execute("""
                INSERT INTO representatives (id, rep_code, name, telegram_id, group_id, topic_id, scope_filter, is_active)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(rep_code) DO UPDATE SET
                    telegram_id = excluded.telegram_id,
                    group_id = excluded.group_id,
                    topic_id = excluded.topic_id,
                    scope_filter = excluded.scope_filter
                """, (
                    rep_info["id"],
                    rep_info["code"],
                    rep_info["name"],
                    rep_info["telegram_id"],
                    rep_info.get("allowed_group_id", rep_info.get("group_id", 0)),
                    rep_info.get("allowed_topic_id", rep_info.get("topic_id", 0)),
                    rep_info["scope_filter"]
                ))
            
        # Seed default pricing plans
        for p in DEFAULT_PLANS:
            cur.execute("""
            INSERT INTO pricing_plans (plan_code, name, gb, days, rep_price, user_price, is_quick, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(plan_code) DO UPDATE SET
                name = excluded.name,
                gb = excluded.gb,
                days = excluded.days,
                is_quick = excluded.is_quick,
                sort_order = excluded.sort_order
            """, p)
            
        conn.commit()

# --- Dynamic System Settings Management ---

def get_setting(key: str, default=None, cast_type=None):
    with get_db() as conn:
        row = conn.cursor().execute("SELECT value FROM system_settings WHERE key = ?", (key,)).fetchone()
        if not row or row["value"] is None:
            return default
        val = row["value"]
        if cast_type is not None:
            try:
                if cast_type is bool:
                    return str(val).lower() in ("1", "true", "yes")
                return cast_type(val)
            except Exception:
                return default
        return val

def set_setting(key: str, value, actor_id=0, actor_name="admin"):
    str_val = str(value).strip()
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE system_settings SET value = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?", (str_val, key))
        conn.commit()
    log_audit(
        rep_id=None, account_code=None, email=None, action="update_setting",
        actor_id=actor_id, actor_name=actor_name, details={"key": key, "new_value": str_val},
        result_status="success"
    )
    return str_val

def get_all_settings():
    with get_db() as conn:
        rows = conn.cursor().execute("SELECT * FROM system_settings ORDER BY key ASC").fetchall()
        return [dict(r) for r in rows]

# --- Reserved Packages Queue Management ---

def create_reservation(rep_id, account_code, email, plan_code, plan_name, gb, days, on_first_connect, charge_amount, actor_id, actor_name):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO reserved_packages (rep_id, account_code, email, plan_code, plan_name, gb, days, on_first_connect, charge_amount, actor_id, actor_name, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """, (rep_id, account_code, email, plan_code, plan_name, float(gb), int(days), int(on_first_connect), int(charge_amount), actor_id, actor_name))
        conn.commit()
        res_id = cur.lastrowid
        log_audit(
            rep_id=rep_id, account_code=account_code, email=email, action="create_reservation",
            actor_id=actor_id, actor_name=actor_name,
            details={"res_id": res_id, "plan_code": plan_code, "plan_name": plan_name, "gb": gb, "days": days, "charge": charge_amount},
            result_status="success"
        )
        return res_id

def get_pending_reservation(account_code: str):
    if not account_code:
        return None
    with get_db() as conn:
        cur = conn.cursor()
        row = cur.execute("""
        SELECT * FROM reserved_packages
        WHERE status = 'pending' AND (LOWER(account_code) = ? OR LOWER(email) LIKE ?)
        ORDER BY id DESC LIMIT 1
        """, (account_code.lower(), f"%{account_code.lower()}%")).fetchone()
        return dict(row) if row else None

def get_reservation_by_id(res_id: int):
    with get_db() as conn:
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM reserved_packages WHERE id = ?", (res_id,)).fetchone()
        return dict(row) if row else None

def get_all_pending_reservations():
    with get_db() as conn:
        cur = conn.cursor()
        rows = cur.execute("""
        SELECT r.*, rep.name as rep_name
        FROM reserved_packages r
        LEFT JOIN representatives rep ON r.rep_id = rep.id
        WHERE r.status = 'pending'
        ORDER BY r.id ASC
        """).fetchall()
        return [dict(r) for r in rows]

def activate_reservation(res_id: int, actor_id=0, actor_name="system"):
    with get_db() as conn:
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM reserved_packages WHERE id = ?", (res_id,)).fetchone()
        if not row:
            return None
        cur.execute("UPDATE reserved_packages SET status = 'activated', activated_at = CURRENT_TIMESTAMP WHERE id = ?", (res_id,))
        conn.commit()
        log_audit(
            rep_id=row["rep_id"], account_code=row["account_code"], email=row["email"],
            action="activate_reservation", actor_id=actor_id, actor_name=actor_name,
            details={"res_id": res_id, "plan_code": row["plan_code"], "plan_name": row["plan_name"]},
            result_status="success"
        )
        return dict(row)

def cancel_reservation(res_id: int, actor_id=0, actor_name="admin"):
    with get_db() as conn:
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM reserved_packages WHERE id = ? AND status = 'pending'", (res_id,)).fetchone()
        if not row:
            return None
        res_data = dict(row)
        cur.execute("UPDATE reserved_packages SET status = 'cancelled' WHERE id = ?", (res_id,))
        conn.commit()
        
    # Refund the charged amount to representative balance if applicable
    refund_amount = res_data.get("charge_amount", 0)
    new_bal = 0
    if res_data.get("rep_id") is not None and refund_amount > 0:
        new_bal = record_ledger_entry(
            rep_id=res_data["rep_id"],
            account_code=res_data["account_code"],
            email=res_data["email"],
            plan_code=res_data["plan_code"],
            tx_type="credit",
            amount=refund_amount,
            description=f"لغو بسته رزرو {res_data['plan_name']} و عودت وجه",
            actor_id=actor_id,
            actor_name=actor_name
        )
    log_audit(
        rep_id=res_data.get("rep_id"), account_code=res_data["account_code"], email=res_data["email"],
        action="cancel_reservation_refund", actor_id=actor_id, actor_name=actor_name,
        details={"res_id": res_id, "refund_amount": refund_amount, "new_balance": new_bal},
        result_status="success"
    )
    return res_data, new_bal

def get_pricing_plans(quick_only=False):
    with get_db() as conn:
        cur = conn.cursor()
        if quick_only:
            rows = cur.execute("SELECT * FROM pricing_plans WHERE is_quick = 1 ORDER BY sort_order ASC").fetchall()
        else:
            rows = cur.execute("SELECT * FROM pricing_plans ORDER BY sort_order ASC").fetchall()
        return [dict(r) for r in rows]

def get_plan(plan_code):
    with get_db() as conn:
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM pricing_plans WHERE plan_code = ?", (plan_code,)).fetchone()
        return dict(row) if row else None

def update_plan_price(plan_code, rep_price=None, user_price=None):
    with get_db() as conn:
        cur = conn.cursor()
        if rep_price is not None and user_price is not None:
            cur.execute("UPDATE pricing_plans SET rep_price = ?, user_price = ? WHERE plan_code = ?", (rep_price, user_price, plan_code))
        elif rep_price is not None:
            cur.execute("UPDATE pricing_plans SET rep_price = ? WHERE plan_code = ?", (rep_price, plan_code))
        elif user_price is not None:
            cur.execute("UPDATE pricing_plans SET user_price = ? WHERE plan_code = ?", (user_price, plan_code))
        conn.commit()
        log_audit(
            rep_id=None, account_code=plan_code, email=None, action="update_plan_price",
            actor_id=0, actor_name="admin", details={"rep_price": rep_price, "user_price": user_price},
            result_status="success"
        )
        return get_plan(plan_code)

def add_or_update_plan(plan_code, name, gb, days, rep_price, user_price, is_quick=0):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO pricing_plans (plan_code, name, gb, days, rep_price, user_price, is_quick, sort_order)
        VALUES (?, ?, ?, ?, ?, ?, ?, (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM pricing_plans))
        ON CONFLICT(plan_code) DO UPDATE SET
            name = excluded.name,
            gb = excluded.gb,
            days = excluded.days,
            rep_price = excluded.rep_price,
            user_price = excluded.user_price,
            is_quick = excluded.is_quick
        """, (plan_code, name, float(gb), int(days), int(rep_price), int(user_price), int(is_quick)))
        conn.commit()
        log_audit(
            rep_id=None, account_code=plan_code, email=None, action="add_or_update_plan",
            actor_id=0, actor_name="admin", details={"name": name, "gb": gb, "days": days, "rep_price": rep_price, "user_price": user_price, "is_quick": is_quick},
            result_status="success"
        )
        return get_plan(plan_code)

def delete_plan(plan_code):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM pricing_plans WHERE plan_code = ?", (plan_code,))
        conn.commit()

# --- Representatives & User Management ---

def get_all_reps():
    with get_db() as conn:
        cur = conn.cursor()
        rows = cur.execute("""
        SELECT r.*, COUNT(a.id) as account_count 
        FROM representatives r 
        LEFT JOIN accounts a ON a.rep_id = r.id 
        GROUP BY r.id
        """).fetchall()
        return [dict(r) for r in rows]

def get_rep_by_id(rep_id):
    with get_db() as conn:
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM representatives WHERE id = ?", (rep_id,)).fetchone()
        return dict(row) if row else None

def get_rep_by_code(rep_code):
    if not rep_code:
        return None
    with get_db() as conn:
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM representatives WHERE LOWER(rep_code) = ?", (rep_code.lower(),)).fetchone()
        return dict(row) if row else None

def get_all_active_reps():
    with get_db() as conn:
        cur = conn.cursor()
        rows = cur.execute("SELECT * FROM representatives WHERE is_active = 1").fetchall()
        return [dict(r) for r in rows]

def add_or_update_rep(rep_code, name, telegram_id, group_id, topic_id, scope_filter):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO representatives (rep_code, name, telegram_id, group_id, topic_id, scope_filter, is_active)
        VALUES (?, ?, ?, ?, ?, ?, 1)
        ON CONFLICT(rep_code) DO UPDATE SET
            name = excluded.name,
            telegram_id = excluded.telegram_id,
            group_id = excluded.group_id,
            topic_id = excluded.topic_id,
            scope_filter = excluded.scope_filter
        """, (rep_code, name, telegram_id, group_id, topic_id, scope_filter))
        conn.commit()

def get_all_bot_users():
    with get_db() as conn:
        cur = conn.cursor()
        rows = cur.execute("""
        SELECT u.*, r.name as rep_name, COUNT(a.id) as linked_accounts_count
        FROM bot_users u
        LEFT JOIN representatives r ON u.rep_id = r.id
        LEFT JOIN accounts a ON a.owner_tg_id = u.telegram_id
        GROUP BY u.telegram_id
        ORDER BY u.created_at DESC
        """).fetchall()
        return [dict(r) for r in rows]

def reassign_account(account_code, new_rep_id=None, new_owner_tg_id=None):
    with get_db() as conn:
        cur = conn.cursor()
        acc = cur.execute("SELECT * FROM accounts WHERE LOWER(account_code) = ? OR LOWER(email) LIKE ? LIMIT 1", (account_code.lower(), f"%{account_code.lower()}%")).fetchone()
        if not acc:
            return {"status": "error", "message": "اکانت یافت نشد"}
        
        updates = []
        params = []
        if new_rep_id is not None:
            updates.append("rep_id = ?")
            params.append(new_rep_id)
        if new_owner_tg_id is not None:
            updates.append("owner_tg_id = ?")
            params.append(new_owner_tg_id)
            
        if not updates:
            return {"status": "ok", "account": dict(acc)}
            
        params.append(acc["id"])
        sql = f"UPDATE accounts SET {', '.join(updates)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?"
        cur.execute(sql, tuple(params))
        conn.commit()
        
        updated = cur.execute("SELECT * FROM accounts WHERE id = ?", (acc["id"],)).fetchone()
        log_audit(
            rep_id=new_rep_id, account_code=account_code, email=acc["email"],
            action="reassign_account", actor_id=0, actor_name="admin",
            details={"new_rep_id": new_rep_id, "new_owner_tg_id": new_owner_tg_id},
            result_status="success"
        )
        return {"status": "ok", "account": dict(updated)}

# --- Idempotency & Accidental Double-Click Guard ---

_RENEWAL_LOCKS = {}

def acquire_renewal_lock(account_code: str, ttl_seconds: int = 20) -> bool:
    """
    Prevents accidental double-clicks or rapid successive duplicate renewal requests.
    Returns True if lock acquired, False if locked (must wait).
    """
    import time
    now = time.time()
    code = str(account_code).strip().lower()
    last_time = _RENEWAL_LOCKS.get(code, 0)
    if now - last_time < ttl_seconds:
        return False
    _RENEWAL_LOCKS[code] = now
    return True

# --- Financial Ledger & Accounting ---

def record_ledger_entry(rep_id, account_code, email, plan_code, tx_type, amount, description, actor_id, actor_name):
    """
    Records a financial transaction and atomically updates the representative's balance_toman.
    tx_type: 'debit' increases debt (cost of renewal/new account)
             'credit' decreases debt (payment/settlement)
    """
    if not rep_id:
        return 0

    with get_db() as conn:
        cur = conn.cursor()
        rep = cur.execute("SELECT balance_toman FROM representatives WHERE id = ?", (rep_id,)).fetchone()
        curr_bal = (rep["balance_toman"] or 0) if rep else 0
        
        if tx_type == "debit":
            new_bal = curr_bal + amount
        elif tx_type == "credit":
            new_bal = curr_bal - amount
        else: # adjust
            new_bal = amount
            
        cur.execute("""
        INSERT INTO financial_ledger (rep_id, account_code, email, plan_code, transaction_type, amount, balance_after, description, actor_id, actor_name)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            rep_id,
            str(account_code),
            str(email or ""),
            str(plan_code or ""),
            tx_type,
            int(amount),
            new_bal,
            description,
            actor_id,
            actor_name
        ))
        
        cur.execute("UPDATE representatives SET balance_toman = ? WHERE id = ?", (new_bal, rep_id))
        conn.commit()
        log_audit(
            rep_id=rep_id, account_code=str(account_code or ""), email=str(email or ""),
            action=f"financial_{tx_type}", actor_id=actor_id, actor_name=actor_name,
            details={"amount": amount, "plan_code": plan_code, "description": description, "balance_after": new_bal},
            result_status="success"
        )
        return new_bal

def register_rep_payment(rep_id, amount, description, actor_id, actor_name):
    return record_ledger_entry(
        rep_id=rep_id,
        account_code="",
        email="",
        plan_code="",
        tx_type="credit",
        amount=amount,
        description=description,
        actor_id=actor_id,
        actor_name=actor_name
    )

def get_rep_financial_summary(rep_id):
    with get_db() as conn:
        cur = conn.cursor()
        rep = cur.execute("SELECT * FROM representatives WHERE id = ?", (rep_id,)).fetchone()
        if not rep:
            return None
        total_debits = cur.execute("SELECT COALESCE(SUM(amount), 0) FROM financial_ledger WHERE rep_id = ? AND transaction_type = 'debit'", (rep_id,)).fetchone()[0]
        total_credits = cur.execute("SELECT COALESCE(SUM(amount), 0) FROM financial_ledger WHERE rep_id = ? AND transaction_type = 'credit'", (rep_id,)).fetchone()[0]
        tx_count = cur.execute("SELECT COUNT(*) FROM financial_ledger WHERE rep_id = ?", (rep_id,)).fetchone()[0]
        return {
            "rep_name": rep["name"],
            "rep_code": rep["rep_code"],
            "balance_toman": rep["balance_toman"] or 0,
            "total_debits": total_debits,
            "total_credits": total_credits,
            "tx_count": tx_count
        }

def get_recent_ledger_entries(rep_id=None, limit=10):
    with get_db() as conn:
        cur = conn.cursor()
        if rep_id:
            rows = cur.execute("SELECT * FROM financial_ledger WHERE rep_id = ? ORDER BY id DESC LIMIT ?", (rep_id, limit)).fetchall()
        else:
            rows = cur.execute("SELECT * FROM financial_ledger ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

# --- User Settings (On First Connect Mode) ---

def get_user_first_connect_mode(user_id: int) -> bool:
    with get_db() as conn:
        cur = conn.cursor()
        row = cur.execute("SELECT first_connect_mode FROM user_settings WHERE user_id = ?", (user_id,)).fetchone()
        return bool(row["first_connect_mode"]) if row else False

def toggle_user_first_connect_mode(user_id: int) -> bool:
    with get_db() as conn:
        cur = conn.cursor()
        curr = get_user_first_connect_mode(user_id)
        new_val = 0 if curr else 1
        cur.execute("""
        INSERT INTO user_settings (user_id, first_connect_mode)
        VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET first_connect_mode = excluded.first_connect_mode
        """, (user_id, new_val))
        conn.commit()
        return bool(new_val)

# --- 3-Layer Access Control (RBAC) & Accounts ---

def get_user_role(user_id: int, chat_id: int, thread_id: int = None) -> tuple[str, dict | None]:
    """
    Returns (role, context_dict)
    Roles:
      'super_admin': Full global access
      'rep': Scoped to rep's accounts
      'end_user': Scoped to personal linked account(s)
      'unauthorized_group': Ignore/silence in group chats
    """
    try:
        user_id = int(user_id)
        chat_id = int(chat_id)
        thread_id = int(thread_id) if thread_id is not None else None
    except (ValueError, TypeError):
        pass

    active_reps = get_all_active_reps()

    # ----------------------------------------------------
    # A) GROUP CHATS & SANDBOX TOPICS (chat_id < 0)
    # ----------------------------------------------------
    if chat_id < 0:
        # 1. End-User Sandbox Topic in My Project
        try:
            sandbox_topic_val = get_setting("end_user_sandbox_topic_id", "55")
            sandbox_topic_id = int(sandbox_topic_val) if sandbox_topic_val else 55
        except (ValueError, TypeError):
            sandbox_topic_id = 55

        sandbox_group_val = get_setting("end_user_sandbox_group_id", str(MY_PROJECT_GROUP_ID))
        try:
            sandbox_group_id = int(sandbox_group_val) if sandbox_group_val else int(MY_PROJECT_GROUP_ID)
        except (ValueError, TypeError):
            sandbox_group_id = int(MY_PROJECT_GROUP_ID)

        if (chat_id == sandbox_group_id or chat_id == MY_PROJECT_GROUP_ID) and thread_id == sandbox_topic_id:
            # Simulate End-User experience for whoever speaks here (even Super Admin)
            return "end_user", None

        # 2. Dedicated representative topic (e.g. topic 20852, topic 54 demo_rep, etc.)
        for r in active_reps:
            if chat_id == r["group_id"] and thread_id == r["topic_id"]:
                # If sender is that representative OR super admin simulating that rep
                if user_id == r["telegram_id"] or user_id == ADMIN_TELEGRAM_ID:
                    return "rep", r
                return "unauthorized_group", None

        # 3. Other group topics:
        if user_id == ADMIN_TELEGRAM_ID:
            return "super_admin", None

        return "unauthorized_group", None

    # ----------------------------------------------------
    # B) PRIVATE DM (chat_id > 0)
    # Authenticate strictly based on user_id
    # ----------------------------------------------------
    # 1. Super Admin in private DM
    if user_id == ADMIN_TELEGRAM_ID:
        return "super_admin", None

    # 2. Representative in private DM
    for r in active_reps:
        if user_id == r["telegram_id"]:
            return "rep", r

    # 3. End-User in private DM
    return "end_user", None

def resolve_account_rep_id(email: str) -> int | None:
    """
    Checks if email belongs to any active representative based on scope_filter.
    Returns rep_id if matched, otherwise None (for admin personal or unassigned accounts).
    """
    if not email:
        return None
    email_lower = str(email).lower()
    active_reps = get_all_active_reps()
    for r in active_reps:
        scope = (r.get("scope_filter") or "").strip().lower()
        if not scope or scope == "all":
            continue
        tokens = [t.strip() for t in scope.split(",") if t.strip()]
        if any(tok in email_lower for tok in tokens):
            return r["id"]
    return None

def link_user_to_account(account_code: str, user_tg_id: int, username: str = "", full_name: str = "") -> dict:
    with get_db() as conn:
        cur = conn.cursor()
        acc = cur.execute("SELECT * FROM accounts WHERE LOWER(account_code) = ? OR LOWER(email) LIKE ? LIMIT 1", (account_code.lower(), f"%{account_code.lower()}%")).fetchone()
        if not acc:
            return {"status": "error", "message": "Account not found"}
        cur.execute("UPDATE accounts SET owner_tg_id = ? WHERE id = ?", (user_tg_id, acc["id"]))
        cur.execute("""
        INSERT INTO bot_users (telegram_id, role, rep_id, username, full_name)
        VALUES (?, 'end_user', ?, ?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET
            username = excluded.username,
            full_name = excluded.full_name
        """, (user_tg_id, acc["rep_id"], username, full_name))
        conn.commit()
        return {"status": "ok", "account": dict(acc)}

def get_user_accounts(user_tg_id: int) -> list:
    with get_db() as conn:
        cur = conn.cursor()
        rows = cur.execute("SELECT * FROM accounts WHERE owner_tg_id = ?", (user_tg_id,)).fetchall()
        return [dict(r) for r in rows]

# --- Account Scoping & Syncing ---

def sync_rep_accounts(rep_code=None):
    with get_db() as conn:
        cur = conn.cursor()
        if rep_code:
            rep = cur.execute("SELECT id, scope_filter FROM representatives WHERE rep_code = ?", (rep_code,)).fetchone()
            target_reps = [rep] if rep else []
        else:
            target_reps = cur.execute("SELECT id, scope_filter FROM representatives WHERE is_active = 1").fetchall()

        if not target_reps:
            return 0

        s = xui_manager.get_session()
        r = s.get(f"{xui_manager.BASE_URL}/panel/api/clients/list")
        if not r.ok:
            return 0
        all_clients = r.json().get("obj", [])

        count = 0
        for rep in target_reps:
            rep_id = rep["id"]
            scope = (rep["scope_filter"] or "").strip().lower()

            if scope == "all":
                scoped_clients = all_clients
            else:
                tokens = [t.strip() for t in scope.split(",") if t.strip()]
                scoped_clients = [
                    c for c in all_clients
                    if any(tok in str(c.get("email", "")).lower() or tok in str(c.get("comment", "")).lower() for tok in tokens)
                ]

            for c in scoped_clients:
                email = c.get("email", "")
                code = xui_manager.extract_account_identifier(email) or email
                comment = c.get("comment", "")
                sub_id = c.get("subId", "")
                enable = 1 if c.get("enable", True) else 0
                total_gb = round(c.get("totalGB", 0) / (1024**3), 2)
                exp_time = c.get("expiryTime", 0)
                tg_id = c.get("tgId") or None

                cur.execute("""
                INSERT INTO accounts (rep_id, account_code, email, customer_name, sub_id, enable, total_gb, expiry_time, owner_tg_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(email) DO UPDATE SET
                    rep_id = excluded.rep_id,
                    account_code = excluded.account_code,
                    customer_name = excluded.customer_name,
                    sub_id = excluded.sub_id,
                    enable = excluded.enable,
                    total_gb = excluded.total_gb,
                    expiry_time = excluded.expiry_time,
                    owner_tg_id = CASE WHEN excluded.owner_tg_id IS NOT NULL THEN excluded.owner_tg_id ELSE accounts.owner_tg_id END,
                    updated_at = CURRENT_TIMESTAMP
                """, (rep_id, str(code), email, comment, sub_id, enable, total_gb, exp_time, tg_id))
                count += 1

        conn.commit()
        return count

def sync_all_active_reps() -> int:
    reps = get_all_active_reps()
    total = 0
    for r in reps:
        total += sync_rep_accounts(r["rep_code"])
    return total

def upsert_account_record(rep_id, email, account_code, customer_name="", sub_id="", enable=1, total_gb=0, expiry_time=0, owner_tg_id=None):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO accounts (rep_id, account_code, email, customer_name, sub_id, enable, total_gb, expiry_time, owner_tg_id, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(email) DO UPDATE SET
            rep_id = excluded.rep_id,
            account_code = excluded.account_code,
            customer_name = CASE WHEN excluded.customer_name != '' THEN excluded.customer_name ELSE customer_name END,
            sub_id = CASE WHEN excluded.sub_id != '' THEN excluded.sub_id ELSE sub_id END,
            enable = excluded.enable,
            total_gb = CASE WHEN excluded.total_gb > 0 THEN excluded.total_gb ELSE total_gb END,
            expiry_time = excluded.expiry_time,
            owner_tg_id = CASE WHEN excluded.owner_tg_id IS NOT NULL THEN excluded.owner_tg_id ELSE accounts.owner_tg_id END,
            updated_at = CURRENT_TIMESTAMP
        """, (rep_id, str(account_code), email, customer_name, sub_id, enable, total_gb, expiry_time, owner_tg_id))
        conn.commit()

def is_account_in_rep_scope(rep_id, query):
    q = str(query).strip().lower()
    with get_db() as conn:
        cur = conn.cursor()
        row = cur.execute("""
        SELECT * FROM accounts 
        WHERE rep_id = ? AND (
            LOWER(email) = ? OR 
            LOWER(account_code) = ? OR 
            LOWER(sub_id) = ? OR 
            LOWER(email) LIKE ? OR
            account_code LIKE ?
        )
        LIMIT 1
        """, (rep_id, q, q, q, f"%{q}%", f"%{q}%")).fetchone()
        if row:
            return dict(row)
    return None

def log_audit(rep_id, account_code, email, action, actor_id, actor_name, details, result_status):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO audit_logs (rep_id, account_code, email, action, actor_id, actor_name, details_json, result_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            rep_id,
            str(account_code),
            str(email or ""),
            action,
            actor_id,
            actor_name,
            json.dumps(details, ensure_ascii=False) if isinstance(details, (dict, list)) else str(details),
            result_status
        ))
        conn.commit()

def sync_all_active_reps():
    return sync_rep_accounts(None)

if __name__ == "__main__":
    init_db()
    count = sync_all_active_reps()
    print(f"Database initialized and synced {count} accounts successfully.")
    plans = get_pricing_plans()
    print(f"Loaded {len(plans)} pricing plans.")
