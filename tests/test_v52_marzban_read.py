"""W21.3 recorded read normalization."""
import asyncio
from test_v52_marzban_contract import FakeTransport, make_adapter, make_client

def test_recorded_reads_normalize():
    transport=FakeTransport()
    transport.queue("client.read",{"state":"success","data":{"client":make_client()}})
    transport.queue("client.configs",{"state":"success","data":{"configs":[{
        "node_key":"synthetic-node-01","label":"Synthetic node","protocol":"vless",
        "uri":"vless://REDACTED:SYNTHETIC@node.invalid:443#synthetic"}]}})
    transport.queue("client.read",{"state":"success","data":{"client":{"username":"synthetic-user-001"}}})
    transport.queue("client.read",{"state":"not_found"})
    transport.queue("client.read",{"state":"unavailable"})
    adapter=make_adapter(transport)
    lookup=asyncio.run(adapter.get_client("synthetic-user-001"))
    assert lookup.state=="found"
    assert lookup.client.ref==(7001,71,"synthetic-user-001")
    assert lookup.client.quota_bytes==1048576 and lookup.client.used_bytes==131072
    assert lookup.client.expires_at==1800000000 and lookup.client.adapter_revision=="marzban-v1"
    configs=asyncio.run(adapter.get_client_configs("synthetic-user-001"))
    assert len(configs)==1 and configs[0].protocol=="vless" and configs[0].node_key=="synthetic-node-01"
    malformed=asyncio.run(adapter.get_client("synthetic-user-001"))
    not_found=asyncio.run(adapter.get_client("synthetic-user-001"))
    unavailable=asyncio.run(adapter.get_client("synthetic-user-001"))
    assert malformed.state=="unavailable" and not_found.state=="not_found" and unavailable.state=="unavailable"
    assert [c[0] for c in transport.calls]==["client.read","client.configs","client.read","client.read","client.read"]
