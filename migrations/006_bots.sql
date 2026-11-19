CREATE TABLE bot_registry (
 bot_id INTEGER PRIMARY KEY, rep_id INTEGER REFERENCES representatives(id),
 is_primary INTEGER NOT NULL DEFAULT 0 CHECK(is_primary IN(0,1)), token_cipher TEXT,
 username TEXT NOT NULL DEFAULT '', status TEXT NOT NULL CHECK(status IN('pending','active','disabled')),
 verified_at INTEGER, revision INTEGER NOT NULL DEFAULT 1
);
CREATE UNIQUE INDEX ux_primary_bot ON bot_registry(is_primary) WHERE is_primary=1;
CREATE UNIQUE INDEX ux_active_rep_bot ON bot_registry(rep_id) WHERE rep_id IS NOT NULL AND status='active';
CREATE TABLE bot_customers (
 bot_id INTEGER NOT NULL REFERENCES bot_registry(bot_id), user_tg_id INTEGER NOT NULL,
 started_at INTEGER NOT NULL, PRIMARY KEY(bot_id,user_tg_id)
);
CREATE TABLE merchant_wallets (
 merchant_key TEXT NOT NULL, user_tg_id INTEGER NOT NULL, balance_toman INTEGER NOT NULL DEFAULT 0 CHECK(balance_toman>=0),
 PRIMARY KEY(merchant_key,user_tg_id), CHECK(merchant_key<>'root')
);
