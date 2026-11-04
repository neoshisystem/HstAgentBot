import asyncio
import time

import pytest
from telegram.error import BadRequest

import config
import database
from receipt_notifications import process_receipt_notification_outbox


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "receipt-outbox.sqlite3"))
    database.init_db()


def submit_existing_receipt():
    code = database.create_payment_receipt(
        user_id=91001,
        role="end_user",
        amount_toman=125_000,
    )
    receipt = database.submit_payment_receipt(
        user_id=91001,
        role="end_user",
        amount_toman=125_000,
        photo_id="telegram-photo-001",
        receipt_id_or_code=code,
        recipient_chat_id=9000,
        recipient_thread_id=77,
        sender_name="Receipt Sender",
        sender_username="receipt_user",
    )
    return receipt


def test_existing_fsm_receipt_and_outbox_are_persisted_together(isolated_db):
    code = database.create_payment_receipt(
        user_id=91002, role="end_user", amount_toman=250_000
    )
    receipt = database.submit_payment_receipt(
        user_id=91002,
        role="end_user",
        amount_toman=250_000,
        photo_id="fsm-photo",
        receipt_id_or_code=code,
        recipient_chat_id=9000,
        recipient_thread_id=77,
        sender_name="Sender",
    )

    outbox = database.get_receipt_notification_outbox(receipt["id"])
    assert receipt["receipt_photo_id"] == "fsm-photo"
    assert outbox["status"] == "pending"
    assert outbox["recipient_chat_id"] == 9000
    assert outbox["recipient_thread_id"] == 77
    assert outbox["sender_name"] == "Sender"
    assert outbox["attempts"] == 0


def test_direct_photo_receipt_and_outbox_are_created_atomically(isolated_db):
    receipt = database.submit_payment_receipt(
        user_id=91003,
        role="end_user",
        amount_toman=300_000,
        photo_id="direct-photo",
        note="bank note",
        recipient_chat_id=9000,
    )
    outbox = database.get_receipt_notification_outbox(receipt["id"])
    assert receipt["status"] == "pending"
    assert receipt["receipt_photo_id"] == "direct-photo"
    assert outbox is not None
    assert outbox["receipt_id"] == receipt["id"]


def test_submission_replay_is_idempotent_and_different_photo_is_rejected(isolated_db):
    receipt = submit_existing_receipt()
    replay = database.submit_payment_receipt(
        user_id=91001,
        role="end_user",
        amount_toman=125_000,
        photo_id="telegram-photo-001",
        receipt_id_or_code=receipt["id"],
        recipient_chat_id=9000,
    )
    assert replay["id"] == receipt["id"]
    assert database.get_receipt_notification_outbox(receipt["id"])["id"] is not None

    with pytest.raises(ValueError, match="different photo"):
        database.submit_payment_receipt(
            user_id=91001,
            role="end_user",
            amount_toman=125_000,
            photo_id="replacement-photo",
            receipt_id_or_code=receipt["id"],
        )
    assert database.get_payment_receipt(receipt["id"])["receipt_photo_id"] == "telegram-photo-001"


def test_outbox_enqueue_failure_rolls_back_receipt_photo_update(isolated_db, monkeypatch):
    code = database.create_payment_receipt(
        user_id=91004, role="end_user", amount_toman=100_000
    )

    def fail_enqueue(*_args, **_kwargs):
        raise RuntimeError("injected outbox insert failure")

    monkeypatch.setattr(database, "_insert_receipt_notification_outbox_cur", fail_enqueue)
    with pytest.raises(RuntimeError, match="injected outbox"):
        database.submit_payment_receipt(
            user_id=91004,
            role="end_user",
            amount_toman=100_000,
            photo_id="rollback-photo",
            receipt_id_or_code=code,
            recipient_chat_id=9000,
        )

    stored = database.get_payment_receipt(code)
    assert stored["receipt_photo_id"] is None
    assert database.get_receipt_notification_outbox(stored["id"]) is None


class FakeBot:
    def __init__(self, photo_error=None, text_error=None, photo_errors=None):
        self.photo_error = photo_error
        self.photo_errors = list(photo_errors) if photo_errors is not None else None
        self.text_error = text_error
        self.calls = []

    async def send_photo(self, **kwargs):
        self.calls.append(("photo", kwargs))
        error = self.photo_errors.pop(0) if self.photo_errors else self.photo_error
        if error:
            raise error
        return object()

    async def send_message(self, **kwargs):
        self.calls.append(("text", kwargs))
        if self.text_error:
            raise self.text_error
        return object()


def test_photo_failure_text_fallback_success_marks_outbox_sent(isolated_db):
    receipt = submit_existing_receipt()
    bot = FakeBot(photo_error=RuntimeError("photo unavailable"))
    result = asyncio.run(process_receipt_notification_outbox(bot, limit=1))

    outbox = database.get_receipt_notification_outbox(receipt["id"])
    assert result == {"attempted": 1, "sent": 1}
    assert [kind for kind, _ in bot.calls] == ["photo", "text"]
    assert outbox["status"] == "sent"
    assert outbox["delivery_method"] == "text_fallback"
    assert outbox["delivered_at"] is not None


def test_photo_and_text_failure_schedule_durable_retry_without_financial_effects(isolated_db):
    receipt = submit_existing_receipt()
    bot = FakeBot(
        photo_error=RuntimeError("photo unavailable"),
        text_error=RuntimeError("admin chat unavailable"),
    )
    before = time.time()
    result = asyncio.run(process_receipt_notification_outbox(bot, limit=1))

    outbox = database.get_receipt_notification_outbox(receipt["id"])
    assert result == {"attempted": 1, "sent": 0}
    assert outbox["status"] == "pending"
    assert outbox["attempts"] == 1
    assert outbox["next_attempt_at"] >= before + 59
    assert "text fallback failed" in outbox["last_error"]
    assert database.get_user_wallet_balance(91001) == 0
    with database.get_db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM payment_receipts").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM financial_ledger").fetchone()[0] == 0


def test_retry_reclaims_expired_lease_and_sent_rows_are_terminal(isolated_db):
    receipt = submit_existing_receipt()
    first = database.claim_next_receipt_notification(now=1000, lease_seconds=30)
    assert first is not None
    assert first["attempts"] == 1
    assert database.claim_next_receipt_notification(now=1010, lease_seconds=30) is None

    second = database.claim_next_receipt_notification(now=1031, lease_seconds=30)
    assert second is not None
    assert second["attempts"] == 2
    assert database.mark_receipt_notification_sent(
        second["notification_id"], second["lease_token"], "photo"
    ) is True
    assert database.claim_next_receipt_notification(now=2000) is None
    outbox = database.get_receipt_notification_outbox(receipt["id"])
    assert outbox["status"] == "sent"
    assert outbox["attempts"] == 2


def test_notification_is_superseded_when_receipt_is_already_reviewed(isolated_db):
    receipt = submit_existing_receipt()
    database.approve_payment_receipt(receipt["id"], 9000, "Admin")
    assert database.claim_next_receipt_notification(now=time.time()) is None
    outbox = database.get_receipt_notification_outbox(receipt["id"])
    assert outbox["status"] == "superseded"


def test_unconfigured_admin_destination_keeps_notification_pending(isolated_db, monkeypatch):
    receipt = database.submit_payment_receipt(
        user_id=91005,
        role="end_user",
        amount_toman=80_000,
        photo_id="no-admin-photo",
        recipient_chat_id=None,
    )
    monkeypatch.setattr(database, "get_setting", lambda *_args, **_kwargs: 0)
    bot = FakeBot()
    result = asyncio.run(process_receipt_notification_outbox(bot, limit=1))
    outbox = database.get_receipt_notification_outbox(receipt["id"])
    assert result == {"attempted": 1, "sent": 0}
    assert bot.calls == []
    assert outbox["status"] == "pending"
    assert "not configured" in outbox["last_error"]


def _submit_receipt_to_destination(chat_id, thread_id=None, user_id=91020):
    return database.submit_payment_receipt(
        user_id=user_id,
        role="end_user",
        amount_toman=120_000,
        photo_id=f"photo-{user_id}",
        recipient_chat_id=chat_id,
        recipient_thread_id=thread_id,
        sender_name="Topic Test",
    )


def test_private_admin_chat_ignores_configured_thread_id(isolated_db):
    receipt = submit_existing_receipt()
    bot = FakeBot()

    result = asyncio.run(process_receipt_notification_outbox(bot, limit=1))

    assert result == {"attempted": 1, "sent": 1}
    assert bot.calls[0][0] == "photo"
    assert bot.calls[0][1]["chat_id"] == 9000
    assert bot.calls[0][1]["message_thread_id"] is None
    assert database.get_receipt_notification_outbox(receipt["id"])["status"] == "sent"


def test_thread_not_found_retries_same_photo_without_thread(isolated_db):
    receipt = _submit_receipt_to_destination(-1009000, 77)
    bot = FakeBot(photo_errors=[BadRequest("Message thread not found"), None])

    result = asyncio.run(process_receipt_notification_outbox(bot, limit=1))

    assert result == {"attempted": 1, "sent": 1}
    photo_calls = [kwargs for kind, kwargs in bot.calls if kind == "photo"]
    assert len(photo_calls) == 2
    assert [call["chat_id"] for call in photo_calls] == [-1009000, -1009000]
    assert [call["message_thread_id"] for call in photo_calls] == [77, None]
    assert all(call["photo"] == f"photo-{receipt['user_id']}" for call in photo_calls)
    outbox = database.get_receipt_notification_outbox(receipt["id"])
    assert outbox["status"] == "sent"
    assert outbox["delivery_method"] == "photo"


def test_thread_not_found_falls_back_to_admin_dm_after_same_chat_retry_fails(
    isolated_db, monkeypatch
):
    receipt = _submit_receipt_to_destination(-1009100, 88, user_id=91021)
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 99001)
    bot = FakeBot(
        photo_errors=[
            BadRequest("Message thread not found"),
            BadRequest("Message thread not found"),
            None,
        ]
    )

    result = asyncio.run(process_receipt_notification_outbox(bot, limit=1))

    assert result == {"attempted": 1, "sent": 1}
    photo_calls = [kwargs for kind, kwargs in bot.calls if kind == "photo"]
    assert len(photo_calls) == 3
    assert [call["chat_id"] for call in photo_calls] == [-1009100, -1009100, 99001]
    assert [call["message_thread_id"] for call in photo_calls] == [88, None, None]
    assert photo_calls[-1]["photo"] == f"photo-{receipt['user_id']}"
    assert [kind for kind, _ in bot.calls] == ["photo", "photo", "photo"]

    outbox = database.get_receipt_notification_outbox(receipt["id"])
    assert outbox["status"] == "sent"
    assert outbox["delivery_method"] == "photo"
    assert database.get_user_wallet_balance(receipt["user_id"]) == 0
    with database.get_db() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM payment_receipts WHERE user_id = ?",
            (receipt["user_id"],),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM financial_ledger WHERE user_id = ?",
            (receipt["user_id"],),
        ).fetchone()[0] == 0


def test_non_topic_bad_request_keeps_standard_text_fallback(isolated_db):
    receipt = submit_existing_receipt()
    bot = FakeBot(photo_errors=[BadRequest("Photo file is too large")])

    result = asyncio.run(process_receipt_notification_outbox(bot, limit=1))

    assert result == {"attempted": 1, "sent": 1}
    assert [kind for kind, _ in bot.calls] == ["photo", "text"]
    assert bot.calls[0][1]["chat_id"] == 9000
    assert bot.calls[0][1]["message_thread_id"] is None
    assert database.get_receipt_notification_outbox(receipt["id"])["delivery_method"] == "text_fallback"


def test_admin_dm_fallback_is_skipped_when_it_is_the_same_chat(isolated_db, monkeypatch):
    receipt = submit_existing_receipt()
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9000)
    bot = FakeBot(
        photo_errors=[
            BadRequest("Message thread not found"),
            BadRequest("Message thread not found"),
        ]
    )

    result = asyncio.run(process_receipt_notification_outbox(bot, limit=1))

    assert result == {"attempted": 1, "sent": 1}
    assert [kind for kind, _ in bot.calls] == ["photo", "photo", "text"]
    assert [call[1]["chat_id"] for call in bot.calls] == [9000, 9000, 9000]
    assert database.get_receipt_notification_outbox(receipt["id"])["status"] == "sent"

