"""W12.4: bounded fanout, exact URI dedupe, cache and wire encoding."""
import asyncio
import base64
import sqlite3
from dataclasses import dataclass

import gateway_service


TOKEN = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopq"[:43]


@dataclass
class Node:
    uri: str


class Panel:
    def __init__(self, nodes=None, fail=False):
        self.nodes, self.fail = nodes or [], fail

    async def get_client_configs(self, identifier):
        if self.fail:
            raise RuntimeError("panel offline")
        await asyncio.sleep(0)
        return self.nodes


class Factory:
    def __init__(self, panels):
        self.panels = panels
        self.calls = []

    def resolve_account_adapter(self, account_id):
        self.calls.append(account_id)
        return self.panels[account_id]


def _database(member_ids=(11, 12)):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
      CREATE TABLE subscription_profiles(
        profile_id INTEGER PRIMARY KEY, owner_tg_id INTEGER, enabled INTEGER,
        deleted_at INTEGER, token_hash TEXT, is_default INTEGER, brand_rep_id INTEGER);
      CREATE TABLE subscription_profile_accounts(
        profile_id INTEGER, account_id INTEGER, sort_order INTEGER, added_at INTEGER);
      CREATE TABLE accounts(
        id INTEGER PRIMARY KEY, deleted_at INTEGER, owner_tg_id INTEGER, rep_id INTEGER,
        panel_id INTEGER, credential_revision TEXT, revision INTEGER, email TEXT);
      CREATE TABLE representatives(id INTEGER, telegram_id INTEGER, is_active INTEGER, deleted_at INTEGER);
      CREATE TABLE account_access_grants(
        account_id INTEGER, grantee_tg_id INTEGER, access_level TEXT, revoked_at INTEGER);
      CREATE TABLE sub_domains(domain_id INTEGER, rep_id INTEGER, hostname TEXT, status TEXT);
    """)
    import hashlib
    conn.execute("INSERT INTO subscription_profiles VALUES(1,101,1,NULL,?,1,NULL)",
                 (hashlib.sha256(TOKEN.encode()).hexdigest(),))
    for order, account_id in enumerate(member_ids):
        conn.execute("INSERT INTO subscription_profile_accounts VALUES(1,?,?,?)",
                     (account_id, order, 1800000000))
        conn.execute("INSERT INTO accounts VALUES(?,NULL,101,NULL,?,?,1,?)",
                     (account_id, account_id, f"cred-{account_id}", f"account-{account_id}@example.invalid"))
    return conn


def test_partial_failure_dedupe_and_formats():
    conn = _database()
    uri = "vless://same-node@example.invalid:443"
    factory = Factory({
        11: Panel([Node(uri), Node(uri)]),
        12: Panel(fail=True),
    })
    cache = {}
    async def run(selected_factory=factory, selected_cache=None):
        return await gateway_service.resolve_subscription_request(
            conn, TOKEN, "canonical.example", canonical_host="canonical.example",
            adapter_factory=selected_factory, cache=cache if selected_cache is None else selected_cache,
        )

    partial = asyncio.run(run())
    assert partial.status == 200 and partial.failures == 1
    assert partial.body == uri.encode("utf-8")
    raw = gateway_service.encode_subscription(partial.body, "raw")
    encoded = gateway_service.encode_subscription(partial.body, "base64")
    assert raw == partial.body
    assert base64.b64decode(encoded) == raw
    assert b"subscription-userinfo" not in raw.lower()

    failed = Factory({11: Panel(fail=True), 12: Panel(fail=True)})
    unavailable = asyncio.run(run(failed, {}))
    assert unavailable.status == 503 and unavailable.failures == 2

    empty_conn = _database(member_ids=())
    empty = asyncio.run(gateway_service.resolve_subscription_request(
        empty_conn, TOKEN, "canonical.example", canonical_host="canonical.example",
        adapter_factory=failed, cache={},
    ))
    assert empty.status == 200 and empty.body == b""
    assert factory.calls == [11, 12]
    with __import__("pytest").raises(ValueError):
        gateway_service.encode_subscription(b"payload", "clash")
    conn.close()
    empty_conn.close()
