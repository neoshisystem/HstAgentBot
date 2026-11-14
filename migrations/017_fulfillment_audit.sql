ALTER TABLE audit_logs ADD COLUMN details_cipher TEXT;
CREATE TABLE goods_fulfillments (
 order_id TEXT PRIMARY KEY REFERENCES payment_orders(order_id),
 state TEXT NOT NULL CHECK(state IN('awaiting_manual','preparing','ready','delivered','exception')),
 inventory_id INTEGER REFERENCES digital_inventory(id), payload_cipher TEXT,
 claim_token TEXT, lease_until INTEGER, exposed_at INTEGER, revision INTEGER NOT NULL DEFAULT 1
);
CREATE TRIGGER ledger_immutable_update BEFORE UPDATE ON financial_ledger
 BEGIN SELECT RAISE(ABORT,'ledger_append_only'); END;
CREATE TRIGGER ledger_immutable_delete BEFORE DELETE ON financial_ledger
 BEGIN SELECT RAISE(ABORT,'ledger_append_only'); END;
CREATE TRIGGER quote_immutable_update BEFORE UPDATE ON quotes
 BEGIN SELECT RAISE(ABORT,'quote_immutable'); END;
CREATE TRIGGER quote_immutable_delete BEFORE DELETE ON quotes
 BEGIN SELECT RAISE(ABORT,'quote_immutable'); END;
