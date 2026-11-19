CREATE TABLE panels (
 id INTEGER PRIMARY KEY, panel_code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 panel_type TEXT NOT NULL, adapter_version TEXT NOT NULL DEFAULT '',
 raw_url TEXT NOT NULL DEFAULT '', protocol TEXT NOT NULL DEFAULT 'https',
 host TEXT NOT NULL DEFAULT '', port INTEGER NOT NULL DEFAULT 443 CHECK(port BETWEEN 1 AND 65535),
 base_path TEXT NOT NULL DEFAULT '', auth_type TEXT NOT NULL DEFAULT 'bearer_token',
 auth_cipher TEXT, sub_base_url TEXT NOT NULL DEFAULT '',
 lifecycle TEXT NOT NULL DEFAULT 'active' CHECK(lifecycle IN('active','capacity','drain','maintenance')),
 is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN(0,1)),
 is_hidden INTEGER NOT NULL DEFAULT 0 CHECK(is_hidden IN(0,1)),
 max_clients INTEGER NOT NULL DEFAULT 0 CHECK(max_clients>=0),
 for_end_users INTEGER NOT NULL DEFAULT 1, for_reps INTEGER NOT NULL DEFAULT 1,
 access_type TEXT NOT NULL DEFAULT 'public' CHECK(access_type IN('public','private','tier_restricted')),
 min_rep_rank INTEGER NOT NULL DEFAULT 0, min_user_rank INTEGER NOT NULL DEFAULT 0,
 username_strategy TEXT NOT NULL DEFAULT 'pool_preallocated', custom_prefix TEXT NOT NULL DEFAULT '',
 renewal_method TEXT NOT NULL DEFAULT 'smart_rollover', connect_first_guard INTEGER NOT NULL DEFAULT 1,
 allow_test_account INTEGER NOT NULL DEFAULT 0, test_duration_hours INTEGER NOT NULL DEFAULT 1,
 test_volume_mib INTEGER NOT NULL DEFAULT 100, allow_addons INTEGER NOT NULL DEFAULT 1,
 extra_gib_rep_price INTEGER NOT NULL DEFAULT 12000, extra_gib_user_price INTEGER NOT NULL DEFAULT 18000,
 extra_day_rep_price INTEGER NOT NULL DEFAULT 5000, extra_day_user_price INTEGER NOT NULL DEFAULT 8000,
 default_inbound_ids_json TEXT NOT NULL DEFAULT '[]', extra_config_json TEXT NOT NULL DEFAULT '{}',
 revision INTEGER NOT NULL DEFAULT 1, created_at INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX ux_panels_default ON panels(is_default) WHERE is_default=1;
CREATE TABLE panel_rep_assignments (
 panel_id INTEGER NOT NULL REFERENCES panels(id), rep_id INTEGER NOT NULL REFERENCES representatives(id),
 effect TEXT NOT NULL CHECK(effect IN('allow','deny')), include_sub_reps INTEGER NOT NULL DEFAULT 1,
 assigned_by INTEGER NOT NULL, PRIMARY KEY(panel_id,rep_id)
);
CREATE TABLE panel_counters (panel_id INTEGER PRIMARY KEY REFERENCES panels(id), next_value INTEGER NOT NULL DEFAULT 1);
