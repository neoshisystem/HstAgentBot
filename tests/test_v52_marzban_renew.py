"""W21.5 renewal/add-on unit conversion and on-hold/unlimited states."""
import asyncio
from test_v52_marzban_contract import FakeTransport, make_adapter, make_client

def test_recorded_renew_addon_units():
    before=make_client()
    after=make_client(data_limit=2097152,used_traffic=262144,expire=1900000123000)
    transport=FakeTransport()
    transport.queue("client.read",{"state":"success","data":{"client":before}},
                    {"state":"success","data":{"client":after}})
    transport.queue("client.renew",{"state":"success"})
    adapter=make_adapter(transport)
    target={"quota_mode":"finite","quota_bytes":2097152,"used_bytes":262144,
            "time_mode":"dated","expires_at":1900000123}
    result=asyncio.run(adapter.renew_client("synthetic-user-001",target,"SYNTHETIC-RENEW-001"))
    assert result.state=="success"
    call=next(c for c in transport.calls if c[0]=="client.renew")
    assert call[1]["data_limit"]==2097152 and call[1]["used_traffic"]==262144
    assert call[1]["expire"]==1900000123000 and result.expected_effect["read_back"] is True
    unlimited=dict(target,quota_mode="unlimited",quota_bytes=None,time_mode="unlimited",expires_at=None)
    wire,expected=adapter._target_payload(unlimited)
    assert wire["quota_mode"]=="unlimited" and wire["quota_bytes"] is None
    assert wire["time_mode"]=="unlimited" and wire["expires_at"] is None
    assert expected["quota_mode"]=="unlimited" and expected["time_mode"]=="unlimited"
    count=len(transport.calls)
    on_hold={"quota_mode":"finite","quota_bytes":1048576,"used_bytes":131072,
             "time_mode":"on_hold","expires_at":None,"add_bytes":1024,"add_days":2}
    blocked=asyncio.run(adapter.add_addon("synthetic-user-001",on_hold,"SYNTHETIC-ADDON-001"))
    assert blocked.state=="definite_failure" and len(transport.calls)==count
