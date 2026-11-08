ALTER TABLE financial_ledger ADD COLUMN business_key TEXT;
ALTER TABLE financial_ledger ADD COLUMN merchant_key TEXT NOT NULL DEFAULT 'root';
ALTER TABLE financial_ledger ADD COLUMN ledger_scope TEXT NOT NULL DEFAULT 'legacy';
ALTER TABLE financial_ledger ADD COLUMN edge_id INTEGER REFERENCES rep_edges(id);
ALTER TABLE financial_ledger ADD COLUMN order_id TEXT REFERENCES payment_orders(order_id);
ALTER TABLE financial_ledger ADD COLUMN operation_id TEXT;
ALTER TABLE financial_ledger ADD COLUMN reversal_of INTEGER REFERENCES financial_ledger(id);
ALTER TABLE financial_ledger ADD COLUMN policy_version TEXT;
CREATE UNIQUE INDEX ux_ledger_business_key ON financial_ledger(business_key) WHERE business_key IS NOT NULL;
CREATE TABLE financial_holds (
 hold_id TEXT PRIMARY KEY, order_id TEXT NOT NULL REFERENCES payment_orders(order_id),
 source_key TEXT NOT NULL, amount_toman INTEGER NOT NULL CHECK(amount_toman>0),
 state TEXT NOT NULL CHECK(state IN('active','captured','released')),
 created_at INTEGER NOT NULL, expires_at INTEGER, captured_ledger_id INTEGER REFERENCES financial_ledger(id),
 UNIQUE(order_id,source_key)
);
CREATE INDEX idx_holds_source ON financial_holds(source_key,state);
