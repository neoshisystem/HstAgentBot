import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
import payment_service
import rep_bot
class Message:
    def __init__(self):
        self.photo = [SimpleNamespace(file_id="photo-1")]
        self.message_thread_id = None
        self.media_group_id = None
        self.caption = None
        self.text = "not a V52 draft"
        self.replies = []
    async def reply_text(self, text, **kwargs): self.replies.append(text)
def test_receipt_route_and_nlp_exit(monkeypatch):
    monkeypatch.setattr(rep_bot.database, "get_user_role", lambda *a, **k: ("end_user", None))
    monkeypatch.setattr(rep_bot.database, "is_user_blocked", lambda _u: False)
    monkeypatch.setattr(rep_bot.database, "get_setting", lambda *a, **k: "0")
    monkeypatch.setattr(rep_bot.database, "get_db", lambda: nullcontext(object()))
    calls = []
    monkeypatch.setattr(payment_service, "submit_receipt", lambda conn, **kw: calls.append(kw) or {"receipt_id": 91, "replayed": True, "order_status": "pending_review"})
    async def no_op(*_a, **_k): return None
    monkeypatch.setattr(rep_bot, "process_receipt_notification_outbox", no_op)
    user, chat, msg = SimpleNamespace(id=900), SimpleNamespace(id=900, type="private"), Message()
    update = SimpleNamespace(message=msg, effective_user=user, effective_chat=chat)
    ctx = SimpleNamespace(user_data={"v52_payment_attempt_id": "11111111-1111-4111-8111-111111111111",
        "v52_payment_order_id": "ord-1", "v52_payment_amount": 2500, "v52_payment_pending": True},
        bot=SimpleNamespace(id=123))
    asyncio.run(rep_bot.handle_photo(update, ctx))
    assert calls and calls[0]["attempt_id"].startswith("11111111")
    assert "ثبت تکراری" in msg.replies[0]
    assert "v52_payment_attempt_id" not in ctx.user_data
    # No registered v5.2 text draft owns this message, so legacy NLP remains eligible.
    assert asyncio.run(rep_bot._route_v52_fsm_text(update, SimpleNamespace(
        user_data={}, bot=SimpleNamespace(id=123)
    ))) is False
