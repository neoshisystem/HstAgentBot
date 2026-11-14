CREATE TABLE digital_categories (
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, parent_id INTEGER REFERENCES digital_categories(id),
 description TEXT NOT NULL DEFAULT '', sort_order INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE digital_products (
 id INTEGER PRIMARY KEY, product_code TEXT NOT NULL UNIQUE, category_id INTEGER NOT NULL REFERENCES digital_categories(id),
 title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '', delivery_type TEXT NOT NULL CHECK(delivery_type IN('auto_inventory','file_document','manual_fulfill')),
 rep_price INTEGER NOT NULL CHECK(rep_price>=0), user_price INTEGER NOT NULL CHECK(user_price>=0),
 file_telegram_id TEXT, file_bot_id INTEGER REFERENCES bot_registry(bot_id),
 stock_warning_threshold INTEGER NOT NULL DEFAULT 3, is_active INTEGER NOT NULL DEFAULT 1, sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE digital_inventory (
 id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL REFERENCES digital_products(id), secret_cipher TEXT NOT NULL,
 notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL CHECK(status IN('available','reserved','sold','defective')),
 order_id TEXT UNIQUE REFERENCES payment_orders(order_id), sold_to_tg_id INTEGER,
 reservation_until INTEGER, exposed_at INTEGER, sold_at INTEGER, created_at INTEGER NOT NULL
);
