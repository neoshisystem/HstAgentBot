import asyncio
from types import SimpleNamespace

import pytest

import rep_bot
import user_portal


class FakeQuery:
    def __init__(self, data=""):
        self.data = data
        self.answers = []
        self.replies = []
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeMessage:
    def __init__(self, text=None, photo=None, caption=None):
        self.text = text
        self.photo = photo or []
        self.caption = caption
        self.message_thread_id = None
        self.replies = []

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


def make_update(data="", role="end_user", user_id=71001, chat_type="private"):
    query = FakeQuery(data)
    message = FakeMessage()
    query.message = message
    chat = SimpleNamespace(
        id=71001 if chat_type == "private" else -10071001,
        type=chat_type,
    )
    user = SimpleNamespace(
        id=user_id, username="customer", full_name="Customer", first_name="Customer"
    )
    update = SimpleNamespace(
        callback_query=query,
        message=message,
        effective_message=SimpleNamespace(message_thread_id=None),
        effective_user=user,
        effective_chat=chat,
    )
    return update, query, message


def patch_role(monkeypatch, role, rep=None, blocked=False):
    monkeypatch.setattr(
        rep_bot.database, "get_user_role",
        lambda *args, **kwargs: (role, rep),
    )
    monkeypatch.setattr(rep_bot.database, "is_user_blocked", lambda _uid: blocked)


@pytest.mark.parametrize("role", ["unauthorized_group", "blocked"])
def test_user_portal_callback_namespace_rejects_foreign_roles(monkeypatch, role):
    update, query, _ = make_update("user:wallet", role=role)
    patch_role(monkeypatch, role)
    called = []
    async def forbidden(*args, **kwargs):
        called.append(True)
    monkeypatch.setattr(rep_bot.user_portal, "handle_user_callbacks", forbidden)

    asyncio.run(rep_bot.handle_callback(update, SimpleNamespace(user_data={})))

    assert not called
    assert query.answers and query.answers[0][1] is True


@pytest.mark.parametrize("role", ["rep", "super_admin"])
def test_user_portal_callback_namespace_admits_higher_roles_viewing_as_end_user(monkeypatch, role):
    """BUG-RBAC-001: switching down into the end-user view must not lock the actor out."""
    update, query, _ = make_update("user:wallet", role=role)
    patch_role(monkeypatch, role)
    called = []
    async def allowed(_update, _context):
        called.append(True)
    monkeypatch.setattr(rep_bot.user_portal, "handle_user_callbacks", allowed)

    asyncio.run(rep_bot.handle_callback(update, SimpleNamespace(user_data={})))

    assert called == [True]
    assert not query.answers


def test_user_portal_callback_namespace_allows_resolved_end_user_in_sandbox(monkeypatch):
    update, query, _ = make_update(
        "user:main", role="end_user", chat_type="supergroup"
    )
    patch_role(monkeypatch, "end_user")
    called = []
    async def allowed(_update, _context):
        called.append(True)
    monkeypatch.setattr(rep_bot.user_portal, "handle_user_callbacks", allowed)

    asyncio.run(rep_bot.handle_callback(update, SimpleNamespace(user_data={})))

    assert called == [True]
    assert not query.answers


def test_user_portal_handler_admits_a_representative_viewing_as_end_user(monkeypatch):
    """BUG-RBAC-001: the portal's own guard must not reject a representative."""
    update, query, _ = make_update("user:wallet")
    monkeypatch.setattr(user_portal.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(user_portal.database, "get_user_role", lambda *args, **kwargs: ("rep", {"id": 5}))
    monkeypatch.setattr(user_portal.database, "get_user_wallet_balance", lambda _uid: 0)
    monkeypatch.setattr(user_portal.database, "get_card_info", lambda: {})
    monkeypatch.setattr(user_portal.database, "get_user_receipts", lambda *args, **kwargs: [])

    asyncio.run(user_portal.handle_user_callbacks(update, SimpleNamespace(user_data={})))

    assert query.edits
    # No rejection alert: the only answers allowed here are informational toasts.
    assert all(show_alert is False for _text, show_alert in query.answers)


def test_show_user_portal_admits_super_admin_and_guarantees_customer_rows(monkeypatch):
    """BUG-RBAC-001: a super-admin entering the end-user view keeps a way back out."""
    update, query, message = make_update(role="super_admin")
    monkeypatch.setattr(user_portal.database, "get_user_role", lambda *args, **kwargs: ("super_admin", None))
    monkeypatch.setattr(user_portal.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(user_portal.database, "get_user_wallet_balance", lambda _uid: 0)
    monkeypatch.setattr(user_portal.database, "get_user_linked_accounts", lambda _uid: [])
    guaranteed = []
    monkeypatch.setattr(user_portal, "ensure_portal_customer_rows",
                        lambda user: guaranteed.append(user.id))

    asyncio.run(user_portal.show_user_portal(update, SimpleNamespace()))

    assert guaranteed == [71001]
    assert message.replies
    assert not query.answers
    keyboard = message.replies[0][1]["reply_markup"]
    callbacks = [item.callback_data for row in keyboard.inline_keyboard for item in row]
    assert "v52:persona:rep" in callbacks
    assert "v52:persona:admin" in callbacks


@pytest.mark.parametrize(
    "callback,role,rep,expected",
    [
        ("user_deposit_start", "rep", {"id": 9}, False),
        ("wallet_charge_start", "rep", {"id": 9}, False),
        ("rep_deposit_start", "end_user", None, False),
        ("user_deposit_start", "end_user", None, True),
        ("wallet_charge_start", "end_user", None, True),
        ("rep_deposit_start", "rep", {"id": 9}, True),
    ],
)
def test_deposit_start_callbacks_are_bound_to_their_role(
    monkeypatch, callback, role, rep, expected
):
    update, query, message = make_update(callback, role=role)
    patch_role(monkeypatch, role, rep)

    context = SimpleNamespace(user_data={})
    asyncio.run(rep_bot.handle_callback(update, context))

    if expected:
        expected_role = "rep" if role == "rep" else "end_user"
        assert context.user_data["fsm_state"] == rep_bot.finance_manager.STATE_AWAITING_RECEIPT_AMOUNT
        assert context.user_data["deposit_role"] == expected_role
        assert message.replies
    else:
        assert "deposit_role" not in context.user_data
        assert query.answers and query.answers[0][1] is True


def test_receipt_upload_callback_requires_matching_end_user_receipt_role(monkeypatch):
    update, query, message = make_update(
        "send_receipt_RCP-END-1", role="rep", user_id=71001
    )
    patch_role(monkeypatch, "rep", {"id": 9})
    monkeypatch.setattr(rep_bot.database, "get_payment_receipt", lambda _code: {
        "tracking_code": "RCP-END-1",
        "user_id": 71001,
        "status": "pending",
        "role": "end_user",
        "rep_id": None,
        "amount_toman": 100000,
    })

    context = SimpleNamespace(user_data={})
    asyncio.run(rep_bot.handle_callback(update, context))

    assert not context.user_data
    assert query.answers and query.answers[0][1] is True
    assert not message.replies


def test_representative_receipt_upload_requires_same_rep_id(monkeypatch):
    update, query, message = make_update(
        "send_receipt_RCP-REP-1", role="rep", user_id=71001
    )
    patch_role(monkeypatch, "rep", {"id": 9})
    monkeypatch.setattr(rep_bot.database, "get_payment_receipt", lambda _code: {
        "tracking_code": "RCP-REP-1",
        "user_id": 71001,
        "status": "pending",
        "role": "rep",
        "rep_id": 10,
        "amount_toman": 200000,
    })

    context = SimpleNamespace(user_data={})
    asyncio.run(rep_bot.handle_callback(update, context))

    assert not context.user_data
    assert query.answers and query.answers[0][1] is True
    assert not message.replies


def test_receipt_cancel_callback_requires_matching_role_before_database_cancel(monkeypatch):
    update, query, _ = make_update(
        "cancel_payment:RCP-REP-2", role="end_user", user_id=71001
    )
    patch_role(monkeypatch, "end_user")
    monkeypatch.setattr(rep_bot.database, "get_payment_receipt", lambda _code: {
        "tracking_code": "RCP-REP-2",
        "user_id": 71001,
        "status": "pending",
        "role": "rep",
        "rep_id": 9,
    })
    monkeypatch.setattr(
        rep_bot.database, "cancel_payment_receipt",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("role mismatch must stop before cancel")),
    )

    asyncio.run(rep_bot.handle_callback(update, SimpleNamespace(user_data={})))

    assert query.answers and query.answers[0][1] is True
    assert "نقش" in query.answers[0][0]


def test_send_receipt_callback_rejects_super_admin_role(monkeypatch):
    update, query, _ = make_update("send_receipt_RCP-1", role="super_admin")
    patch_role(monkeypatch, "super_admin")
    monkeypatch.setattr(
        rep_bot.database, "get_payment_receipt",
        lambda *_: (_ for _ in ()).throw(AssertionError("non-wallet roles must be rejected first")),
    )

    asyncio.run(rep_bot.handle_callback(update, SimpleNamespace(user_data={})))

    assert query.answers and query.answers[0][1] is True


def test_photo_receipt_fsm_rejects_non_wallet_roles_before_persistence(monkeypatch):
    user = SimpleNamespace(id=71001, username="customer", full_name="Customer")
    chat = SimpleNamespace(id=71001, type="private")
    photo = [SimpleNamespace(file_id="telegram-photo-id")]
    message = FakeMessage(text=None, photo=photo, caption="500000")
    update = SimpleNamespace(
        message=message,
        effective_user=user,
        effective_chat=chat,
    )
    monkeypatch.setattr(rep_bot.database, "get_user_role", lambda *args, **kwargs: ("super_admin", None))
    monkeypatch.setattr(rep_bot.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(
        rep_bot.database, "create_payment_receipt",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("no receipt for Super Admin photo upload")),
    )

    asyncio.run(rep_bot.handle_photo(update, SimpleNamespace(user_data={})))

    assert message.replies and "نقش" in message.replies[0][0]


def test_amount_entry_fsm_rejects_role_changed_after_deposit_start(monkeypatch):
    update, _, message = make_update(role="super_admin")
    message.text = "500000"
    patch_role(monkeypatch, "super_admin")
    monkeypatch.setattr(
        rep_bot.database, "create_payment_receipt",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("role mismatch must not create receipt")),
    )
    context = SimpleNamespace(user_data={
        "fsm_state": rep_bot.finance_manager.STATE_AWAITING_RECEIPT_AMOUNT,
        "deposit_role": "end_user",
    })

    asyncio.run(rep_bot.handle_message(update, context))

    assert message.replies and "نقش" in message.replies[0][0]
    assert context.user_data["fsm_state"] == rep_bot.finance_manager.STATE_AWAITING_RECEIPT_AMOUNT



@pytest.mark.parametrize(
    "callback,expected_text",
    [
        ("user:wallet", "کیف پول من"),
        ("user:receipts", "سوابق پرداخت‌ها و فیش‌های واریزی"),
    ],
)
def test_post_receipt_user_navigation_callbacks_open_their_pages(
    monkeypatch, callback, expected_text
):
    update, query, _ = make_update(callback, role="end_user")
    monkeypatch.setattr(user_portal.database, "is_user_blocked", lambda _uid: False)
    monkeypatch.setattr(
        user_portal.database, "get_user_role",
        lambda *_args, **_kwargs: ("end_user", None),
    )
    monkeypatch.setattr(user_portal.database, "get_user_wallet_balance", lambda _uid: 42_000)
    monkeypatch.setattr(user_portal.database, "get_card_info", lambda: {})
    monkeypatch.setattr(user_portal.database, "get_user_receipts", lambda *_args, **_kwargs: [])

    asyncio.run(user_portal.handle_user_callbacks(update, SimpleNamespace(user_data={})))

    assert query.edits
    assert expected_text in query.edits[0][0]


@pytest.mark.parametrize(
    "role,expected_callbacks",
    [
        ("rep", [["rep:finance"], ["rep:main"]]),
        ("end_user", [["user:wallet", "user:receipts"], ["user:main"]]),
    ],
)
def test_handle_photo_success_attaches_role_specific_navigation_keyboard(
    monkeypatch, role, expected_callbacks
):
    user_id = 94001 if role == "rep" else 94002
    rep = {"id": 94, "name": "Receipt Rep"} if role == "rep" else None
    update, _query, message = make_update(
        role=role,
        user_id=user_id,
        chat_type="supergroup" if role == "rep" else "private",
    )
    message.photo = [SimpleNamespace(file_id=f"photo-{user_id}")]
    patch_role(monkeypatch, role, rep)
    monkeypatch.setattr(
        rep_bot.database, "get_setting",
        lambda _key, default=0, cast_type=int: 0,
    )
    submissions = []
    monkeypatch.setattr(
        rep_bot.database, "submit_payment_receipt",
        lambda **kwargs: (
            submissions.append(kwargs)
            or {
                "id": 501,
                "tracking_code": "RCP-NAV-TEST",
                "amount_toman": 500_000,
            }
        ),
    )

    async def no_op_outbox(_bot, limit=10):
        return {"attempted": 0, "sent": 0}

    monkeypatch.setattr(rep_bot, "process_receipt_notification_outbox", no_op_outbox)
    context = SimpleNamespace(bot=object(), user_data={
        "fsm_state": "awaiting_receipt_photo",
        "active_receipt_code": "RCP-NAV-TEST",
        "deposit_amount": 500_000,
        "deposit_role": role,
    })

    asyncio.run(rep_bot.handle_photo(update, context))

    assert submissions and submissions[0]["role"] == role
    assert message.replies
    keyboard = message.replies[0][1]["reply_markup"]
    callbacks = [
        [button.callback_data for button in row]
        for row in keyboard.inline_keyboard
    ]
    assert callbacks == expected_callbacks
    assert "active_receipt_code" not in context.user_data
    assert "deposit_amount" not in context.user_data


@pytest.mark.parametrize(
    "role,expected_callbacks",
    [
        ("rep", [["rep:finance"], ["rep:main"]]),
        ("end_user", [["user:wallet", "user:receipts"], ["user:main"]]),
    ],
)
def test_handle_photo_rejects_image_document_with_recoverable_role_navigation(
    monkeypatch, role, expected_callbacks
):
    user_id = 94101 if role == "rep" else 94102
    rep = {"id": 94, "name": "Receipt Rep"} if role == "rep" else None
    update, _query, message = make_update(
        role=role,
        user_id=user_id,
        chat_type="supergroup" if role == "rep" else "private",
    )
    message.photo = []
    message.document = SimpleNamespace(mime_type="image/png", file_id=f"document-{user_id}")
    patch_role(monkeypatch, role, rep)

    asyncio.run(rep_bot.handle_photo(update, SimpleNamespace(user_data={})))

    assert message.replies
    assert "Photo" in message.replies[0][0]
    keyboard = message.replies[0][1]["reply_markup"]
    callbacks = [
        [button.callback_data for button in row]
        for row in keyboard.inline_keyboard
    ]
    assert callbacks == expected_callbacks
