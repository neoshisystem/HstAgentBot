CREATE TABLE bank_cards (
 id INTEGER PRIMARY KEY, merchant_key TEXT NOT NULL, card_number_cipher TEXT NOT NULL,
 sheba_cipher TEXT, card_holder TEXT NOT NULL, bank_name TEXT NOT NULL,
 target_scope TEXT NOT NULL DEFAULT 'all', priority INTEGER NOT NULL DEFAULT 1,
 daily_limit_toman INTEGER NOT NULL DEFAULT 0 CHECK(daily_limit_toman>=0), is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE payment_attempts (
 attempt_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES payment_orders(order_id), rail TEXT NOT NULL,
 merchant_key TEXT NOT NULL, assigned_card_id INTEGER REFERENCES bank_cards(id),
 amount_toman INTEGER NOT NULL CHECK(amount_toman>0), state TEXT NOT NULL,
 external_payment_key TEXT UNIQUE, evidence_cipher TEXT, created_at INTEGER NOT NULL, verified_at INTEGER
);
ALTER TABLE payment_receipts ADD COLUMN order_id TEXT REFERENCES payment_orders(order_id);
ALTER TABLE payment_receipts ADD COLUMN payment_attempt_id TEXT REFERENCES payment_attempts(attempt_id);
ALTER TABLE payment_receipts ADD COLUMN bot_id INTEGER REFERENCES bot_registry(bot_id);
ALTER TABLE payment_receipts ADD COLUMN bank_card_id INTEGER REFERENCES bank_cards(id);
ALTER TABLE payment_receipts ADD COLUMN merchant_key TEXT NOT NULL DEFAULT 'root';
ALTER TABLE payment_receipts ADD COLUMN submitted_tehran_date TEXT;
CREATE UNIQUE INDEX ux_receipt_attempt ON payment_receipts(payment_attempt_id) WHERE payment_attempt_id IS NOT NULL;
CREATE INDEX idx_receipt_daily_card ON payment_receipts(bank_card_id,submitted_tehran_date,status);
