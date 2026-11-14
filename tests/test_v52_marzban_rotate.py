"""W21.6 distinct subscription and connection-credential rotation."""
import asyncio
from test_v52_marzban_contract import FakeTransport, make_adapter, make_client

def test_distinct_rotation_capabilities():
    before=make_client()
    after_sub=make_client(subscription_url="SYNTHETIC-NEW-SUBSCRIPTION")
    creds={"uuid":"REDACTED:SYNTHETIC-ROTATED-UUID","password":"REDACTED:SYNTHETIC-ROTATED-PASSWORD",
           "flow":"SYNTHETIC-ROTATED-FLOW","security":"SYNTHETIC-ROTATED-SECURITY"}
    after_creds=make_client(subscription_url="SYNTHETIC-NEW-SUBSCRIPTION",credentials=creds)
    transport=FakeTransport()
    transport.queue("client.read",{"state":"success","data":{"client":before}},
        {"state":"success","data":{"client":after_sub}},
        {"state":"success","data":{"client":after_sub}},
        {"state":"success","data":{"client":after_creds}})
    transport.queue("subscription.rotate",{"state":"success"})
    transport.queue("credentials.rotate",{"state":"success"})
    adapter=make_adapter(transport)
    url_result=asyncio.run(adapter.rotate_subscription_url("synthetic-user-001","SYNTHETIC-NEW-SUBSCRIPTION","SYNTHETIC-ROTATE-URL"))
    credential_result=asyncio.run(adapter.rotate_connection_credentials("synthetic-user-001",creds,"SYNTHETIC-ROTATE-CREDS"))
    assert url_result.state=="success" and credential_result.state=="success"
    manifest=adapter.capability_manifest()
    assert manifest["rotate_subscription_url"]["status"]=="recorded_test_only"
    assert manifest["rotate_connection_credentials"]["status"]=="recorded_test_only"
    calls=len(transport.calls)
    disconnected=asyncio.run(adapter.disconnect_existing_sessions("synthetic-user-001","SYNTHETIC-DISCONNECT"))
    assert disconnected.state=="unknown"
    assert disconnected.expected_effect["existing_sessions_disconnected"] is False
    assert len(transport.calls)==calls
