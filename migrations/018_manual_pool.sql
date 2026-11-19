CREATE TABLE manual_pool_clients (
 id INTEGER PRIMARY KEY, panel_id INTEGER NOT NULL REFERENCES panels(id),
 external_identifier TEXT NOT NULL, account_id INTEGER UNIQUE REFERENCES accounts(id),
 status TEXT NOT NULL DEFAULT 'available' CHECK(status IN('available','allocated','disabled','defective')),
 config_cipher TEXT NOT NULL, quota_mode TEXT NOT NULL CHECK(quota_mode IN('finite','unlimited','unknown')),
 quota_bytes INTEGER, used_bytes INTEGER, time_mode TEXT NOT NULL CHECK(time_mode IN('dated','unlimited','on_hold','unknown')),
 expires_at INTEGER, enabled INTEGER NOT NULL DEFAULT 1, allocation_key TEXT UNIQUE,
 revision INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL,
 UNIQUE(panel_id,external_identifier),
 CHECK(quota_bytes IS NULL OR quota_bytes>=0), CHECK(used_bytes IS NULL OR used_bytes>=0)
);
CREATE INDEX idx_manual_pool_available ON manual_pool_clients(panel_id,status);
