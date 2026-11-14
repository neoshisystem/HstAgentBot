import asyncio
from types import SimpleNamespace
import rep_panel
class Query:
    def __init__(self, data): self.data, self.edits = data, []
    async def answer(self, text=None, show_alert=False): pass
    async def edit_message_text(self, text, **kwargs): self.edits.append((text, kwargs))
def test_rep_without_bot_can_issue_manually(monkeypatch):
    monkeypatch.setattr(rep_panel.database, "get_pricing_plans",
        lambda **_k: [{"plan_code": "basic", "name": "Basic", "rep_price": 1000, "gb": 10, "days": 30}])
    q = Query("rep:new_acc_plans")
    user, chat = SimpleNamespace(id=700), SimpleNamespace(id=700, type="private")
    update = SimpleNamespace(callback_query=q, effective_user=user, effective_chat=chat,
        effective_message=SimpleNamespace(message_thread_id=None), message=SimpleNamespace())
    asyncio.run(rep_panel.handle_rep_callbacks(update, SimpleNamespace(user_data={}), {"id": 7, "name": "Rep", "scope_filter": "all"}))
    kb = q.edits[0][1]["reply_markup"]
    assert any(b.callback_data == "rep:new_plan_chosen:basic" for row in kb.inline_keyboard for b in row)
