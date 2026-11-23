CREATE TABLE refund_requests (
 refund_id TEXT PRIMARY KEY, order_id TEXT REFERENCES payment_orders(order_id), merchant_key TEXT NOT NULL,
 user_tg_id INTEGER NOT NULL, amount_toman INTEGER NOT NULL CHECK(amount_toman>0),
 state TEXT NOT NULL CHECK(state IN('requested','processing','settled','rejected','unknown')),
 wallet_hold_key TEXT NOT NULL UNIQUE, bank_details_cipher TEXT NOT NULL, evidence_cipher TEXT,
 reviewer_tg_id INTEGER, created_at INTEGER NOT NULL, settled_at INTEGER
);
CREATE TABLE wallet_refund_holds (
 hold_key TEXT PRIMARY KEY, merchant_key TEXT NOT NULL, user_tg_id INTEGER NOT NULL,
 amount_toman INTEGER NOT NULL CHECK(amount_toman>0), state TEXT NOT NULL CHECK(state IN('active','captured','released')),
 created_at INTEGER NOT NULL
);
CREATE TABLE coupons (
 code TEXT PRIMARY KEY, issuer_key TEXT NOT NULL, discount_toman INTEGER NOT NULL CHECK(discount_toman>0),
 budget_toman INTEGER NOT NULL CHECK(budget_toman>=0), used_toman INTEGER NOT NULL DEFAULT 0,
 max_uses INTEGER NOT NULL DEFAULT 1, expires_at INTEGER NOT NULL, is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE coupon_uses (
 code TEXT NOT NULL REFERENCES coupons(code), order_id TEXT NOT NULL UNIQUE REFERENCES payment_orders(order_id),
 amount_toman INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN('held','used','released')), PRIMARY KEY(code,order_id)
);
CREATE TABLE scheduled_jobs (
 job_key TEXT PRIMARY KEY, kind TEXT NOT NULL, due_at INTEGER NOT NULL, state TEXT NOT NULL,
 lease_token TEXT, lease_until INTEGER, result_json TEXT
);
