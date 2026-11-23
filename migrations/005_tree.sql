ALTER TABLE representatives ADD COLUMN parent_rep_id INTEGER REFERENCES representatives(id);
ALTER TABLE representatives ADD COLUMN tier TEXT NOT NULL DEFAULT 'normal';
ALTER TABLE representatives ADD COLUMN billing_mode TEXT NOT NULL DEFAULT 'credit' CHECK(billing_mode IN('credit','prepaid_wallet'));
ALTER TABLE representatives ADD COLUMN can_create_sub_reps INTEGER NOT NULL DEFAULT 0;
ALTER TABLE representatives ADD COLUMN max_sub_reps INTEGER NOT NULL DEFAULT 0 CHECK(max_sub_reps>=0);
ALTER TABLE representatives ADD COLUMN custom_prefix TEXT NOT NULL DEFAULT '';
ALTER TABLE representatives ADD COLUMN markup_bps INTEGER NOT NULL DEFAULT 0 CHECK(markup_bps>=0);
ALTER TABLE representatives ADD COLUMN deleted_at INTEGER;
CREATE TABLE rep_edges (
 id INTEGER PRIMARY KEY, parent_key TEXT NOT NULL, child_rep_id INTEGER NOT NULL REFERENCES representatives(id),
 billing_mode TEXT NOT NULL CHECK(billing_mode IN('credit','prepaid_wallet')),
 debt_toman INTEGER NOT NULL DEFAULT 0, credit_limit_toman INTEGER NOT NULL DEFAULT 0 CHECK(credit_limit_toman>=0),
 active INTEGER NOT NULL DEFAULT 1 CHECK(active IN(0,1)), started_at INTEGER NOT NULL, ended_at INTEGER
);
CREATE UNIQUE INDEX ux_rep_active_edge ON rep_edges(child_rep_id) WHERE active=1;
