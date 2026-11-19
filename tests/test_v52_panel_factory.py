import sqlite3

import pytest

from panel_adapters.base import AdapterUnavailable, UnsupportedCapability
from panel_adapters.factory import PanelAdapterFactory
from panel_adapters.manual_pool import ManualPoolRepository


def test_factory_resolves_xui_without_fallback():
    panels = {1: {"id": 1, "type": "3x-ui", "raw_url": "https://one.invalid",
                  "auth_cipher": "C", "revision": 1}}
    calls = []
    class Session:
        def request(self, *args, **kwargs):
            raise AssertionError("factory test must not reach network")
    factory = PanelAdapterFactory(
        panel_loader=lambda pid: panels.get(pid),
        account_loader=lambda aid: {"id": aid, "panel_id": 1},
        session_factory=lambda p: (calls.append(p["id"]) or Session()),
        decryptor=lambda *_: "TOKEN",
        transaction_probe=lambda: False,
        account_id_resolver=lambda *_: 101,
    )
    adapter = factory.resolve_account_adapter(101)
    assert adapter.panel_id == 1 and calls == [1]
    with pytest.raises(AdapterUnavailable):
        PanelAdapterFactory(panel_loader=lambda _: (_ for _ in ()).throw(RuntimeError()),
                            account_loader=lambda _: {"panel_id": 1},
                            session_factory=lambda _: Session()).get_adapter(1)


def test_factory_selects_manual_and_invalidates_cache(tmp_path):
    db = tmp_path / "manual.sqlite3"
    c = sqlite3.connect(db)
    c.executescript("CREATE TABLE manual_pool_clients(id INTEGER PRIMARY KEY,panel_id INTEGER,external_identifier TEXT,account_id INTEGER,status TEXT,config_cipher TEXT,quota_mode TEXT,quota_bytes INTEGER,used_bytes INTEGER,time_mode TEXT,expires_at INTEGER,enabled INTEGER,allocation_key TEXT,revision INTEGER,created_at INTEGER);")
    c.commit(); c.close()
    panel = {"id": 2, "type": "manual-pool", "revision": 1}
    factory = PanelAdapterFactory(panel_loader=lambda _: panel, account_loader=lambda _: {"panel_id": 2},
                                  session_factory=None, manual_repository=ManualPoolRepository(db))
    first = factory.get_adapter(2)
    assert first is factory.get_adapter(2)
    panel["revision"] = 2
    assert first is not factory.get_adapter(2)


def test_unknown_type_fails_closed():
    with pytest.raises(UnsupportedCapability):
        PanelAdapterFactory(panel_loader=lambda _: {"id": 9, "type": "unknown"},
                            account_loader=lambda _: {"panel_id": 9},
                            session_factory=None).get_adapter(9)
