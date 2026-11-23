"""S60 Task 5 — percentage coupons without a 19th migration, and the two prompts.

One test per behaviour that was wrong or missing:

1. ``coupons.discount_percent`` is added idempotently by
   ``database.ensure_plan_catalog_schema`` (``migrations/*.sql`` stays at 18);
2. ``promotion_service`` accepts a 1..100 rate and holds the percentage of the price,
   capped by ``discount_toman``, while a fixed coupon keeps its old arithmetic;
3. the ``v52:couponnew`` wizard opens on a discount-type picker and previews in the
   kind the operator chose;
4. ``prompt_search_user`` / ``prompt_inspect`` delete the operator's raw message and
   return to a clean parent.

Offline only: no Telegram, no network, no live database.
"""
from __future__ import annotations

import asyncio
import sqlite3

import pytest

import admin_panel
import database
import finance_admin_ui
import promotion_service
import ui_v52

NOW = 1_800_000_000


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #

def _db():
    """The migration-016 ``coupons`` table, which still carries its original CHECK."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE coupons (
            code TEXT PRIMARY KEY, issuer_key TEXT NOT NULL,
            discount_toman INTEGER NOT NULL CHECK(discount_toman>0),
            budget_toman INTEGER NOT NULL CHECK(budget_toman>=0),
            used_toman INTEGER NOT NULL DEFAULT 0,
            max_uses INTEGER NOT NULL DEFAULT 1,
            expires_at INTEGER NOT NULL, is_active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE coupon_uses (
            code TEXT NOT NULL REFERENCES coupons(code),
            order_id TEXT NOT NULL UNIQUE, amount_toman INTEGER NOT NULL,
            state TEXT NOT NULL
        );
        CREATE TABLE payment_orders (
            order_id TEXT PRIMARY KEY, merchant_key TEXT, status TEXT
        );
    """)
    database.ensure_plan_catalog_schema(conn)
    conn.commit()
    return conn


@pytest.fixture()
def conn():
    connection = _db()
    yield connection
    connection.close()


def _order(conn, order_id, status="awaiting_payment"):
    conn.execute("INSERT INTO payment_orders VALUES(?,?,?)", (order_id, "root", status))
    conn.commit()


# --------------------------------------------------------------------------- #
# 1. The column arrives without a migration
# --------------------------------------------------------------------------- #

def test_the_percent_column_is_added_by_the_schema_helper(conn):
    columns = {row[1] for row in conn.execute("PRAGMA table_info(coupons)")}
    assert "discount_percent" in columns


def test_the_schema_helper_is_idempotent(conn):
    assert database._ensure_column(
        conn, "coupons", "discount_percent", "INTEGER NOT NULL DEFAULT 0") is False


def test_an_existing_coupon_defaults_to_a_fixed_coupon(conn):
    conn.execute("INSERT INTO coupons(code,issuer_key,discount_toman,budget_toman,"
                 "expires_at) VALUES('LEGACY','root',5000,50000,?)", (NOW + 86400,))
    conn.commit()
    assert promotion_service.coupon_row(conn, "LEGACY")["discount_percent"] == 0


def test_the_schema_helper_refuses_an_unsafe_identifier(conn):
    with pytest.raises(ValueError):
        database._ensure_column(conn, "coupons; DROP TABLE coupons", "x", "INTEGER")
    with pytest.raises(ValueError):
        database._ensure_column(conn, "coupons", "x; DROP TABLE coupons", "INTEGER")


def test_the_schema_helper_skips_an_absent_table(conn):
    assert database._ensure_column(conn, "no_such_table", "c", "INTEGER") is False


def test_the_migration_count_is_still_eighteen():
    from pathlib import Path
    repo = Path(__file__).resolve().parent.parent
    assert len(list((repo / "migrations").glob("*.sql"))) == 18


# --------------------------------------------------------------------------- #
# 2. The service accepts a rate
# --------------------------------------------------------------------------- #

def test_a_fixed_coupon_still_stores_its_amount(conn):
    created = promotion_service.create_coupon(
        conn, code="FIX10", discount_toman=10000, budget_toman=100000,
        max_uses=5, expires_at=NOW + 86400)
    assert created["discount_toman"] == 10000
    assert created["discount_percent"] == 0


def test_a_percentage_coupon_stores_its_rate(conn):
    created = promotion_service.create_coupon(
        conn, code="PCT20", discount_toman=5000, budget_toman=100000,
        max_uses=5, expires_at=NOW + 86400, discount_percent=20)
    assert created["discount_percent"] == 20
    # ``discount_toman`` becomes the cap, which keeps CHECK(discount_toman>0) true.
    assert created["discount_toman"] == 5000


def test_a_percentage_coupon_without_a_cap_opens_it(conn):
    created = promotion_service.create_coupon(
        conn, code="PCT15", discount_toman=None, budget_toman=100000,
        max_uses=5, expires_at=NOW + 86400, discount_percent=15)
    assert created["discount_percent"] == 15
    assert created["discount_toman"] == promotion_service.MAX_COUPON_DISCOUNT_TOMAN


def test_a_percentage_hold_is_the_rate_of_the_price(conn):
    promotion_service.create_coupon(
        conn, code="PCT20", discount_toman=5000, budget_toman=100000,
        max_uses=5, expires_at=NOW + 86400, discount_percent=20)
    _order(conn, "o1")
    held = promotion_service.hold_coupon(
        conn, order_id="o1", code="PCT20", price_toman=10000, now=NOW)
    assert held["discount_toman"] == 2000


def test_a_percentage_hold_never_exceeds_its_cap(conn):
    promotion_service.create_coupon(
        conn, code="PCT20", discount_toman=5000, budget_toman=100000,
        max_uses=5, expires_at=NOW + 86400, discount_percent=20)
    _order(conn, "o2")
    held = promotion_service.hold_coupon(
        conn, order_id="o2", code="PCT20", price_toman=40000, now=NOW)
    assert held["discount_toman"] == 5000


def test_a_fixed_hold_is_still_clamped_to_the_price(conn):
    promotion_service.create_coupon(
        conn, code="FIX10", discount_toman=10000, budget_toman=100000,
        max_uses=5, expires_at=NOW + 86400)
    _order(conn, "o3")
    held = promotion_service.hold_coupon(
        conn, order_id="o3", code="FIX10", price_toman=3000, now=NOW)
    assert held["discount_toman"] == 3000


@pytest.mark.parametrize("bad", [101, -5, "abc"])
def test_an_out_of_range_rate_is_refused(conn, bad):
    with pytest.raises(promotion_service.PromotionError):
        promotion_service.create_coupon(
            conn, code="BADRATE", discount_toman=1000, budget_toman=1000,
            max_uses=1, expires_at=NOW + 1, discount_percent=bad)


def test_a_hundred_percent_rate_is_allowed(conn):
    created = promotion_service.create_coupon(
        conn, code="P100", discount_toman=9999, budget_toman=9999,
        max_uses=1, expires_at=NOW + 1, discount_percent=100)
    assert created["discount_percent"] == 100


def test_the_reads_expose_the_rate(conn):
    promotion_service.create_coupon(
        conn, code="PCT20", discount_toman=5000, budget_toman=100000,
        max_uses=5, expires_at=NOW + 86400, discount_percent=20)
    assert promotion_service.coupon_row(conn, "PCT20")["discount_percent"] == 20
    assert promotion_service.list_coupons(conn)[0]["discount_percent"] == 20


def test_the_rate_can_be_edited(conn):
    promotion_service.create_coupon(
        conn, code="PCT20", discount_toman=5000, budget_toman=100000,
        max_uses=5, expires_at=NOW + 86400, discount_percent=20)
    assert promotion_service.update_coupon(
        conn, "PCT20", discount_percent=35)["discount_percent"] == 35


# --------------------------------------------------------------------------- #
# 3. The wizard opens on the type picker
# --------------------------------------------------------------------------- #

def _coupon_flow():
    return ui_v52.flow_by_key(finance_admin_ui.COUPON_WIZARD_KEY)


def test_the_first_step_is_the_discount_type():
    flow = _coupon_flow()
    assert flow.steps[0].key == "discount_kind"
    assert flow.steps[0].choices == finance_admin_ui.COUPON_TYPE_CHOICES
    assert [value for _label, value in flow.steps[0].choices] == ["fixed", "percent"]


def test_the_preview_names_the_kind_the_operator_chose():
    flow = _coupon_flow()
    base = {"discount_value": 20, "budget_toman": 500000, "code": "PCT20",
            "max_uses": 10, "days": 30}
    assert "📊 درصدی" in flow.preview(dict(base, discount_kind="percent"))
    assert "💵 مبلغ ثابت" in flow.preview(dict(base, discount_kind="fixed"))


def test_the_preview_warns_about_an_impossible_rate():
    flow = _coupon_flow()
    preview = flow.preview({"discount_kind": "percent", "discount_value": 150,
                            "budget_toman": 500000, "code": "PCT150",
                            "max_uses": 10, "days": 30})
    assert "⚠️" in preview


def test_a_percent_sign_on_the_value_is_accepted():
    assert finance_admin_ui._wz_coupon_value("20%") == 20
    assert finance_admin_ui._wz_coupon_value("۲۰٪") == 20


def test_the_one_line_form_still_works_for_both_kinds():
    flow = _coupon_flow()
    assert flow.legacy("WELCOME10 | 10000 | 1000000 | 100 | 90")["discount_kind"] == "fixed"
    assert flow.legacy("WELCOME10 | 20% | 1000000 | 100 | 90")["discount_kind"] == "percent"


def test_the_wizard_commits_a_percentage_coupon(monkeypatch, tmp_path):
    import config as _config
    monkeypatch.setattr(_config, "ADMIN_TELEGRAM_ID", 777, raising=False)
    monkeypatch.setattr(finance_admin_ui, "_db", lambda: _db_ctx(tmp_path))
    notice = finance_admin_ui._commit_new_coupon(
        {"discount_kind": "percent", "discount_value": 20, "budget_toman": 500000,
         "code": "PCT20", "max_uses": 10, "days": 30}, {"actor_tg_id": 777})
    assert "PCT20" in notice
    assert "۲۰٪" in notice or "20٪" in notice


class _db_ctx:
    """A context manager over a fresh in-memory coupon database."""

    def __init__(self, _tmp_path):
        self._conn = _db()

    def __enter__(self):
        return self._conn

    def __exit__(self, *exc):
        self._conn.close()
        return False


# --------------------------------------------------------------------------- #
# 4. The two prompts delete the raw input
# --------------------------------------------------------------------------- #

class _Bot:
    def __init__(self, fail_delete=False):
        self.deleted = []
        self.sent = []
        self._fail_delete = fail_delete

    async def delete_message(self, chat_id, message_id):
        if self._fail_delete:
            raise RuntimeError("message to delete not found")
        self.deleted.append((chat_id, message_id))

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text, kwargs))


class _Chat:
    id = 555


class _Message:
    message_id = 77


class _Update:
    def __init__(self):
        self.effective_chat = _Chat()
        self.effective_message = _Message()
        self.message = _Message()


class _Context:
    def __init__(self, fail_delete=False):
        self.bot = _Bot(fail_delete=fail_delete)


def test_the_prompt_deletes_the_raw_input_then_sends_one_message():
    context = _Context()
    asyncio.run(admin_panel._replace_incoming(_Update(), context, "hello"))
    assert context.bot.deleted == [(555, 77)]
    assert context.bot.sent == [(555, "hello", {})]


def test_a_failed_delete_never_fails_the_flow():
    context = _Context(fail_delete=True)
    asyncio.run(admin_panel._replace_incoming(_Update(), context, "still works"))
    assert context.bot.deleted == []
    assert context.bot.sent == [(555, "still works", {})]


def test_the_inspection_back_button_returns_to_the_scope_section():
    keyboard = admin_panel.build_back_to_admin_keyboard(parent_cb="admin:scope")
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "admin:scope" in callbacks


def test_the_search_back_button_returns_to_the_user_list():
    keyboard = admin_panel.build_back_to_admin_keyboard([], parent_cb="admin:users:1")
    callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert "admin:users:1" in callbacks
