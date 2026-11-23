"""S57 regression tests — unified tariff centre, tier pricing, wiring and seeding.

The mission had four axes, and each one is pinned here against the real product
modules and a real temporary SQLite database:

1. ``v52:plans`` and ``admin:plans`` render one tariff & category centre, and every
   plan card carries multi-select checkboxes for its categories, panels, audience
   and tariff kind.
2. A tier can hold a fixed price per plan, and the live end-user and representative
   flows charge that price — including the intent guards that used to reject every
   tier-discounted purchase with «plan_changed».
3. The bank card has one authoritative store, a verified subscription domain drives
   the panel's subscription base, the free-pool view shows the real pool, and the
   panel card exposes its advanced columns.
4. ``database.init_db`` seeds a testable sample ecosystem exactly once and never
   rewrites an operator's own rows.

Offline only: no panel, no Telegram, no network.
"""
from contextlib import contextmanager
import asyncio
import base64
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from migrations_runner import apply_migrations

import admin_panel
import config
import database
import domain_service
import finance_admin_ui
import internal_panel_compat
import panel_admin_ui
import pricing_service
import promotion_service
import refund_service
import rep_panel
import scheduler_service
import tier_admin_ui
import tier_service
import user_portal
import v52_router

ROOT_TG_ID = 101258148
NOW = 1794237000
VIP_TG_ID = 555001
GOLD_REP_ID = 7001
GOLD_REP_TG_ID = 7002


def _load_helpers():
    helper_path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_v52_s57_helpers", helper_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helpers = _load_helpers()


def _apply_migrations(conn):
    root_path = Path(__file__).resolve().parent.parent
    apply_migrations(conn, root_path / "migrations", now=NOW)


@contextmanager
def _ecosystem_db(monkeypatch, tmp_path, *, filename):
    """A migrated database carrying the seeded sample ecosystem, as a restart yields.

    ``make_db`` runs ``init_db`` before any migration exists, so the seed's
    migration-owned steps are skipped there; the second pass is exactly what a
    service restart performs on a fully provisioned database.
    """
    # The sample store catalogue, the bank-card number and a refund destination are
    # all secret-bearing, so pin a deterministic synthetic master key rather than
    # depending on the deployment's own (never a real key).
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    # CI has no ``.env``, so the configured root is 0 there and every root-only page
    # would refuse a synthetic actor. Pin the identity instead of inheriting the
    # deployment's — the same value the fixture's context uses.
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", ROOT_TG_ID, raising=False)
    monkeypatch.setattr(database, "ADMIN_TELEGRAM_ID", ROOT_TG_ID, raising=False)
    with helpers.make_db(database, monkeypatch, tmp_path, filename=filename):
        with database.get_db() as conn:
            ensure_usage_guide = getattr(database, "ensure_product_usage_guide_column", None)
            if ensure_usage_guide:
                ensure_usage_guide(conn)
            _apply_migrations(conn)
            # A real installation always has a default panel; the seed links the
            # default plans to it, so the fixture must provide one.
            conn.execute(
                "INSERT OR IGNORE INTO panels(id,panel_code,name,panel_type,lifecycle,"
                "is_default,sub_base_url,revision) "
                "VALUES(1,'s57-default','پنل اصلی','3x-ui','active',1,'',1)")
            conn.commit()
        with database.get_db() as conn:
            database.ensure_plan_catalog_schema(conn)
            database.ensure_tier_plan_prices_table(conn)
            database._seed_default_sample_ecosystem(conn)
            conn.commit()
        yield


def _root_ctx(**overrides):
    ctx = {"actor_tg_id": ROOT_TG_ID, "telegram_id": ROOT_TG_ID, "bot_id": 5,
           "chat_id": ROOT_TG_ID, "chat_type": "private", "persona": "admin",
           "is_root": True}
    ctx.update(overrides)
    return ctx


def _callbacks(page):
    return [button.callback_data for row in page.reply_markup.inline_keyboard
            for button in row]


def _buttons(page):
    return [button for row in page.reply_markup.inline_keyboard for button in row]


def _seed_vip_user(conn, *, tier="vip", balance=2_000_000):
    conn.execute(
        "INSERT INTO bot_users(telegram_id,role,tier,full_name) VALUES(?, 'end_user', ?, 'VIP') "
        "ON CONFLICT(telegram_id) DO UPDATE SET tier=excluded.tier",
        (VIP_TG_ID, tier),
    )
    conn.execute(
        "INSERT INTO user_wallets(user_id,balance_toman) VALUES(?,?) "
        "ON CONFLICT(user_id) DO UPDATE SET balance_toman=excluded.balance_toman",
        (VIP_TG_ID, int(balance)),
    )


def _seed_gold_rep(conn, *, tier="gold"):
    conn.execute(
        "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,tier,is_active) "
        "VALUES(?, 'S57-GOLD', 'نماینده طلایی', ?, 's57', ?, 1) "
        "ON CONFLICT(id) DO UPDATE SET tier=excluded.tier, is_active=1",
        (GOLD_REP_ID, GOLD_REP_TG_ID, tier),
    )


# --------------------------------------------------------------------------
# 1. One unified tariff & category centre
# --------------------------------------------------------------------------

def test_the_super_admin_menu_carries_one_unified_tariff_entry_point(monkeypatch):
    """The two disconnected tariff menus are merged into a single labelled entry."""
    monkeypatch.setattr(admin_panel, "_v52_admin_enabled", lambda: True)
    labels = {button.callback_data: button.text
              for row in admin_panel.build_admin_main_keyboard().inline_keyboard
              for button in row}
    assert labels["v52:plans"] == "🏷 مدیریت تعرفه‌ها، دسته‌بندی‌ها و پنل‌ها"
    # The old duplicate entry is gone from the v5.2 menu, and its slot now carries a
    # surface that did not exist before rather than a second tariff menu.
    assert "admin:plans" not in labels
    assert labels["v52:coupons"] == "🎁 کدهای تخفیف و کوپن‌ها"

    monkeypatch.setattr(admin_panel, "_v52_admin_enabled", lambda: False)
    legacy = {button.callback_data: button.text
              for row in admin_panel.build_admin_main_keyboard().inline_keyboard
              for button in row}
    # The legacy route still works and names the same surface the same way.
    assert legacy["admin:plans"] == "🏷 مدیریت تعرفه‌ها، دسته‌بندی‌ها و پنل‌ها"


def test_both_tariff_routes_resolve_to_the_one_hub(monkeypatch, tmp_path):
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-hub.sqlite3"):
        route = v52_router.route_callback("v52:plans")
        assert isinstance(route, v52_router.Route) and route.handler == "plan_hub"
        hub = finance_admin_ui.tariff_hub(_root_ctx())
        assert hub.kind == "page"
        assert "مدیریت تعرفه‌ها، دسته‌بندی‌ها و پنل‌ها" in hub.text
        labels = [button.text for button in _buttons(hub)]
        assert any("۲۰ گیگ ۱ ماهه" in label for label in labels)
        # Every plan row opens the one plan card.
        assert "v52:plan:20gb_30d" in _callbacks(hub)


def test_the_plan_card_exposes_every_multi_select_group(monkeypatch, tmp_path):
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-card.sqlite3"):
        page = finance_admin_ui.plan_detail(_root_ctx(), "20gb_30d")
        callbacks = _callbacks(page)
        assert "v52:pk:20gb_30d:fixed" in callbacks
        assert "v52:pk:20gb_30d:unlimited" in callbacks
        assert "v52:pk:20gb_30d:custom" in callbacks
        assert "v52:pa:20gb_30d:end_user" in callbacks
        assert "v52:pa:20gb_30d:rep" in callbacks
        assert "v52:pc:20gb_30d:economic" in callbacks
        assert "v52:pc:20gb_30d:vip" in callbacks
        assert "v52:pp:20gb_30d:1" in callbacks
        # The four legacy edit controls survive on the same card, including the
        # volume/duration button that ``set_plan_quick`` used to drop.
        assert "admin:prompt_edit_plan_specs:20gb_30d" in callbacks
        assert "admin:prompt_edit_plan:20gb_30d" in callbacks
        assert "admin:set_plan_quick:20gb_30d:0" in callbacks
        assert "admin:set_plan_active:20gb_30d:0" in callbacks


def test_toggling_a_category_checkbox_links_and_unlinks_the_plan(monkeypatch, tmp_path):
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-cat-toggle.sqlite3"):
        page = finance_admin_ui.toggle_plan_category(_root_ctx(), "20gb_30d", "vip")
        assert page.kind == "page"
        with database.get_db() as conn:
            links = {row["category_key"]: int(row["is_primary"]) for row in conn.execute(
                "SELECT category_key,is_primary FROM plan_category_links WHERE plan_code='20gb_30d'")}
        assert links == {"economic": 1, "vip": 0}

        finance_admin_ui.toggle_plan_category(_root_ctx(), "20gb_30d", "economic")
        with database.get_db() as conn:
            remaining = [row["category_key"] for row in conn.execute(
                "SELECT category_key FROM plan_category_links WHERE plan_code='20gb_30d'")]
            primary = conn.execute(
                "SELECT category_key FROM pricing_plans WHERE plan_code='20gb_30d'").fetchone()[0]
        # Removing the primary promotes the remaining link, so the pointer never
        # dangles at a category the plan no longer carries.
        assert remaining == ["vip"] and primary == "vip"


def test_toggling_a_panel_checkbox_updates_the_plan_panel_binding(monkeypatch, tmp_path):
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-panel-toggle.sqlite3"):
        with database.get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO panels(id,panel_code,name,panel_type,lifecycle,is_default) "
                "VALUES(2,'s57-panel','پنل دوم','3x-ui','active',0)")
            conn.commit()
        finance_admin_ui.toggle_plan_panel(_root_ctx(), "20gb_30d", 2)
        with database.get_db() as conn:
            allowed = conn.execute(
                "SELECT allowed FROM plan_panels WHERE plan_code='20gb_30d' AND panel_id=2"
            ).fetchone()[0]
        assert int(allowed) == 1
        finance_admin_ui.toggle_plan_panel(_root_ctx(), "20gb_30d", 2)
        with database.get_db() as conn:
            allowed = conn.execute(
                "SELECT allowed FROM plan_panels WHERE plan_code='20gb_30d' AND panel_id=2"
            ).fetchone()[0]
        assert int(allowed) == 0


def test_the_audience_checkbox_refuses_to_disable_both_sides(monkeypatch, tmp_path):
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-audience.sqlite3"):
        finance_admin_ui.toggle_plan_audience(_root_ctx(), "20gb_30d", "rep")
        with database.get_db() as conn:
            assert pricing_service.plan_audience(conn, "20gb_30d") == {
                "plan_code": "20gb_30d", "for_end_users": True, "for_reps": False}
        refused = finance_admin_ui.toggle_plan_audience(_root_ctx(), "20gb_30d", "end_user")
        assert refused.kind == "error"
        assert "حداقل برای یکی از دو مخاطب" in refused.text


def test_the_tariff_kind_checkbox_sets_the_kind(monkeypatch, tmp_path):
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-kind.sqlite3"):
        page = finance_admin_ui.apply_plan_kind(_root_ctx(), "20gb_30d", "unlimited")
        assert page.kind == "page" and "حجم یا زمان نامحدود" in page.text
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT plan_kind FROM pricing_plans WHERE plan_code='20gb_30d'"
            ).fetchone()[0] == "unlimited"


def test_plan_category_crud_is_persian_and_cleans_up_its_links(monkeypatch, tmp_path):
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-cat-crud.sqlite3"):
        created = finance_admin_ui.apply_plan_category_new(
            _root_ctx(), "gaming2 | سرویس‌های گیمینگ ۲ | 🎮")
        assert created.kind == "result" and "gaming2" in created.text

        edited = finance_admin_ui.apply_plan_category_edit(
            _root_ctx(), {"category_key": "gaming2"}, "گیمینگ حرفه‌ای | 🎯 | 9 | 1")
        assert edited.kind == "result" and "گیمینگ حرفه‌ای" in edited.text

        finance_admin_ui.toggle_plan_category(_root_ctx(), "20gb_30d", "gaming2")
        deleted = finance_admin_ui.delete_plan_category(_root_ctx(), "gaming2")
        assert deleted.kind == "result" and "20gb_30d" in deleted.text
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM plan_category_links WHERE category_key='gaming2'"
            ).fetchone()[0] == 0
            # The plan keeps its original primary category rather than a dangling key.
            assert conn.execute(
                "SELECT category_key FROM pricing_plans WHERE plan_code='20gb_30d'"
            ).fetchone()[0] == "economic"


# --------------------------------------------------------------------------
# 2. Tier-specific plan pricing
# --------------------------------------------------------------------------

def test_effective_price_priority_is_custom_then_discount_then_base(monkeypatch, tmp_path):
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-priority.sqlite3"):
        with database.get_db() as conn:
            _seed_vip_user(conn)
            _seed_gold_rep(conn)
            conn.commit()
            # (1) a fixed tier price wins outright.
            custom = pricing_service.resolve_effective_plan_price(
                "20gb_30d", user_id=VIP_TG_ID, conn=conn)
            assert custom["base_price"] == 450000
            assert custom["effective_price"] == 380000
            assert custom["is_custom_price"] is True

            # (2) without a fixed price the tier discount applies to the base price.
            discounted = pricing_service.resolve_tier_plan_price(
                conn, role_scope="rep", tier_code="gold", plan_code="30gb_30d")
            assert discounted["base_price"] == 350000
            assert discounted["discount_bps"] == 2000
            assert discounted["effective_price"] == 280000
            assert discounted["is_custom_price"] is False

            # (3) an unknown tier falls back to the untiered list price.
            plain = pricing_service.resolve_tier_plan_price(
                conn, role_scope="end_user", tier_code="normal", plan_code="30gb_30d")
            assert plain["effective_price"] == plain["base_price"] == 550000


def test_the_tier_price_matrix_agrees_with_the_resolver(monkeypatch, tmp_path):
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-matrix.sqlite3"):
        with database.get_db() as conn:
            _seed_gold_rep(conn)
            conn.commit()
        page = tier_admin_ui.tier_price_matrix(_root_ctx(), "rep", "gold")
        assert page.kind == "page"
        assert "نماینده طلایی" in page.text and "20%" in page.text
        assert "v52:tierprice:rep:gold:30gb_30d" in _callbacks(page)
        assert "v52:tierpriceclear:rep:gold:20gb_30d" in _callbacks(page)

        applied = tier_admin_ui.apply_tier_plan_price(
            _root_ctx(), {"actor_type": "rep", "tier_code": "gold", "plan_code": "30gb_30d"},
            "275000")
        assert applied.kind == "result" and "275,000" in applied.text
        with database.get_db() as conn:
            assert tier_service.get_tier_plan_price(conn, "rep", "gold", "30gb_30d") == 275000
            assert pricing_service.resolve_effective_plan_price(
                "30gb_30d", rep_id=GOLD_REP_ID, conn=conn)["effective_price"] == 275000

        cleared = tier_admin_ui.clear_tier_plan_price(_root_ctx(), "rep", "gold", "30gb_30d")
        assert cleared.kind == "page"
        with database.get_db() as conn:
            assert tier_service.get_tier_plan_price(conn, "rep", "gold", "30gb_30d") is None


def test_a_vip_user_is_shown_and_charged_their_tier_price(monkeypatch, tmp_path):
    """The end-user purchase path must both display and debit the tier price."""
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-vip-buy.sqlite3"):
        with database.get_db() as conn:
            _seed_vip_user(conn)
            conn.commit()
        monkeypatch.setattr(user_portal.database, "is_user_blocked", lambda _uid: False)
        monkeypatch.setattr(user_portal.database, "get_user_role",
                            lambda *args, **kwargs: ("end_user", None))
        update, query, _message = _make_user_update("user:buy_service")
        asyncio.run(user_portal.handle_user_callbacks(update, SimpleNamespace(user_data={})))

        assert query.edits, "the buy menu must render"
        text, kwargs = query.edits[-1]
        buttons = [button for row in kwargs["reply_markup"].inline_keyboard for button in row]
        plan_buttons = [b for b in buttons if b.callback_data.startswith("user:confirm_buy:")]
        assert plan_buttons, "the buy menu must offer the plans"
        assert any("380,000" in b.text for b in plan_buttons)
        assert "✨" in next(b.text for b in plan_buttons if "380,000" in b.text)

        intent_id = next(b.callback_data.rsplit(":", 1)[1]
                         for b in plan_buttons if "380,000" in b.text)
        intent = database.get_user_action_intent(intent_id, VIP_TG_ID)
        assert int(intent["amount"]) == 380000

        # The guard that used to reject every tier-discounted purchase now accepts
        # exactly the amount the menu showed.
        started = database.begin_user_action_intent(intent_id, VIP_TG_ID, "buy")
        assert started["status"] == "started"
        assert started["new_balance"] == 2_000_000 - 380000


def test_the_end_user_guard_still_rejects_a_stale_amount(monkeypatch, tmp_path):
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-stale-user.sqlite3"):
        with database.get_db() as conn:
            _seed_vip_user(conn)
            conn.commit()
        intent_id = database.create_user_action_intent(
            user_id=VIP_TG_ID, action_type="buy", plan_code="20gb_30d",
            plan_name="۲۰ گیگ ۱ ماهه", gb=20.0, days=30, amount=12345)
        assert database.begin_user_action_intent(
            intent_id, VIP_TG_ID, "buy")["status"] == "plan_changed"


def test_a_gold_representative_is_charged_their_tier_price(monkeypatch, tmp_path):
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-gold-rep.sqlite3"):
        with database.get_db() as conn:
            _seed_gold_rep(conn)
            conn.commit()
        monkeypatch.setattr(rep_panel.database, "is_user_blocked", lambda _uid: False)
        monkeypatch.setattr(rep_panel.database, "get_user_role",
                            lambda *args, **kwargs: ("rep", {"id": GOLD_REP_ID}))
        rep = {"id": GOLD_REP_ID, "name": "نماینده طلایی", "telegram_id": GOLD_REP_TG_ID}
        update, query, _message = _make_rep_update("rep:new_acc_plans")
        asyncio.run(rep_panel.handle_rep_callbacks(
            update, SimpleNamespace(user_data={}), rep))

        text, kwargs = query.edits[-1]
        buttons = [button for row in kwargs["reply_markup"].inline_keyboard for button in row]
        plan_buttons = [b for b in buttons if b.callback_data.startswith("rep:new_plan_chosen:")]
        assert any("250,000" in b.text for b in plan_buttons)

        update, query, _message = _make_rep_update("rep:new_plan_chosen:20gb_30d")
        asyncio.run(rep_panel.handle_rep_callbacks(
            update, SimpleNamespace(user_data={}), rep))
        text, kwargs = query.edits[-1]
        issue = [b for row in kwargs["reply_markup"].inline_keyboard for b in row
                 if b.callback_data.startswith("rep:issue_acc_direct:")]
        assert issue, "the issuance confirmation must be offered"
        intent_id = issue[0].callback_data.split(":")[2]
        intent = database.get_rep_action_intent(intent_id, GOLD_REP_ID, GOLD_REP_TG_ID)
        assert int(intent["amount"]) == 250000
        assert "250,000" in text

        started = database.begin_rep_action_intent(intent_id, GOLD_REP_ID, GOLD_REP_TG_ID)
        assert started["status"] == "started"


# --------------------------------------------------------------------------
# 3. Bank card, domains, pool view and panel settings
# --------------------------------------------------------------------------

def test_updating_the_card_writes_both_stores_and_the_payment_reader_sees_it(monkeypatch, tmp_path):
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-card.sqlite3"):
        database.update_card_info("6037-9911-2233-4455", "علی رضا محمدی", "بانک سامان")
        info = database.get_card_info()
        assert info["card_number"] == "6037-9911-2233-4455"
        assert info["card_holder"] == "علی رضا محمدی"
        assert info["source"] == "bank_cards"
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT merchant_key,card_holder,bank_name,is_active FROM bank_cards "
                "WHERE merchant_key='root'").fetchone()
            assert row is not None and int(row["is_active"]) == 1
            assert row["card_holder"] == "علی رضا محمدی"
            # The legacy singleton keeps the readable value the old menus render.
            legacy = conn.execute(
                "SELECT card_number FROM card_settings WHERE id=1").fetchone()[0]
        assert legacy == "6037-9911-2233-4455"

        # A second edit updates the same row instead of creating a duplicate.
        database.update_card_info("6104-3388-0000-1111", "سارا احمدی", "بانک پاسارگاد")
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM bank_cards WHERE merchant_key='root'").fetchone()[0] == 1
        assert database.get_card_info()["card_number"] == "6104-3388-0000-1111"


def test_activating_a_subscription_domain_syncs_the_panel_base_url(monkeypatch, tmp_path):
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-domain.sqlite3"):
        with database.get_db() as conn:
            domain_service.ensure_domain_columns(conn)
            conn.execute(
                "INSERT OR IGNORE INTO panels(id,panel_code,name,panel_type,lifecycle,is_default,"
                "sub_base_url) VALUES(3,'s57-sub','پنل ساب','3x-ui','active',0,'https://old.example')")
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,is_active) "
                "VALUES(41,'S57-DOM','نماینده دامنه',4101,'s57',1)")
            conn.execute(
                "INSERT INTO sub_domains(domain_id,rep_id,hostname,challenge,status,role,panel_id,"
                "is_primary,verified_at,tls_checked_at,created_at) "
                "VALUES(51,41,'sub.s57.example','n'||substr(hex(randomblob(24)),1,40),'verified',"
                "'subscription',3,0,?,?,?)", (NOW, NOW, NOW))
            conn.commit()

        with database.get_db() as conn:
            conn.execute("BEGIN IMMEDIATE")
            from confirmation_service import create_confirmation
            confirmation = create_confirmation(
                conn, actor_tg_id=4101, bot_id=5, action="domain.activate",
                target_type="sub_domain", target_id=51, expected_revision=NOW,
                payload={"domain_id": 51}, now=NOW)
        nonce = confirmation["nonce"]
        assert nonce

        with database.get_db() as conn:
            domain_service.activate_domain(
                conn, 4101, 51, nonce=nonce, bot_id=5, now=NOW)
            conn.commit()
            stored = conn.execute(
                "SELECT sub_base_url FROM panels WHERE id=3").fetchone()[0]
        assert stored == "https://sub.s57.example"

        # The resolver the compatibility facade exposes agrees with the column.
        with database.get_db() as conn:
            assert domain_service.panel_subscription_base(conn, 3) == "https://sub.s57.example"
        assert internal_panel_compat.resolve_subscription_base(3) == "https://sub.s57.example"


def test_retiring_the_only_active_domain_clears_the_panel_base_url(monkeypatch, tmp_path):
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-domain-off.sqlite3"):
        with database.get_db() as conn:
            domain_service.ensure_domain_columns(conn)
            conn.execute(
                "INSERT OR IGNORE INTO panels(id,panel_code,name,panel_type,lifecycle,is_default,"
                "sub_base_url) VALUES(4,'s57-sub-off','پنل ساب ۲','3x-ui','active',0,'')")
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,is_active) "
                "VALUES(42,'S57-DOM2','نماینده دامنه ۲',4202,'s57',1)")
            conn.execute(
                "INSERT INTO sub_domains(domain_id,rep_id,hostname,challenge,status,role,panel_id,"
                "is_primary,verified_at,tls_checked_at,created_at) "
                "VALUES(52,42,'off.s57.example','n'||substr(hex(randomblob(24)),1,40),'active',"
                "'subscription',4,1,?,?,?)", (NOW, NOW, NOW))
            conn.commit()
            domain_service.sync_panel_sub_base_url(conn, 4)
            conn.commit()
            assert conn.execute(
                "SELECT sub_base_url FROM panels WHERE id=4").fetchone()[0] == "https://off.s57.example"

            domain_service.set_domain_status(conn, 4202, 52, status="disabled")
            conn.commit()
            # A retired host must not survive as the panel's own fallback value.
            assert conn.execute(
                "SELECT sub_base_url FROM panels WHERE id=4").fetchone()[0] == ""


def test_the_pool_view_lists_unassigned_accounts_with_real_newlines(monkeypatch, tmp_path):
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-pool.sqlite3"):
        with database.get_db() as conn:
            conn.execute(
                "INSERT INTO accounts(id,rep_id,account_code,email,enable,owner_tg_id,deleted_at) "
                "VALUES(9001,NULL,'S57-POOL-1','pool1@example.invalid',1,NULL,NULL)")
            conn.execute(
                "INSERT INTO accounts(id,rep_id,account_code,email,enable,owner_tg_id,deleted_at) "
                "VALUES(9002,NULL,'S57-OWNED','owned@example.invalid',1,777,NULL)")
            conn.commit()
        monkeypatch.setattr(admin_panel.database, "get_user_role",
                            lambda *args, **kwargs: ("super_admin", None))
        update, query, _message = _make_admin_update("admin:pool")
        asyncio.run(admin_panel.handle_admin_callbacks(update, SimpleNamespace(user_data={})))
        text, _kwargs = query.edits[-1]
        assert "S57-POOL-1" in text
        assert "S57-OWNED" not in text
        assert "\\n" not in text
        assert "\n" in text


def test_advanced_panel_settings_write_each_group_under_a_revision_cas(monkeypatch, tmp_path):
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-panel-adv.sqlite3"):
        with database.get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO panels(id,panel_code,name,panel_type,lifecycle,is_default,"
                "revision) VALUES(5,'s57-adv','پنل پیشرفته','3x-ui','active',0,1)")
            conn.commit()
        page = panel_admin_ui.panel_advanced_settings(_root_ctx(), 5)
        assert page.kind == "page"
        for field in panel_admin_ui.ADVANCED_FIELDS:
            assert f"v52:paneladvset:5:{field}" in _callbacks(page)

        applied = panel_admin_ui.apply_panel_advanced(
            _root_ctx(), {"panel_id": 5, "field": "capacity"}, "500")
        assert applied.kind == "page" and "500" in applied.text
        panel_admin_ui.apply_panel_advanced(
            _root_ctx(), {"panel_id": 5, "field": "naming"}, "hst | rep_prefix_sequential")
        panel_admin_ui.apply_panel_advanced(
            _root_ctx(), {"panel_id": 5, "field": "suburl"}, "https://adv.s57.example/hsubx")
        panel_admin_ui.apply_panel_advanced(
            _root_ctx(), {"panel_id": 5, "field": "test"}, "1 | 24 | 1024")
        panel_admin_ui.apply_panel_advanced(
            _root_ctx(), {"panel_id": 5, "field": "addons"}, "1 | 18000 | 8000")
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT max_clients,custom_prefix,username_strategy,sub_base_url,"
                "allow_test_account,test_duration_hours,test_volume_mib,allow_addons,"
                "extra_gib_user_price,extra_day_user_price,revision FROM panels WHERE id=5"
            ).fetchone()
        assert int(row["max_clients"]) == 500
        assert row["custom_prefix"] == "hst"
        assert row["username_strategy"] == "rep_prefix_sequential"
        assert row["sub_base_url"] == "https://adv.s57.example/hsubx"
        assert int(row["allow_test_account"]) == 1
        assert int(row["test_duration_hours"]) == 24
        assert int(row["test_volume_mib"]) == 1024
        assert int(row["allow_addons"]) == 1
        assert int(row["extra_gib_user_price"]) == 18000
        assert int(row["extra_day_user_price"]) == 8000
        assert int(row["revision"]) == 6

        refused = panel_admin_ui.apply_panel_advanced(
            _root_ctx(), {"panel_id": 5, "field": "naming"}, "hst | not-a-strategy")
        assert refused.kind == "error" and "الگوی نام‌گذاری نامعتبر" in refused.text


# --------------------------------------------------------------------------
# 4. Coupons, refunds and the expiry watchdog
# --------------------------------------------------------------------------

def test_coupon_crud_and_the_delete_guard(monkeypatch, tmp_path):
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-coupons.sqlite3"):
        page = finance_admin_ui.coupons_page(_root_ctx())
        assert page.kind == "page" and "WELCOME10" in page.text

        created = finance_admin_ui.apply_coupon_new(
            _root_ctx(), "S57TEN | 10000 | 500000 | 5 | 30")
        assert created.kind == "result" and "S57TEN" in created.text
        with database.get_db() as conn:
            assert promotion_service.coupon_row(conn, "s57ten")["max_uses"] == 5

        edited = finance_admin_ui.apply_coupon_edit(
            _root_ctx(), {"code": "S57TEN"}, "15000 | 900000 | 9 | ")
        assert edited.kind == "result" and "15,000" in edited.text

        toggled = finance_admin_ui.toggle_coupon(_root_ctx(), "S57TEN")
        assert toggled.kind == "page"
        with database.get_db() as conn:
            assert promotion_service.coupon_row(conn, "S57TEN")["is_active"] is False

        deleted = finance_admin_ui.delete_coupon(_root_ctx(), "S57TEN")
        assert deleted.kind == "page"
        with database.get_db() as conn:
            assert promotion_service.coupon_row(conn, "S57TEN") is None

        # A coupon an order already holds cannot be deleted, only retired.
        with database.get_db() as conn:
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,"
                "verified_at,revision) VALUES(5,NULL,1,'synthetic','s57_primary','active',?,2)",
                (NOW,))
            conn.execute(
                "INSERT INTO quotes(quote_id,actor_tg_id,bot_id,merchant_key,purpose,amount_toman,"
                "spec_json,price_edges_json,policy_version,created_at,expires_at,fingerprint) "
                "VALUES('s57-coupon-quote',6601,5,'root','vpn_buy',100000,'{}','[]','v1.0',?,?,"
                "'fp-coupon')", (NOW, NOW + 1800))
            conn.execute(
                "INSERT INTO payment_orders(order_id,quote_id,actor_tg_id,bot_id,merchant_key,"
                "purpose,payment_rail,amount_toman,status,created_at,expires_at,revision) "
                "VALUES('s57-coupon-order','s57-coupon-quote',6601,5,'root','vpn_buy','wallet',"
                "100000,'accepted',?,?,1)", (NOW, NOW + 1800))
            conn.execute(
                "INSERT INTO coupon_uses(code,order_id,amount_toman,state) "
                "VALUES('WELCOME10','s57-coupon-order','10000','held')")
            conn.commit()
        refused = finance_admin_ui.delete_coupon(_root_ctx(), "WELCOME10")
        assert refused.kind == "error" and "غیرفعال کنید" in refused.text


def test_the_admin_refund_flow_creates_a_request_and_a_wallet_hold(monkeypatch, tmp_path):
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-refund.sqlite3"):
        with database.get_db() as conn:
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,"
                "verified_at,revision) VALUES(5,NULL,1,'synthetic','s57_primary','active',?,2)",
                (NOW,))
            conn.execute(
                "INSERT INTO financial_ledger(user_id,merchant_key,transaction_type,amount,"
                "balance_after,description,actor_id,actor_name,created_at) "
                "VALUES(6601,'root','credit',500000,500000,'شارژ آزمایشی',6601,'S57',?)", (NOW,))
            conn.execute(
                "INSERT INTO quotes(quote_id,actor_tg_id,bot_id,merchant_key,purpose,amount_toman,"
                "spec_json,price_edges_json,policy_version,created_at,expires_at,fingerprint) "
                "VALUES('s57-quote-1',6601,5,'root','vpn_buy',300000,'{}','[]','v1.0',?,?,'fp-1')",
                (NOW, NOW + 1800))
            conn.execute(
                "INSERT INTO payment_orders(order_id,quote_id,actor_tg_id,bot_id,merchant_key,"
                "purpose,payment_rail,amount_toman,status,created_at,expires_at,revision) "
                "VALUES('s57-order-1','s57-quote-1',6601,5,'root','vpn_buy','wallet',300000,"
                "'accepted',?,?,1)", (NOW, NOW + 1800))
            conn.commit()

        preview = finance_admin_ui.preview_order_refund(_root_ctx(), "s57-order-1")
        assert preview.kind == "page" and "300,000" in preview.text
        assert "v52:refundstart:s57-order-1" in _callbacks(preview)

        armed = finance_admin_ui.begin_refund_destination(_root_ctx(), "s57-order-1")
        assert armed.kind == "form"

        result = finance_admin_ui.apply_refund_destination(
            _root_ctx(),
            {"order_id": "s57-order-1", "user_tg_id": 6601, "merchant_key": "root",
             "amount_toman": 300000},
            "6037-9911-2233-4455")
        assert result.kind == "result" and "processing" in result.text
        with database.get_db() as conn:
            request = conn.execute(
                "SELECT state,amount_toman FROM refund_requests WHERE refund_id=?",
                ("admin-refund:s57-order-1",)).fetchone()
            hold = conn.execute(
                "SELECT state,amount_toman FROM wallet_refund_holds WHERE hold_key=?",
                ("refund:admin-refund:s57-order-1:wallet",)).fetchone()
        assert request is not None and request["state"] == "processing"
        assert hold is not None and hold["state"] == "active"
        assert int(hold["amount_toman"]) == 300000


def test_expiry_warnings_are_claimed_once_per_tehran_day(monkeypatch, tmp_path):
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-expiry.sqlite3"):
        with database.get_db() as conn:
            conn.execute(
                "INSERT INTO accounts(id,rep_id,account_code,email,enable,expiry_time,"
                "owner_tg_id,deleted_at) VALUES(9501,NULL,'S57-EXP','exp@example.invalid',1,?,"
                "6601,NULL)", (NOW + 2 * 86400,))
            conn.execute(
                "INSERT INTO accounts(id,rep_id,account_code,email,enable,expiry_time,"
                "owner_tg_id,deleted_at) VALUES(9502,NULL,'S57-FAR','far@example.invalid',1,?,"
                "6601,NULL)", (NOW + 40 * 86400,))
            conn.commit()
            first = scheduler_service.due_expiry_warnings(conn, now=NOW)
            second = scheduler_service.due_expiry_warnings(conn, now=NOW)
            conn.commit()
        assert [item["account_code"] for item in first] == ["S57-EXP"]
        assert first[0]["days_left"] == 2
        assert second == []
        # A later Tehran day may warn again, so a long outage cannot silence it.
        with database.get_db() as conn:
            later = scheduler_service.due_expiry_warnings(conn, now=NOW + 86400)
            conn.commit()
        assert [item["account_code"] for item in later] == ["S57-EXP"]


# --------------------------------------------------------------------------
# 5. Idempotent sample seeding
# --------------------------------------------------------------------------

def test_the_sample_ecosystem_is_seeded_once_and_never_clobbers_operator_data(monkeypatch, tmp_path):
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-seed.sqlite3"):
        with database.get_db() as conn:
            conn.execute(
                "UPDATE tier_policies SET title_fa='سطح دست‌ساز',discount_bps=777 "
                "WHERE actor_type='rep' AND tier_code='gold'")
            conn.execute(
                "UPDATE plan_categories SET title_fa='دسته دست‌ساز' WHERE category_key='vip'")
            conn.commit()
        with database.get_db() as conn:
            database._seed_default_sample_ecosystem(conn)
            database._seed_default_sample_ecosystem(conn)
            conn.commit()
            tiers = dict(conn.execute(
                "SELECT tier_code,title_fa FROM tier_policies WHERE actor_type='rep'").fetchall())
            discount = conn.execute(
                "SELECT discount_bps FROM tier_policies WHERE actor_type='rep' AND tier_code='gold'"
            ).fetchone()[0]
            category = conn.execute(
                "SELECT title_fa FROM plan_categories WHERE category_key='vip'").fetchone()[0]
            counts = {
                "categories": conn.execute("SELECT COUNT(*) FROM plan_categories").fetchone()[0],
                "links": conn.execute("SELECT COUNT(*) FROM plan_category_links").fetchone()[0],
                "coupons": conn.execute("SELECT COUNT(*) FROM coupons").fetchone()[0],
                "tier_prices": conn.execute("SELECT COUNT(*) FROM tier_plan_prices").fetchone()[0],
            }
        assert tiers["gold"] == "سطح دست‌ساز"
        assert int(discount) == 777
        assert category == "دسته دست‌ساز"
        assert counts["categories"] == len(database.DEFAULT_PLAN_CATEGORIES)
        assert counts["links"] == len(database.DEFAULT_PLAN_CATEGORY_OF)
        assert counts["coupons"] == 1
        assert counts["tier_prices"] == len(database.DEFAULT_TIER_PLAN_PRICES)


def test_the_seed_repairs_the_bootstrap_plan_panel_denial(monkeypatch, tmp_path):
    """``fill_required`` wrote ``allowed=0``; a seeded plan must be sellable again."""
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-seed-panels.sqlite3"):
        with database.get_db() as conn:
            # The bootstrap's denial of every default plan on the only active panel
            # is repaired by the seed.
            assert conn.execute(
                "SELECT allowed FROM plan_panels WHERE plan_code='20gb_30d' AND panel_id=1"
            ).fetchone()[0] == 1
            # A plan that some other panel already serves keeps its explicit denial:
            # the repair only touches a plan with no allowed panel anywhere.
            conn.execute(
                "INSERT OR IGNORE INTO panels(id,panel_code,name,panel_type,lifecycle,is_default) "
                "VALUES(9,'s57-seed-p9','پنل ۹','3x-ui','active',0)")
            conn.execute(
                "INSERT INTO plan_panels(plan_code,panel_id,allowed) VALUES('30gb_30d',9,1) "
                "ON CONFLICT(plan_code,panel_id) DO UPDATE SET allowed=1")
            conn.execute(
                "UPDATE plan_panels SET allowed=0 WHERE plan_code='30gb_30d' AND panel_id=1")
            conn.commit()
            database._seed_default_sample_ecosystem(conn)
            conn.commit()
            denied = conn.execute(
                "SELECT allowed FROM plan_panels WHERE plan_code='30gb_30d' AND panel_id=1"
            ).fetchone()[0]
        assert int(denied) == 0


def test_the_seeded_ecosystem_is_testable_at_every_hierarchy_level(monkeypatch, tmp_path):
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-seed-levels.sqlite3"):
        with database.get_db() as conn:
            tiers = {(row["actor_type"], row["tier_code"]): int(row["discount_bps"])
                     for row in conn.execute(
                         "SELECT actor_type,tier_code,discount_bps FROM tier_policies")}
            coupons = [row["code"] for row in conn.execute("SELECT code FROM coupons")]
            categories = [row["title_fa"] for row in conn.execute(
                "SELECT title_fa FROM plan_categories ORDER BY sort_order")]
        # Both sides of the ladder exist, and the representative rungs carry a real
        # discount so the tier price matrix is exercisable end to end.
        assert tiers[("rep", "normal")] == 0
        assert tiers[("rep", "silver")] == 1000
        assert tiers[("rep", "gold")] == 2000
        assert tiers[("end_user", "normal")] == 0
        assert coupons == ["WELCOME10"]
        assert len(categories) == len(database.DEFAULT_PLAN_CATEGORIES)


# --------------------------------------------------------------------------
# 6. Router coverage for every new surface
# --------------------------------------------------------------------------

def test_every_v52_callback_emitted_by_the_new_pages_is_registered(monkeypatch, tmp_path):
    with _ecosystem_db(monkeypatch, tmp_path, filename="s57-callbacks.sqlite3"):
        with database.get_db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO panels(id,panel_code,name,panel_type,lifecycle,is_default) "
                "VALUES(7,'s57-cb','پنل کالبک','3x-ui','active',0)")
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,is_active) "
                "VALUES(71,'S57-CB','نماینده کالبک',7101,'s57',1)")
            conn.commit()
        ctx = _root_ctx()
        pages = [
            finance_admin_ui.tariff_hub(ctx),
            finance_admin_ui.plan_detail(ctx, "20gb_30d"),
            finance_admin_ui.plan_categories_page(ctx),
            finance_admin_ui.begin_plan_category_new(ctx),
            finance_admin_ui.begin_plan_category_edit(ctx, "economic"),
            finance_admin_ui.coupons_page(ctx),
            finance_admin_ui.begin_coupon_new(ctx),
            finance_admin_ui.begin_coupon_edit(ctx, "WELCOME10"),
            finance_admin_ui.refunds_page(ctx),
            tier_admin_ui.list_tiers(ctx),
            tier_admin_ui.begin_tier_edit(ctx, "rep", "gold"),
            tier_admin_ui.tier_price_matrix(ctx, "rep", "gold"),
            tier_admin_ui.begin_tier_plan_price(ctx, "rep", "gold", "30gb_30d"),
            panel_admin_ui.panel_advanced_settings(ctx, 7),
            panel_admin_ui.begin_panel_advanced(ctx, 7, "capacity"),
        ]
        for page in pages:
            assert page.kind != "error", page.text[:200]
            for callback in _callbacks(page):
                if not callback.startswith("v52:"):
                    continue
                assert isinstance(v52_router.route_callback(callback), v52_router.Route), callback


def test_the_new_draft_states_are_registered_for_text(monkeypatch, tmp_path):
    """A state accepted by ``set`` but not by ``consume`` silently swallows input."""
    for state in ("plan_category_new", "plan_category_edit", "tier_plan_price",
                  "coupon_new", "coupon_edit", "refund_destination", "panel_advanced"):
        assert state in v52_router.TEXT_STATES
    fsm = v52_router.DraftFSM()
    identity = {"bot_id": 5, "actor_tg_id": ROOT_TG_ID, "chat_id": ROOT_TG_ID}
    fsm.set(identity, "coupon_new", now=NOW)
    route = v52_router.route_text(identity, "S57X | 1000 | 10000 | 1 | 10", now=NOW, fsm=fsm)
    assert isinstance(route, v52_router.Route) and route.name == "coupon_new"


# --------------------------------------------------------------------------
# Telegram doubles
# --------------------------------------------------------------------------

class _FakeQuery:
    def __init__(self, data=""):
        self.data = data
        self.answers = []
        self.edits = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class _FakeMessage:
    def __init__(self):
        self.replies = []
        self.message_thread_id = None

    async def reply_text(self, text, **kwargs):
        self.replies.append((text, kwargs))


def _make_user_update(data=""):
    query = _FakeQuery(data)
    message = _FakeMessage()
    query.message = message
    chat = SimpleNamespace(id=71001, type="private")
    user = SimpleNamespace(id=VIP_TG_ID, username="vip", full_name="VIP User",
                           first_name="VIP")
    update = SimpleNamespace(
        callback_query=query, message=message,
        effective_message=SimpleNamespace(message_thread_id=None),
        effective_user=user, effective_chat=chat)
    return update, query, message


def _make_rep_update(data=""):
    query = _FakeQuery(data)
    message = _FakeMessage()
    query.message = message
    chat = SimpleNamespace(id=-10071001, type="supergroup")
    user = SimpleNamespace(id=GOLD_REP_TG_ID, username="goldrep", full_name="Gold Rep",
                           first_name="Gold")
    update = SimpleNamespace(
        callback_query=query, message=message,
        effective_message=SimpleNamespace(message_thread_id=77),
        effective_user=user, effective_chat=chat)
    return update, query, message


def _make_admin_update(data=""):
    query = _FakeQuery(data)
    message = _FakeMessage()
    chat = SimpleNamespace(id=ROOT_TG_ID, type="private")
    user = SimpleNamespace(id=ROOT_TG_ID, full_name="Super Admin", username="root")
    update = SimpleNamespace(
        callback_query=query, message=message,
        effective_message=SimpleNamespace(message_thread_id=None),
        effective_user=user, effective_chat=chat)
    return update, query, message
