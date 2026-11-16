"""S56 regression tests — multi-line inventory, dynamic ticket statuses, tiers, rep edges.

Four defects are pinned here, each with the assertion that would have caught it:

1. ``BUG-SHOP-002`` — the digital-inventory confirmation callback was wired to the
   *entry* handler, so the parsed batch was dropped and the operator saw
   «0 inserted» after a preview that said «تعداد اقلام: 1». The end-to-end test
   below drives preview → confirm → real insert and counts rows in the database.
2. ``BUG-SUP-007`` — ticket statuses were a hard-coded dictionary. The catalog is
   now editable, and the S52 label contract must survive that change.
3. ``BUG-TIER-001`` — the tier catalog was read-only from the product, so an
   operator could not create a discount that the pricing engine would apply.
4. ``BUG-TIER-001`` — a promotion wrote ``representatives.parent_rep_id`` but never
   the matching ``rep_edges`` row, leaving a representative whose every later
   purchase failed with «representative must have exactly one active parent edge».

Offline only: no panel, no Telegram, no network. Telegram is simulated with the
two rejections that actually matter — a photo message whose body is a caption
(``text is None``) and ``BadRequest: Message is not modified``.
"""
import asyncio
import base64
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import config
import crypto_store
import database
import goods_fulfillment
import goods_service
import inventory_admin_ui
import inventory_parser
import pricing_service
import profile_360
import rep_tree_service
import support_service
import support_ui
import tier_admin_ui
import tier_service
import ticket_status_service
import ticket_status_ui
import v52_router

ROOT_TG_ID = 101258148
NOW = 1794237000


def _load_helpers():
    helper_path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_v52_s56_helpers", helper_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helpers = _load_helpers()


# --------------------------------------------------------------------------
# Telegram doubles
# --------------------------------------------------------------------------

class BadRequest(Exception):
    pass


class FakeMessage:
    """A message that can be a text message or a caption-bearing photo."""

    def __init__(self, *, photo=False, text="body", caption=None, chat_id=555,
                 thread_id=None, message_id=900):
        self.photo = ("file-id",) if photo else None
        self.document = None
        self.text = None if photo else text
        self.caption = caption if photo else None
        self.chat_id = chat_id
        self.message_thread_id = thread_id
        self.message_id = message_id
        self.sent = []

    def get_bot(self):
        return self._bot

    def _bind(self, bot):
        self._bot = bot
        return self


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(message_id=1000)


class FakeQuery:
    """Reproduces the two real Telegram rejections the handlers must survive."""

    def __init__(self, message, bot, data=""):
        self.message = message
        self.data = data
        self._bot = bot
        message._bind(bot)
        self.answers = []
        self.text_edits = []
        self.caption_edits = []
        self.markup_edits = []

    def get_bot(self):
        return self._bot

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text=None, **kwargs):
        if self.message.photo:
            raise BadRequest("There is no text in the message to edit")
        if text == self.message.text and kwargs.get("reply_markup") is None:
            raise BadRequest("Message is not modified")
        self.text_edits.append((text, kwargs))
        self.message.text = text

    async def edit_message_caption(self, caption=None, **kwargs):
        if not self.message.photo and not self.message.caption:
            raise BadRequest("There is no caption in the message to edit")
        if caption == self.message.caption and kwargs.get("reply_markup") is None:
            raise BadRequest("Message is not modified")
        self.caption_edits.append((caption, kwargs))
        self.message.caption = caption

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup_edits.append(reply_markup)


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------

def _master_key(monkeypatch):
    """A deterministic 32-byte master key; never a real deployment secret."""
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )


def _migrated_db(monkeypatch, tmp_path, *, filename):
    """Open an isolated DB with every migration applied; yield the context manager."""
    return helpers.make_db(database, monkeypatch, tmp_path, filename=filename)


def _apply_migrations(conn):
    from migrations_runner import apply_migrations
    root_path = Path(__file__).resolve().parent.parent
    apply_migrations(conn, root_path / "migrations", now=NOW)


def _seed_product(conn, *, product_id=9, delivery_type="auto_inventory"):
    conn.execute("INSERT INTO digital_categories(id,title) VALUES(1,'دسته آزمایشی')")
    conn.execute(
        "INSERT INTO digital_products(id,product_code,category_id,title,delivery_type,"
        "rep_price,user_price) VALUES(?,?,1,'اکانت آزمایشی',?,1000,2000)",
        (product_id, f"P{product_id}", delivery_type),
    )


def _root_ctx(**overrides):
    ctx = {"actor_tg_id": ROOT_TG_ID, "bot_id": 5, "chat_type": "private",
           "persona": "admin", "is_root": True}
    ctx.update(overrides)
    return ctx


def _callbacks(page):
    return [button.callback_data for row in page.reply_markup.inline_keyboard
            for button in row]


def _token(callback_data):
    return callback_data.rsplit(":", 1)[-1]


# --------------------------------------------------------------------------
# 1. Multi-line inventory import (BUG-SHOP-002)
# --------------------------------------------------------------------------

def test_a_separator_line_groups_every_line_between_into_one_item():
    batch = "user1@example.invalid\npass-one\napi-ONE\n---\nsingle-line-item"
    items = inventory_parser.parse_batch(batch)
    assert items == ["user1@example.invalid\npass-one\napi-ONE", "single-line-item"]


def test_both_documented_separators_are_recognised():
    assert inventory_parser.has_separator("a\n---\nb") is True
    assert inventory_parser.has_separator("a\n###\nb") is True
    # A separator must be a standalone line: a password containing dashes is data.
    assert inventory_parser.has_separator("user---name") is False
    assert inventory_parser.resolve_mode("a\n---\nb") == "block"
    assert inventory_parser.resolve_mode("a\nb") == "line"


def test_line_mode_still_treats_every_line_as_its_own_item():
    items = inventory_parser.parse_batch("one\ntwo\nthree", mode="line")
    assert items == ["one", "two", "three"]


def test_the_preview_says_how_many_items_are_multi_line():
    summary = inventory_parser.describe_batch(
        ["user@x.invalid\npass", "single", "a\nb\nc"])
    assert "تعداد اقلام: 3" in summary
    assert "2 قلم چندخطی" in summary


def test_confirmed_digital_import_really_inserts_every_item(monkeypatch, tmp_path):
    """The whole point of BUG-SHOP-002: confirm must insert, not drop the batch."""
    _master_key(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", ROOT_TG_ID)
    with _migrated_db(monkeypatch, tmp_path, filename="s56-import.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_product(conn)
            conn.commit()

        batch = "user1@example.invalid\npass-one\napi-ONE\n---\nsingle-line-item"
        preview = inventory_admin_ui.begin_digital_import(_root_ctx(), 9, batch)
        assert preview.kind == "preview"
        assert "تعداد اقلام: 2" in preview.text
        assert "1 قلم چندخطی" in preview.text
        # The preview must never leak a secret.
        assert "pass-one" not in preview.text

        with database.get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM digital_inventory").fetchone()[0] == 0

        confirm = [cb for cb in _callbacks(preview) if cb.startswith("v52:invok:")]
        assert confirm, "the preview must offer a registered confirmation callback"
        result = inventory_admin_ui.confirm_digital_import(_root_ctx(), _token(confirm[0]))
        assert result.kind == "result", result.text
        assert "تعداد رکوردهای رمزنگاری‌شده: 2" in result.text

        with database.get_db() as conn:
            rows = conn.execute(
                "SELECT id,secret_cipher FROM digital_inventory WHERE product_id=9 "
                "ORDER BY id").fetchall()
        assert len(rows) == 2
        stored = [crypto_store.decrypt_secret(row["secret_cipher"], goods_service._COLUMNS_LABEL)
                  for row in rows]
        # The multi-line block survives as ONE record with its inner newlines intact.
        assert stored[0] == "user1@example.invalid\npass-one\napi-ONE"
        assert stored[1] == "single-line-item"


def test_a_consumed_confirmation_token_cannot_insert_twice(monkeypatch, tmp_path):
    _master_key(monkeypatch)
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", ROOT_TG_ID)
    with _migrated_db(monkeypatch, tmp_path, filename="s56-replay.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_product(conn)
            conn.commit()

        preview = inventory_admin_ui.begin_digital_import(_root_ctx(), 9, "one\ntwo")
        token = _token([cb for cb in _callbacks(preview) if cb.startswith("v52:invok:")][0])
        assert inventory_admin_ui.confirm_digital_import(_root_ctx(), token).kind == "result"
        replay = inventory_admin_ui.confirm_digital_import(_root_ctx(), token)
        assert replay.kind == "error"
        assert "قبلاً تأیید شده" in replay.text
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM digital_inventory").fetchone()[0] == 2


def test_delivery_text_carries_the_usage_guide_and_the_multiline_item(monkeypatch, tmp_path):
    _master_key(monkeypatch)
    with _migrated_db(monkeypatch, tmp_path, filename="s56-delivery.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,"
                "status,verified_at,revision) VALUES(5,NULL,1,'x','bot','active',?,1)", (NOW,))
            _seed_product(conn)
            conn.execute(
                "INSERT INTO quotes(quote_id,actor_tg_id,bot_id,merchant_key,purpose,"
                "product_id,amount_toman,spec_json,price_edges_json,policy_version,"
                "created_at,expires_at,fingerprint) "
                "VALUES('q1',555,5,'root','digital_product_buy',9,2000,'{}','[]','v1.0',?,?, 'f')",
                (NOW, NOW + 1800))
            conn.execute(
                "INSERT INTO payment_orders(order_id,quote_id,actor_tg_id,bot_id,merchant_key,"
                "purpose,payment_rail,amount_toman,status,created_at,expires_at) "
                "VALUES('o1','q1',555,5,'root','digital_product_buy','wallet',2000,'completed',?,?)",
                (NOW, NOW + 1800))
            conn.commit()

        goods_service.set_product_usage_guide(9, "وارد شوید و رمز را عوض کنید")
        text = goods_fulfillment.delivery_text_for("o1", "user@x.invalid\npass-two")
        assert "راهنمای استفاده" in text
        assert "وارد شوید و رمز را عوض کنید" in text
        # The item stays verbatim inside a copyable block, newline included.
        assert "<pre>user@x.invalid\npass-two</pre>" in text


# --------------------------------------------------------------------------
# 2. Dynamic ticket statuses (BUG-SUP-007)
# --------------------------------------------------------------------------

def test_the_seeded_labels_satisfy_the_frozen_s52_contract(monkeypatch, tmp_path):
    """The catalog must not silently reword a status the S52 test pins."""
    with _migrated_db(monkeypatch, tmp_path, filename="s56-labels.sqlite3"):
        assert "باز" in support_service.ticket_status_label("unseen")
        assert "باز" in support_service.ticket_status_label("escalated")
        assert "پاسخ" in support_service.ticket_status_label("answered")
        assert "بسته" in support_service.ticket_status_label("closed")


def test_a_superseded_seed_wording_is_reconciled_but_an_operator_rename_is_kept(
        monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-reconcile.sqlite3"):
        with database.get_db() as conn:
            database.ensure_ticket_statuses_table(conn)
            # Simulate the previous build's wording, then re-run the seed.
            conn.execute("UPDATE ticket_statuses SET title_fa='ارجاع‌شده' WHERE code='escalated'")
            conn.execute("UPDATE ticket_statuses SET title_fa='بررسی ویژه مدیر' WHERE code='closed'")
            conn.commit()
            database.ensure_ticket_statuses_table(conn)
            conn.commit()
            rows = {row["code"]: row["title_fa"] for row in conn.execute(
                "SELECT code,title_fa FROM ticket_statuses")}
        assert rows["escalated"] == "باز (ارجاع‌شده)"
        assert rows["closed"] == "بررسی ویژه مدیر"


def test_root_can_create_rename_disable_and_delete_a_status(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-status-crud.sqlite3"):
        ctx = _root_ctx()
        created = ticket_status_ui.apply_ticket_status_new(
            ctx, "vip_review | بررسی ویژه | ⭐ | answered")
        assert created.kind == "result", created.text
        assert "بررسی ویژه" in created.text

        with database.get_db() as conn:
            assert ticket_status_service.label_for(conn, "vip_review") == "⭐ بررسی ویژه"

        edited = ticket_status_ui.apply_ticket_status_edit(
            ctx, {"code": "vip_review"}, "بررسی ویژه مدیر |  |  | 35 | ")
        assert edited.kind == "result", edited.text
        with database.get_db() as conn:
            row = ticket_status_service.get_status(conn, "vip_review")
        assert row["title_fa"] == "بررسی ویژه مدیر" and row["sort_order"] == 35

        assert ticket_status_ui.toggle_ticket_status(ctx, "vip_review").kind == "page"
        with database.get_db() as conn:
            assert ticket_status_service.get_status(conn, "vip_review")["is_active"] is False

        assert ticket_status_ui.delete_ticket_status(ctx, "vip_review").kind == "result"
        with database.get_db() as conn:
            assert ticket_status_service.get_status(conn, "vip_review") is None


def test_a_system_status_is_disabled_rather_than_deleted(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-status-system.sqlite3"):
        page = ticket_status_ui.delete_ticket_status(_root_ctx(), "closed")
        assert page.kind == "result"
        assert "سیستمی" in page.text
        with database.get_db() as conn:
            row = ticket_status_service.get_status(conn, "closed")
        assert row is not None and row["is_active"] is False


def test_the_picker_offers_every_active_status_and_marks_the_current_one(
        monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-picker.sqlite3"):
        with database.get_db() as conn:
            ticket_status_service.create_status(
                conn, code="vip_review", title_fa="بررسی ویژه", emoji="⭐",
                group_key="answered")
            conn.commit()
        rows = ticket_status_ui.status_picker_rows("10001", current="answered")
        callbacks = [button.callback_data for row in rows for button in row]
        assert "v52:ticketstatus:set:10001:vip_review" in callbacks
        assert "v52:ticketstatus:set:10001:answered" in callbacks
        labels = [button.text for row in rows for button in row]
        assert any(label.startswith("• ") and "در حال بررسی" in label for label in labels)
        # Every emitted callback must resolve through the registry, never dead-end.
        for callback in callbacks:
            assert isinstance(v52_router.route_callback(callback), v52_router.Route)


def test_changing_a_status_moves_the_ticket_header_to_the_dynamic_label(
        monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-set-status.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,"
                "status,verified_at,revision) VALUES(5,NULL,1,'x','bot','active',?,1)", (NOW,))
            conn.execute(
                "INSERT INTO support_tickets(ticket_id,sender_tg_id,bot_id,account_id,rep_id,"
                "ticket_type,assigned_key,status,created_at,updated_at) "
                "VALUES('10001',555,5,NULL,NULL,'general','root','unseen',?,?)", (NOW, NOW))
            ticket_status_service.create_status(
                conn, code="vip_review", title_fa="بررسی ویژه", emoji="⭐",
                group_key="answered")
            conn.commit()

        page = ticket_status_ui.set_ticket_status(_root_ctx(), "10001", "vip_review")
        assert page.kind == "result", page.text
        assert "⭐ بررسی ویژه" in page.text

        with database.get_db() as conn:
            status = conn.execute(
                "SELECT status FROM support_tickets WHERE ticket_id='10001'").fetchone()["status"]
        assert status == "vip_review"
        header = support_ui.ticket_status_header(status, "10001")
        assert "#10001" in header and "⭐ بررسی ویژه" in header


def test_an_inactive_status_cannot_be_applied(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-inactive.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,"
                "status,verified_at,revision) VALUES(5,NULL,1,'x','bot','active',?,1)", (NOW,))
            conn.execute(
                "INSERT INTO support_tickets(ticket_id,sender_tg_id,bot_id,account_id,rep_id,"
                "ticket_type,assigned_key,status,created_at,updated_at) "
                "VALUES('10001',555,5,NULL,NULL,'general','root','unseen',?,?)", (NOW, NOW))
            ticket_status_service.create_status(
                conn, code="vip_review", title_fa="بررسی ویژه", group_key="answered")
            ticket_status_service.update_status(conn, "vip_review", is_active=False)
            conn.commit()

        page = ticket_status_ui.set_ticket_status(_root_ctx(), "10001", "vip_review")
        assert page.kind == "error"
        assert "فعال نیست" in page.text
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT status FROM support_tickets WHERE ticket_id='10001'"
            ).fetchone()["status"] == "unseen"


def test_the_picker_is_offered_to_an_operator_and_hidden_from_a_customer(
        monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-picker-scope.sqlite3"):
        operator = support_ui.ticket_page_rows("10001", "unseen", is_operator=True)
        operator_callbacks = [button.callback_data for row in operator for button in row]
        assert any(cb.startswith("v52:ticketstatus:set:") for cb in operator_callbacks)
        assert "v52:close:10001" in operator_callbacks

        customer = support_ui.ticket_page_rows(
            "10001", "unseen", is_operator=False, is_author=True)
        customer_callbacks = [button.callback_data for row in customer for button in row]
        assert not any(cb.startswith("v52:ticketstatus:set:") for cb in customer_callbacks)
        assert "v52:close:10001" in customer_callbacks


def test_the_ticket_section_offers_the_settings_surface_to_root_only(
        monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-ticketsettings.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.commit()
        monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", ROOT_TG_ID)
        root_page = support_ui.tickets_page(_root_ctx())
        assert "v52:ticketsettings" in _callbacks(root_page)

        rep_page = support_ui.tickets_page(
            {"actor_tg_id": 303, "bot_id": 5, "chat_type": "private", "persona": "rep"})
        assert "v52:ticketsettings" not in _callbacks(rep_page)


def test_the_settings_page_refuses_a_non_root_actor(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-settings-rbac.sqlite3"):
        page = ticket_status_ui.ticket_status_settings(
            {"actor_tg_id": 303, "bot_id": 5, "chat_type": "private",
             "persona": "rep", "is_root": False})
        assert page.kind == "error"
        assert "مدیر کل" in page.text


# --------------------------------------------------------------------------
# 3. Tier catalog + live pricing (BUG-TIER-001)
# --------------------------------------------------------------------------

def _seed_pricing(conn, *, rep_tier="normal", user_tier="normal"):
    conn.execute(
        "INSERT INTO pricing_plans(plan_code,name,gb,days,rep_price,user_price,revision) "
        "VALUES('p1','پلن آزمایشی',10,30,100000,150000,1)")
    conn.execute(
        "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,tier,"
        "parent_rep_id) VALUES(7,'R7','نماینده ۷',777,'',?,NULL)", (rep_tier,))
    conn.execute(
        "INSERT INTO rep_edges(parent_key,child_rep_id,billing_mode,started_at) "
        "VALUES('root',7,'credit',?)", (NOW,))
    conn.execute(
        "INSERT INTO bot_users(telegram_id,role,tier) VALUES(555,'end_user',?)", (user_tier,))
    conn.execute(
        "INSERT INTO tier_policies(actor_type,tier_code,title_fa,rank,discount_bps) "
        "VALUES('rep','normal','عادی',0,0)")
    conn.execute(
        "INSERT INTO tier_policies(actor_type,tier_code,title_fa,rank,discount_bps) "
        "VALUES('end_user','normal','عادی',0,0)")
    conn.commit()


def test_editing_a_tier_discount_changes_the_calculated_representative_price(
        monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-live-price.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_pricing(conn)
            before = pricing_service.price_chain(
                conn, plan_code="p1", seller_key="root", buyer_rep_id=7, now=NOW)
        assert before["amount_toman"] == 100000

        page = tier_admin_ui.apply_tier_edit(
            _root_ctx(), {"actor_type": "rep", "tier_code": "normal"}, "عادی |  | 5")
        assert page.kind == "result", page.text
        assert "5%" in page.text

        with database.get_db() as conn:
            after = pricing_service.price_chain(
                conn, plan_code="p1", seller_key="root", buyer_rep_id=7, now=NOW)
        assert after["amount_toman"] == 95000
        assert after["price_edges"][0]["discount_bps"] == 500


def test_a_new_tier_created_from_the_panel_is_applied_to_the_end_user_price(
        monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-new-tier.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_pricing(conn)
            conn.commit()

        created = tier_admin_ui.apply_tier_new(
            _root_ctx(), "end_user | gold | طلایی | 3 | 10 | 2 | 3 | 5000000 | 0")
        assert created.kind == "result", created.text
        assert "طلایی" in created.text and "10%" in created.text

        with database.get_db() as conn:
            conn.execute("UPDATE bot_users SET tier='gold' WHERE telegram_id=555")
            conn.commit()
            priced = pricing_service.price_chain(
                conn, plan_code="p1", seller_key="root", buyer_tg_id=555, now=NOW)
        assert priced["amount_toman"] == 135000
        assert priced["price_edges"][-1]["discount_bps"] == 1000


def test_a_tier_still_in_use_cannot_be_deleted(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-tier-inuse.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_pricing(conn)
            conn.commit()
        page = tier_admin_ui.delete_tier(_root_ctx(), "rep", "normal")
        assert page.kind == "error"
        assert "در حال استفاده است" in page.text
        with database.get_db() as conn:
            assert tier_service.get_tier(conn, "rep", "normal") is not None


def test_an_unused_tier_can_be_deleted(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-tier-delete.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_pricing(conn)
            conn.commit()
        # ``end_user/normal`` is occupied by the seeded buyer, so the unused tier
        # is created first and then retired.
        assert tier_admin_ui.apply_tier_new(
            _root_ctx(), "end_user | bronze | برنزی | 1").kind == "result"
        assert tier_admin_ui.delete_tier(_root_ctx(), "end_user", "bronze").kind == "result"
        with database.get_db() as conn:
            assert tier_service.get_tier(conn, "end_user", "bronze") is None
            assert tier_service.get_tier(conn, "end_user", "normal") is not None


def test_the_tier_list_page_renders_every_policy_in_persian(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-tier-list.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_pricing(conn)
            conn.commit()
        page = tier_admin_ui.list_tiers(_root_ctx())
        assert page.kind == "page"
        assert "سطوح کاربری و قیمت‌گذاری" in page.text
        assert "نمایندگی" in page.text and "کاربری" in page.text
        assert "v52:tier:new" in _callbacks(page)
        assert "v52:tier:edit:rep:normal" in _callbacks(page)


def test_a_tier_code_must_be_a_lowercase_identifier(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-tier-code.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.commit()
        refused = tier_admin_ui.apply_tier_new(_root_ctx(), "rep | gold-tier | طلایی")
        assert refused.kind == "error"
        assert "حرف کوچک لاتین" in refused.text

        # A mixed-case code is normalised rather than rejected.
        accepted = tier_admin_ui.apply_tier_new(_root_ctx(), "rep | GOLD | طلایی")
        assert accepted.kind == "result", accepted.text
        with database.get_db() as conn:
            assert tier_service.get_tier(conn, "rep", "gold") is not None


def test_a_percentage_above_one_hundred_is_refused(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-tier-percent.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_pricing(conn)
            conn.commit()
        page = tier_admin_ui.apply_tier_edit(
            _root_ctx(), {"actor_type": "rep", "tier_code": "normal"}, "عادی |  | 120")
        assert page.kind == "error"
        assert "۰ و ۱۰۰ درصد" in page.text


# --------------------------------------------------------------------------
# 4. The financial edge a promotion used to lose (BUG-TIER-001)
# --------------------------------------------------------------------------

def _promote(monkeypatch, *, kind, parent_rep_id):
    """Drive the real promotion handler against the caller's migrated DB.

    The caller owns the ``_migrated_db`` context: ``make_db`` restores
    ``database.DB_PATH`` on exit, so a helper that opened its own context would
    hand the caller a connection to the sentinel path.
    """
    with database.get_db() as conn:
        conn.execute(
            "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,"
            "can_create_sub_reps,max_sub_reps) VALUES(3,'PARENT','نماینده والد',199,'',1,5)")
        # The parent is a complete independent representative: without its own root
        # edge the child's price chain could never resolve, and the test would be
        # measuring the fixture instead of the promotion.
        conn.execute(
            "INSERT INTO rep_edges(parent_key,child_rep_id,billing_mode,started_at) "
            "VALUES('root',3,'credit',?)", (NOW,))
        conn.execute(
            "INSERT INTO bot_users(telegram_id,role,full_name,username) "
            "VALUES(555,'end_user','نماینده جدید','newrep')")
        conn.commit()

    monkeypatch.setattr(profile_360, "_rep_row_for", lambda _uid: None)
    bot = FakeBot()
    query = FakeQuery(FakeMessage(photo=False), bot)
    update = SimpleNamespace(callback_query=query,
                             effective_user=SimpleNamespace(id=ROOT_TG_ID))
    handled = asyncio.run(profile_360._apply_promotion(
        update, SimpleNamespace(user_data={}), 555, kind, parent_rep_id, "", ""))
    assert handled is True
    assert query.answers and query.answers[-1][0].startswith("✅")

    with database.get_db() as conn:
        rep = conn.execute(
            "SELECT id,parent_rep_id,is_active FROM representatives WHERE telegram_id=555"
        ).fetchone()
        edges = conn.execute(
            "SELECT parent_key,active,billing_mode FROM rep_edges WHERE child_rep_id=?",
            (int(rep["id"]),)).fetchall()
        role = conn.execute(
            "SELECT role FROM bot_users WHERE telegram_id=555").fetchone()["role"]
    return rep, edges, role


def test_promoting_a_sub_rep_writes_the_matching_financial_edge(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-promo-sub.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.commit()
        rep, edges, role = _promote(monkeypatch, kind="sub", parent_rep_id=3)
        assert int(rep["parent_rep_id"]) == 3
        assert role == "rep"
        assert len(edges) == 1
        assert edges[0]["parent_key"] == "rep:3"
        assert int(edges[0]["active"]) == 1


def test_promoting_an_independent_rep_writes_a_root_edge(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-promo-root.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.commit()
        rep, edges, role = _promote(monkeypatch, kind="independent", parent_rep_id=None)
        assert rep["parent_rep_id"] is None
        assert role == "rep"
        assert len(edges) == 1 and edges[0]["parent_key"] == "root"


def test_replaying_the_edge_link_never_creates_a_second_active_edge(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-edge-idempotent.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter) "
                "VALUES(7,'R7','نماینده ۷',777,'')")
            conn.commit()
            first = rep_tree_service.link_rep_edge(conn, 7, None, now=NOW)
            conn.commit()
            again = rep_tree_service.link_rep_edge(conn, 7, None, now=NOW)
            conn.commit()
            count = conn.execute(
                "SELECT COUNT(*) FROM rep_edges WHERE child_rep_id=7 AND active=1").fetchone()[0]
        assert first["created"] is True and first["changed"] is True
        assert again["created"] is False and again["changed"] is False
        assert again["edge_id"] == first["edge_id"]
        assert count == 1


def test_moving_a_rep_to_a_new_parent_retargets_the_same_edge(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-edge-move.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter) "
                "VALUES(7,'R7','نماینده ۷',777,'')")
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter) "
                "VALUES(8,'R8','والد جدید',888,'')")
            conn.commit()
            rep_tree_service.link_rep_edge(conn, 7, None, now=NOW)
            conn.commit()
            moved = rep_tree_service.link_rep_edge(conn, 7, 8, now=NOW)
            conn.commit()
            rows = conn.execute(
                "SELECT id,parent_key FROM rep_edges WHERE child_rep_id=7 AND active=1"
            ).fetchall()
        assert moved["changed"] is True and moved["created"] is False
        assert len(rows) == 1 and rows[0]["parent_key"] == "rep:8"


def test_linking_a_parent_that_would_create_a_cycle_is_refused(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-edge-cycle.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,"
                "parent_rep_id) VALUES(7,'R7','والد',777,'',NULL)")
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,"
                "parent_rep_id) VALUES(8,'R8','فرزند',888,'',7)")
            conn.commit()
            try:
                rep_tree_service.link_rep_edge(conn, 7, 8, now=NOW)
                raised = None
            except rep_tree_service.TreeValidationError as exc:
                raised = str(exc)
        assert raised is not None and "cycle" in raised


def test_a_promoted_rep_has_a_resolvable_price_chain(monkeypatch, tmp_path):
    """The regression BUG-TIER-001 actually caused: pricing failed after promotion."""
    with _migrated_db(monkeypatch, tmp_path, filename="s56-promo-pricing.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            conn.commit()
        rep, _edges, _role = _promote(monkeypatch, kind="sub", parent_rep_id=3)
        with database.get_db() as conn:
            conn.execute(
                "INSERT INTO pricing_plans(plan_code,name,gb,days,rep_price,user_price,revision) "
                "VALUES('p1','پلن آزمایشی',10,30,100000,150000,1)")
            conn.execute(
                "INSERT INTO tier_policies(actor_type,tier_code,title_fa,rank,discount_bps) "
                "VALUES('rep','normal','عادی',0,0)")
            conn.commit()
            priced = pricing_service.price_chain(
                conn, plan_code="p1", seller_key="rep:3", buyer_rep_id=int(rep["id"]), now=NOW)
        assert priced["amount_toman"] == 100000
        assert priced["price_edges"][-1]["seller_key"] == "rep:3"


# --------------------------------------------------------------------------
# 5. Persian transparency (ARCH-UX-002) on the new surfaces
# --------------------------------------------------------------------------

#: Raw English identifiers that must never reach an operator-facing page. Each is
#: a real column or setting key of this schema, not a guess.
_RAW_TECHNICAL_KEYS = (
    "discount_bps", "cashback_bps", "daily_test_quota", "default_credit_limit",
    "min_monthly_sales_toman", "actor_type", "tier_code", "group_key",
    "sort_order", "is_active", "is_system", "title_fa", "parent_key",
    "billing_mode", "credit_limit_toman", "usage_guide", "secret_cipher",
)


def _assert_no_raw_key(text, *, allow_codes=()):
    """The technical code being edited is allowed inside a code span; nothing else."""
    stripped = text
    for code in allow_codes:
        stripped = stripped.replace(f"<code>{code}</code>", "")
    for key in _RAW_TECHNICAL_KEYS:
        assert key not in stripped, f"raw technical key leaked into a page: {key}"


def test_the_new_management_pages_never_show_a_raw_english_key(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-transparency.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_pricing(conn)
            conn.commit()

        tiers = tier_admin_ui.list_tiers(_root_ctx())
        _assert_no_raw_key(tiers.text)
        _assert_no_raw_key(tier_admin_ui.begin_tier_new(_root_ctx()).text)
        _assert_no_raw_key(
            tier_admin_ui.begin_tier_edit(_root_ctx(), "rep", "normal").text,
            allow_codes=("normal",))
        _assert_no_raw_key(
            tier_admin_ui.apply_tier_edit(
                _root_ctx(), {"actor_type": "rep", "tier_code": "normal"}, "عادی |  | 5").text,
            allow_codes=("normal",))

        statuses = ticket_status_ui.ticket_status_settings(_root_ctx())
        _assert_no_raw_key(statuses.text, allow_codes=("unseen", "escalated", "answered", "closed"))
        _assert_no_raw_key(
            ticket_status_ui.begin_ticket_status_new(_root_ctx()).text)
        _assert_no_raw_key(
            ticket_status_ui.begin_ticket_status_edit(_root_ctx(), "closed").text,
            allow_codes=("closed",))


def test_the_tier_pages_explain_the_effect_in_persian(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-transparency-2.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_pricing(conn)
            conn.commit()
        page = tier_admin_ui.list_tiers(_root_ctx())
        assert "تخفیف" in page.text
        assert "سهمیه تست روزانه" in page.text
        guide = tier_admin_ui.begin_tier_new(_root_ctx()).text
        assert "درصد تخفیف" in guide and "درصد کش‌بک" in guide
        assert "سقف اعتبار" in guide and "حداقل فروش ماهانه" in guide


def test_every_callback_emitted_by_the_new_pages_is_registered(monkeypatch, tmp_path):
    with _migrated_db(monkeypatch, tmp_path, filename="s56-callbacks.sqlite3"):
        with database.get_db() as conn:
            _apply_migrations(conn)
            _seed_pricing(conn)
            conn.commit()
        pages = [
            tier_admin_ui.list_tiers(_root_ctx()),
            tier_admin_ui.begin_tier_new(_root_ctx()),
            tier_admin_ui.begin_tier_edit(_root_ctx(), "rep", "normal"),
            ticket_status_ui.ticket_status_settings(_root_ctx()),
            ticket_status_ui.begin_ticket_status_new(_root_ctx()),
            ticket_status_ui.begin_ticket_status_edit(_root_ctx(), "closed"),
        ]
        for page in pages:
            for callback in _callbacks(page):
                assert isinstance(v52_router.route_callback(callback), v52_router.Route), callback
