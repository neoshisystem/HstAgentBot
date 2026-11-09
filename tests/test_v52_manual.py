import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from panel_adapters.manual_adapter import ManualPoolAdapter
from manual_pool_repository import ManualPoolEmpty, ManualPoolRepository

SCHEMA = """CREATE TABLE manual_pool_clients(
 id INTEGER PRIMARY KEY, panel_id INTEGER, external_identifier TEXT, account_id INTEGER,
 status TEXT, config_cipher TEXT, quota_mode TEXT, quota_bytes INTEGER, used_bytes INTEGER,
 time_mode TEXT, expires_at INTEGER, enabled INTEGER, allocation_key TEXT UNIQUE,
 revision INTEGER, created_at INTEGER, UNIQUE(panel_id,external_identifier));"""

def test_pool_claim_no_duplicate_or_fake_health(tmp_path):
    db=tmp_path/"pool.sqlite3"; c=sqlite3.connect(db); c.executescript(SCHEMA)
    c.execute("INSERT INTO manual_pool_clients(panel_id,external_identifier,status,config_cipher,quota_mode,time_mode,enabled,revision,created_at) VALUES(1,'x','available','C','finite','unknown',1,1,1)")
    c.commit(); c.close(); r=ManualPoolRepository(db)
    gate=Barrier(2)
    def claim(op,account):
        gate.wait()
        try: return r.claim_client(panel_id=1,operation_id=op,account_id=account)
        except ManualPoolEmpty: return None
    with ThreadPoolExecutor(max_workers=2) as p:
        futures=[p.submit(claim,"a",101),p.submit(claim,"b",102)]
        vals=[f.result() for f in futures]
    assert sum(v is not None for v in vals)==1
    winner=next(v for v in vals if v)
    adapter=ManualPoolAdapter({"id":1},r,account_id_resolver=lambda *_: winner["account_id"])
    health=__import__("asyncio").run(adapter.verify_connection())
    assert health.reachable is False and health.cpu_percent is None and health.core_running is None
