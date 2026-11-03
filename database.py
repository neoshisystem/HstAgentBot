import os
import sys
import sqlite3
import json
import secrets
import time
import uuid
from contextlib import contextmanager
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
from server_integrations import load_integration
xui_manager = load_integration("xui_manager")

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

def _insert_audit(cur, rep_id, account_code, email, action, actor_id, actor_name, details, result_status):
    cur.execute("""
        INSERT INTO audit_logs (rep_id, account_code, email, action, actor_id, actor_name, details_json, result_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        rep_id, str(account_code or ""), str(email or ""), action, actor_id,
        actor_name, json.dumps(details, ensure_ascii=False) if isinstance(details, (dict, list)) else str(details),
        result_status
    ))


def _record_rep_ledger_entry(cur, rep_id, account_code, email, plan_code, tx_type, amount, description, actor_id, actor_name):
    amount = int(amount)
    if amount <= 0:
        raise ValueError("Financial ledger amount must be a positive integer")
    if tx_type not in ("debit", "credit"):
        raise ValueError("Unsupported representative transaction type")
    rep = cur.execute(
        "SELECT balance_toman, credit_limit_toman, is_active FROM representatives WHERE id = ?",
        (rep_id,)
    ).fetchone()
    if not rep:
        raise ValueError("Representative not found")
    current = int(rep["balance_toman"] or 0)
    if tx_type == "debit":
        if not bool(rep["is_active"]):
            raise ValueError("Representative is inactive; new debits are not allowed")
        limit = int(rep["credit_limit_toman"] if rep["credit_limit_toman"] is not None else 5_000_000)
        if limit > 0 and current + amount > limit:
            raise ValueError(
                f"Representative credit limit exceeded: current={current}, charge={amount}, limit={limit}"
            )
    new_balance = current + amount if tx_type == "debit" else current - amount
    cur.execute("""
        INSERT INTO financial_ledger
        (rep_id, account_code, email, plan_code, transaction_type, amount, balance_after, description, actor_id, actor_name)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        rep_id, str(account_code or ""), str(email or ""), str(plan_code or ""),
        tx_type, amount, new_balance, description, actor_id, actor_name
    ))
    cur.execute("UPDATE representatives SET balance_toman = ? WHERE id = ?", (new_balance, rep_id))
    return new_balance


def _credit_user_wallet(cur, user_id, amount, description, actor_id, actor_name):
    amount = int(amount)
    if amount <= 0:
        raise ValueError("Wallet credit amount must be a positive integer")
    cur.execute("""
        INSERT INTO user_wallets (user_id, balance_toman, updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id) DO UPDATE SET
            balance_toman = user_wallets.balance_toman + excluded.balance_toman,
            updated_at = CURRENT_TIMESTAMP
    """, (user_id, amount))
    row = cur.execute("SELECT balance_toman FROM user_wallets WHERE user_id = ?", (user_id,)).fetchone()
    new_balance = int(row["balance_toman"])
    cur.execute("""
        INSERT INTO financial_ledger
        (rep_id, user_id, transaction_type, amount, balance_after, description, actor_id, actor_name)
        VALUES (NULL, ?, 'credit', ?, ?, ?, ?, ?)
    """, (user_id, amount, new_balance, description, actor_id, actor_name))
    return new_balance


@contextmanager
def get_db():
    """Yield a configured SQLite connection and always close it.

    The context commits on success and rolls back on failure. Callers may still
    explicitly commit within the context when a result must be durable before
    a subsequent operation.
    """
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
def init_db():
    db_parent = os.path.dirname(DB_PATH)
    if db_parent:
        os.makedirs(db_parent, exist_ok=True)
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
            rep_id INTEGER DEFAULT NULL,
            user_id INTEGER DEFAULT NULL,
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
        cur.execute("CREATE INDEX IF NOT EXISTS idx_ledger_user ON financial_ledger(user_id)")
        
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

        # 10. Card-to-Card System Settings
        cur.execute("""
        CREATE TABLE IF NOT EXISTS card_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            card_number TEXT NOT NULL,
            card_holder TEXT NOT NULL,
            bank_name TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cur.execute("""
        INSERT OR IGNORE INTO card_settings (id, card_number, card_holder, bank_name)
        VALUES (1, '6037-9918-0000-0000', 'مدیریت حساب', 'بانک ملی')
        """)

        # 11. End-User Personal Wallets
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_wallets (
            user_id INTEGER PRIMARY KEY,
            balance_toman INTEGER DEFAULT 0,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)

        # 12. Payment Receipts (Dual-role Card-to-Card verification queue)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS payment_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tracking_code TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL, -- 'rep' or 'end_user'
            rep_id INTEGER,
            amount_toman INTEGER NOT NULL,
            receipt_photo_id TEXT,
            receipt_note TEXT,
            status TEXT DEFAULT 'pending', -- 'pending', 'approved', 'rejected'
            rejection_reason TEXT,
            reviewed_by INTEGER,
            reviewed_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (rep_id) REFERENCES representatives(id)
        )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_receipts_status ON payment_receipts(status)")

        # 13. Cross-process operation locks for sensitive portal actions
        cur.execute("""
        CREATE TABLE IF NOT EXISTS operation_locks (
            lock_key TEXT PRIMARY KEY,
            token TEXT NOT NULL,
            expires_at REAL NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS user_action_intents (
            intent_id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            action_type TEXT NOT NULL CHECK (action_type IN ('buy', 'renew')),
            account_code TEXT,
            account_email TEXT,
            plan_code TEXT NOT NULL,
            plan_name TEXT NOT NULL,
            gb REAL NOT NULL,
            days INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            operation_lock_key TEXT,
            operation_lock_token TEXT,
            result_note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at REAL NOT NULL
        )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_user_action_intents_user_status "
            "ON user_action_intents(user_id, status, expires_at)"
        )

        # Migrations check
        cols_acc = [c[1] for c in cur.execute("PRAGMA table_info(accounts)").fetchall()]
        if "owner_tg_id" not in cols_acc:
            cur.execute("ALTER TABLE accounts ADD COLUMN owner_tg_id INTEGER DEFAULT NULL")
            
        cols_rep = [c[1] for c in cur.execute("PRAGMA table_info(representatives)").fetchall()]
        if "balance_toman" not in cols_rep:
            cur.execute("ALTER TABLE representatives ADD COLUMN balance_toman INTEGER DEFAULT 0")
        if "credit_limit_toman" not in cols_rep:
            cur.execute("ALTER TABLE representatives ADD COLUMN credit_limit_toman INTEGER DEFAULT 5000000")

        cols_plans = [c[1] for c in cur.execute("PRAGMA table_info(pricing_plans)").fetchall()]
        if "is_active" not in cols_plans:
            cur.execute("ALTER TABLE pricing_plans ADD COLUMN is_active INTEGER DEFAULT 1")

        led_info = cur.execute("PRAGMA table_info(financial_ledger)").fetchall()
        rep_col = next((c for c in led_info if c[1] == "rep_id"), None)
        if rep_col and rep_col[3] == 1:  # notnull is set
            cur.execute("""
            CREATE TABLE financial_ledger_migration (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                rep_id INTEGER DEFAULT NULL,
                user_id INTEGER DEFAULT NULL,
                account_code TEXT,
                email TEXT,
                plan_code TEXT,
                transaction_type TEXT NOT NULL,
                amount INTEGER NOT NULL,
                balance_after INTEGER NOT NULL,
                description TEXT NOT NULL,
                actor_id INTEGER NOT NULL,
                actor_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (rep_id) REFERENCES representatives(id)
            )
            """)
            cur.execute("""
            INSERT INTO financial_ledger_migration (id, rep_id, account_code, email, plan_code, transaction_type, amount, balance_after, description, actor_id, actor_name, created_at)
            SELECT id, rep_id, account_code, email, plan_code, transaction_type, amount, balance_after, description, actor_id, actor_name, created_at FROM financial_ledger
            """)
            cur.execute("DROP TABLE financial_ledger")
            cur.execute("ALTER TABLE financial_ledger_migration RENAME TO financial_ledger")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ledger_rep ON financial_ledger(rep_id)")
            cur.execute("CREATE INDEX IF NOT EXISTS idx_ledger_user ON financial_ledger(user_id)")
        else:
            cols_led = [c[1] for c in led_info]
            if "user_id" not in cols_led:
                cur.execute("ALTER TABLE financial_ledger ADD COLUMN user_id INTEGER DEFAULT NULL")

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
    """Create one pending reservation and charge the representative in one transaction."""
    charge_amount = int(charge_amount)
    if charge_amount < 0:
        raise ValueError("Reservation charge cannot be negative")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        existing = cur.execute("""
            SELECT id FROM reserved_packages
            WHERE status = 'pending' AND LOWER(account_code) = LOWER(?)
            LIMIT 1
        """, (account_code,)).fetchone()
        if existing:
            raise ValueError("A pending reservation already exists for this account")

        if rep_id is not None and charge_amount > 0:
            rep = cur.execute(
                "SELECT balance_toman, credit_limit_toman FROM representatives WHERE id = ?",
                (rep_id,)
            ).fetchone()
            if not rep:
                raise ValueError("Representative not found")
            current_debt = int(rep["balance_toman"] or 0)
            credit_limit = int(rep["credit_limit_toman"] if rep["credit_limit_toman"] is not None else 5000000)
            if credit_limit > 0 and current_debt + charge_amount > credit_limit:
                raise ValueError("Representative credit limit would be exceeded")

        cur.execute("""
            INSERT INTO reserved_packages
            (rep_id, account_code, email, plan_code, plan_name, gb, days, on_first_connect, charge_amount, actor_id, actor_name, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')
        """, (
            rep_id, account_code, email, plan_code, plan_name, float(gb), int(days),
            int(on_first_connect), charge_amount, actor_id, actor_name
        ))
        res_id = cur.lastrowid

        if rep_id is not None and charge_amount > 0:
            new_balance = _record_rep_ledger_entry(
                cur, rep_id, account_code, email, plan_code, "debit", charge_amount,
                f"رزرو بسته {plan_name}", actor_id, actor_name
            )
        else:
            new_balance = 0

        _insert_audit(
            cur, rep_id, account_code, email, "create_reservation",
            actor_id, actor_name,
            {"res_id": res_id, "plan_code": plan_code, "plan_name": plan_name,
             "gb": gb, "days": days, "charge": charge_amount, "balance_after": new_balance},
            "success"
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
    """Mark a pending reservation activated once; return None on stale/replayed IDs."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        row = cur.execute(
            "SELECT * FROM reserved_packages WHERE id = ? AND status = 'pending'",
            (res_id,)
        ).fetchone()
        if not row:
            return None
        cur.execute("""
            UPDATE reserved_packages
            SET status = 'activated', activated_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'pending'
        """, (res_id,))
        if cur.rowcount != 1:
            return None
        data = dict(row)
        data["status"] = "activated"
        _insert_audit(
            cur, rep_id=row["rep_id"], account_code=row["account_code"], email=row["email"],
            action="activate_reservation", actor_id=actor_id, actor_name=actor_name,
            details={"res_id": res_id, "plan_code": row["plan_code"], "plan_name": row["plan_name"]},
            result_status="success"
        )
        return data
def cancel_reservation(res_id: int, actor_id=0, actor_name="admin"):
    """Cancel one pending reservation and refund its representative atomically."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        activation_lock = cur.execute(
            "SELECT 1 FROM operation_locks WHERE lock_key = ? AND expires_at > ?",
            (f"reservation-activation:{int(res_id)}", time.time())
        ).fetchone()
        if activation_lock:
            return None
        row = cur.execute(
            "SELECT * FROM reserved_packages WHERE id = ? AND status = 'pending'",
            (res_id,)
        ).fetchone()
        if not row:
            return None

        res_data = dict(row)
        cur.execute(
            "UPDATE reserved_packages SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
            (res_id,)
        )
        if cur.rowcount != 1:
            return None

        refund_amount = int(res_data.get("charge_amount") or 0)
        new_bal = 0
        if res_data.get("rep_id") is not None and refund_amount > 0:
            new_bal = _record_rep_ledger_entry(
                cur, rep_id=res_data["rep_id"], account_code=res_data["account_code"],
                email=res_data["email"], plan_code=res_data["plan_code"],
                tx_type="credit", amount=refund_amount,
                description=f"لغو بسته رزرو {res_data['plan_name']} و عودت وجه",
                actor_id=actor_id, actor_name=actor_name
            )
        _insert_audit(
            cur, rep_id=res_data.get("rep_id"), account_code=res_data["account_code"],
            email=res_data["email"], action="cancel_reservation_refund",
            actor_id=actor_id, actor_name=actor_name,
            details={"res_id": res_id, "refund_amount": refund_amount, "new_balance": new_bal},
            result_status="success"
        )
        return res_data, new_bal
def get_pricing_plans(quick_only=False, active_only=False):
    with get_db() as conn:
        cur = conn.cursor()
        clauses = []
        if active_only:
            clauses.append("is_active = 1")
        if quick_only:
            clauses.append("is_quick = 1")
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = cur.execute(f"SELECT * FROM pricing_plans {where_sql} ORDER BY sort_order ASC").fetchall()
        return [dict(r) for r in rows]

def toggle_plan_active(plan_code: str) -> int:
    with get_db() as conn:
        cur = conn.cursor()
        p = cur.execute("SELECT is_active FROM pricing_plans WHERE plan_code = ?", (plan_code,)).fetchone()
        curr = p["is_active"] if (p and p["is_active"] is not None) else 1
        new_val = 0 if curr == 1 else 1
        cur.execute("UPDATE pricing_plans SET is_active = ? WHERE plan_code = ?", (new_val, plan_code))
        conn.commit()
        return new_val

def get_plan(plan_code):
    with get_db() as conn:
        cur = conn.cursor()
        row = cur.execute("SELECT * FROM pricing_plans WHERE plan_code = ?", (plan_code,)).fetchone()
        return dict(row) if row else None

def update_plan_price(plan_code, rep_price=None, user_price=None):
    if rep_price is not None and int(rep_price) <= 0:
        raise ValueError("Representative price must be positive")
    if user_price is not None and int(user_price) <= 0:
        raise ValueError("User price must be positive")
    with get_db() as conn:
        cur = conn.cursor()
        if rep_price is not None and user_price is not None:
            cur.execute(
                "UPDATE pricing_plans SET rep_price = ?, user_price = ? WHERE plan_code = ?",
                (int(rep_price), int(user_price), plan_code)
            )
        elif rep_price is not None:
            cur.execute("UPDATE pricing_plans SET rep_price = ? WHERE plan_code = ?", (int(rep_price), plan_code))
        elif user_price is not None:
            cur.execute("UPDATE pricing_plans SET user_price = ? WHERE plan_code = ?", (int(user_price), plan_code))
        else:
            raise ValueError("At least one price must be supplied")
        if cur.rowcount != 1:
            return None
    return get_plan(plan_code)
def add_or_update_plan(plan_code, name, gb, days, rep_price, user_price, is_quick=0):
    plan_code = str(plan_code or "").strip()
    name = str(name or "").strip()
    gb, days = float(gb), int(days)
    rep_price, user_price = int(rep_price), int(user_price)
    if not plan_code or not name or gb <= 0 or days <= 0 or rep_price <= 0 or user_price <= 0:
        raise ValueError("Plan code/name, volume, duration and both prices must be valid positive values")
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
        """, (plan_code, name, gb, days, rep_price, user_price, int(bool(is_quick))))
        conn.commit()
        log_audit(
            rep_id=None, account_code=plan_code, email=None, action="add_or_update_plan",
            actor_id=0, actor_name="admin",
            details={"name": name, "gb": gb, "days": days, "rep_price": rep_price, "user_price": user_price, "is_quick": is_quick},
            result_status="success"
        )
        return get_plan(plan_code)
def delete_plan(plan_code: str) -> bool:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        pending = cur.execute(
            "SELECT 1 FROM reserved_packages WHERE plan_code = ? AND status = 'pending' LIMIT 1",
            (plan_code,)
        ).fetchone()
        if pending:
            raise ValueError("Cannot delete a plan while pending reservations use it")
        cur.execute("DELETE FROM pricing_plans WHERE plan_code = ?", (plan_code,))
        return cur.rowcount == 1
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
    if not account_code or ((new_rep_id is None) == (new_owner_tg_id is None)):
        return {"status": "error", "message": "یک مقصد معتبر (نماینده یا کاربر) باید مشخص شود."}
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        row = cur.execute("""
            SELECT * FROM accounts
            WHERE LOWER(account_code) = LOWER(?) OR LOWER(email) = LOWER(?)
            LIMIT 1
        """, (account_code, account_code)).fetchone()
        if not row:
            return {"status": "error", "message": "اکانت یافت نشد."}
        if new_rep_id is not None:
            rep = cur.execute("SELECT id FROM representatives WHERE id = ? AND is_active = 1", (new_rep_id,)).fetchone()
            if not rep:
                return {"status": "error", "message": "نماینده مقصد یافت نشد یا فعال نیست."}
            cur.execute(
                "UPDATE accounts SET rep_id = ?, owner_tg_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_rep_id, row["id"])
            )
        else:
            user = cur.execute("SELECT telegram_id FROM bot_users WHERE telegram_id = ?", (int(new_owner_tg_id),)).fetchone()
            if not user:
                return {"status": "error", "message": "کاربر مقصد در سیستم ثبت نشده است."}
            cur.execute(
                "UPDATE accounts SET owner_tg_id = ?, rep_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (int(new_owner_tg_id), row["id"])
            )
        return {"status": "ok", "account_code": row["account_code"]}
def acquire_operation_lock(lock_key: str, ttl_seconds: int | None = 300) -> str | None:
    """Acquire a DB-backed operation lease.

    Pass ttl_seconds=None for an unresolved external side effect. Such a lock
    never expires automatically and must be resolved by a Super Admin after
    inspecting the external panel and local state.
    """
    key = str(lock_key or "").strip()
    if not key:
        raise ValueError("Operation lock key is required")
    if ttl_seconds is None:
        expires_at = 253402300799.0  # 9999-12-31 UTC; effectively persistent
    else:
        expires_at = time.time() + max(1, int(ttl_seconds))
    now = time.time()
    token = uuid.uuid4().hex
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("DELETE FROM operation_locks WHERE expires_at <= ?", (now,))
        row = cur.execute(
            "SELECT token FROM operation_locks WHERE lock_key = ?", (key,)
        ).fetchone()
        if row:
            return None
        cur.execute(
            "INSERT INTO operation_locks (lock_key, token, expires_at) VALUES (?, ?, ?)",
            (key, token, expires_at)
        )
        return token


def get_operation_locks() -> list[dict]:
    """List current operation leases without exposing their private ownership tokens."""
    with get_db() as conn:
        rows = conn.execute("""
            SELECT lock_key, expires_at, created_at
            FROM operation_locks
            WHERE expires_at > ?
            ORDER BY created_at ASC
        """, (time.time(),)).fetchall()
        return [dict(row) for row in rows]


def resolve_operation_lock(
    lock_key: str, actor_id: int, actor_name: str, evidence_note: str
) -> bool:
    """Release an unresolved operation lock after an administrator records evidence."""
    key = str(lock_key or "").strip()
    note = str(evidence_note or "").strip()
    if not key or len(note) < 15:
        raise ValueError("A lock key and evidence note of at least 15 characters are required")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        row = cur.execute(
            "SELECT lock_key, created_at FROM operation_locks WHERE lock_key = ?",
            (key,)
        ).fetchone()
        if not row:
            return False
        cur.execute(
            "DELETE FROM operation_locks WHERE lock_key = ?",
            (key,)
        )
        deleted = cur.rowcount
        if deleted != 1:
            return False
        _insert_audit(
            cur, rep_id=None, account_code="", email="", action="resolve_operation_lock",
            actor_id=actor_id, actor_name=actor_name,
            details={"lock_key": key, "lock_created_at": row["created_at"], "evidence_note": note},
            result_status="success"
        )
        return True


def release_operation_lock(lock_key: str, token: str) -> bool:
    """Release a lease only when the supplied token still owns it."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM operation_locks WHERE lock_key = ? AND token = ?",
            (str(lock_key), str(token))
        )
        return cur.rowcount == 1


def acquire_renewal_lock(account_code: str, ttl_seconds: int = 20) -> bool:
    """Prevent concurrent repeated renewal requests for the same account."""
    code = str(account_code or "").strip().lower()
    return bool(acquire_operation_lock(f"renewal:{code}", ttl_seconds))

# --- Financial Ledger & Accounting ---

def record_ledger_entry(rep_id, account_code, email, plan_code, tx_type, amount, description, actor_id, actor_name):
    """Atomically updates representative balance and its ledger entry."""
    if not rep_id:
        if int(amount) <= 0:
            raise ValueError("Financial amount must be positive")
        return 0
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        new_balance = _record_rep_ledger_entry(
            cur, rep_id, account_code, email, plan_code, tx_type, amount,
            description, actor_id, actor_name
        )
        _insert_audit(
            cur, rep_id, account_code, email, f"financial_{tx_type}",
            actor_id, actor_name,
            {"amount": int(amount), "plan_code": plan_code, "description": description, "balance_after": new_balance},
            "success"
        )
        return new_balance
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

record_payment = register_rep_payment

def record_charge(rep_id, account_code, email, plan_code, amount, description, actor_id=0, actor_name="Admin"):
    return record_ledger_entry(
        rep_id=rep_id,
        account_code=account_code,
        email=email,
        plan_code=plan_code,
        tx_type="debit",
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

def get_rep_balance(rep_id: int) -> int:
    with get_db() as conn:
        cur = conn.cursor()
        rep = cur.execute("SELECT balance_toman FROM representatives WHERE id = ?", (rep_id,)).fetchone()
        return (rep["balance_toman"] or 0) if rep else 0

def get_recent_ledger_entries(rep_id=None, limit=10):
    with get_db() as conn:
        cur = conn.cursor()
        query = """
        SELECT l.*, 
               r.name as rep_name,
               u.full_name as user_full_name,
               u.username as user_username
        FROM financial_ledger l
        LEFT JOIN representatives r ON l.rep_id = r.id
        LEFT JOIN bot_users u ON l.user_id = u.telegram_id
        """
        if rep_id:
            query += " WHERE l.rep_id = ? ORDER BY l.id DESC LIMIT ?"
            rows = cur.execute(query, (rep_id, limit)).fetchall()
        else:
            query += " ORDER BY l.id DESC LIMIT ?"
            rows = cur.execute(query, (limit,)).fetchall()
        return [dict(r) for r in rows]

def get_financial_ledger_paginated(rep_id=None, page=1, page_size=10) -> tuple[list, int, int]:
    page_size = max(1, min(int(page_size), 100))
    page = max(1, int(page))
    with get_db() as conn:
        cur = conn.cursor()
        base_where = "1=1"
        params = []
        if rep_id is not None:
            base_where += " AND l.rep_id = ?"
            params.append(rep_id)

        total = cur.execute(
            f"SELECT COUNT(*) FROM financial_ledger l WHERE {base_where}", tuple(params)
        ).fetchone()[0]
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        offset = (page - 1) * page_size

        query = f"""
        SELECT l.*,
               r.name as rep_name,
               r.rep_code as rep_code,
               u.full_name as user_full_name,
               u.username as user_username
        FROM financial_ledger l
        LEFT JOIN representatives r ON l.rep_id = r.id
        LEFT JOIN bot_users u ON l.user_id = u.telegram_id
        WHERE {base_where}
        ORDER BY l.id DESC
        LIMIT ? OFFSET ?
        """
        rows = cur.execute(query, tuple(params + [page_size, offset])).fetchall()
        return [dict(r) for r in rows], total, total_pages
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

def get_user_owned_account(user_id: int, account_code_or_email: str) -> dict | None:
    """Return an exact account only when it belongs to the requesting Telegram user."""
    identifier = str(account_code_or_email or "").strip().lower()
    if not identifier:
        return None
    with get_db() as conn:
        row = conn.execute("""
            SELECT * FROM accounts
            WHERE owner_tg_id = ? AND (LOWER(account_code) = ? OR LOWER(email) = ?)
            LIMIT 1
        """, (int(user_id), identifier, identifier)).fetchone()
        return dict(row) if row else None


def link_user_to_account(account_code: str, user_tg_id: int, username: str = "", full_name: str = "", allow_reassign: bool = False) -> dict:
    """Link an exact account identifier without allowing cross-user account takeover."""
    identifier = str(account_code or "").strip().lower()
    if not identifier:
        return {"status": "error", "message": "Account identifier is required"}
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        acc = cur.execute("""
            SELECT * FROM accounts
            WHERE LOWER(account_code) = ? OR LOWER(email) = ?
            ORDER BY id DESC LIMIT 1
        """, (identifier, identifier)).fetchone()
        if not acc:
            return {"status": "error", "message": "Account not found"}
        owner_id = acc["owner_tg_id"]
        if owner_id is not None and int(owner_id) != int(user_tg_id) and not allow_reassign:
            return {"status": "error", "message": "Account is already linked to another Telegram user"}
        cur.execute(
            "UPDATE accounts SET owner_tg_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (int(user_tg_id), acc["id"])
        )
        cur.execute("""
            INSERT INTO bot_users (telegram_id, role, rep_id, username, full_name)
            VALUES (?, 'end_user', ?, ?, ?)
            ON CONFLICT(telegram_id) DO UPDATE SET
                username = CASE WHEN excluded.username != '' THEN excluded.username ELSE bot_users.username END,
                full_name = CASE WHEN excluded.full_name != '' THEN excluded.full_name ELSE bot_users.full_name END
        """, (int(user_tg_id), acc["rep_id"], username or "", full_name or ""))
        return {"status": "ok", "account": dict(acc)}
def get_user_accounts(user_tg_id: int) -> list:
    with get_db() as conn:
        cur = conn.cursor()
        rows = cur.execute("SELECT * FROM accounts WHERE owner_tg_id = ?", (user_tg_id,)).fetchall()
        return [dict(r) for r in rows]

get_user_linked_accounts = get_user_accounts

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

def sync_all_active_reps_detailed() -> dict:
    """Reconcile panel accounts against active representative scopes without arbitrary reassignment."""
    reps = get_all_active_reps()
    if not reps:
        return {"status": "success", "count": 0, "ambiguous": 0, "unmatched": 0, "message": "No active representatives"}

    try:
        session = xui_manager.get_session()
        response = session.get(f"{xui_manager.BASE_URL}/panel/api/clients/list", timeout=20)
        if not response.ok:
            return {
                "status": "error", "count": 0, "ambiguous": 0, "unmatched": 0,
                "message": f"3X-UI returned HTTP {response.status_code}"
            }
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("obj"), list):
            return {
                "status": "error", "count": 0, "ambiguous": 0, "unmatched": 0,
                "message": "3X-UI response did not contain a client list"
            }
        clients = [c for c in payload["obj"] if isinstance(c, dict) and c.get("email")]
    except Exception as exc:
        return {
            "status": "error", "count": 0, "ambiguous": 0, "unmatched": 0,
            "message": f"Failed to read 3X-UI clients: {exc}"
        }

    rep_scopes = []
    for rep in reps:
        scope = str(rep.get("scope_filter") or "").strip().lower()
        tokens = [token.strip() for token in scope.split(",") if token.strip()]
        rep_scopes.append((rep, scope, tokens))

    count = ambiguous = unmatched = 0
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        existing_rows = cur.execute("SELECT email, rep_id, owner_tg_id FROM accounts").fetchall()
        existing = {str(row["email"]).lower(): dict(row) for row in existing_rows}

        for client in clients:
            email = str(client["email"]).strip()
            email_l = email.lower()
            comment = str(client.get("comment") or "")
            candidates = []
            for rep, scope, tokens in rep_scopes:
                if scope == "all" or any(
                    token in email_l or token in comment.lower() for token in tokens
                ):
                    candidates.append(rep)

            previous = existing.get(email_l)
            previous_rep_id = previous.get("rep_id") if previous else None
            selected_rep_id = None

            if len(candidates) == 1:
                selected_rep_id = candidates[0]["id"]
            elif len(candidates) > 1:
                candidate_ids = {item["id"] for item in candidates}
                if previous_rep_id in candidate_ids:
                    selected_rep_id = previous_rep_id
                else:
                    ambiguous += 1
                    continue
            else:
                unmatched += 1
                # Retain an existing assignment, but do not invent a new one.
                selected_rep_id = previous_rep_id

            code = xui_manager.extract_account_identifier(email) or email
            total_gb = round(float(client.get("totalGB") or 0) / (1024 ** 3), 2)
            expiry_time = int(client.get("expiryTime") or 0)
            enabled = 1 if client.get("enable", True) else 0
            sub_id = str(client.get("subId") or "")
            tg_id = client.get("tgId") or (previous.get("owner_tg_id") if previous else None)

            cur.execute("""
                INSERT INTO accounts
                    (rep_id, account_code, email, customer_name, sub_id, enable,
                     total_gb, expiry_time, owner_tg_id, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(email) DO UPDATE SET
                    rep_id = excluded.rep_id,
                    account_code = excluded.account_code,
                    customer_name = excluded.customer_name,
                    sub_id = excluded.sub_id,
                    enable = excluded.enable,
                    total_gb = excluded.total_gb,
                    expiry_time = excluded.expiry_time,
                    owner_tg_id = CASE
                        WHEN excluded.owner_tg_id IS NOT NULL THEN excluded.owner_tg_id
                        ELSE accounts.owner_tg_id
                    END,
                    updated_at = CURRENT_TIMESTAMP
            """, (
                selected_rep_id, str(code), email, comment, sub_id, enabled,
                total_gb, expiry_time, tg_id
            ))
            count += 1

    return {
        "status": "success", "count": count, "ambiguous": ambiguous,
        "unmatched": unmatched,
        "message": "Reconciliation completed without arbitrary scope reassignment"
    }


def sync_all_active_reps() -> int:
    """Backward-compatible wrapper for callers that only need a count."""
    result = sync_all_active_reps_detailed()
    return int(result.get("count", 0)) if result.get("status") == "success" else 0


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
    """Resolve an exact account identifier only within the representative's scope."""
    q = str(query or "").strip().lower()
    if not q:
        return None
    with get_db() as conn:
        row = conn.execute("""
            SELECT * FROM accounts
            WHERE rep_id = ? AND (
                LOWER(email) = ? OR
                LOWER(account_code) = ? OR
                LOWER(sub_id) = ?
            )
            LIMIT 1
        """, (rep_id, q, q, q)).fetchone()
        return dict(row) if row else None

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


# ==========================================
# Phase 4: Financial & Dual-Wallet Operations
# ==========================================

def get_card_info() -> dict:
    with get_db() as conn:
        cur = conn.cursor()
        row = cur.execute("SELECT card_number, card_holder, bank_name, updated_at FROM card_settings WHERE id = 1").fetchone()
        if row:
            return dict(row)
    return {"card_number": "6037-9918-0000-0000", "card_holder": "مدیریت حساب", "bank_name": "بانک ملی"}

def update_card_info(card_number: str, card_holder: str, bank_name: str):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO card_settings (id, card_number, card_holder, bank_name, updated_at)
        VALUES (1, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(id) DO UPDATE SET
            card_number = excluded.card_number,
            card_holder = excluded.card_holder,
            bank_name = excluded.bank_name,
            updated_at = CURRENT_TIMESTAMP
        """, (card_number, card_holder, bank_name))
        conn.commit()

def get_user_wallet_balance(user_id: int) -> int:
    with get_db() as conn:
        cur = conn.cursor()
        row = cur.execute("SELECT balance_toman FROM user_wallets WHERE user_id = ?", (user_id,)).fetchone()
        return row["balance_toman"] if row else 0

def record_user_payment(user_id: int, amount: int, description: str, actor_id: int, actor_name: str = "") -> int:
    """Credit a user wallet and ledger in one serialized SQLite transaction."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        return _credit_user_wallet(cur, user_id, amount, description, actor_id, actor_name)



def create_user_action_intent(
    user_id: int,
    action_type: str,
    plan_code: str,
    plan_name: str,
    gb: float,
    days: int,
    amount: int,
    account_code: str = "",
    account_email: str = "",
    ttl_seconds: int = 900,
) -> str:
    """Create one opaque, expiring idempotency key for a single user menu action."""
    action = str(action_type or "").strip().lower()
    if action not in ("buy", "renew"):
        raise ValueError("Unsupported user action type")
    amount, days, gb = int(amount), int(days), float(gb)
    if amount <= 0 or days <= 0 or gb <= 0 or not str(plan_code or "").strip():
        raise ValueError("A valid plan snapshot and positive price are required")
    code = str(account_code or "").strip()
    email = str(account_email or "").strip()
    if action == "renew" and (not code or not email):
        raise ValueError("Renewal intent requires the canonical account code and email")

    intent_id = secrets.token_hex(16)
    expires_at = time.time() + max(60, int(ttl_seconds))
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO user_action_intents
            (intent_id, user_id, action_type, account_code, account_email,
             plan_code, plan_name, gb, days, amount, status, expires_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
        """, (
            intent_id, int(user_id), action, code or None, email or None,
            str(plan_code), str(plan_name), gb, days, amount, expires_at
        ))
    return intent_id


def get_user_action_intent(intent_id: str, user_id: int) -> dict | None:
    """Fetch an intent only for its owning Telegram user; never expose lock tokens."""
    key = str(intent_id or "").strip()
    if not key:
        return None
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM user_action_intents WHERE intent_id = ? AND user_id = ?",
            (key, int(user_id))
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result.pop("operation_lock_token", None)
        return result


def begin_user_action_intent(
    intent_id: str,
    user_id: int,
    expected_action: str,
) -> dict:
    """Atomically consume an intent, acquire its durable lock and debit the wallet.

    A consumed token is never executable again. A new menu creates a new token,
    allowing a deliberate later purchase or renewal of the same plan.
    """
    key = str(intent_id or "").strip()
    action = str(expected_action or "").strip().lower()
    if not key or action not in ("buy", "renew"):
        raise ValueError("A valid intent token and action are required")

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        row = cur.execute(
            "SELECT * FROM user_action_intents WHERE intent_id = ? AND user_id = ?",
            (key, int(user_id))
        ).fetchone()
        if not row:
            return {"status": "not_found"}
        intent = dict(row)
        if intent["action_type"] != action:
            return {"status": "wrong_action"}
        if intent["status"] != "pending":
            return {"status": "already_processed", "intent_status": intent["status"]}
        now = time.time()
        if float(intent["expires_at"]) <= now:
            cur.execute(
                "UPDATE user_action_intents SET status = 'expired', updated_at = CURRENT_TIMESTAMP "
                "WHERE intent_id = ? AND status = 'pending'",
                (key,)
            )
            return {"status": "expired"}

        plan = cur.execute(
            "SELECT name, gb, days, user_price, is_active FROM pricing_plans WHERE plan_code = ?",
            (intent["plan_code"],)
        ).fetchone()
        if (
            not plan
            or not int(plan["is_active"] or 0)
            or int(plan["user_price"]) != int(intent["amount"])
            or str(plan["name"]) != str(intent["plan_name"])
            or float(plan["gb"]) != float(intent["gb"])
            or int(plan["days"]) != int(intent["days"])
        ):
            cur.execute(
                "UPDATE user_action_intents SET status = 'cancelled', result_note = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE intent_id = ? AND status = 'pending'",
                ("Plan changed or became inactive after the menu was displayed", key)
            )
            return {"status": "plan_changed"}

        if action == "renew":
            owner = cur.execute("""
                SELECT 1 FROM accounts
                WHERE owner_tg_id = ?
                  AND LOWER(account_code) = LOWER(?)
                  AND LOWER(email) = LOWER(?)
                LIMIT 1
            """, (
                int(user_id), str(intent["account_code"] or ""),
                str(intent["account_email"] or "")
            )).fetchone()
            if not owner:
                cur.execute(
                    "UPDATE user_action_intents SET status = 'cancelled', result_note = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE intent_id = ? AND status = 'pending'",
                    ("Account ownership changed or could not be re-verified", key)
                )
                return {"status": "not_owned"}
            lock_key = f"account-renewal:{str(intent['account_code']).lower()}"
        else:
            lock_key = f"user-buy:{int(user_id)}"

        # The lock and wallet mutation are in this same transaction. A second
        # intent that collides with an active operation is cancelled; it must
        # not unexpectedly execute later from a stale button.
        cur.execute("DELETE FROM operation_locks WHERE expires_at <= ?", (now,))
        existing = cur.execute(
            "SELECT 1 FROM operation_locks WHERE lock_key = ?", (lock_key,)
        ).fetchone()
        if existing:
            cur.execute(
                "UPDATE user_action_intents SET status = 'cancelled', result_note = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE intent_id = ? AND status = 'pending'",
                ("Another operation currently owns the account/user lock; reopen the menu", key)
            )
            return {"status": "busy"}

        wallet = cur.execute(
            "SELECT balance_toman FROM user_wallets WHERE user_id = ?",
            (int(user_id),)
        ).fetchone()
        current = int(wallet["balance_toman"] or 0) if wallet else 0
        amount = int(intent["amount"])
        if current < amount:
            return {"status": "insufficient_funds", "balance": current, "required": amount}

        new_balance = current - amount
        cur.execute("""
            UPDATE user_wallets SET balance_toman = ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND balance_toman >= ?
        """, (new_balance, int(user_id), amount))
        if cur.rowcount != 1:
            return {"status": "insufficient_funds", "balance": current, "required": amount}

        cur.execute("""
            INSERT INTO financial_ledger
            (rep_id, user_id, account_code, email, plan_code, transaction_type,
             amount, balance_after, description, actor_id, actor_name)
            VALUES (NULL, ?, ?, ?, ?, 'debit', ?, ?, ?, ?, 'User')
        """, (
            int(user_id), intent["account_code"], intent["account_email"],
            intent["plan_code"], amount,
            new_balance,
            (f"خرید سرویس {intent['plan_name']}" if action == "buy" else
             f"تمدید سرویس {intent['account_code']} ({intent['plan_name']})"),
            int(user_id)
        ))

        lock_token = uuid.uuid4().hex
        cur.execute(
            "INSERT INTO operation_locks (lock_key, token, expires_at) VALUES (?, ?, ?)",
            (lock_key, lock_token, 253402300799.0)
        )
        cur.execute("""
            UPDATE user_action_intents
            SET status = 'processing', operation_lock_key = ?, operation_lock_token = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE intent_id = ? AND user_id = ? AND status = 'pending'
        """, (lock_key, lock_token, key, int(user_id)))
        if cur.rowcount != 1:
            raise RuntimeError("Could not atomically claim the user action intent")

        intent["status"] = "processing"
        return {
            "status": "started",
            "intent": intent,
            "lock_key": lock_key,
            "lock_token": lock_token,
            "new_balance": new_balance,
        }


def _verify_user_action_lock(cur, intent_id: str, user_id: int, lock_key: str, token: str):
    intent = cur.execute(
        "SELECT * FROM user_action_intents WHERE intent_id = ? AND user_id = ?",
        (str(intent_id), int(user_id))
    ).fetchone()
    if not intent or intent["status"] != "processing":
        raise ValueError("User action intent is not in processing state")
    if str(intent["operation_lock_key"] or "") != str(lock_key):
        raise ValueError("User action intent lock key mismatch")
    if not secrets.compare_digest(str(intent["operation_lock_token"] or ""), str(token)):
        raise ValueError("User action intent lock ownership mismatch")
    lock = cur.execute(
        "SELECT token FROM operation_locks WHERE lock_key = ?", (str(lock_key),)
    ).fetchone()
    if not lock or not secrets.compare_digest(str(lock["token"]), str(token)):
        raise ValueError("Operation lock ownership mismatch")
    return intent


def complete_user_action_intent(
    intent_id: str, user_id: int, lock_key: str, token: str, result_note: str = ""
) -> bool:
    """Atomically mark a user action successful and release its exact owned lock."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        _verify_user_action_lock(cur, intent_id, user_id, lock_key, token)
        cur.execute(
            "DELETE FROM operation_locks WHERE lock_key = ? AND token = ?",
            (str(lock_key), str(token))
        )
        if cur.rowcount != 1:
            raise RuntimeError("Could not release the owned operation lock")
        cur.execute("""
            UPDATE user_action_intents
            SET status = 'succeeded', operation_lock_key = NULL,
                operation_lock_token = NULL, result_note = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE intent_id = ? AND user_id = ? AND status = 'processing'
        """, (str(result_note or "")[:500], str(intent_id), int(user_id)))
        if cur.rowcount != 1:
            raise RuntimeError("Could not finalize the user action intent")
        return True


def mark_user_action_intent_for_reconciliation(
    intent_id: str, user_id: int, lock_key: str, token: str, result_note: str
) -> bool:
    """Mark an uncertain external outcome without releasing its persistent lock."""
    note = str(result_note or "").strip()
    if len(note) < 5:
        raise ValueError("A reconciliation note is required")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        _verify_user_action_lock(cur, intent_id, user_id, lock_key, token)
        cur.execute("""
            UPDATE user_action_intents
            SET status = 'needs_reconciliation', result_note = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE intent_id = ? AND user_id = ? AND status = 'processing'
        """, (note[:500], str(intent_id), int(user_id)))
        if cur.rowcount != 1:
            raise RuntimeError("Could not mark user action for reconciliation")
        return True


def refund_user_wallet_and_release_operation_lock(
    user_id: int,
    amount: int,
    description: str,
    actor_id: int,
    actor_name: str,
    lock_key: str,
    token: str,
    intent_id: str | None = None,
) -> int:
    """Atomically refund a user and consume the exact operation-lock token.

    If the refund or lock resolution fails, the transaction rolls back and the
    persistent lock remains available for audited reconciliation. Reusing a
    consumed token cannot issue a second refund.
    """
    amount = int(amount)
    key = str(lock_key or "").strip()
    lock_token = str(token or "")
    if amount <= 0:
        raise ValueError("Refund amount must be a positive integer")
    if not key or not lock_token:
        raise ValueError("Operation lock key and token are required")

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        if intent_id is not None:
            intent = _verify_user_action_lock(cur, intent_id, user_id, key, lock_token)
            if int(intent["amount"]) != amount:
                raise ValueError("Refund amount does not match the consumed user action intent")
        else:
            lock = cur.execute(
                "SELECT token FROM operation_locks WHERE lock_key = ?",
                (key,),
            ).fetchone()
            if not lock or not secrets.compare_digest(str(lock["token"]), lock_token):
                raise ValueError("Operation lock ownership mismatch; refund was not applied")

        new_balance = _credit_user_wallet(
            cur, user_id, amount, description, actor_id, actor_name
        )
        cur.execute(
            "DELETE FROM operation_locks WHERE lock_key = ? AND token = ?",
            (key, lock_token),
        )
        if cur.rowcount != 1:
            raise RuntimeError("Could not atomically resolve the owned operation lock")
        if intent_id is not None:
            cur.execute("""
                UPDATE user_action_intents
                SET status = 'refunded', operation_lock_key = NULL,
                    operation_lock_token = NULL, result_note = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE intent_id = ? AND user_id = ? AND status = 'processing'
            """, (
                str(description or "Refunded after definite external failure")[:500],
                str(intent_id), int(user_id)
            ))
            if cur.rowcount != 1:
                raise RuntimeError("Could not finalize refunded user action intent")
        return new_balance
def deduct_user_wallet(user_id: int, amount: int, description: str, plan_code: str = "", account_code: str = "", email: str = "") -> tuple[bool, int]:
    """Atomically debit a positive amount; return (success, resulting balance)."""
    amount = int(amount)
    if amount <= 0:
        raise ValueError("Wallet debit amount must be a positive integer")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        row = cur.execute("SELECT balance_toman FROM user_wallets WHERE user_id = ?", (user_id,)).fetchone()
        current = int(row["balance_toman"] or 0) if row else 0
        if current < amount:
            return False, current
        new_balance = current - amount
        cur.execute("""
            UPDATE user_wallets SET balance_toman = ?, updated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND balance_toman >= ?
        """, (new_balance, user_id, amount))
        if cur.rowcount != 1:
            return False, current
        cur.execute("""
            INSERT INTO financial_ledger
            (rep_id, user_id, account_code, email, plan_code, transaction_type, amount, balance_after, description, actor_id, actor_name)
            VALUES (NULL, ?, ?, ?, ?, 'debit', ?, ?, ?, ?, 'User')
        """, (user_id, account_code, email, plan_code, amount, new_balance, description, user_id))
        return True, new_balance
def create_payment_receipt(user_id: int, role: str, amount_toman: int, rep_id: int | None = None, photo_id: str | None = None, note: str | None = None) -> str:
    """Create a receipt and return the one canonical persisted tracking code."""
    amount_toman = int(amount_toman)
    if amount_toman <= 0:
        raise ValueError("Receipt amount must be a positive integer")
    if role not in ("rep", "end_user"):
        raise ValueError("Invalid receipt role")
    if role == "rep" and rep_id is None:
        raise ValueError("Representative receipt requires a representative ID")
    with get_db() as conn:
        cur = conn.cursor()
        for _ in range(8):
            code = f"RCP-{secrets.randbelow(100_000_000):08d}"
            try:
                cur.execute("""
                    INSERT INTO payment_receipts
                    (tracking_code, user_id, role, rep_id, amount_toman, receipt_photo_id, receipt_note, status)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')
                """, (code, user_id, role, rep_id, amount_toman, photo_id, note))
                return code
            except sqlite3.IntegrityError:
                continue
        raise RuntimeError("Could not allocate a unique payment receipt tracking code")
def get_payment_receipt(receipt_id_or_code):
    with get_db() as conn:
        cur = conn.cursor()
        if isinstance(receipt_id_or_code, int) or str(receipt_id_or_code).isdigit():
            row = cur.execute("SELECT * FROM payment_receipts WHERE id = ?", (int(receipt_id_or_code),)).fetchone()
        else:
            row = cur.execute("SELECT * FROM payment_receipts WHERE tracking_code = ?", (str(receipt_id_or_code),)).fetchone()
        if row:
            return dict(row)
    return None

def get_pending_payment_receipts():
    with get_db() as conn:
        cur = conn.cursor()
        rows = cur.execute("SELECT * FROM payment_receipts WHERE status = 'pending' ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]

def approve_payment_receipt(receipt_id_or_code, admin_tg_id: int, admin_name: str = "Admin") -> dict:
    """Approve a receipt and apply the associated credit exactly once, atomically."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        if isinstance(receipt_id_or_code, int) or str(receipt_id_or_code).isdigit():
            row = cur.execute("SELECT * FROM payment_receipts WHERE id = ?", (int(receipt_id_or_code),)).fetchone()
        else:
            row = cur.execute("SELECT * FROM payment_receipts WHERE tracking_code = ?", (str(receipt_id_or_code),)).fetchone()
        if not row:
            raise ValueError("Receipt not found")
        receipt = dict(row)
        if receipt["status"] != "pending":
            return receipt

        cur.execute("""
            UPDATE payment_receipts
            SET status = 'approved', reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'pending'
        """, (admin_tg_id, receipt["id"]))
        if cur.rowcount != 1:
            latest = cur.execute("SELECT * FROM payment_receipts WHERE id = ?", (receipt["id"],)).fetchone()
            return dict(latest) if latest else receipt

        amount = int(receipt["amount_toman"])
        if amount <= 0:
            raise ValueError("Receipt has an invalid amount")
        if receipt["role"] == "rep":
            if receipt.get("rep_id") is None:
                raise ValueError("Representative receipt has no representative account")
            _record_rep_ledger_entry(
                cur, receipt["rep_id"], "", "", "",
                "credit", amount, f"تسویه فیش کارت‌به‌کارت {receipt['tracking_code']}",
                admin_tg_id, admin_name
            )
        elif receipt["role"] == "end_user":
            _credit_user_wallet(
                cur, receipt["user_id"], amount,
                f"شارژ کیف پول با فیش {receipt['tracking_code']}",
                admin_tg_id, admin_name
            )
        else:
            raise ValueError("Receipt role is invalid")

        updated = cur.execute("SELECT * FROM payment_receipts WHERE id = ?", (receipt["id"],)).fetchone()
        return dict(updated)
def reject_payment_receipt(receipt_id_or_code, admin_tg_id: int, reason: str = "عدم تطابق با گردش حساب") -> dict:
    """Reject a pending receipt once; repeated callbacks are read-only."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        if isinstance(receipt_id_or_code, int) or str(receipt_id_or_code).isdigit():
            row = cur.execute("SELECT * FROM payment_receipts WHERE id = ?", (int(receipt_id_or_code),)).fetchone()
        else:
            row = cur.execute("SELECT * FROM payment_receipts WHERE tracking_code = ?", (str(receipt_id_or_code),)).fetchone()
        if not row:
            raise ValueError("Receipt not found")
        if row["status"] != "pending":
            return dict(row)
        cur.execute("""
            UPDATE payment_receipts
            SET status = 'rejected', rejection_reason = ?, reviewed_by = ?, reviewed_at = CURRENT_TIMESTAMP
            WHERE id = ? AND status = 'pending'
        """, (reason, admin_tg_id, row["id"]))
        latest = cur.execute("SELECT * FROM payment_receipts WHERE id = ?", (row["id"],)).fetchone()
        return dict(latest) if latest else dict(row)
def get_rep_credit_limit(rep_id: int) -> int:
    with get_db() as conn:
        cur = conn.cursor()
        row = cur.execute("SELECT credit_limit_toman FROM representatives WHERE id = ?", (rep_id,)).fetchone()
        return row["credit_limit_toman"] if row and row["credit_limit_toman"] is not None else 5000000

def update_rep_credit_limit(rep_id: int, credit_limit: int):
    credit_limit = int(credit_limit)
    if credit_limit < 0:
        raise ValueError("Credit limit cannot be negative; use 0 for unlimited")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE representatives SET credit_limit_toman = ? WHERE id = ?", (credit_limit, rep_id))
        if cur.rowcount != 1:
            raise ValueError("Representative not found")
def check_rep_credit_limit(rep_id: int, pending_charge: int) -> tuple[bool, int, int]:
    with get_db() as conn:
        cur = conn.cursor()
        row = cur.execute("SELECT balance_toman, credit_limit_toman FROM representatives WHERE id = ?", (rep_id,)).fetchone()
        if not row:
            return False, 0, 0
        curr_debt = row["balance_toman"] or 0
        limit = row["credit_limit_toman"] if row["credit_limit_toman"] is not None else 5000000
        if limit == 0:  # 0 indicates unlimited credit
            return True, curr_debt, 0
        is_allowed = (curr_debt + pending_charge) <= limit
        return is_allowed, curr_debt, limit

# ==========================================
# Phase 4 v4.0.0-beta.2: Complete CRUD & Portal Queries
# ==========================================

def ensure_bot_user(user_id: int, username: str = "", full_name: str = "", role: str = "end_user", rep_id: int = None) -> dict:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
        INSERT INTO bot_users (telegram_id, role, rep_id, username, full_name, is_blocked)
        VALUES (?, ?, ?, ?, ?, 0)
        ON CONFLICT(telegram_id) DO UPDATE SET
            username = CASE WHEN excluded.username != '' THEN excluded.username ELSE bot_users.username END,
            full_name = CASE WHEN excluded.full_name != '' THEN excluded.full_name ELSE bot_users.full_name END
        """, (user_id, role, rep_id, username or "", full_name or ""))
        conn.commit()
        row = cur.execute("SELECT * FROM bot_users WHERE telegram_id = ?", (user_id,)).fetchone()
        return dict(row) if row else {}

def is_user_blocked(user_id: int) -> bool:
    with get_db() as conn:
        cur = conn.cursor()
        row = cur.execute("SELECT is_blocked FROM bot_users WHERE telegram_id = ?", (user_id,)).fetchone()
        return bool(row["is_blocked"]) if row else False

def toggle_user_block(user_id: int) -> int:
    with get_db() as conn:
        cur = conn.cursor()
        user = cur.execute("SELECT is_blocked FROM bot_users WHERE telegram_id = ?", (user_id,)).fetchone()
        new_val = 0 if (user and user["is_blocked"] == 1) else 1
        cur.execute("""
        INSERT INTO bot_users (telegram_id, is_blocked)
        VALUES (?, ?)
        ON CONFLICT(telegram_id) DO UPDATE SET is_blocked = excluded.is_blocked
        """, (user_id, new_val))
        conn.commit()
        return new_val

def get_bot_user_by_tg_id(user_id: int) -> dict | None:
    with get_db() as conn:
        cur = conn.cursor()
        u = cur.execute("""
        SELECT u.*, r.name as rep_name, COUNT(a.id) as linked_accounts_count, COALESCE(w.balance_toman, 0) as wallet_balance
        FROM bot_users u
        LEFT JOIN representatives r ON u.rep_id = r.id
        LEFT JOIN accounts a ON a.owner_tg_id = u.telegram_id
        LEFT JOIN user_wallets w ON w.user_id = u.telegram_id
        WHERE u.telegram_id = ?
        GROUP BY u.telegram_id
        """, (user_id,)).fetchone()
        if u:
            return dict(u)
        
        # Fallback if not yet in bot_users
        w = cur.execute("SELECT balance_toman FROM user_wallets WHERE user_id = ?", (user_id,)).fetchone()
        acc_cnt = cur.execute("SELECT COUNT(*) FROM accounts WHERE owner_tg_id = ?", (user_id,)).fetchone()[0]
        return {
            "telegram_id": user_id,
            "username": "",
            "full_name": "ثبت نشده",
            "role": "end_user",
            "rep_name": None,
            "is_blocked": 0,
            "linked_accounts_count": acc_cnt,
            "wallet_balance": w["balance_toman"] if w else 0
        }

def search_bot_users(query: str) -> list:
    q = f"%{query.strip().lower()}%"
    with get_db() as conn:
        cur = conn.cursor()
        rows = cur.execute("""
        SELECT u.*, r.name as rep_name, COUNT(a.id) as linked_accounts_count, COALESCE(w.balance_toman, 0) as wallet_balance
        FROM bot_users u
        LEFT JOIN representatives r ON u.rep_id = r.id
        LEFT JOIN accounts a ON a.owner_tg_id = u.telegram_id
        LEFT JOIN user_wallets w ON w.user_id = u.telegram_id
        WHERE CAST(u.telegram_id AS TEXT) LIKE ? OR LOWER(u.username) LIKE ? OR LOWER(u.full_name) LIKE ?
        GROUP BY u.telegram_id
        ORDER BY u.created_at DESC
        LIMIT 20
        """, (q, q, q)).fetchall()
        return [dict(r) for r in rows]

def update_user_wallet_manual(user_id: int, delta_amount: int, description: str, actor_id: int, actor_name: str = "Admin") -> int:
    """Apply a manual wallet adjustment without permitting an accidental negative balance."""
    delta_amount = int(delta_amount)
    if delta_amount == 0:
        raise ValueError("Adjustment amount must not be zero")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        row = cur.execute("SELECT balance_toman FROM user_wallets WHERE user_id = ?", (user_id,)).fetchone()
        current = int(row["balance_toman"] or 0) if row else 0
        new_balance = current + delta_amount
        if new_balance < 0:
            raise ValueError("Insufficient wallet balance for this deduction")
        cur.execute("""
            INSERT INTO user_wallets (user_id, balance_toman, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(user_id) DO UPDATE SET
                balance_toman = excluded.balance_toman, updated_at = CURRENT_TIMESTAMP
        """, (user_id, new_balance))
        tx_type = "credit" if delta_amount > 0 else "debit"
        cur.execute("""
            INSERT INTO financial_ledger
            (rep_id, user_id, transaction_type, amount, balance_after, description, actor_id, actor_name)
            VALUES (NULL, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, tx_type, abs(delta_amount), new_balance, description, actor_id, actor_name))
        return new_balance
def unlink_account_owner(account_code_or_email: str, user_tg_id: int | None = None) -> bool:
    """Unlink an exact account; when user_tg_id is supplied, only its owner may unlink it."""
    identifier = str(account_code_or_email or "").strip().lower()
    if not identifier:
        return False
    with get_db() as conn:
        cur = conn.cursor()
        sql = """
            UPDATE accounts SET owner_tg_id = NULL, updated_at = CURRENT_TIMESTAMP
            WHERE (LOWER(account_code) = ? OR LOWER(email) = ?)
        """
        params = [identifier, identifier]
        if user_tg_id is not None:
            sql += " AND owner_tg_id = ?"
            params.append(int(user_tg_id))
        cur.execute(sql, tuple(params))
        return cur.rowcount > 0
def change_user_tg_id(old_tg_id: int, new_tg_id: int) -> bool:
    old_tg_id, new_tg_id = int(old_tg_id), int(new_tg_id)
    if old_tg_id <= 0 or new_tg_id <= 0:
        raise ValueError("Telegram IDs must be positive")
    if old_tg_id == new_tg_id:
        return True
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        existing = cur.execute(
            "SELECT 1 FROM bot_users WHERE telegram_id = ?", (new_tg_id,)
        ).fetchone()
        wallet_exists = cur.execute(
            "SELECT 1 FROM user_wallets WHERE user_id = ?", (new_tg_id,)
        ).fetchone()
        settings_exists = cur.execute(
            "SELECT 1 FROM user_settings WHERE user_id = ?", (new_tg_id,)
        ).fetchone()
        owned_exists = cur.execute(
            "SELECT 1 FROM accounts WHERE owner_tg_id = ? LIMIT 1", (new_tg_id,)
        ).fetchone()
        if existing or wallet_exists or settings_exists or owned_exists:
            raise ValueError("The new Telegram ID is already present in the system")
        changed = cur.execute("SELECT 1 FROM bot_users WHERE telegram_id = ?", (old_tg_id,)).fetchone()
        if not changed:
            raise ValueError("Original Telegram user was not found")
        cur.execute("UPDATE bot_users SET telegram_id = ? WHERE telegram_id = ?", (new_tg_id, old_tg_id))
        cur.execute("UPDATE accounts SET owner_tg_id = ? WHERE owner_tg_id = ?", (new_tg_id, old_tg_id))
        cur.execute("UPDATE user_wallets SET user_id = ? WHERE user_id = ?", (new_tg_id, old_tg_id))
        cur.execute("UPDATE user_settings SET user_id = ? WHERE user_id = ?", (new_tg_id, old_tg_id))
        cur.execute("UPDATE financial_ledger SET user_id = ? WHERE user_id = ?", (new_tg_id, old_tg_id))
        cur.execute("UPDATE payment_receipts SET user_id = ? WHERE user_id = ?", (new_tg_id, old_tg_id))
        return True
def delete_bot_user(tg_id: int) -> bool:
    """Remove portal profile and links while retaining financial history and wallet records."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        exists = cur.execute("SELECT 1 FROM bot_users WHERE telegram_id = ?", (tg_id,)).fetchone()
        if not exists:
            return False
        cur.execute(
            "UPDATE accounts SET owner_tg_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE owner_tg_id = ?",
            (tg_id,)
        )
        cur.execute("DELETE FROM bot_users WHERE telegram_id = ?", (tg_id,))
        return cur.rowcount == 1
def toggle_rep_active(rep_id: int) -> int:
    with get_db() as conn:
        cur = conn.cursor()
        rep = cur.execute("SELECT is_active FROM representatives WHERE id = ?", (rep_id,)).fetchone()
        new_val = 0 if (rep and rep["is_active"] == 1) else 1
        cur.execute("UPDATE representatives SET is_active = ? WHERE id = ?", (new_val, rep_id))
        conn.commit()
        return new_val

def delete_rep(rep_id: int) -> bool:
    """Archive a representative instead of destroying financial/audit references."""
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("BEGIN IMMEDIATE")
        cur.execute(
            "UPDATE representatives SET is_active = 0 WHERE id = ?",
            (rep_id,)
        )
        return cur.rowcount == 1
def update_rep_full(rep_id: int, name: str, rep_code: str, telegram_id: int, group_id: int, topic_id: int, scope_filter: str):
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("""
        UPDATE representatives
        SET name = ?, rep_code = ?, telegram_id = ?, group_id = ?, topic_id = ?, scope_filter = ?
        WHERE id = ?
        """, (name, rep_code, telegram_id, group_id, topic_id, scope_filter, rep_id))
        conn.commit()

def toggle_plan_quick(plan_code: str) -> int:
    with get_db() as conn:
        cur = conn.cursor()
        p = cur.execute("SELECT is_quick FROM pricing_plans WHERE plan_code = ?", (plan_code,)).fetchone()
        new_val = 0 if (p and p["is_quick"] == 1) else 1
        cur.execute("UPDATE pricing_plans SET is_quick = ? WHERE plan_code = ?", (new_val, plan_code))
        conn.commit()
        return new_val

def update_plan_specs(plan_code: str, name: str, gb: float, days: int, rep_price: int = None, user_price: int = None):
    name = str(name or "").strip()
    gb, days = float(gb), int(days)
    if not name or gb <= 0 or days <= 0:
        raise ValueError("Plan name, volume and duration must be positive")
    if rep_price is not None and int(rep_price) <= 0:
        raise ValueError("Representative price must be positive")
    if user_price is not None and int(user_price) <= 0:
        raise ValueError("User price must be positive")
    with get_db() as conn:
        cur = conn.cursor()
        if rep_price is not None and user_price is not None:
            cur.execute("""
            UPDATE pricing_plans
            SET name = ?, gb = ?, days = ?, rep_price = ?, user_price = ?
            WHERE plan_code = ?
            """, (name, gb, days, int(rep_price), int(user_price), plan_code))
        else:
            cur.execute("""
            UPDATE pricing_plans
            SET name = ?, gb = ?, days = ?
            WHERE plan_code = ?
            """, (name, gb, days, plan_code))
        if cur.rowcount != 1:
            raise ValueError("Plan not found")
def get_rep_accounts_paginated(rep_id: int, filter_type: str = 'all', page: int = 1, page_size: int = 10) -> tuple[list, int, int]:
    import time
    page_size = max(1, min(int(page_size), 100))
    page = max(1, int(page))
    now_ms = int(time.time() * 1000)
    with get_db() as conn:
        cur = conn.cursor()
        base_where = "rep_id = ?"
        params = [rep_id]

        if filter_type == "active":
            base_where += " AND enable = 1 AND total_gb > 0 AND (expiry_time = 0 OR expiry_time > ?)"
            params.append(now_ms)
        elif filter_type == "critical":
            base_where += " AND (enable = 0 OR (expiry_time > 0 AND expiry_time <= ?))"
            params.append(now_ms)
        elif filter_type == "reserved":
            base_where += " AND account_code IN (SELECT account_code FROM reserved_packages WHERE status = 'pending')"

        total = cur.execute(
            f"SELECT COUNT(*) FROM accounts WHERE {base_where}", tuple(params)
        ).fetchone()[0]
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        offset = (page - 1) * page_size
        rows = cur.execute(
            f"SELECT * FROM accounts WHERE {base_where} ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(params + [page_size, offset])
        ).fetchall()
        return [dict(r) for r in rows], total, total_pages
def get_rep_reservations(rep_id: int) -> list:
    with get_db() as conn:
        cur = conn.cursor()
        rows = cur.execute("""
        SELECT * FROM reserved_packages
        WHERE rep_id = ? AND status = 'pending'
        ORDER BY created_at DESC
        """, (rep_id,)).fetchall()
        return [dict(r) for r in rows]

def get_rep_receipts(rep_id: int, limit: int = 10) -> list:
    with get_db() as conn:
        cur = conn.cursor()
        rows = cur.execute("""
        SELECT * FROM payment_receipts
        WHERE rep_id = ?
        ORDER BY id DESC
        LIMIT ?
        """, (rep_id, limit)).fetchall()
        return [dict(r) for r in rows]

def get_user_receipts(user_id: int, limit: int = 10) -> list:
    with get_db() as conn:
        cur = conn.cursor()
        rows = cur.execute("""
        SELECT * FROM payment_receipts
        WHERE user_id = ?
        ORDER BY id DESC
        LIMIT ?
        """, (user_id, limit)).fetchall()
        return [dict(r) for r in rows]

def get_all_receipts(status: str = None, limit: int = 30) -> list:
    with get_db() as conn:
        cur = conn.cursor()
        if status:
            rows = cur.execute("SELECT * FROM payment_receipts WHERE status = ? ORDER BY id DESC LIMIT ?", (status, limit)).fetchall()
        else:
            rows = cur.execute("SELECT * FROM payment_receipts ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

if __name__ == "__main__":
    init_db()
    count = sync_all_active_reps()
    print(f"Database initialized and synced {count} accounts successfully.")
    plans = get_pricing_plans()
    print(f"Loaded {len(plans)} pricing plans.")
