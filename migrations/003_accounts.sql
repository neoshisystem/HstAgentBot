CREATE TABLE accounts_v52 (
 id INTEGER PRIMARY KEY AUTOINCREMENT, rep_id INTEGER REFERENCES representatives(id) ON DELETE SET NULL,
 account_code TEXT NOT NULL, email TEXT NOT NULL, customer_name TEXT,
 sub_id TEXT, enable INTEGER DEFAULT 1, total_gb REAL DEFAULT 0, expiry_time INTEGER DEFAULT 0,
 owner_tg_id INTEGER, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
 updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, first_connect_mode INTEGER DEFAULT 0,
 rollover_enabled INTEGER DEFAULT 1, panel_id INTEGER REFERENCES panels(id),
 sub_id_cipher TEXT, seller_key TEXT NOT NULL DEFAULT 'root', revision INTEGER NOT NULL DEFAULT 1,
 acl_revision INTEGER NOT NULL DEFAULT 1, is_test INTEGER NOT NULL DEFAULT 0,
 is_personal_use INTEGER NOT NULL DEFAULT 0, deleted_at INTEGER,
 UNIQUE(panel_id,email COLLATE NOCASE)
);
INSERT INTO accounts_v52(id,rep_id,account_code,email,customer_name,sub_id,enable,total_gb,expiry_time,owner_tg_id,created_at,updated_at,first_connect_mode,rollover_enabled)
 SELECT id,rep_id,account_code,email,customer_name,sub_id,enable,total_gb,expiry_time,owner_tg_id,created_at,updated_at,first_connect_mode,rollover_enabled FROM accounts;
DROP TABLE accounts;
ALTER TABLE accounts_v52 RENAME TO accounts;
CREATE INDEX idx_accounts_email ON accounts(email);
CREATE INDEX idx_accounts_owner ON accounts(owner_tg_id);
CREATE INDEX idx_accounts_rep_code ON accounts(rep_id,account_code);
CREATE INDEX idx_accounts_panel_code ON accounts(panel_id,account_code);
ALTER TABLE reserved_packages ADD COLUMN panel_id INTEGER REFERENCES panels(id);
ALTER TABLE reserved_packages ADD COLUMN account_id INTEGER REFERENCES accounts(id);
