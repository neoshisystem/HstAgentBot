import sqlite3

import database


def _create_main_baseline_fixture(db_path):
    """DDL for the historical main schema snapshot (database.py blob c4db0778...)."""
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript("""
        CREATE TABLE representatives (
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
        );
        CREATE TABLE accounts (
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
        );
        CREATE TABLE pricing_plans (
            plan_code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            gb REAL NOT NULL,
            days INTEGER NOT NULL,
            rep_price INTEGER NOT NULL,
            user_price INTEGER NOT NULL,
            is_quick INTEGER DEFAULT 0,
            sort_order INTEGER DEFAULT 0
        );
        CREATE TABLE financial_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rep_id INTEGER NOT NULL,
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
        );
        CREATE INDEX idx_accounts_rep_code ON accounts(rep_id, account_code);
        CREATE INDEX idx_accounts_email ON accounts(email);
        CREATE INDEX idx_accounts_owner ON accounts(owner_tg_id);
        CREATE INDEX idx_ledger_rep ON financial_ledger(rep_id);

        INSERT INTO representatives
            (id, rep_code, name, scope_filter, balance_toman, is_active)
        VALUES (11, 'OLD-REP', 'Legacy Representative', 'all', 450000, 1);
        INSERT INTO accounts
            (id, rep_id, account_code, email, customer_name, sub_id, enable,
             total_gb, expiry_time, owner_tg_id)
        VALUES
            (21, 11, 'OLD-ACC', 'legacy@example.test', 'Legacy Customer',
             'legacy-sub', 1, 15, 1900000000, 91011);
        INSERT INTO pricing_plans
            (plan_code, name, gb, days, rep_price, user_price, is_quick, sort_order)
        VALUES ('OLD-PLAN', 'Legacy Plan', 15, 30, 200000, 250000, 0, 1);
        INSERT INTO financial_ledger
            (id, rep_id, account_code, email, plan_code, transaction_type,
             amount, balance_after, description, actor_id, actor_name)
        VALUES
            (77, 11, 'OLD-ACC', 'legacy@example.test', 'OLD-PLAN', 'debit',
             200000, 250000, 'legacy debit', 91011, 'legacy actor');
        """)
        connection.commit()
    finally:
        connection.close()


def _create_t02b_fixture(db_path):
    """DDL copied from Product database.py at T02B commit 6742cc83..."""
    connection = sqlite3.connect(db_path)
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript("""
        CREATE TABLE representatives (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            rep_code TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            telegram_id INTEGER,
            group_id INTEGER,
            topic_id INTEGER,
            scope_filter TEXT NOT NULL,
            balance_toman INTEGER DEFAULT 0,
            credit_limit_toman INTEGER DEFAULT 5000000,
            is_active INTEGER DEFAULT 1,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE accounts (
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
        );
        CREATE TABLE pricing_plans (
            plan_code TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            gb REAL NOT NULL,
            days INTEGER NOT NULL,
            rep_price INTEGER NOT NULL,
            user_price INTEGER NOT NULL,
            is_quick INTEGER DEFAULT 0,
            sort_order INTEGER DEFAULT 0
        );
        CREATE TABLE payment_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tracking_code TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            rep_id INTEGER,
            amount_toman INTEGER NOT NULL,
            receipt_photo_id TEXT,
            receipt_note TEXT,
            status TEXT DEFAULT 'pending',
            rejection_reason TEXT,
            reviewed_by INTEGER,
            reviewed_at TIMESTAMP,
            cancelled_by INTEGER,
            cancelled_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (rep_id) REFERENCES representatives(id)
        );
        CREATE INDEX idx_receipts_status ON payment_receipts(status);
        CREATE TABLE rep_renewal_intents (
            intent_id TEXT PRIMARY KEY,
            rep_id INTEGER NOT NULL,
            actor_tg_id INTEGER NOT NULL,
            account_code TEXT NOT NULL,
            account_email TEXT NOT NULL,
            plan_code TEXT NOT NULL,
            plan_name TEXT NOT NULL,
            gb REAL NOT NULL,
            days INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'processing',
            operation_lock_key TEXT NOT NULL,
            operation_lock_token TEXT NOT NULL,
            result_note TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (rep_id) REFERENCES representatives(id)
        );
        CREATE INDEX idx_rep_renewal_intents_owner_status
            ON rep_renewal_intents(rep_id, actor_tg_id, status, created_at);
        CREATE TABLE account_renewal_confirmations (
            intent_id TEXT PRIMARY KEY,
            actor_id INTEGER NOT NULL,
            actor_role TEXT NOT NULL CHECK (actor_role IN ('rep', 'super_admin')),
            rep_id INTEGER,
            account_code TEXT NOT NULL,
            account_email TEXT NOT NULL,
            plan_code TEXT NOT NULL,
            plan_name TEXT NOT NULL,
            gb REAL NOT NULL,
            days INTEGER NOT NULL,
            amount INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at REAL NOT NULL
        );
        CREATE INDEX idx_account_renewal_confirmations_actor_status
            ON account_renewal_confirmations(actor_id, status, expires_at);

        INSERT INTO representatives
            (id, rep_code, name, scope_filter, balance_toman, credit_limit_toman, is_active)
        VALUES (31, 'T02B-REP', 'T02B Representative', 'all', 880000, 5000000, 1);
        INSERT INTO payment_receipts
            (id, tracking_code, user_id, role, rep_id, amount_toman, receipt_photo_id, status)
        VALUES (41, 'T02B-RCP', 92031, 'rep', 31, 500000, 'historic-photo-id', 'pending');
        INSERT INTO rep_renewal_intents
            (intent_id, rep_id, actor_tg_id, account_code, account_email,
             plan_code, plan_name, gb, days, amount, status,
             operation_lock_key, operation_lock_token)
        VALUES
            ('t02b-renewal', 31, 92031, 'T02B-ACC', 't02b@example.test',
             'P30', '30 Day', 30, 30, 500000, 'processing', 'lock:old', 'token:old');
        INSERT INTO account_renewal_confirmations
            (intent_id, actor_id, actor_role, rep_id, account_code, account_email,
             plan_code, plan_name, gb, days, amount, status, expires_at)
        VALUES
            ('t02b-confirm', 92031, 'rep', 31, 'T02B-ACC', 't02b@example.test',
             'P30', '30 Day', 30, 30, 500000, 'processing', 2000000000);
        """)
        connection.commit()
    finally:
        connection.close()


def _columns(connection, table):
    return {
        row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }


def test_main_legacy_schema_migration_preserves_financial_data_and_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "main-legacy.sqlite3"
    _create_main_baseline_fixture(str(db_path))
    monkeypatch.setattr(database, "DB_PATH", str(db_path))

    database.init_db()
    database.init_db()

    connection = sqlite3.connect(db_path)
    try:
        rep = connection.execute(
            "SELECT id, balance_toman, credit_limit_toman FROM representatives WHERE id=11"
        ).fetchone()
        account = connection.execute(
            "SELECT id, rep_id, account_code, owner_tg_id FROM accounts WHERE id=21"
        ).fetchone()
        plan = connection.execute(
            "SELECT plan_code, is_active FROM pricing_plans WHERE plan_code='OLD-PLAN'"
        ).fetchone()
        ledger = connection.execute(
            "SELECT id, rep_id, user_id, amount, balance_after, description "
            "FROM financial_ledger WHERE id=77"
        ).fetchone()
        ledger_columns = _columns(connection, "financial_ledger")
        not_null = {
            row[1]: row[3] for row in
            connection.execute("PRAGMA table_info(financial_ledger)").fetchall()
        }
        assert rep == (11, 450000, 5000000)
        assert account == (21, 11, "OLD-ACC", 91011)
        assert plan == ("OLD-PLAN", 1)
        assert ledger == (77, 11, None, 200000, 250000, "legacy debit")
        assert "user_id" in ledger_columns
        assert not_null["rep_id"] == 0
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT COUNT(*) FROM financial_ledger").fetchone()[0] == 1
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(financial_ledger)").fetchall()
        }
        assert {"idx_ledger_rep", "idx_ledger_user"} <= indexes
    finally:
        connection.close()


def test_t02b_schema_migration_adds_atomic_renewal_link_columns_without_data_loss(tmp_path, monkeypatch):
    db_path = tmp_path / "t02b-legacy.sqlite3"
    _create_t02b_fixture(str(db_path))
    monkeypatch.setattr(database, "DB_PATH", str(db_path))

    database.init_db()
    database.init_db()

    connection = sqlite3.connect(db_path)
    try:
        rep_intent_columns = _columns(connection, "rep_renewal_intents")
        confirmation_columns = _columns(connection, "account_renewal_confirmations")
        assert "confirmation_id" in rep_intent_columns
        assert {
            "operation_ref_type",
            "operation_ref_id",
            "operation_lock_key",
            "operation_lock_token",
            "result_note",
        } <= confirmation_columns
        intent = connection.execute(
            "SELECT intent_id, rep_id, actor_tg_id, status, operation_lock_key "
            "FROM rep_renewal_intents WHERE intent_id='t02b-renewal'"
        ).fetchone()
        confirmation = connection.execute(
            "SELECT intent_id, actor_id, actor_role, rep_id, status, operation_ref_type "
            "FROM account_renewal_confirmations WHERE intent_id='t02b-confirm'"
        ).fetchone()
        receipt = connection.execute(
            "SELECT id, tracking_code, receipt_photo_id, cancelled_by, cancelled_at "
            "FROM payment_receipts WHERE id=41"
        ).fetchone()
        assert intent == ("t02b-renewal", 31, 92031, "processing", "lock:old")
        assert confirmation == ("t02b-confirm", 92031, "rep", 31, "processing", None)
        assert receipt == (41, "T02B-RCP", "historic-photo-id", None, None)
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT COUNT(*) FROM rep_renewal_intents").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM account_renewal_confirmations").fetchone()[0] == 1
    finally:
        connection.close()



def test_init_db_releases_pending_thread_blocked_outbox_rows_idempotently_without_financial_changes(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "receipt-outbox-thread-migration.sqlite3"
    monkeypatch.setattr(database, "DB_PATH", str(db_path))
    database.init_db()

    receipts = {}
    for user_id in (93001, 93002, 93003, 93004):
        receipts[user_id] = database.submit_payment_receipt(
            user_id=user_id,
            role="end_user",
            amount_toman=175_000,
            photo_id=f"migration-photo-{user_id}",
            recipient_chat_id=-1009300,
            recipient_thread_id=77,
        )

    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO user_wallets (user_id, balance_toman) VALUES (?, ?)",
            (93099, 765_432),
        )
        conn.execute(
            """
            INSERT INTO financial_ledger
                (rep_id, user_id, transaction_type, amount, balance_after,
                 description, actor_id, actor_name)
            VALUES (NULL, ?, 'credit', ?, ?, ?, ?, ?)
            """,
            (93099, 765_432, 765_432, "migration baseline", 9000, "test"),
        )

        for user_id, error, status in (
            (93001, "BadRequest: Message thread not found", "pending"),
            (93002, "BadRequest: invalid thread identifier", "pending"),
            (93003, "network timeout while sending photo", "pending"),
            (93004, "BadRequest: Message thread not found", "sent"),
        ):
            conn.execute(
                """
                UPDATE receipt_notification_outbox
                SET status = ?, recipient_thread_id = 77, next_attempt_at = 987654321,
                    lease_token = ?, lease_until = 987654999, last_error = ?, attempts = 4
                WHERE receipt_id = ?
                """,
                (status, f"lease-{user_id}", error, receipts[user_id]["id"]),
            )

    with database.get_db() as conn:
        wallets_before = [
            tuple(row) for row in conn.execute(
                "SELECT user_id, balance_toman FROM user_wallets ORDER BY user_id"
            ).fetchall()
        ]
        ledger_before = [
            tuple(row) for row in conn.execute(
                "SELECT id, rep_id, user_id, transaction_type, amount, balance_after, description "
                "FROM financial_ledger ORDER BY id"
            ).fetchall()
        ]
        receipt_rows_before = conn.execute(
            "SELECT COUNT(*) FROM payment_receipts"
        ).fetchone()[0]

    # Running init_db twice proves that the migration is safe to repeat.
    database.init_db()
    database.init_db()

    with database.get_db() as conn:
        recovered = conn.execute(
            """
            SELECT status, recipient_thread_id, next_attempt_at, lease_token, lease_until, attempts
            FROM receipt_notification_outbox WHERE receipt_id = ?
            """,
            (receipts[93001]["id"],),
        ).fetchone()
        recovered_thread_variant = conn.execute(
            """
            SELECT status, recipient_thread_id, next_attempt_at, lease_token, lease_until, attempts
            FROM receipt_notification_outbox WHERE receipt_id = ?
            """,
            (receipts[93002]["id"],),
        ).fetchone()
        unrelated = conn.execute(
            """
            SELECT status, recipient_thread_id, next_attempt_at, lease_token, lease_until, attempts
            FROM receipt_notification_outbox WHERE receipt_id = ?
            """,
            (receipts[93003]["id"],),
        ).fetchone()
        terminal = conn.execute(
            """
            SELECT status, recipient_thread_id, next_attempt_at, lease_token, lease_until, attempts
            FROM receipt_notification_outbox WHERE receipt_id = ?
            """,
            (receipts[93004]["id"],),
        ).fetchone()
        wallets_after = [
            tuple(row) for row in conn.execute(
                "SELECT user_id, balance_toman FROM user_wallets ORDER BY user_id"
            ).fetchall()
        ]
        ledger_after = [
            tuple(row) for row in conn.execute(
                "SELECT id, rep_id, user_id, transaction_type, amount, balance_after, description "
                "FROM financial_ledger ORDER BY id"
            ).fetchall()
        ]
        receipts_after = conn.execute(
            "SELECT COUNT(*) FROM payment_receipts"
        ).fetchone()[0]

    assert tuple(recovered) == ("pending", None, 0.0, None, None, 4)
    assert tuple(recovered_thread_variant) == ("pending", None, 0.0, None, None, 4)
    assert tuple(unrelated) == ("pending", 77, 987654321.0, "lease-93003", 987654999.0, 4)
    assert tuple(terminal) == ("sent", 77, 987654321.0, "lease-93004", 987654999.0, 4)
    assert wallets_after == wallets_before
    assert ledger_after == ledger_before
    assert receipts_after == receipt_rows_before
