CREATE TABLE support_tickets (
 ticket_id TEXT PRIMARY KEY, sender_tg_id INTEGER NOT NULL, bot_id INTEGER NOT NULL REFERENCES bot_registry(bot_id),
 account_id INTEGER REFERENCES accounts(id), rep_id INTEGER REFERENCES representatives(id),
 ticket_type TEXT NOT NULL CHECK(ticket_type IN('general','finance','disorder','abuse_report','delete_request')),
 assigned_key TEXT NOT NULL, status TEXT NOT NULL, snapshot_cipher TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
);
CREATE TABLE support_messages (
 id INTEGER PRIMARY KEY, ticket_id TEXT NOT NULL REFERENCES support_tickets(ticket_id), actor_tg_id INTEGER NOT NULL,
 visibility TEXT NOT NULL CHECK(visibility IN('customer','internal','root_only')),
 body_cipher TEXT NOT NULL, attachment_file_id TEXT, attachment_bot_id INTEGER, created_at INTEGER NOT NULL
);
CREATE TABLE forum_topics (
 id INTEGER PRIMARY KEY, owner_key TEXT NOT NULL, bot_id INTEGER NOT NULL REFERENCES bot_registry(bot_id),
 group_chat_id INTEGER NOT NULL, generation INTEGER NOT NULL DEFAULT 1, topic_key TEXT NOT NULL,
 title_fa TEXT NOT NULL, thread_id INTEGER, state TEXT NOT NULL, updated_at INTEGER NOT NULL,
 UNIQUE(owner_key,bot_id,group_chat_id,generation,topic_key)
);
