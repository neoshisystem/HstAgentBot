import asyncio
from types import SimpleNamespace
import account_actions_ui
import rep_bot
class Message:
    message_id = 1
    message_thread_id = None
class Query:
    def __init__(self, data): self.data, self.answers, self.edits = data, [], []
    async def answer(self, text=None, show_alert=False): self.answers.append((text, show_alert))
    async def edit_message_text(self, text, **kwargs): self.edits.append((text, kwargs))
def test_old_destructive_button_never_executes(monkeypatch):
    monkeypatch.setattr(rep_bot.database, "get_setting", lambda *a, **k: "1")
    monkeypatch.setattr(rep_bot.database, "get_user_role", lambda *a, **k: ("rep", {"id": 7, "scope_filter": "all"}))
    monkeypatch.setattr(rep_bot.database, "is_user_blocked", lambda _u: False)
    monkeypatch.setattr(rep_bot.database, "get_account_by_code_or_email", lambda _c: {"id": 12, "account_code": "ACC-12"})
    monkeypatch.setattr(account_actions_ui, "preview_rotation", lambda *a, **k: SimpleNamespace(text="preview only", reply_markup=None))
    mutations = []
    monkeypatch.setattr(rep_bot.sub_manager, "reset_client_subscription", lambda *a, **k: mutations.append(1) or {"status": "ok"})
    q = Query("reset_sub:ACC-12")
    user, chat = SimpleNamespace(id=700), SimpleNamespace(id=700, type="private")
    update = SimpleNamespace(callback_query=q, effective_user=user, effective_chat=chat,
        effective_message=SimpleNamespace(message_thread_id=None), message=Message())
    asyncio.run(rep_bot.handle_callback(update, SimpleNamespace(user_data={}, bot=SimpleNamespace(id=123))))
    assert mutations == []
    assert q.edits and q.edits[0][0] == "preview only"
