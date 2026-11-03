import asyncio
import time

import pytest

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
    def __init__(self, photo_error=None, text_error=None):
        self.photo_error = photo_error
        self.text_error = text_error
        self.calls = []

    async def send_photo(self, **kwargs):
        self.calls.append(("photo", kwargs))
        if self.photo_error:
            raise self.photo_error
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
