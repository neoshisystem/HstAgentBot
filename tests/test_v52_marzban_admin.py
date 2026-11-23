"""W21.7 administrative effect contracts and read-back preservation."""
import asyncio
from test_v52_marzban_contract import FakeTransport, make_adapter, make_client

def test_admin_effect_contract():
    initial=make_client()
    disabled=make_client(enabled=False)
    traffic=make_client(enabled=False,data_limit=2097152)
    expiry=make_client(enabled=False,data_limit=2097152,expire=1900000000000)
    transport=FakeTransport()
    transport.queue("client.read",
        {"state":"success","data":{"client":initial}},
        {"state":"success","data":{"client":disabled}},
        {"state":"success","data":{"client":disabled}},
        {"state":"success","data":{"client":traffic}},
        {"state":"success","data":{"client":traffic}},
        {"state":"success","data":{"client":expiry}},
        {"state":"success","data":{"client":expiry}},
        {"state":"not_found"})
    transport.queue("client.enabled",{"state":"success"})
    transport.queue("client.traffic",{"state":"success"})
    transport.queue("client.expiry",{"state":"success"})
    transport.queue("client.delete",{"state":"success"})
    adapter=make_adapter(transport)
    results=[
        asyncio.run(adapter.set_client_enabled("synthetic-user-001",False,"SYNTHETIC-ADMIN-01")),
        asyncio.run(adapter.set_traffic("synthetic-user-001","set_quota_bytes",2097152,"SYNTHETIC-ADMIN-02")),
        asyncio.run(adapter.set_expiry("synthetic-user-001",1900000000,"SYNTHETIC-ADMIN-03")),
        asyncio.run(adapter.delete_client("synthetic-user-001","SYNTHETIC-ADMIN-04")),
    ]
    assert [r.state for r in results]==["success"]*4
    assert all(r.evidence.get("stage") in {"readback","delete_readback"} for r in results)
    traffic_call=next(c for c in transport.calls if c[0]=="client.traffic")
    expiry_call=next(c for c in transport.calls if c[0]=="client.expiry")
    assert traffic_call[1]["data_limit"]==2097152 and expiry_call[1]["expire"]==1900000000000
    assert results[1].evidence["unrelated_fields_preserved"] is True
    assert results[2].evidence["unrelated_fields_preserved"] is True
    assert all(c[2] for c in transport.calls if c[0]!="client.read")
