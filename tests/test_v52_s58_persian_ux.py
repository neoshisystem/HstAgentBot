"""S58 Persian-language UX pass (ARCH-UX-002) and the deduplicated nav footer.

Four modules were polished so that nothing an operator can see is a raw Python
mapping, a bare English identifier or a technical key, and ``ui_v52.render_page``
now closes a page with only the navigation controls it is still missing. Three
operator-visible properties are pinned here:

1. No page body rendered by the four modules shows a raw Python mapping or a bare
   English snake_case identifier — every label is fluent Persian.
2. No non-root page carries two back buttons: a page that ships its own exit row
   keeps it, and the footer adds only what is missing.
3. Every callback the footer emits is registered in ``v52_router``. An
   unregistered callback renders the W18 bridge error page instead of navigating.

Offline only: no panel, no Telegram, no network.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

import access_ui
import account_actions_ui
import bot_admin_ui
import checkout_ui
import config
import service_errors_fa
import ui_v52
import v52_router

REPO_ROOT = Path(__file__).resolve().parent.parent

ROOT_TG_ID = 9901
USER_TG_ID = 7001
#: A real attempt id is a UUID; the router only accepts 16-40 hex characters.
ATTEMPT_ID = "0123456789abcdef0123456789abcdef"
#: Operator-supplied data that is echoed back verbatim on the activation card.
BOT_USERNAME = "synthetic_bot"

#: A bare English snake_case identifier — the shape ARCH-UX-002 forbids on a page.
_SNAKE = re.compile(r"(?<![A-Za-z0-9_])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![A-Za-z0-9_])")
#: A Python mapping repr, raw or HTML-escaped: ``{'quota_bytes': 1}``.
_MAPPING = re.compile(r"\{[^{}]*['\"][A-Za-z_][A-Za-z0-9_]*['\"]\s*:")
_MAPPING_CALL = re.compile(r"\b(?:dict|OrderedDict|defaultdict)\(")
_LATIN_WORD = re.compile(r"[A-Za-z]{4,}")
_BACK_MARKERS = ("بازگشت", "↩", "🔙")

#: The internal keys a frozen renewal snapshot carries; none may reach a page.
_RENEWAL_KEYS = ("quota_bytes", "used_bytes", "expires_at", "quota_mode", "time_mode")

#: The internal operation tokens of ``account_actions_ui``. Each one is replaced by
#: a Persian label plus a one-line explanation of what the operation does.
_ACTION_TOKENS = ("rotate_subscription_url", "rotate_connection_credentials",
                  "reset_usage", "set_quota_bytes", "set_expiry_utc",
                  "set_remaining_days", "explicit unlimited")

_RAISE = re.compile(r"""raise\s+\w*Error\(\s*f?["']([^"']{12,})["']""")
_SCANNED_MODULES = ("checkout_ui.py", "account_actions_ui.py", "access_ui.py",
                    "bot_admin_ui.py")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _plain(page) -> str:
    """The rendered body with HTML entities resolved, so a repr cannot hide."""
    return html.unescape(page.text)


def _labels(page) -> list[str]:
    if page.reply_markup is None:
        return []
    return [str(item.text) for row in page.reply_markup.inline_keyboard for item in row]


def _callbacks(page) -> list[str]:
    if page.reply_markup is None:
        return []
    return [item.callback_data for row in page.reply_markup.inline_keyboard for item in row]


def _assert_no_raw_mapping(text: str) -> None:
    assert not _MAPPING.search(text), f"a raw mapping reached a page:\n{text}"
    assert not _MAPPING_CALL.search(text), f"a mapping call reached a page:\n{text}"


def _assert_no_snake_identifier(text: str, *, allow: tuple[str, ...] = ()) -> None:
    """``allow`` carries operator-supplied data that is echoed back verbatim."""
    scanned = text
    for value in allow:
        scanned = scanned.replace(value, "")
    found = _SNAKE.findall(scanned)
    assert not found, f"raw English identifier(s) on a page: {found}\n{text}"


def _renewal_quote() -> dict:
    """A frozen renewal quote in the shape ``services.renewal_calculator`` returns."""
    before = {"quota_mode": "finite", "quota_bytes": 1073741824, "used_bytes": 268435456,
              "time_mode": "dated", "expires_at": 1900000000}
    after = {"quota_mode": "finite", "quota_bytes": 2147483648, "used_bytes": 0,
             "time_mode": "dated", "expires_at": 1902592000, "days": 30}
    return {
        "quote_id": "a" * 32,
        "purpose": "vpn_renew",
        "amount_toman": 875,
        "price_edges": [{"seller_key": "root", "buyer_key": "user:7001",
                         "price_toman": 875}],
        "snapshot": {"spec": {"before": before, "after": after},
                     "before_after_renewal": {"before": before, "after": after}},
        "coupon": {"code": "SAVE", "discount_toman": 125},
    }


def _corpus() -> list[tuple[str, object]]:
    """Every page the four modules render, with the parent the bridge passes."""
    user = {"actor_tg_id": USER_TG_ID, "telegram_id": USER_TG_ID, "persona": "user"}
    root = {"actor_tg_id": ROOT_TG_ID, "chat_type": "private", "persona": "admin"}
    quote = _renewal_quote()
    buy_quote = {**quote, "purpose": "vpn_buy"}
    order = {"order_id": "ord-1", "actor_tg_id": USER_TG_ID, "amount_toman": 900,
             "status": "awaiting_payment"}
    card = {"card_number": "1111222233334444", "card_holder": "Test Holder",
            "bank_name": "Synthetic"}
    grant = {"grantee_tg_id": 7003, "access_level": "manager", "revoked_at": None}
    registration = {
        "bot_id": 123456, "username": BOT_USERNAME, "rep_id": 22, "status": "pending",
        "confirmation": {"callback_ok": "v52:ok:abcdefghijklmnopqrstuvwx",
                         "callback_no": "v52:no:abcdefghijklmnopqrstuvwx"},
    }
    return [
        ("checkout.preview_buy",
         checkout_ui.preview_buy(user, "basic", quote=buy_quote, parent_callback="v52:shop")),
        ("checkout.preview_renew",
         checkout_ui.preview_renew(user, 12, "basic", quote=quote, parent_callback="v52:sub:12")),
        ("checkout.begin_addon.form",
         checkout_ui.begin_addon(user, 12, parent_callback="v52:sub:12")),
        ("checkout.begin_addon.held",
         checkout_ui.begin_addon(user, 12, parent_callback="v52:sub:12", on_hold=True)),
        ("checkout.begin_addon.preview",
         checkout_ui.begin_addon(user, 12, "gib", 2, parent_callback="v52:sub:12")),
        ("checkout.choose_rail.wallet",
         checkout_ui.choose_rail(user, order, "wallet", wallet_balance=899,
                                 parent_callback="v52:orders")),
        ("checkout.choose_rail.card",
         checkout_ui.choose_rail(user, order, "card_to_card", parent_callback="v52:orders")),
        ("checkout.preview_receipt",
         checkout_ui.preview_receipt(user, {"attempt_id": ATTEMPT_ID, "amount_toman": 900},
                                     order=order, bank_card=card,
                                     parent_callback="v52:orders")),
        ("checkout.show_order",
         checkout_ui.show_order(user, order, payment={"status": "pending"},
                                operation={"state": "not_started"},
                                delivery={"state": "not_started"},
                                parent_callback="v52:orders")),
        ("account_actions.preview_rotation.sub",
         account_actions_ui.preview_rotation(user, 91, "sub", parent_callback="v52:sub:91")),
        ("account_actions.preview_rotation.cred",
         account_actions_ui.preview_rotation(user, 91, "cred", parent_callback="v52:sub:91")),
        ("account_actions.begin_traffic.form",
         account_actions_ui.begin_traffic(root, 42, parent_callback="v52:sub:42")),
        ("account_actions.begin_traffic.quota",
         account_actions_ui.begin_traffic(root, 42, "set_quota_bytes", 3, unit="GiB",
                                          parent_callback="v52:sub:42")),
        ("account_actions.begin_traffic.unlimited",
         account_actions_ui.begin_traffic(root, 42, "set_quota_bytes", unlimited=True,
                                          parent_callback="v52:sub:42")),
        ("account_actions.begin_traffic.reset",
         account_actions_ui.begin_traffic(root, 42, "reset_usage", parent_callback="v52:sub:42")),
        ("account_actions.begin_expiry.form",
         account_actions_ui.begin_expiry(root, 42, parent_callback="v52:sub:42")),
        ("account_actions.begin_expiry.utc",
         account_actions_ui.begin_expiry(root, 42, "set_expiry_utc", "2026-12-01T12:00:00",
                                         parent_callback="v52:sub:42")),
        ("account_actions.begin_expiry.days",
         account_actions_ui.begin_expiry(root, 42, "set_remaining_days", 5,
                                         parent_callback="v52:sub:42")),
        ("account_actions.begin_expiry.unlimited",
         account_actions_ui.begin_expiry(root, 42, "set_expiry_utc", unlimited=True,
                                         parent_callback="v52:sub:42")),
        ("access.show_grants",
         access_ui.show_grants(user, 71, grants=[grant], owner_tg_id=USER_TG_ID,
                               parent_callback="v52:sub:71")),
        ("access.begin_grant.levels",
         access_ui.begin_grant(user, 71, parent_callback="v52:grants:71")),
        ("access.begin_grant.grantee",
         access_ui.begin_grant(user, 71, 7003, parent_callback="v52:grants:71")),
        ("access.preview_grant",
         access_ui.preview_grant(user, 71, 7003, "manager", parent_callback="v52:grants:71")),
        ("access.preview_revoke",
         access_ui.preview_revoke(user, 71, 7003, grant=grant, parent_callback="v52:grants:71")),
        ("bot_admin.begin_register_bot.form",
         bot_admin_ui.begin_register_bot(root, parent_callback="v52:bots")),
        ("bot_admin.begin_register_bot.received",
         bot_admin_ui.begin_register_bot(root, 22, declared_bot_id=123456,
                                         token="123456:SECRET", chat_type="private",
                                         parent_callback="v52:bots")),
        ("bot_admin.preview_bot_activation",
         bot_admin_ui.preview_bot_activation(root, registration, parent_callback="v52:bots")),
    ]


@pytest.fixture
def pages(monkeypatch):
    """The rendered corpus, with the root identity and the ACL check pinned."""
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", ROOT_TG_ID)
    monkeypatch.setattr(access_ui.access_service, "authorize_account",
                        lambda *args, **kwargs: True)
    return _corpus()


# --------------------------------------------------------------------------- #
# 1. No raw mapping and no bare English identifier on any page
# --------------------------------------------------------------------------- #

def test_the_renewal_preview_shows_volume_and_expiry_in_persian(pages):
    """The before/after renewal used to print ``dict(...)`` of the frozen spec."""
    page = dict(pages)["checkout.preview_renew"]
    text = _plain(page)
    assert "وضعیت پیش از تمدید:" in text and "وضعیت پس از تمدید:" in text
    assert "1073741824 بایت" in text and "2147483648 بایت" in text
    assert "268435456" in text, "the used volume is part of the before/after picture"
    assert text.count("• انقضا:") == 2
    assert "زمان افزوده‌شدهٔ این تمدید: 30 روز" in text
    for key in _RENEWAL_KEYS:
        assert key not in text, key
    _assert_no_raw_mapping(text)
    _assert_no_snake_identifier(text)


def test_no_page_body_shows_a_raw_mapping_or_a_snake_case_identifier(pages):
    for name, page in pages:
        text = _plain(page)
        _assert_no_raw_mapping(text)
        _assert_no_snake_identifier(text, allow=(BOT_USERNAME,))


def test_the_action_pages_name_each_operation_in_persian(pages):
    for name, page in pages:
        text = _plain(page)
        for token in _ACTION_TOKENS:
            assert token not in text, (name, token)
    traffic = _plain(dict(pages)["account_actions.begin_traffic.form"])
    assert "بازنشانی مصرف — " in traffic and "تنظیم سقف حجم — " in traffic
    expiry = _plain(dict(pages)["account_actions.begin_expiry.form"])
    assert "تعیین تاریخ و زمان انقضا — " in expiry
    assert "تعیین تعداد روز باقی‌مانده — " in expiry
    unlimited = _plain(dict(pages)["account_actions.begin_traffic.unlimited"])
    assert "نامحدود (انتخاب صریح)" in unlimited
    rotation = _plain(dict(pages)["account_actions.preview_rotation.sub"])
    assert "عملیات: تغییر لینک سابسکریپشن" in rotation
    assert "لینک اشتراک فعلی باطل" in rotation


def test_the_grant_level_buttons_are_persian_and_keep_their_callbacks(pages):
    for name in ("access.begin_grant.levels", "access.begin_grant.grantee"):
        page = dict(pages)[name]
        assert _labels(page)[:2] == ["👁 مشاهده‌گر (Viewer)", "⚙️ مدیر حساب (Manager)"]
        assert _callbacks(page)[:2] == ["v52:gset:71:viewer", "v52:gset:71:manager"]


# --------------------------------------------------------------------------- #
# 2. One back button per page
# --------------------------------------------------------------------------- #

def test_no_non_root_page_carries_two_back_buttons(pages):
    for name, page in pages:
        labels = _labels(page)
        backs = [label for label in labels
                 if any(marker in label for marker in _BACK_MARKERS)]
        assert len(backs) <= 1, (name, backs)
        assert any("خانه" in label for label in labels), (name, labels)


def test_the_footer_deduplicates_a_caller_supplied_back_row():
    """A page with its own exit row keeps it; the footer adds only what is missing."""
    parent = "v52:sub:12"
    own_back = [ui_v52.button("بازگشت به حساب", "v52:account:12")]
    page = ui_v52.render_page("عنوان", "متن", persona="user",
                              parent_callback=parent, rows=[own_back])
    assert _callbacks(page) == ["v52:account:12", "v52:home:user"]
    assert _labels(page)[-1] == "⌂ خانه"

    plain = ui_v52.render_page("عنوان", "متن", persona="user", parent_callback=parent)
    assert _callbacks(plain) == [parent, "v52:home:user"]

    # A page that already carries both exits — the panel wizard's own rows — gets no
    # footer at all instead of a duplicate back and a duplicate home.
    wizard_rows = [[ui_v52.button("🔙 بازگشت به مرحله قبل", "v52:panelnew")],
                   [ui_v52.button("🏠 بازگشت به منوی اصلی", "v52:home:admin")]]
    wizard = ui_v52.render_page("عنوان", "متن", persona="admin",
                                parent_callback="v52:panels", rows=wizard_rows)
    assert _callbacks(wizard) == ["v52:panelnew", "v52:home:admin"]

    # A root page still carries no navigation footer.
    root_page = ui_v52.render_page("عنوان", "متن", persona="admin", root=True,
                                   parent_callback=parent, rows=[own_back])
    assert _callbacks(root_page) == ["v52:account:12"]


def test_the_footer_never_emits_an_unregistered_callback():
    for persona in ("admin", "rep", "user"):
        for parent in (None, f"v52:home:{persona}", "v52:sub:12"):
            page = ui_v52.render_page("عنوان", "متن", persona=persona,
                                      parent_callback=parent)
            callbacks = _callbacks(page)
            assert callbacks, "a non-root page must always expose at least one exit"
            for callback in callbacks:
                assert not callback.startswith("v52:back:"), callback
                assert isinstance(v52_router.route_callback(callback),
                                  v52_router.Route), callback


# --------------------------------------------------------------------------- #
# 3. Persian error pages and a complete translation map
# --------------------------------------------------------------------------- #

def test_every_error_page_from_the_four_modules_is_persian(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", ROOT_TG_ID)
    monkeypatch.setattr(access_ui.access_service, "authorize_account",
                        lambda *args, **kwargs: True)
    user = {"actor_tg_id": USER_TG_ID, "persona": "user"}
    root = {"actor_tg_id": ROOT_TG_ID, "chat_type": "private", "persona": "admin"}
    quote = _renewal_quote()
    order = {"order_id": "ord-1", "actor_tg_id": USER_TG_ID, "amount_toman": 900,
             "status": "awaiting_payment"}
    errors = [
        ("checkout.plan_code",
         checkout_ui.preview_buy(user, "", quote={**quote, "purpose": "vpn_buy"})),
        ("checkout.renew.plan_code",
         checkout_ui.preview_renew(user, 12, "", quote=quote)),
        ("checkout.rail.mixed", checkout_ui.choose_rail(user, order, "mixed")),
        ("checkout.order.foreign",
         checkout_ui.show_order(user, {**order, "actor_tg_id": 9999})),
        ("checkout.addon.on_hold",
         checkout_ui.begin_addon(user, 12, "day", 5, on_hold=True)),
        ("checkout.receipt.private",
         checkout_ui.preview_receipt({"actor_tg_id": 9999}, {"amount_toman": 900},
                                     order=order)),
        ("account_actions.traffic.zero",
         account_actions_ui.begin_traffic(root, 42, "set_quota_bytes", 0, unit="GiB")),
        ("account_actions.traffic.unit",
         account_actions_ui.begin_traffic(root, 42, "set_quota_bytes", 1, unit="TB")),
        ("account_actions.traffic.mode",
         account_actions_ui.begin_traffic(root, 42, "set_everything")),
        ("account_actions.expiry.bad_date",
         account_actions_ui.begin_expiry(root, 42, "set_expiry_utc", "not-a-date")),
        ("account_actions.expiry.bad_zone",
         account_actions_ui.begin_expiry(root, 42, "set_expiry_utc",
                                         "2026-12-01T12:00:00", timezone_name="Bad/Zone")),
        ("account_actions.expiry.days",
         account_actions_ui.begin_expiry(root, 42, "set_remaining_days", -3)),
        ("account_actions.rotation.type",
         account_actions_ui.preview_rotation(user, 91, "other")),
        ("access.grant.level", access_ui.begin_grant(user, 71, access_level="owner")),
        ("access.revoke.stale",
         access_ui.preview_revoke(user, 71, 7003,
                                  grant={"grantee_tg_id": 7003, "access_level": "manager",
                                         "revoked_at": 1})),
        ("bot_admin.rep_id",
         bot_admin_ui.begin_register_bot(root, declared_bot_id=1, token="1:x",
                                         chat_type="private")),
        ("bot_admin.status",
         bot_admin_ui.preview_bot_activation(root, {"bot_id": 1, "username": "b",
                                                    "status": "active"})),
        ("bot_admin.username",
         bot_admin_ui.preview_bot_activation(root, {"bot_id": 1, "status": "pending"})),
    ]
    for name, page in errors:
        assert page.kind == "error", name
        text = _plain(page)
        assert service_errors_fa.is_persian(text), (name, text)
        _assert_no_raw_mapping(text)
        _assert_no_snake_identifier(text)


def _english_raise_literals() -> list[tuple[str, str]]:
    """Every English ``raise`` literal the four modules can put on a page."""
    found: list[tuple[str, str]] = []
    for name in _SCANNED_MODULES:
        source = (REPO_ROOT / name).read_text(encoding="utf-8")
        for match in _RAISE.finditer(source):
            message = match.group(1)
            if not service_errors_fa.is_persian(message):
                found.append((name, message))
    return found


def test_the_translation_scan_finds_the_messages_it_claims_to_cover():
    messages = {message for _name, message in _english_raise_literals()}
    assert len(messages) >= 40, sorted(messages)
    for expected in ("plan_code is required", "order belongs to another actor",
                     "mode must be reset_usage or set_quota_bytes",
                     "rotation_type must be sub or cred",
                     "only a pending verified bot can be activated",
                     "{label} must be a positive integer"):
        assert expected in messages, expected


def test_every_english_message_the_four_modules_raise_is_translated():
    for name, message in _english_raise_literals():
        rendered = service_errors_fa.translate(message)
        assert rendered != service_errors_fa.DEFAULT_FALLBACK, (name, message)
        assert service_errors_fa.is_persian(rendered), (name, message)
        assert not _LATIN_WORD.search(rendered), (name, message, rendered)


def test_the_messages_built_at_runtime_are_translated_too():
    """``_positive``/``_money`` interpolate the field name into the message."""
    for message in ("actor_tg_id must be a positive integer",
                    "account_id must be a positive integer",
                    "grantee_tg_id must be a positive integer",
                    "rep_id must be a positive integer",
                    "declared_bot_id must be a positive integer",
                    "bot_id must be a positive integer",
                    "amount_toman must be a non-negative integer toman amount",
                    "price edge must be a non-negative integer toman amount",
                    "wallet_balance must be a non-negative integer toman amount",
                    "unsupported account action: issue",
                    "Invalid isoformat string: 'not-a-date'",
                    "No time zone found with key Bad/Zone",
                    "invalid literal for int() with base 10: 'abc'"):
        rendered = service_errors_fa.translate(message)
        assert rendered != service_errors_fa.DEFAULT_FALLBACK, message
        assert service_errors_fa.is_persian(rendered), message
        assert not _LATIN_WORD.search(rendered), (message, rendered)
