"""W12.5: isolated aiohttp endpoint contract, privacy and failure statuses."""
import asyncio
import base64
import hashlib
import logging
import sqlite3
from contextlib import contextmanager

from aiohttp.test_utils import TestClient, TestServer
import sub_gateway


TOKEN_OK = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopq"[:43]
TOKEN_EMPTY = "ZYXWVUTSRQPONMLKJIHGFEDCBAponmlkjihgfedc"[:43]


class FailingFactory:
    def resolve_account_adapter(self, account_id):
        raise RuntimeError("offline")


@contextmanager
def _connection(conn):
    yield conn


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
    conn.executemany(
        "INSERT INTO subscription_profiles VALUES(?,?,1,NULL,?,1,NULL)",
        [(1,101,hashlib.sha256(TOKEN_OK.encode()).hexdigest()),
         (2,202,hashlib.sha256(TOKEN_EMPTY.encode()).hexdigest())],
    )
    conn.execute("INSERT INTO subscription_profile_accounts VALUES(1,11,0,1800000000)")
    conn.execute("INSERT INTO accounts VALUES(11,NULL,101,NULL,1,'cred-1',1,'http@example.invalid')")
    return conn


def test_http_contract_and_redacted_log(caplog):
    async def exercise():
        conn = _database()
        app = sub_gateway.create_app(
            db_factory=lambda: _connection(conn),
            adapter_factory=FailingFactory(),
            canonical_host="canonical.example",
            trusted_proxy_cidrs=("10.0.0.0/8",),
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            with caplog.at_level(logging.INFO, logger="hstagentbot.subscription_gateway"):
                ok = await client.get(
                    f"/s/{TOKEN_OK}?format=raw", headers={"Host": "canonical.example"}
                )
                assert ok.status == 503
                assert ok.headers["Cache-Control"] == "no-store"
                assert ok.content_type == "text/plain"
                assert ok.charset.lower() == "utf-8"
                error_body = await ok.read()
                assert b"Traceback" not in error_body and b"offline" not in error_body

                empty = await client.get(
                    f"/s/{TOKEN_EMPTY}?format=base64", headers={"Host": "canonical.example"}
                )
                assert empty.status == 200 and await empty.read() == b""
                assert empty.headers["Cache-Control"] == "no-store"

                bad_format = await client.get(
                    f"/s/{TOKEN_EMPTY}?format=json", headers={"Host": "canonical.example"}
                )
                assert bad_format.status == 400

                invalid = await client.get(
                    f"/s/{'x' * 100}?format=raw", headers={"Host": "canonical.example"}
                )
                assert invalid.status == 404

                spoof = await client.get(
                    f"/s/{TOKEN_EMPTY}?format=raw",
                    headers={"Host": "untrusted.example", "X-Forwarded-Host": "canonical.example"},
                )
                assert spoof.status == 404
                assert len(app.router.routes()) <= 3
                messages = [record.getMessage() for record in caplog.records
                            if record.name == "hstagentbot.subscription_gateway"]
                assert all(TOKEN_OK not in message and TOKEN_EMPTY not in message for message in messages)
        finally:
            await client.close()
            conn.close()

    asyncio.run(exercise())
