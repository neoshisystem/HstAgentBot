import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from panel_adapters.manual_pool import ManualPoolAdapter, ManualPoolConflict, ManualPoolEmpty, ManualPoolRepository

SCHEMA = """
CREATE TABLE panels(id INTEGER PRIMARY KEY);
CREATE TABLE accounts(id INTEGER PRIMARY KEY);
CREATE TABLE manual_pool_clients (
 id INTEGER PRIMARY KEY, panel_id INTEGER NOT NULL, external_identifier TEXT NOT NULL,
 account_id INTEGER UNIQUE, status TEXT NOT NULL, config_cipher TEXT NOT NULL,
 quota_mode TEXT NOT NULL, quota_bytes INTEGER, used_bytes INTEGER,
 time_mode TEXT NOT NULL, expires_at INTEGER, enabled INTEGER NOT NULL,
 allocation_key TEXT UNIQUE, revision INTEGER NOT NULL, created_at INTEGER NOT NULL,
 UNIQUE(panel_id,external_identifier)
);
"""

def repo(tmp_path):
    path = tmp_path / "pool.sqlite3"
    db = sqlite3.connect(path); db.executescript(SCHEMA)
    db.execute("INSERT INTO panels VALUES(1)")
    db.execute("INSERT INTO accounts VALUES(101)")
    db.execute("INSERT INTO accounts VALUES(102)")
    db.commit(); db.close()
    return ManualPoolRepository(path)

def test_import_validation_and_secret_storage(tmp_path):
    r = repo(tmp_path)
    with pytest.raises(PermissionError):
        r.import_client(panel_id=1, external_identifier="x", config_cipher="C",
                        config_uris=["vless://id@node.invalid:443"], actor_is_root=False)
    r.import_client(panel_id=1, external_identifier="x", config_cipher="CIPHERTEXT",
                    config_uris=["vless://id@node.invalid:443"], actor_is_root=True, created_at=1)
    raw = (tmp_path / "pool.sqlite3").read_bytes()
    assert b"vless://id@node.invalid:443" not in raw
    assert b"CIPHERTEXT" in raw
    with pytest.raises(ManualPoolConflict):
        r.import_client(panel_id=1, external_identifier="x", config_cipher="C2",
                        config_uris=["vless://id@node.invalid:443"], actor_is_root=True)

def test_concurrent_claim_is_cas_safe(tmp_path):
    r = repo(tmp_path)
    r.import_client(panel_id=1, external_identifier="one", config_cipher="C",
                    config_uris=["vless://id@node.invalid:443"], actor_is_root=True, created_at=1)
    gate = Barrier(2)
    def claim(op, account):
        gate.wait()
        try:
            return r.claim_client(panel_id=1, operation_id=op, account_id=account)
        except ManualPoolEmpty:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = [f.result() for f in [pool.submit(claim, "op-a", 101), pool.submit(claim, "op-b", 102)]]
    assert sum(v is not None for v in values) == 1
    winner = next(v for v in values if v)
    replay = r.claim_client(panel_id=1, operation_id=winner["allocation_key"], account_id=winner["account_id"])
    assert replay["id"] == winner["id"]

def test_manual_adapter_is_offline_and_redacts_config(tmp_path):
    r = repo(tmp_path)
    r.import_client(panel_id=1, external_identifier="one", config_cipher="CIPH",
                    config_uris=["vless://id@node.invalid:443"], actor_is_root=True, created_at=1)
    r.claim_client(panel_id=1, operation_id="op-1", account_id=101)
    adapter = ManualPoolAdapter({"id": 1}, r,
        decryptor=lambda _: json.dumps(["vless://id@node.invalid:443#phone"]),
        account_id_resolver=lambda *_: 101)
    found = asyncio.run(adapter.get_client("one"))
    assert found.state == "found" and found.client.used_bytes is None
    configs = asyncio.run(adapter.get_client_configs("one"))
    assert configs[0].protocol == "vless" and configs[0].label == "phone"
    assert asyncio.run(adapter.renew_client("one", {}, "op-renew")).state == "definite_failure"
    assert adapter.verify_connection is not None
