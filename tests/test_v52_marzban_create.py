"""W21.4 stable create identity, candidate preflight and timeout semantics."""
import asyncio
from test_v52_marzban_contract import FakeTransport, make_adapter, make_client

def test_recorded_create_and_timeout():
    candidate={"uuid":"REDACTED:SYNTHETIC-NEW-UUID"}
    created=make_client(username="synthetic-user-002",operation_id="SYNTHETIC-CREATE-001",credentials=candidate)
    transport=FakeTransport()
    transport.queue("client.read",{"state":"not_found"},{"state":"success","data":{"client":created}})
    transport.queue("client.create",{"state":"success"})
    persisted=[]
    adapter=make_adapter(transport,operation_preflight=lambda op, creds, cap:persisted.append((op,creds,cap)) or True)
    spec={"operation_id":"SYNTHETIC-CREATE-001","email":"synthetic-user-002","quota_mode":"finite",
          "quota_bytes":2097152,"duration_seconds":86400,"on_first_connect":False,"inbound_ids":(11,),
          "comment":"synthetic","candidate_credentials":candidate}
    result=asyncio.run(adapter.create_client(spec))
    assert result.state=="success"
    assert result.expected_effect["canonical_ref"]==(7001,71,"synthetic-user-002")
    assert persisted==[("SYNTHETIC-CREATE-001",candidate,"create_client")]
    create_call=next(c for c in transport.calls if c[0]=="client.create")
    assert create_call[1]["username"]=="synthetic-user-002"
    assert create_call[1]["operation_id"]=="SYNTHETIC-CREATE-001"
    timeout_transport=FakeTransport()
    timeout_transport.queue("client.read",{"state":"not_found"})
    timeout_transport.queue("client.create",TimeoutError("synthetic timeout"))
    timeout_result=asyncio.run(make_adapter(timeout_transport).create_client(spec))
    assert timeout_result.state=="unknown"
    assert [c[0] for c in timeout_transport.calls].count("client.create")==1
