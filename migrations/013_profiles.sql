CREATE TABLE subscription_profiles (
 profile_id INTEGER PRIMARY KEY, owner_tg_id INTEGER NOT NULL, name TEXT NOT NULL,
 is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN(0,1)),
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN(0,1)),
 brand_rep_id INTEGER REFERENCES representatives(id), created_bot_id INTEGER NOT NULL REFERENCES bot_registry(bot_id),
 token_hash TEXT NOT NULL UNIQUE, token_cipher TEXT NOT NULL, token_version INTEGER NOT NULL DEFAULT 1,
 revision INTEGER NOT NULL DEFAULT 1, deleted_at INTEGER, created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX ux_default_profile ON subscription_profiles(owner_tg_id) WHERE is_default=1;
CREATE TABLE subscription_profile_accounts (
 profile_id INTEGER NOT NULL REFERENCES subscription_profiles(profile_id), account_id INTEGER NOT NULL REFERENCES accounts(id),
 added_by_tg_id INTEGER NOT NULL, sort_order INTEGER NOT NULL DEFAULT 0, added_at INTEGER NOT NULL,
 PRIMARY KEY(profile_id,account_id)
);
CREATE TRIGGER protect_default_profile_delete BEFORE DELETE ON subscription_profiles WHEN OLD.is_default=1
 BEGIN SELECT RAISE(ABORT,'default_profile_not_deletable'); END;
CREATE TRIGGER protect_default_profile_update BEFORE UPDATE ON subscription_profiles
 WHEN OLD.is_default=1 AND (NEW.is_default<>1 OR NEW.deleted_at IS NOT NULL OR NEW.owner_tg_id<>OLD.owner_tg_id)
 BEGIN SELECT RAISE(ABORT,'default_profile_not_deletable'); END;
