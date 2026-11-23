import asyncio

import finance_manager


class MessageStub:
    def __init__(self, photo_error=None):
        self.photo_error = photo_error
        self.calls = []

    async def reply_photo(self, **kwargs):
        self.calls.append(("photo", kwargs))
        if self.photo_error:
            raise self.photo_error
        return "photo-sent"

    async def reply_text(self, **kwargs):
        self.calls.append(("text", kwargs))
        return "text-sent"


def test_receipt_detail_replays_persisted_photo():
    message = MessageStub()
    receipt = {"id": 17, "receipt_photo_id": "telegram-file-id"}
    markup = object()

    result = asyncio.run(
        finance_manager.reply_with_receipt_detail(
            message, receipt, "<b>Receipt</b>", reply_markup=markup
        )
    )

    assert result == "photo-sent"
    assert len(message.calls) == 1
    kind, payload = message.calls[0]
    assert kind == "photo"
    assert payload["photo"] == "telegram-file-id"
    assert payload["caption"] == "<b>Receipt</b>"
    assert payload["reply_markup"] is markup


def test_receipt_detail_falls_back_to_text_when_photo_send_fails():
    message = MessageStub(photo_error=RuntimeError("simulated Telegram photo failure"))
    receipt = {"id": 18, "receipt_photo_id": "unavailable-file-id"}

    result = asyncio.run(
        finance_manager.reply_with_receipt_detail(
            message, receipt, "<b>Receipt details</b>"
        )
    )

    assert result == "text-sent"
    assert [kind for kind, _ in message.calls] == ["photo", "text"]
    assert message.calls[1][1]["text"] == "<b>Receipt details</b>"


def test_receipt_detail_uses_text_when_no_photo_is_stored():
    message = MessageStub()
    receipt = {"id": 19, "receipt_photo_id": None}

    result = asyncio.run(
        finance_manager.reply_with_receipt_detail(
            message, receipt, "<b>Receipt without image</b>"
        )
    )

    assert result == "text-sent"
    assert len(message.calls) == 1
    assert message.calls[0][0] == "text"


def test_admin_receipt_notification_keyboard_has_details_action():
    keyboard = finance_manager.build_admin_receipt_review_keyboard(20)
    callbacks = [
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    ]

    assert "admin_detail_rcp_20" in callbacks
    assert "admin_approve_rcp_20" in callbacks
    assert "admin_reject_rcp_20" in callbacks
