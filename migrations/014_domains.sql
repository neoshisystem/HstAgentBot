CREATE TABLE sub_domains (
 domain_id INTEGER PRIMARY KEY, rep_id INTEGER NOT NULL REFERENCES representatives(id),
 hostname TEXT NOT NULL UNIQUE COLLATE NOCASE, challenge TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN('pending','verified','active','disabled')),
 verified_at INTEGER, tls_checked_at INTEGER, created_at INTEGER NOT NULL
);
CREATE UNIQUE INDEX ux_active_rep_domain ON sub_domains(rep_id) WHERE status='active';
