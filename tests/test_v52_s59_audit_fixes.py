"""S59 regression tests for the eight CP0059 audit findings.

One test per finding, each pinned to the exact defect that was fixed, so the fix
cannot silently regress:

1. the wizard owns the message before the legacy prompt renders (no double edit),
2. ``admin:plan_detail`` and friends render the unified tariff card,
3. «هر دو نرخ» actually changes both prices,
4. ``admin_edit_rep`` writes the chosen field and shows its real previous value,
5. the settlement and wallet previews compute the resulting amount,
6. a token-only private answer is a complete bot registration,
7. the add-category / add-coupon / add-card forms are wizards with a preview,
8. ``access_ui`` shows Persian level names while keeping the pinned tokens.

Offline only: no Telegram, no network, no live database.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

import access_ui
import bot_admin_ui
import finance_admin_ui
import ui_v52

REPO_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# 1. The wizard is started before the legacy prompt renders
# --------------------------------------------------------------------------- #

def test_the_wizard_fast_path_precedes_the_legacy_prompt_chain():
    """CP0059 edited the message with the legacy text and then rendered the wizard.

    The wizard must be resolved and returned from *before* the legacy
    ``if action == "prompt_card_settings":`` chain, otherwise the operator sees the
    old prompt flicker and the same message is edited twice.
    """
    source = (REPO_ROOT / "admin_panel.py").read_text(encoding="utf-8")
    fast_path = source.index("if _wizard_flow_key and (_wizard_seed or not _wizard_needs_seed):")
    legacy_chain = source.index('if action == "prompt_card_settings":')
    assert fast_path < legacy_chain, "the wizard fast-path must come first"
    # ...and it must leave the handler rather than fall through into the old chain.
    between = source[fast_path:legacy_chain]
    assert "await ui_v52.wizard_render(update, context, _wizard_state)" in between
    assert re.search(r"await query\.answer\(\)\s*\n\s*return\b", between), \
        "the fast-path must answer and return before the legacy prompt"


# --------------------------------------------------------------------------- #
# 2. Every plan entry point renders the unified card
# --------------------------------------------------------------------------- #

def test_plan_callbacks_render_the_unified_tariff_card():
    """Every plan entry point must carry the unified controls.

    ``plan_detail`` renders the shared card outright. ``set_plan_quick`` and
    ``set_plan_active`` keep their legacy Markdown body — an existing test pins that
    text — but must attach the same unified control rows, so neither is a half-view.
    """
    source = (REPO_ROOT / "admin_panel.py").read_text(encoding="utf-8")
    assert "finance_admin_ui.plan_detail" in source
    assert "finance_admin_ui.tariff_hub" in source
    for action in ("set_plan_quick", "set_plan_active"):
        start = source.index(f'elif action == "{action}":')
        block = source[start:source.index("elif action ==", start + 10)]
        assert "_plan_card_keyboard(" in block, f"{action} lost the unified controls"


def test_plan_controls_are_shared_by_both_views():
    """The detail card and the hub must build their rows from one helper."""
    source = (REPO_ROOT / "finance_admin_ui.py").read_text(encoding="utf-8")
    assert "def plan_control_rows" in source


# --------------------------------------------------------------------------- #
# 3. «هر دو نرخ» changes both prices
# --------------------------------------------------------------------------- #

def test_both_price_option_resolves_two_numbers():
    """Choosing «هر دو نرخ» and typing two numbers must move both prices.

    The engine hands ``parse`` only the raw text, so the shared resolver is what
    decides whether one number or a pair was given.
    """
    import admin_panel
    parse = admin_panel._wz_price_input
    resolve = admin_panel._wz_resolve_prices

    # The step parses the raw text; one number or a pair.
    assert parse("120000 90000") == [120000, 90000]
    assert parse("150000") == [150000]
    # A thousands separator belongs to the number, it does not start a second one.
    assert parse("۱۲۰٬۰۰۰") == [120000]
    assert parse("۱۲۰٬۰۰۰ ۹۰٬۰۰۰") == [120000, 90000]
    with pytest.raises(ValueError):
        parse("1 2 3")

    # «both» with a pair sets each rate to its own value.
    assert resolve("both", parse("120000 90000"), 100000, 80000) == (120000, 90000)
    # «both» with one number applies it to both rates rather than being ignored.
    assert resolve("both", parse("150000"), 100000, 80000) == (150000, 150000)
    # A single-field edit only moves that field.
    assert resolve("rep_price", parse("130000"), 100000, 80000) == (130000, 80000)
    assert resolve("user_price", parse("70000"), 100000, 80000) == (100000, 70000)


def test_the_edit_plan_flow_uses_the_shared_resolver():
    """Preview and commit must resolve the answer through one helper.

    ``commit_edit`` is defined before the flow is registered, so the whole edit
    section is searched rather than only the ``register_flow`` call.
    """
    source = (REPO_ROOT / "admin_panel.py").read_text(encoding="utf-8")
    # The write path resolves it in the plan flow's ``commit_edit``...
    assert "_wz_resolve_prices(" in source[source.index('"admin_edit_plan"'):], \
        "the commit must resolve the chosen rate(s)"
    # ...and the preview resolves it through the same helper, so the card cannot
    # promise a change the commit does not make.
    label = source[source.index("def _wz_price_change_label(values) -> str:"):]
    label = label[:label.index("\ndef ", 10)]
    assert "_wz_resolve_prices(" in label, "the preview must use the shared resolver"


# --------------------------------------------------------------------------- #
# 4. admin_edit_rep writes the chosen field
# --------------------------------------------------------------------------- #

def test_the_rep_edit_fields_are_writable_columns():
    """``group_topic`` was not a column, so choosing it never wrote anything."""
    import admin_panel
    choices = {key for _label, key in admin_panel.EDIT_REP_FIELD_CHOICES}
    assert "group_topic" not in choices, "group_topic is not a column"
    assert {"group_id", "topic_id"} <= choices, choices
    assert choices <= set(admin_panel.EDIT_REP_FIELD_FA), \
        "every choice needs a Persian label for the preview"


def test_integer_rep_fields_reject_text():
    """A Telegram/group/topic id must never reach the database as text."""
    import admin_panel
    parse = admin_panel._parse_edit_rep_value
    assert parse("123456", "telegram_id") == 123456
    assert parse("123456", "group_id") == 123456
    assert parse("123456", "topic_id") == 123456
    assert parse("نماینده الف", "name") == "نماینده الف"
    for bad in ("abc", "", "   "):
        with pytest.raises(ValueError):
            parse(bad, "telegram_id")


def test_the_rep_preview_shows_the_real_previous_value():
    """The seed used to carry ``previous_value = ""``, so the card always showed «—»."""
    import admin_panel
    values = {"field": "telegram_id", "current": {"telegram_id": 4242},
              "new_value": "4243"}
    assert admin_panel._wz_rep_previous_value(values) == "4242"
    assert admin_panel._wz_rep_previous_value({"field": "name", "current": {}}) == "—"
    # The preview reports an unusable value instead of losing the whole form.
    assert admin_panel._wz_rep_preview_value(
        {"field": "telegram_id", "new_value": "abc"}).startswith("⚠️")
    assert admin_panel._wz_rep_preview_value(
        {"field": "telegram_id", "new_value": "4243"}) == "4243"


# --------------------------------------------------------------------------- #
# 5. The money previews compute the resulting amount
# --------------------------------------------------------------------------- #

def test_settlement_preview_computes_the_remaining_debt():
    import admin_panel
    assert admin_panel._wz_debt_after({"current_debt": 500000, "amount": 200000}) == 300000
    # An explicit value still wins.
    assert admin_panel._wz_debt_after({"current_debt": 1, "amount": 1,
                                       "debt_after": 99}) == 99


def test_wallet_preview_computes_the_resulting_balance():
    import admin_panel
    assert admin_panel._wz_balance_after({"current_balance": 50000,
                                          "amount": 25000}) == 75000
    # A negative change (a deduction) is reflected rather than ignored.
    assert admin_panel._wz_balance_after({"current_balance": 50000,
                                          "amount": -20000}) == 30000


# --------------------------------------------------------------------------- #
# 6. A token-only answer registers a bot
# --------------------------------------------------------------------------- #

def test_a_token_alone_is_a_complete_registration_answer(monkeypatch):
    import config
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9901, raising=False)
    root = {"actor_tg_id": 9901, "chat_type": "private"}
    secret = "123456:SECRET_SYNTHETIC_TOKEN"

    assert bot_admin_ui.parse_bot_credentials(secret) == (123456, secret)
    # The older ``<bot_id> <token>`` line keeps working.
    assert bot_admin_ui.parse_bot_credentials(f"123456 {secret}") == (123456, secret)

    page = bot_admin_ui.begin_register_bot(root, 22, text=secret, chat_type="private")
    assert page.kind == "form"
    assert secret not in page.text, "the token must never be rendered"


def test_an_unusable_token_is_rejected_with_a_persian_reason(monkeypatch):
    import config
    import service_errors_fa
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9901, raising=False)
    root = {"actor_tg_id": 9901, "chat_type": "private"}

    for bad in ("", "   ", "garbage", "nope:xx"):
        with pytest.raises(ValueError):
            bot_admin_ui.parse_bot_credentials(bad)

    page = bot_admin_ui.begin_register_bot(root, 22, text="garbage", chat_type="private")
    assert page.kind == "error"
    assert service_errors_fa.is_persian(page.text)


def test_registration_from_text_ends_on_the_activation_preview(monkeypatch):
    """The whole flow: one message with the token -> verify -> confirmation card."""
    import asyncio
    import config
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9901, raising=False)
    root = {"actor_tg_id": 9901, "chat_type": "private"}
    seen = {}

    async def verify(**kwargs):
        seen.update(kwargs)
        return {"bot_id": kwargs["bot_id"], "username": "synthetic_bot",
                "rep_id": kwargs["rep_id"], "status": "pending",
                "confirmation": {"callback_ok": "v52:ok:abcdefghijklmnopqrstuvwx",
                                 "callback_no": "v52:no:abcdefghijklmnopqrstuvwx"}}

    page = asyncio.run(bot_admin_ui.begin_registration_from_text(
        root, "123456:SECRET_SYNTHETIC_TOKEN", verify=verify, rep_id=22))
    assert seen["bot_id"] == 123456 and seen["rep_id"] == 22
    assert page.kind == "preview"
    assert "123456" in page.text and "synthetic_bot" in page.text
    assert "SECRET_SYNTHETIC_TOKEN" not in page.text


# --------------------------------------------------------------------------- #
# 7. The three add-forms are wizards with a preview
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("key", [finance_admin_ui.PLAN_CATEGORY_WIZARD_KEY,
                                 finance_admin_ui.COUPON_WIZARD_KEY,
                                 finance_admin_ui.BANK_CARD_WIZARD_KEY])
def test_each_add_form_is_a_registered_wizard_with_a_preview(key):
    flow = ui_v52.flow_by_key(key)
    assert flow is not None, key
    assert flow.steps and callable(flow.preview) and callable(flow.commit)


@pytest.mark.parametrize("key", [finance_admin_ui.PLAN_CATEGORY_WIZARD_KEY,
                                 finance_admin_ui.COUPON_WIZARD_KEY])
def test_the_pipe_forms_keep_a_legacy_parser(key):
    """The two forms that had a pipe format must keep their single-line fast-path.

    The bank card never had one — it was set through ``admin:prompt_card_settings`` —
    so it is deliberately excluded.
    """
    assert ui_v52.flow_by_key(key).legacy is not None


def test_the_new_category_wizard_collects_and_previews():
    flow = ui_v52.flow_by_key(finance_admin_ui.PLAN_CATEGORY_WIZARD_KEY)
    state = ui_v52.start_wizard({}, flow.key, return_callback="v52:plancats")
    ui_v52.wizard_choose(state, 0, 0)
    ui_v52.wizard_apply_text(state, "سرویس‌های گیمینگ")
    ui_v52.wizard_generate(state, 2)
    preview = flow.preview(state["values"])
    assert "سرویس‌های گیمینگ" in preview
    assert re.search(r"cat_[0-9a-f]{6}", preview)


def test_the_legacy_pipe_lines_still_parse():
    """The S57 tests drive these formats, so the fast-path must keep recognising them."""
    category = ui_v52.flow_by_key(finance_admin_ui.PLAN_CATEGORY_WIZARD_KEY)
    assert category.legacy("gaming | سرویس‌های گیمینگ | 🎮") == {
        "category_key": "gaming", "title_fa": "سرویس‌های گیمینگ", "emoji": "🎮"}
    assert category.legacy("nonsense") is None

    coupon = ui_v52.flow_by_key(finance_admin_ui.COUPON_WIZARD_KEY)
    assert coupon.legacy("WELCOME10 | 10000 | 1000000 | 100 | 90") == {
        "code": "WELCOME10", "discount_kind": "fixed", "discount_value": 10000,
        "budget_toman": 1000000, "max_uses": 100, "days": 90}
    # S60 — a ``%`` on the discount field makes the one-line form a percentage coupon.
    assert coupon.legacy("WELCOME10 | 20% | 1000000 | 100 | 90") == {
        "code": "WELCOME10", "discount_kind": "percent", "discount_value": 20,
        "budget_toman": 1000000, "max_uses": 100, "days": 90}
    assert coupon.legacy("WELCOME10 | x | y | z | w") is None


def test_the_card_wizard_never_previews_the_full_number():
    flow = ui_v52.flow_by_key(finance_admin_ui.BANK_CARD_WIZARD_KEY)
    state = ui_v52.start_wizard({}, flow.key, return_callback="v52:cards")
    ui_v52.wizard_apply_text(state, "6037-9911-2233-4455")
    ui_v52.wizard_apply_text(state, "علی رضا محمدی")
    ui_v52.wizard_choose(state, 2, 0)
    preview = flow.preview(state["values"])
    assert "6037" not in preview and "4455" in preview
    assert "••••" in preview


def test_the_coupon_wizard_warns_when_the_budget_is_below_the_discount():
    flow = ui_v52.flow_by_key(finance_admin_ui.COUPON_WIZARD_KEY)
    state = ui_v52.start_wizard({}, flow.key, return_callback="v52:coupons")
    # S60 — step 1 is now the discount type, so a fixed coupon is chosen first.
    ui_v52.wizard_choose(state, 0, 0)
    ui_v52.wizard_apply_text(state, "10000")
    ui_v52.wizard_apply_text(state, "5000")
    ui_v52.wizard_generate(state, 3)
    ui_v52.wizard_apply_text(state, "100")
    ui_v52.wizard_apply_text(state, "90")
    assert "⚠️" in flow.preview(state["values"])


def test_the_add_card_callback_is_routed():
    import v52_router
    assert isinstance(v52_router.route_callback("v52:card:new"), v52_router.Route)


# --------------------------------------------------------------------------- #
# 8. access_ui shows Persian level names
# --------------------------------------------------------------------------- #

def test_the_grant_levels_are_named_in_persian(monkeypatch):
    import config
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9901, raising=False)
    monkeypatch.setattr(access_ui.access_service, "authorize_account",
                        lambda *a, **k: True)
    user = {"actor_tg_id": 7001, "persona": "user"}
    grant = {"grantee_tg_id": 7003, "access_level": "manager", "revoked_at": None}

    listed = access_ui.show_grants(user, 71, grants=[grant], owner_tg_id=7001)
    assert "مدیر حساب" in listed.text
    # The bare token stays, because an existing contract pins it.
    assert "manager" in listed.text

    preview = access_ui.preview_grant(user, 71, 7003, "manager")
    assert "مدیر حساب" in preview.text and "viewer" not in preview.text

    revoke = access_ui.preview_revoke(user, 71, 7003, grant=grant)
    assert "سطح فعلی: مدیر حساب" in revoke.text
    # The sentence an existing test pins must survive verbatim.
    assert "مالک اصلی و سایر grantها دست‌نخورده" in revoke.text


def test_the_level_buttons_keep_their_pinned_labels_and_callbacks(monkeypatch):
    import config
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9901, raising=False)
    monkeypatch.setattr(access_ui.access_service, "authorize_account",
                        lambda *a, **k: True)
    page = access_ui.begin_grant({"actor_tg_id": 7001, "persona": "user"}, 71)
    labels = [item.text for row in page.reply_markup.inline_keyboard for item in row]
    callbacks = [item.callback_data for row in page.reply_markup.inline_keyboard
                 for item in row]
    assert labels[:2] == ["👁 مشاهده‌گر (Viewer)", "⚙️ مدیر حساب (Manager)"]
    assert callbacks[:2] == ["v52:gset:71:viewer", "v52:gset:71:manager"]
