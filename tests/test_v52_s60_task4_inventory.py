"""S60 Task 4 — block-first inventory import and the structured-item wizard.

One test per behaviour that was wrong or missing:

1. ``inventory_parser.resolve_mode`` treats a separator-free *labelled* credential
   card as one item, while a bare list of codes still splits per line;
2. ``begin_digital_import(mode="block")`` keeps a pasted card whole;
3. the ``inv_structured_item`` wizard exists with four skippable steps, assembles a
   card that the parser recognises, and inserts exactly one record;
4. the inventory page offers the two explicit modes and the wizard, and the routes
   resolve to handlers that exist.

Offline only: no Telegram, no network, no live database.
"""
from __future__ import annotations

import pytest

import config
import inventory_admin_ui
import inventory_parser
import ui_v52
import v52_router

PRODUCT_ID = 5
ROOT_ID = 777


@pytest.fixture(autouse=True)
def _root_actor(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", ROOT_ID, raising=False)


def _ctx(**overrides):
    ctx = {"actor_tg_id": ROOT_ID, "persona": "admin", "chat_type": "private",
           "bot_id": 1}
    ctx.update(overrides)
    return ctx


# --------------------------------------------------------------------------- #
# 1. The parser heuristic
# --------------------------------------------------------------------------- #

def test_a_labelled_card_is_one_item():
    card = "نام کاربری: ali@x.invalid\nرمز عبور: p@ss\nکلید: API-ONE"
    assert inventory_parser.resolve_mode(card) == "block"
    assert inventory_parser.parse_batch(card) == [card]


def test_a_bare_list_of_codes_still_splits_per_line():
    assert inventory_parser.resolve_mode("a\nb") == "line"
    assert inventory_parser.resolve_mode("one\ntwo\nthree") == "line"
    assert inventory_parser.parse_batch("one\ntwo\nthree") == ["one", "two", "three"]


def test_an_english_product_code_is_not_mistaken_for_a_labelled_field():
    """``KEY-AAAA`` is a code; a hyphen may only separate a *Persian* label."""
    assert inventory_parser.looks_like_credential_card("KEY-AAAA\nKEY-BBBB") is False
    assert inventory_parser.resolve_mode("KEY-AAAA\nKEY-BBBB") == "line"


def test_a_single_line_is_never_a_card():
    assert inventory_parser.looks_like_credential_card("نام کاربری: ali") is False


def test_an_emoji_labelled_card_is_one_item():
    assert inventory_parser.looks_like_credential_card("📧 ali@x.invalid\n🔑 p@ss") is True


def test_a_separator_still_wins():
    assert inventory_parser.resolve_mode("a\n---\nb") == "block"


def test_an_explicit_mode_is_never_overridden():
    assert inventory_parser.resolve_mode("نام کاربری: a\nرمز عبور: b", "line") == "line"
    assert inventory_parser.resolve_mode("plain", "block") == "block"


# --------------------------------------------------------------------------- #
# 2. The import honours the chosen mode
# --------------------------------------------------------------------------- #

class _Stager:
    def __init__(self):
        self.calls = []

    def __call__(self, ctx, *, kind, target_id, items, meta, now=None):
        self.calls.append({"kind": kind, "target_id": target_id,
                           "items": list(items), "meta": dict(meta)})
        return {"callback_confirm": "v52:invok:" + "a" * 24,
                "callback_cancel": "v52:invno:" + "a" * 24}


def test_a_pasted_card_stays_one_item_in_block_mode():
    stager = _Stager()
    card = "نام کاربری: ali@x.invalid\nرمز عبور: p@ss\nکلید: API-ONE"
    page = inventory_admin_ui.begin_digital_import(
        _ctx(), PRODUCT_ID, card, mode="block", stager=stager)
    assert stager.calls[0]["items"] == [card]
    assert "یک قلم" in page.text or "چندخطی" in page.text


def test_line_mode_splits_a_pasted_card():
    stager = _Stager()
    page = inventory_admin_ui.begin_digital_import(
        _ctx(), PRODUCT_ID, "a\nb\nc", mode="line", stager=stager)
    assert stager.calls[0]["items"] == ["a", "b", "c"]
    assert page.reply_markup is not None


def test_the_import_is_root_and_private_only():
    stager = _Stager()
    page = inventory_admin_ui.begin_digital_import(
        _ctx(actor_tg_id=1), PRODUCT_ID, "a", mode="line", stager=stager)
    assert stager.calls == []
    assert "root" in page.text.lower() or "مدیریت" in page.text


# --------------------------------------------------------------------------- #
# 3. The structured-item wizard
# --------------------------------------------------------------------------- #

def test_the_wizard_is_registered_with_four_skippable_steps():
    flow = ui_v52.flow_by_key(inventory_admin_ui.STRUCTURED_ITEM_WIZARD_KEY)
    assert flow is not None
    assert [step.key for step in flow.steps] == ["username", "password", "secret", "guide"]
    assert all(step.skippable for step in flow.steps)


def test_the_assembled_card_is_recognised_by_the_parser():
    card = inventory_admin_ui._structured_card(
        {"username": "ali@x.invalid", "password": "p@ss",
         "secret": "API-ONE", "guide": "change the password"})
    assert card == ("نام کاربری: ali@x.invalid\nرمز عبور: p@ss\n"
                    "کلید: API-ONE\nتوضیحات: change the password")
    assert inventory_parser.resolve_mode(card) == "block"
    assert inventory_parser.parse_batch(card) == [card]


def test_an_empty_answer_is_dropped_from_the_card():
    assert inventory_admin_ui._structured_card({"secret": "ONLY-KEY"}) == "کلید: ONLY-KEY"
    assert inventory_admin_ui._structured_card({}) == ""


def test_the_preview_escapes_the_card():
    preview = inventory_admin_ui._preview_structured_item({"username": "a<b", "secret": "K"})
    assert "a&lt;b" in preview
    assert "<pre>" in preview


def test_the_commit_inserts_exactly_one_record(monkeypatch):
    inserted = []

    def fake_import(product_id, items, **kwargs):
        inserted.append((product_id, list(items)))
        return [1]

    import goods_service
    monkeypatch.setattr(goods_service, "import_inventory", fake_import)
    notice = inventory_admin_ui._commit_structured_item(
        {"product_id": PRODUCT_ID, "username": "ali", "secret": "K"}, {"actor_tg_id": ROOT_ID})
    assert inserted == [(PRODUCT_ID, ["نام کاربری: ali\nکلید: K"])]
    assert str(PRODUCT_ID) in notice


def test_the_commit_refuses_an_empty_card():
    import pytest
    with pytest.raises(ValueError):
        inventory_admin_ui._commit_structured_item({"product_id": PRODUCT_ID}, {})


# --------------------------------------------------------------------------- #
# 4. The routes
# --------------------------------------------------------------------------- #

def test_the_stock_chooser_offers_both_modes_and_the_wizard(monkeypatch):
    import goods_ui
    monkeypatch.setattr(goods_ui.database, "get_db", lambda: _FakeDb())
    text, rows = goods_ui.begin_stock_import(_ctx(), PRODUCT_ID)
    callbacks = [b.callback_data for row in rows for b in row]
    assert f"v52:shop:stock:line:{PRODUCT_ID}" in callbacks
    assert f"v52:shop:stock:block:{PRODUCT_ID}" in callbacks
    assert f"v52:shop:stock:wiz:{PRODUCT_ID}" in callbacks


def test_the_mode_route_arms_the_fsm_with_the_mode(monkeypatch):
    import goods_ui
    import v52_router as router
    armed = {}

    class _Fsm:
        def set(self, ctx, name, payload=None, parent_callback=None, now=None):
            armed.update({"name": name, "payload": payload})

    monkeypatch.setattr(router, "DEFAULT_FSM", _Fsm())
    monkeypatch.setattr(goods_ui.database, "get_db", lambda: _FakeDb())
    text, rows = goods_ui.begin_stock_import_mode(_ctx(), PRODUCT_ID, "block")
    assert armed["payload"] == {"product_id": PRODUCT_ID, "mode": "block"}
    assert "---" in text


class _FakeDb:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE digital_products("
                     "id INTEGER, title TEXT, delivery_type TEXT, is_active INTEGER)")
        conn.execute("INSERT INTO digital_products VALUES(?,?,?,?)",
                     (PRODUCT_ID, "کارت تست", "auto_inventory", 1))
        return conn.execute(sql, params)


def test_an_unknown_mode_is_refused():
    import goods_ui
    text, _rows = goods_ui.begin_stock_import_mode(_ctx(), PRODUCT_ID, "nonsense")
    assert "نامعتبر" in text


def test_every_new_stock_route_resolves_to_a_known_handler():
    expected = {
        f"v52:shop:stock:{PRODUCT_ID}": "begin_product_stock",
        f"v52:shop:stock:line:{PRODUCT_ID}": "begin_product_stock_mode",
        f"v52:shop:stock:block:{PRODUCT_ID}": "begin_product_stock_mode",
        f"v52:shop:stock:wiz:{PRODUCT_ID}": "begin_structured_item",
    }
    for callback, handler in expected.items():
        route = v52_router.route_callback(callback)
        assert route is not None, callback
        assert route.handler == handler, callback


def test_the_mode_route_carries_the_mode_in_its_params():
    route = v52_router.route_callback(f"v52:shop:stock:block:{PRODUCT_ID}")
    assert route.params == {"mode": "block", "product_id": PRODUCT_ID}
