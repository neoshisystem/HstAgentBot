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

def make_rep_card_update(data="reset_sub:ACC-1"):
    query = FakeQuery(data)
    user = SimpleNamespace(id=88001, username="rep", full_name="Rep", first_name="Rep")
    chat = SimpleNamespace(id=88001, type="private")
    update = SimpleNamespace(
        callback_query=query,
        effective_user=user,
        effective_chat=chat,
        effective_message=SimpleNamespace(message_thread_id=None),
        message=query.message,
    )
    return update, query


def patch_rep_card_preflight(monkeypatch):
    rep = {"id": 5, "name": "Rep", "scope_filter": "all"}
    account = {
        "id": 1, "rep_id": 5, "account_code": "ACC-1",
        "email": "acc-1@example.test", "sub_id": "old-sub",
    }
    monkeypatch.setattr(rep_bot.database, "get_user_role", lambda *a, **k: ("rep", rep))
    monkeypatch.setattr(rep_bot.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(rep_bot.database, "is_account_in_rep_scope", lambda *_: account)
    monkeypatch.setattr(rep_bot.database, "get_user_first_connect_mode", lambda _uid: False)
    monkeypatch.setattr(
        rep_bot.database, "get_account_renewal_modes",
        lambda *_: {"first_connect_mode": False, "rollover_enabled": True},
    )
    monkeypatch.setattr(
        rep_bot, "xui_manager",
        SimpleNamespace(inspect_client=lambda *_a, **_k: {
            "status": "ok",
            "data": {
                "raw": {"email": "acc-1@example.test", "enable": True, "comment": ""},
                "email": "acc-1@example.test",
                "sub_id": "old-sub",
            },
        }),
    )
    return rep


def test_rep_account_card_reset_partial_persists_id_and_releases_shared_lock(monkeypatch):
    update, query = make_rep_card_update()
    patch_rep_card_preflight(monkeypatch)
    events, persisted = [], []
    monkeypatch.setattr(
        rep_bot.database, "acquire_operation_lock",
        lambda key, **kwargs: events.append(("acquire", key)) or "token",
    )
    monkeypatch.setattr(
        rep_bot.database, "release_operation_lock",
        lambda key, token: events.append(("release", key, token)),
    )
    monkeypatch.setattr(
        rep_bot.sub_manager, "reset_client_subscription",
        lambda *a, **k: {"status": "partial", "new_sub_id": "rotated-sub"},
    )
    monkeypatch.setattr(rep_bot.database, "upsert_account_record", lambda **kwargs: persisted.append(kwargs))
    monkeypatch.setattr(rep_bot.database, "log_audit", lambda **kwargs: None)

    asyncio.run(rep_bot.handle_callback(update, SimpleNamespace(user_data={})))

    assert persisted and persisted[0]["sub_id"] == "rotated-sub"
    assert events[0][0] == "acquire" and events[-1][0] == "release"
    assert events[0][1] == "subscription-reset:acc-1@example.test"
    assert query.answers and "لینک عمومی ساخته نشد" in query.answers[-1][0]


def test_rep_account_card_reset_unknown_keeps_lock(monkeypatch):
    update, query = make_rep_card_update()
    patch_rep_card_preflight(monkeypatch)
    released, persisted = [], []
    monkeypatch.setattr(rep_bot.database, "acquire_operation_lock", lambda *a, **k: "token")
    monkeypatch.setattr(rep_bot.database, "release_operation_lock", lambda *a, **k: released.append(True))
    monkeypatch.setattr(
        rep_bot.sub_manager, "reset_client_subscription",
        lambda *a, **k: {"status": "unknown", "candidate_sub_id": "possible"},
    )
    monkeypatch.setattr(rep_bot.database, "upsert_account_record", lambda **kwargs: persisted.append(kwargs))

    asyncio.run(rep_bot.handle_callback(update, SimpleNamespace(user_data={})))

    assert not released
    assert not persisted
    assert query.answers and "قفل تطبیق حفظ شد" in query.answers[-1][0]


def test_rep_account_card_reset_local_persistence_failure_keeps_lock(monkeypatch):
    update, query = make_rep_card_update()
    patch_rep_card_preflight(monkeypatch)
    released = []
    monkeypatch.setattr(rep_bot.database, "acquire_operation_lock", lambda *a, **k: "token")
    monkeypatch.setattr(rep_bot.database, "release_operation_lock", lambda *a, **k: released.append(True))
    monkeypatch.setattr(
        rep_bot.sub_manager, "reset_client_subscription",
        lambda *a, **k: {"status": "ok", "new_sub_id": "rotated-sub", "new_sub_url": "https://sub.example.test/rotated-sub"},
    )
    monkeypatch.setattr(
        rep_bot.database, "upsert_account_record",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("injected DB failure")),
    )

    asyncio.run(rep_bot.handle_callback(update, SimpleNamespace(user_data={})))

    assert not released
    assert query.answers and "ثبت محلی انجام نشد" in query.answers[-1][0]



def test_user_reset_first_click_is_preview_only_when_v52_is_enabled(monkeypatch):
    update, query = make_update("user:reset_sub:ACC-1")
    setup_user_role(monkeypatch)
    monkeypatch.setattr(user_portal, "_v52_ui_enabled", lambda: True)
    events = []

    async def show_preview(*args, **kwargs):
        events.append("preview")

    monkeypatch.setattr(user_portal, "_show_legacy_reset_preview", show_preview)
    monkeypatch.setattr(
        user_portal.database, "acquire_operation_lock",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("first click acquired mutation lock")),
    )
    monkeypatch.setattr(
        user_portal.sub_manager, "reset_client_subscription",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("first click reached panel mutation")),
    )
    asyncio.run(user_portal.handle_user_callbacks(update, SimpleNamespace(user_data={})))
    assert events == ["preview"]
    assert query.answers == []
