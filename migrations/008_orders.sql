CREATE TABLE quotes (
 quote_id TEXT PRIMARY KEY, actor_tg_id INTEGER NOT NULL, bot_id INTEGER NOT NULL REFERENCES bot_registry(bot_id),
 merchant_key TEXT NOT NULL, purpose TEXT NOT NULL, account_id INTEGER REFERENCES accounts(id),
 plan_code TEXT, product_id INTEGER, panel_id INTEGER REFERENCES panels(id),
 amount_toman INTEGER NOT NULL CHECK(amount_toman>=0), spec_json TEXT NOT NULL,
 price_edges_json TEXT NOT NULL, policy_version TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
 fingerprint TEXT NOT NULL, CHECK(expires_at=created_at+1800)
);
CREATE TABLE payment_orders (
 order_id TEXT PRIMARY KEY, quote_id TEXT NOT NULL UNIQUE REFERENCES quotes(quote_id),
 actor_tg_id INTEGER NOT NULL, bot_id INTEGER NOT NULL REFERENCES bot_registry(bot_id), merchant_key TEXT NOT NULL,
 purpose TEXT NOT NULL CHECK(purpose IN('wallet_topup','rep_settlement','vpn_buy','vpn_renew','vpn_addon','digital_product_buy')),
 payment_rail TEXT NOT NULL CHECK(payment_rail IN('wallet','card_to_card','crypto_tron','online_gateway','exempt')),
 amount_toman INTEGER NOT NULL CHECK(amount_toman>=0),
 status TEXT NOT NULL CHECK(status IN('awaiting_payment','pending_review','accepted','completed','exception','expired','cancelled','closed_refunded')),
 received_toman INTEGER NOT NULL DEFAULT 0 CHECK(received_toman>=0),
 created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE capacity_reservations (
 reservation_id TEXT PRIMARY KEY, order_id TEXT NOT NULL UNIQUE REFERENCES payment_orders(order_id),
 panel_id INTEGER NOT NULL REFERENCES panels(id), account_id INTEGER REFERENCES accounts(id),
 state TEXT NOT NULL CHECK(state IN('active','allocated','released')), expires_at INTEGER
);
