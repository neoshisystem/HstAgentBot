import asyncio
from types import SimpleNamespace

import rep_bot
import user_portal


class FakeMessage:
    def __init__(self):
        self.message_thread_id = None
        self.text = None
        self.replies = []
        self.photos = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))

    async def reply_photo(self, photo=None, **kwargs):
        self.photos.append((photo, kwargs))


class FakeQuery:
    def __init__(self, data=""):
        self.data = data
        self.answers = []
        self.message = FakeMessage()
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))

    async def edit_message_reply_markup(self, **kwargs):
        self.edits.append(kwargs)


def make_update(data, user_id=73001, chat_type="private", role_user_id=None):
    query = FakeQuery(data)
    user = SimpleNamespace(
        id=user_id, username="subscriber", full_name="Subscriber",
        first_name="Subscriber",
    )
    chat = SimpleNamespace(id=user_id, type=chat_type)
    update = SimpleNamespace(
        callback_query=query,
        effective_user=user,
        effective_chat=chat,
        effective_message=SimpleNamespace(message_thread_id=None),
        message=query.message,
    )
    return update, query


def owned_account():
    return {
        "id": 1, "rep_id": None, "account_code": "ACC-1",
        "email": "acc-1@example.test", "sub_id": "old-sub",
        "owner_tg_id": 73001,
    }


def setup_user_role(monkeypatch):
    monkeypatch.setattr(user_portal.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(user_portal.database, "get_user_role", lambda *a, **k: ("end_user", None))
    monkeypatch.setattr(user_portal.database, "get_user_owned_account", lambda *_: owned_account())


def test_user_reset_partial_result_persists_new_sub_id_and_releases_lock(monkeypatch):
    update, query = make_update("user:reset_sub:ACC-1")
    setup_user_role(monkeypatch)
    events = []
    monkeypatch.setattr(
        user_portal.database, "acquire_operation_lock",
        lambda key, **kwargs: events.append(("acquire", key)) or "lock-token",
    )
    monkeypatch.setattr(
        user_portal.database, "release_operation_lock",
        lambda key, token: events.append(("release", key, token)),
    )
    monkeypatch.setattr(
        user_portal.sub_manager, "reset_client_subscription",
        lambda *a, **k: {"status": "partial", "new_sub_id": "rotated-id"},
    )
    persisted = []
    monkeypatch.setattr(
        user_portal.database, "upsert_account_record",
        lambda **kwargs: persisted.append(kwargs),
    )

    asyncio.run(user_portal.handle_user_callbacks(update, SimpleNamespace(user_data={})))

    assert persisted and persisted[0]["sub_id"] == "rotated-id"
    assert events[0][0] == "acquire" and events[-1][0] == "release"
    assert query.answers and "شناسه جدید" in query.answers[-1][0]


def test_user_reset_unknown_result_keeps_lock_and_does_not_persist_candidate(monkeypatch):
    update, query = make_update("user:reset_sub:ACC-1")
    setup_user_role(monkeypatch)
    released, persisted = [], []
    monkeypatch.setattr(user_portal.database, "acquire_operation_lock", lambda *a, **k: "lock-token")
    monkeypatch.setattr(
        user_portal.database, "release_operation_lock",
        lambda *a, **k: released.append(True),
    )
    monkeypatch.setattr(
        user_portal.sub_manager, "reset_client_subscription",
        lambda *a, **k: {"status": "unknown", "candidate_sub_id": "maybe-rotated"},
    )
    monkeypatch.setattr(
        user_portal.database, "upsert_account_record",
        lambda **kwargs: persisted.append(kwargs),
    )

    asyncio.run(user_portal.handle_user_callbacks(update, SimpleNamespace(user_data={})))

    assert not persisted
    assert not released
    assert query.answers and "دوباره تلاش نکنید" in query.answers[-1][0]


def test_user_reset_local_write_failure_keeps_lock(monkeypatch):
    update, query = make_update("user:reset_sub:ACC-1")
    setup_user_role(monkeypatch)
    released = []
    monkeypatch.setattr(user_portal.database, "acquire_operation_lock", lambda *a, **k: "lock-token")
    monkeypatch.setattr(user_portal.database, "release_operation_lock", lambda *a, **k: released.append(True))
    monkeypatch.setattr(
        user_portal.sub_manager, "reset_client_subscription",
        lambda *a, **k: {"status": "ok", "new_sub_id": "rotated-id", "new_sub_url": "https://sub.example.test/rotated-id"},
    )
    monkeypatch.setattr(
        user_portal.database, "upsert_account_record",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("injected DB write failure")),
    )

    asyncio.run(user_portal.handle_user_callbacks(update, SimpleNamespace(user_data={})))

    assert not released
    assert query.answers and "ثبت محلی کامل نشد" in query.answers[-1][0]


def test_user_qr_generation_failure_is_reported_without_crashing(monkeypatch):
    update, query = make_update("user:get_qr:ACC-1")
    setup_user_role(monkeypatch)
    monkeypatch.setattr(
        user_portal, "xui_manager",
        SimpleNamespace(inspect_client=lambda *a, **k: {"status": "ok", "data": {"sub_link": "https://sub.example.test/token", "sub_id": "token"}}),
    )
    monkeypatch.setattr(user_portal.sub_manager, "generate_qr_code", lambda _payload: (_ for _ in ()).throw(RuntimeError("QR unavailable")))

    asyncio.run(user_portal.handle_user_callbacks(update, SimpleNamespace(user_data={})))

    assert query.answers and query.answers[-1][1] is True
    assert "QR ناموفق" in query.answers[-1][0]
