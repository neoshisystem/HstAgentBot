ALTER TABLE bot_users ADD COLUMN tier TEXT NOT NULL DEFAULT 'normal';
ALTER TABLE user_settings ADD COLUMN active_persona TEXT NOT NULL DEFAULT 'end_user';
CREATE TABLE account_access_grants (
 account_id INTEGER NOT NULL REFERENCES accounts(id), grantee_tg_id INTEGER NOT NULL,
 access_level TEXT NOT NULL CHECK(access_level IN('viewer','manager')),
 granted_by_tg_id INTEGER NOT NULL, created_at INTEGER NOT NULL, revoked_at INTEGER,
 revision INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(account_id,grantee_tg_id)
);
CREATE INDEX idx_grants_user ON account_access_grants(grantee_tg_id,revoked_at);
CREATE TABLE daily_test_usage (
 actor_tg_id INTEGER NOT NULL, tehran_date TEXT NOT NULL, used INTEGER NOT NULL DEFAULT 0 CHECK(used>=0),
 PRIMARY KEY(actor_tg_id,tehran_date)
);
