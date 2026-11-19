import asyncio
from types import SimpleNamespace
import user_portal
class Query:
    def __init__(self, data): self.data, self.answers = data, []
    async def answer(self, text=None, show_alert=False): self.answers.append((text, show_alert))
def test_primary_rep_service_readonly(monkeypatch):
    monkeypatch.setattr(user_portal.database, "get_setting", lambda *a, **k: "1")
    monkeypatch.setattr(user_portal.database, "is_user_blocked", lambda _u: False)
    monkeypatch.setattr(user_portal.database, "get_user_role", lambda *a, **k: ("end_user", None))
    monkeypatch.setattr(user_portal.database, "get_user_owned_account", lambda *_: {"id": 12, "rep_id": 7, "account_code": "ACC-12", "email": "a@test"})
    calls = []
    monkeypatch.setattr(user_portal.database, "get_pricing_plans", lambda **_k: calls.append(1) or [])
    q = Query("user:renew_plans:ACC-12")
    user, chat = SimpleNamespace(id=501), SimpleNamespace(id=501, type="private")
    update = SimpleNamespace(callback_query=q, effective_user=user, effective_chat=chat,
        effective_message=SimpleNamespace(message_thread_id=None), message=SimpleNamespace())
    asyncio.run(user_portal.handle_user_callbacks(update, SimpleNamespace(user_data={})))
    assert q.answers and q.answers[-1][1] is True
    assert calls == []
