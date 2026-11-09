import asyncio
from panel_adapters.xui_openapi import XUIOpenAPIAdapter

class R:
    def __init__(self,status,payload): self.status_code=status; self.payload=payload
    def json(self): return self.payload
class S:
    def __init__(self,*r): self.r=list(r); self.calls=[]
    def request(self,m,u,**kw): self.calls.append((m,u,kw)); return self.r.pop(0)

def test_addon_preserves_period_and_usage():
    def wire(q=10,e=1800000000000):
        return {"email":"a@example.test","enable":True,"subId":"s","totalGB":q,"expiryTime":e,"traffic":{"up":2,"down":3},
                "comment":"keep","limitIp":1,"reset":0,"resetDay":0,"resetMax":0,"resetWeekday":0,"security":"auto","tgId":1,"id":"u"}
    s=S(R(200,{"success":True,"obj":wire()}),R(200,{"success":True,"obj":{"adjusted":1}}),R(200,{"success":True,"obj":wire(15,1800000000000+3*86400)}))
    a=XUIOpenAPIAdapter({"id":1,"raw_url":"https://p.test","auth_cipher":"C"},s,decryptor=lambda *_:"T",transaction_probe=lambda:False,account_id_resolver=lambda *_:101)
    r=asyncio.run(a.add_addon("a@example.test",{"add_bytes":5,"add_days":3},"op"))
    assert r.state=="success" and r.expected_effect["add_bytes"]==5
