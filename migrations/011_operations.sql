CREATE TABLE action_confirmations (
 nonce TEXT PRIMARY KEY, actor_tg_id INTEGER NOT NULL, bot_id INTEGER NOT NULL,
 action TEXT NOT NULL, target_type TEXT NOT NULL, target_id TEXT NOT NULL,
 expected_revision INTEGER NOT NULL, payload_json TEXT NOT NULL,
 expires_at INTEGER NOT NULL, consumed_at INTEGER, operation_id TEXT
);
CREATE TABLE panel_operations (
 operation_id TEXT PRIMARY KEY, order_id TEXT REFERENCES payment_orders(order_id),
 actor_tg_id INTEGER NOT NULL, bot_id INTEGER NOT NULL, account_id INTEGER REFERENCES accounts(id),
 panel_id INTEGER NOT NULL REFERENCES panels(id), kind TEXT NOT NULL, request_cipher TEXT NOT NULL,
 expected_effect_json TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN('prepared','dispatching','succeeded','definite_failure','needs_reconciliation')),
 lock_key TEXT NOT NULL, lock_token TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
 generation INTEGER NOT NULL DEFAULT 0, lease_until INTEGER, evidence_json TEXT,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE operation_attempts (
 id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL REFERENCES panel_operations(operation_id),
 attempt_no INTEGER NOT NULL, started_at INTEGER NOT NULL, finished_at INTEGER,
 outcome TEXT, evidence_json TEXT, UNIQUE(operation_id,attempt_no)
);
CREATE TABLE delivery_outbox (
 id INTEGER PRIMARY KEY, event_key TEXT NOT NULL, bot_id INTEGER NOT NULL REFERENCES bot_registry(bot_id),
 chat_id INTEGER NOT NULL, thread_id INTEGER, purpose TEXT NOT NULL,
 resource_type TEXT NOT NULL, resource_id TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at INTEGER NOT NULL DEFAULT 0,
 lease_token TEXT, lease_until INTEGER, last_error_code TEXT, message_id INTEGER,
 UNIQUE(event_key,bot_id,chat_id,purpose)
);
