CREATE TABLE policy_decisions (
 decision_id TEXT PRIMARY KEY, status TEXT NOT NULL CHECK(status='APPROVED'),
 authority TEXT NOT NULL, approved_date TEXT NOT NULL, policy_version TEXT NOT NULL,
 decision_text TEXT NOT NULL
);
CREATE TABLE feature_flags (
 name TEXT PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0 CHECK(enabled IN(0,1))
);
