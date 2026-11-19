"""S58 guided-wizard acceptance tests; offline and backed by tmp_path SQLite."""
from __future__ import annotations

import asyncio
import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import config
import database
import goods_ui
import rep_bot
import ticket_status_ui
import tier_admin_ui
import ui_v52
import v52_router

ROOT_TG_ID = 101258148


def _load_helpers():
    path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_v52_s58_helpers", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helpers = _load_helpers()


class FakeMessage:
    def __init__(self, message_id=31, chat_id=ROOT_TG_ID):
        self.message_id = message_id
        self.chat_id = chat_id
        self.sent = []

    async def reply_text(self, text, **kwargs):
        self.sent.append((text, kwargs))

    async def delete(self):
        return None


class FakeQuery:
    def __init__(self, message, data=""):
        self.message = message
        self.data = data

    async def answer(self):
        return None


class FakeBot:
    async def edit_message_text(self, **kwargs):
        return None

    async def delete_message(self, **kwargs):
        return None


@pytest.fixture
def wizard_env(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", ROOT_TG_ID, raising=False)
    monkeypatch.setattr(database, "ADMIN_TELEGRAM_ID", ROOT_TG_ID, raising=False)
    monkeypatch.setattr(rep_bot, "ADMIN_TELEGRAM_ID", ROOT_TG_ID, raising=False)
    with helpers.make_db(database, monkeypatch, tmp_path, filename="s58-wizards.sqlite3"):
        # The wizard flows write to migration-owned tables (tier_policies,
        # ticket_statuses, digital_products). ``make_db`` only bootstraps, so the
        # migrations must run before any assertion counts rows in them.
        from migrations_runner import apply_migrations
        with database.get_db() as conn:
            apply_migrations(conn, Path(__file__).resolve().parent.parent / "migrations",
                             now=1794500000)
            conn.commit()
        ctx = {"actor_tg_id": ROOT_TG_ID, "telegram_id": ROOT_TG_ID,
               "chat_id": ROOT_TG_ID, "bot_id": 5, "chat_type": "private",
               "persona": "admin", "is_root": True}
        yield ctx


def _callback_data(page):
    return [b.callback_data for row in page.reply_markup.inline_keyboard for b in row]


def _flow_harness(ctx, flow_key, seed=None):
    user_data = {}
    state = ui_v52.start_wizard(user_data, flow_key,
                                return_callback="v52:home:admin", seed=seed or {},
                                message_id=31, chat_id=ROOT_TG_ID)
    user = SimpleNamespace(id=ROOT_TG_ID, full_name="S58 operator", username="s58")
    update = SimpleNamespace(effective_user=user)
    return user_data, state, update


def _confirm(flow_key, ctx, seed=None):
    user_data, state, update = _flow_harness(ctx, flow_key, seed)
    notice = ui_v52.wizard_confirm(state, {"id": ROOT_TG_ID, "name": "test", "username": ""})
    assert notice
    return user_data, state, update


def _choose_and_text(state, data):
    action, args = ui_v52.wizard_parse_callback(data)
    assert action == "pick"
    return ui_v52.wizard_choose(state, *args)


def test_tier_wizard_previews_then_creates_only_on_confirm(wizard_env):
    ctx = wizard_env
    data, state, _ = _flow_harness(ctx, "tier_admin_new")
    assert _choose_and_text(state, "v52:wz:pick:0:0")[0] == "step"
    assert ui_v52.wizard_apply_text(state, "کاربر الماس")[0] == "step"
    state["values"]["tier_code"] = "diamond_user"
    assert _choose_and_text(state, "v52:wz:pick:2:2")[0] == "preview"
    preview = ui_v52.render_wizard_preview(state)
    assert "پیش‌نمایش" in preview.text
    assert "الماس" in preview.text
    assert "v52:wz:confirm" in _callback_data(preview)
    assert database.get_db
    with database.get_db() as conn:
        assert conn.execute("SELECT 1 FROM tier_policies WHERE actor_type='end_user' AND tier_code='diamond_user'").fetchone() is None
    ui_v52.wizard_confirm(state, {"id": ROOT_TG_ID, "name": "operator", "username": ""})
    with database.get_db() as conn:
        row = conn.execute("SELECT title_fa,discount_bps FROM tier_policies WHERE actor_type='end_user' AND tier_code='diamond_user'").fetchone()
    assert row is not None and row[0] == "کاربر الماس" and row[1] == 1000


def test_ticket_status_wizard_previews_then_creates_only_on_confirm(wizard_env):
    ctx = wizard_env
    _, state, _ = _flow_harness(ctx, "ticket_status_new")
    assert _choose_and_text(state, "v52:wz:pick:0:1")[0] == "step"
    assert _choose_and_text(state, "v52:wz:pick:1:8")[0] == "step"
    assert ui_v52.wizard_apply_text(state, "نیازمند پیگیری")[0] == "preview"
    state["values"]["code"] = "needs_followup"
    preview = ui_v52.render_wizard_preview(state)
    assert "پیش‌نمایش" in preview.text and "پیگیری" in preview.text
    assert "v52:wz:confirm" in _callback_data(preview)
    with database.get_db() as conn:
        assert conn.execute("SELECT 1 FROM ticket_statuses WHERE code='needs_followup'").fetchone() is None
    ui_v52.wizard_confirm(state, {"id": ROOT_TG_ID, "name": "operator", "username": ""})
    with database.get_db() as conn:
        row = conn.execute("SELECT title_fa,emoji,group_key FROM ticket_statuses WHERE code='needs_followup'").fetchone()
    assert row is not None and tuple(row) == ("نیازمند پیگیری", "📌", "answered")


def test_product_wizard_defers_insert_until_preview_confirm(wizard_env):
    ctx = wizard_env
    payload = {"step": "title"}
    assert goods_ui.build_product_wizard_step(ctx, payload, value="محصول آزمایشی")[0]
    payload = {"step": "category", "title": "محصول آزمایشی"}
    goods_ui.build_product_wizard_step(ctx, payload, value="S58 دسته")
    payload = {"step": "delivery", "title": "محصول آزمایشی", "category_title": "S58 دسته"}
    goods_ui.build_product_wizard_step(ctx, payload, value="auto_inventory")
    payload = {"step": "price", "title": "محصول آزمایشی", "category_title": "S58 دسته", "delivery_type": "auto_inventory"}
    goods_ui.build_product_wizard_step(ctx, payload, value="12000")
    payload = {"step": "description", "title": "محصول آزمایشی", "category_title": "S58 دسته", "delivery_type": "auto_inventory", "base_price": 12000}
    goods_ui.build_product_wizard_step(ctx, payload, value="راهنمای استفاده")
    payload = {"step": "audience", "title": "محصول آزمایشی", "category_title": "S58 دسته", "delivery_type": "auto_inventory", "base_price": 12000, "description": "راهنمای استفاده"}
    text, rows = goods_ui.build_product_wizard_step(ctx, payload, value="all")
    assert "پیش‌نمایش" in text
    assert any("confirm" in b.callback_data for row in rows for b in row)
    with database.get_db() as conn:
        assert conn.execute("SELECT 1 FROM digital_products WHERE title='محصول آزمایشی'").fetchone() is None
    entry = v52_router.DEFAULT_FSM.peek(ctx, now=int(time.time()))
    assert entry is not None and entry.state == "goods_product_draft"
    confirm_text, confirm_rows = goods_ui.confirm_product_wizard(ctx)
    assert "محصول ثبت شد" in confirm_text
    with database.get_db() as conn:
        assert conn.execute("SELECT 1 FROM digital_products WHERE title='محصول آزمایشی'").fetchone() is not None
