"""W12.3: subscription ACL revocation and owner transfer defeat warm cache."""
from dataclasses import dataclass
import asyncio
import hashlib
import sqlite3

import gateway_service


TOKEN = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopq"[:43]


@dataclass
class Node:
    uri: str


class Adapter:
    def __init__(self, uri, calls):
        self.uri, self.calls = uri, calls

    async def get_client_configs(self, identifier):
        self.calls.append(identifier)
        return [Node(self.uri)]


class Factory:
    def __init__(self):
        self.calls = []
        self.fail = set()

    def resolve_account_adapter(self, account_id):
        if account_id in self.fail:
            raise RuntimeError("synthetic panel unavailable")
        return Adapter(f"vless://node-{account_id}@example.invalid:443", self.calls)


def _database():
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
    conn.execute(
        "INSERT INTO subscription_profiles VALUES(1,101,1,NULL,?,1,NULL)",
        (hashlib.sha256(TOKEN.encode()).hexdigest(),),
    )
    conn.executemany("INSERT INTO subscription_profile_accounts VALUES(1,?,?,?)",
                     [(11,0,1800000000),(12,1,1800000000)])
    conn.execute("INSERT INTO accounts VALUES(11,NULL,101,NULL,1,'cred-1',1,'one@example.invalid')")
    conn.execute("INSERT INTO accounts VALUES(12,NULL,505,NULL,2,'cred-2',1,'two@example.invalid')")
    conn.execute("INSERT INTO account_access_grants VALUES(12,101,'viewer',NULL)")
    return conn


def test_revocation_before_cache():
    conn = _database()
    factory = Factory()
    cache = {}
    async def run(token=TOKEN, host="canonical.example"):
        return await gateway_service.resolve_subscription_request(
            conn, token, host, canonical_host="canonical.example",
            adapter_factory=factory, cache=cache,
        )

    first = asyncio.run(run())
    assert first.status == 200
    assert b"node-11" in first.body and b"node-12" in first.body
    assert len(factory.calls) == 2

    # Revoke account 12's viewer grant; its warm config cache must not bypass ACL.
    conn.execute("UPDATE account_access_grants SET revoked_at=1800000001 WHERE account_id=12")
    after_revoke = asyncio.run(run())
    assert after_revoke.status == 200
    assert b"node-11" in after_revoke.body and b"node-12" not in after_revoke.body
    assert len(factory.calls) == 2

    # Transfer direct ownership as well; all resources are now filtered before cache reads.
    conn.execute("UPDATE accounts SET owner_tg_id=909 WHERE id=11")
    after_transfer = asyncio.run(run())
    assert after_transfer.status == 200 and after_transfer.body == b""
    assert len(factory.calls) == 2

    assert asyncio.run(run(token="invalid-token")).status == 404
    assert asyncio.run(run(host="wrong.example")).status == 404
    conn.execute("UPDATE subscription_profiles SET enabled=0 WHERE profile_id=1")
    paused = asyncio.run(run())
    conn.execute("UPDATE subscription_profiles SET enabled=1,deleted_at=1 WHERE profile_id=1")
    deleted = asyncio.run(run())
    assert paused.status == deleted.status == 404
    conn.close()
