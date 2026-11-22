"""Strict, registered v5.2 callback/text/photo router and isolated draft FSM.

The router resolves callbacks only; business mutations remain inside the declared
service handlers. FSM identity includes bot, human actor and chat to prevent
cross-bot or cross-chat draft leakage.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Callable

from ui_v52 import render_error

MAX_CALLBACK_BYTES = 64
DEFAULT_TTL_SECONDS = 900
_ID = re.compile(r"^[1-9][0-9]{0,17}$")
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,47}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{16,40}$")

#: Draft states that accept a plain text answer. The registry lives in one place
#: because ``DraftFSM.set`` and ``DraftFSM.consume`` must agree exactly: a state
#: accepted by one and not the other silently swallows the operator's answer.
TEXT_STATES: frozenset[str] = frozenset({
    "goods_manual_secret", "support_ticket_body", "support_reply",
    "panel_connection_url", "panel_connection_token",
    "inventory_manual_batch", "inventory_digital_batch",
    "ownership_transfer", "goods_product_draft",
    "goods_product_price", "goods_product_guide",
    "panel_connection_name",
    "ticket_status_new", "ticket_status_edit",
    "tier_new", "tier_edit",
    "domain_hostname",
    # S57: plan-category administration, the tier price matrix, coupons and the
    # admin-initiated refund destination.
    "plan_category_new", "plan_category_edit",
    "tier_plan_price", "refund_destination",
    "coupon_new", "coupon_edit",
    "panel_advanced",
    # S60: the private-DM answer that carries a dedicated bot's token. Without it the
    # "send me the token" form was a dead end — the state was never settable, so the
    # operator's token fell through to the ordinary text handlers.
    "bot_register_token",
})

#: Draft states that also accept a photo attachment.
PHOTO_STATES: frozenset[str] = frozenset({"support_ticket_body", "support_reply"})


@dataclass(frozen=True)
class Route:
    name: str
    handler: str
    params: dict[str, Any]
    scope: str


@dataclass
class FSMEntry:
    bot_id: int
    telegram_id: int
    chat_id: int
    state: str
    parent_callback: str | None
    expires_at: int
    payload: dict[str, Any]


_CALLBACK_PATTERNS: tuple[tuple[re.Pattern[str], str, str, Callable[[re.Match[str]], dict[str, Any]]], ...] = (
    (re.compile(r"^v52:home:(admin|rep|user)$"), "route_home", "persona", lambda m: {"persona": m[1]}),
    # Legacy placeholder emitted by older builds' navigation rows; kept registered so
    # messages already delivered to users keep navigating instead of erroring.
    (re.compile(r"^v52:back:(admin|rep|user)$"), "route_home", "persona", lambda m: {"persona": m[1]}),
    (re.compile(r"^v52:sub:([1-9][0-9]{0,17})$"), "open_account_profiles", "view_config", lambda m: {"account_id": int(m[1])}),
    (re.compile(r"^v52:profiles$"), "list_profiles", "actor", lambda m: {}),
    (re.compile(r"^v52:profile:([1-9][0-9]{0,17})$"), "show_profile", "owner/root", lambda m: {"profile_id": int(m[1])}),
    (re.compile(r"^v52:padd:([1-9][0-9]{0,17}):([1-9][0-9]{0,17})$"), "add_member", "owner/root", lambda m: {"profile_id": int(m[1]), "account_id": int(m[2])}),
    (re.compile(r"^v52:premove:([1-9][0-9]{0,17}):([1-9][0-9]{0,17})$"), "preview_remove", "owner/root", lambda m: {"profile_id": int(m[1]), "account_id": int(m[2])}),
    (re.compile(r"^v52:pnew:([0-9]{1,18})$"), "begin_create", "actor", lambda m: {"account_id": int(m[1])}),
    (re.compile(r"^v52:plink:([1-9][0-9]{0,17})$"), "show_link", "owner/root", lambda m: {"profile_id": int(m[1])}),
    (re.compile(r"^v52:pqr:([1-9][0-9]{0,17})$"), "show_qr", "owner/root", lambda m: {"profile_id": int(m[1])}),
    (re.compile(r"^v52:prot:([1-9][0-9]{0,17})$"), "preview_rotate", "owner/root", lambda m: {"profile_id": int(m[1])}),
    (re.compile(r"^v52:ppause:([1-9][0-9]{0,17}):(0|1)$"), "preview_enabled", "owner/root", lambda m: {"profile_id": int(m[1]), "enabled": m[2] == "1"}),
    (re.compile(r"^v52:pdel:([1-9][0-9]{0,17})$"), "preview_delete", "owner/root", lambda m: {"profile_id": int(m[1])}),
    (re.compile(r"^v52:pclear:([1-9][0-9]{0,17})$"), "preview_clear", "owner/root", lambda m: {"profile_id": int(m[1])}),
    (re.compile(r"^v52:prename:([1-9][0-9]{0,17})$"), "begin_rename", "owner/root", lambda m: {"profile_id": int(m[1])}),
    (re.compile(r"^v52:rotate:(sub|cred):([1-9][0-9]{0,17})$"), "preview_rotation", "manager/owner/seller/root", lambda m: {"rotation_type": m[1], "account_id": int(m[2])}),
    (re.compile(r"^v52:traffic:([1-9][0-9]{0,17})$"), "begin_traffic", "root", lambda m: {"account_id": int(m[1])}),
    (re.compile(r"^v52:expiry:([1-9][0-9]{0,17})$"), "begin_expiry", "root", lambda m: {"account_id": int(m[1])}),
    (re.compile(r"^v52:grants:([1-9][0-9]{0,17})$"), "show_grants", "owner/seller/root", lambda m: {"account_id": int(m[1])}),
    (re.compile(r"^v52:gset:([1-9][0-9]{0,17}):(viewer|manager)$"), "begin_grant", "owner/seller/root", lambda m: {"account_id": int(m[1]), "level": m[2]}),
    (re.compile(r"^v52:grevoke:([1-9][0-9]{0,17}):([1-9][0-9]{0,17})$"), "preview_revoke", "owner/seller/root", lambda m: {"account_id": int(m[1]), "grantee_tg_id": int(m[2])}),
    (re.compile(r"^v52:bot:([1-9][0-9]{0,17})$"), "begin_register_bot", "root", lambda m: {"rep_id": int(m[1])}),
    (re.compile(r"^v52:domain:([1-9][0-9]{0,17})$"), "begin_register", "same rep/root", lambda m: {"rep_id": int(m[1])}),
    (re.compile(r"^v52:panel:([1-9][0-9]{0,17})$"), "show_panel", "root", lambda m: {"panel_id": int(m[1])}),
    (re.compile(r"^v52:panelconfig:([1-9][0-9]{0,17})$"), "begin_panel_config", "root", lambda m: {"panel_id": int(m[1])}),
    (re.compile(r"^v52:paneltest:([1-9][0-9]{0,17})$"), "test_panel_connection", "root", lambda m: {"panel_id": int(m[1])}),
    (re.compile(r"^v52:panelauto:([1-9][0-9]{0,17})$"), "auto_activate_panel", "root", lambda m: {"panel_id": int(m[1])}),
    # Admin navigation surfaces emitted by the merged menus and the panel/bot
    # wizards. Every one of them must resolve to a bridge handler, otherwise the
    # button dead-ends in the "W18 bridge" error page.
    (re.compile(r"^v52:panels$"), "list_panels", "root", lambda m: {}),
    (re.compile(r"^v52:panel$"), "list_panels", "root", lambda m: {}),
    (re.compile(r"^v52:panelnew$"), "begin_add_panel", "root", lambda m: {}),
    (re.compile(r"^v52:panelnew:(?:type:)?(3x-ui|marzban|hiddify|rebecca|manual)$"), "begin_panel_draft", "root", lambda m: {"panel_type": m[1]}),
    (re.compile(r"^v52:panelsettype:([1-9][0-9]{0,17}):(3x-ui|marzban|hiddify|rebecca|manual)$"), "set_panel_type", "root", lambda m: {"panel_id": int(m[1]), "panel_type": m[2]}),
    (re.compile(r"^v52:panelname:([1-9][0-9]{0,17})$"), "begin_panel_name", "root", lambda m: {"panel_id": int(m[1])}),
    (re.compile(r"^v52:panelurl:([1-9][0-9]{0,17})$"), "begin_panel_url", "root", lambda m: {"panel_id": int(m[1])}),
    (re.compile(r"^v52:panelcred:([1-9][0-9]{0,17})$"), "begin_panel_credential", "root", lambda m: {"panel_id": int(m[1])}),
    (re.compile(r"^v52:panelnew:(test|confirm)$"), "confirm_panel_draft", "root", lambda m: {"step": m[1]}),
    (re.compile(r"^v52:panelpolicy:([1-9][0-9]{0,17})$"), "preview_panel_policy", "root", lambda m: {"panel_id": int(m[1])}),
    # S57 advanced panel settings: capacity, naming, subscription host, test
    # account and add-on prices, all of which already exist as ``panels`` columns.
    (re.compile(r"^v52:paneladv:([1-9][0-9]{0,17})$"), "panel_advanced", "root", lambda m: {"panel_id": int(m[1])}),
    (re.compile(r"^v52:paneladvset:([1-9][0-9]{0,17}):(capacity|naming|suburl|test|addons)$"), "begin_panel_advanced", "root DM", lambda m: {"panel_id": int(m[1]), "field": m[2]}),
    (re.compile(r"^v52:panelinbound:([1-9][0-9]{0,17})$"), "choose_panel_inbound", "root", lambda m: {"panel_id": int(m[1])}),
    (re.compile(r"^v52:panelsetinbound:([1-9][0-9]{0,17}):([1-9][0-9]{0,17})$"), "set_panel_inbound", "root", lambda m: {"panel_id": int(m[1]), "inbound_id": int(m[2])}),
    (re.compile(r"^v52:bots$"), "list_bots", "root", lambda m: {}),
    (re.compile(r"^v52:bot:(primary|rep)$"), "begin_register_bot", "root", lambda m: {"kind": m[1]}),
    # S60: the bot-management surface. ``v52:bots`` lists every registered bot and
    # each row opens a detail card whose buttons land here. The patterns cannot
    # shadow ``^v52:bot:(<id>)$`` above because none of them is all-digits after
    # ``v52:bot:``.
    (re.compile(r"^v52:bot:view:([1-9][0-9]{0,17})$"), "bot_view", "root", lambda m: {"bot_id": int(m[1])}),
    (re.compile(r"^v52:bot:enable:([1-9][0-9]{0,17})$"), "bot_enable", "root DM", lambda m: {"bot_id": int(m[1])}),
    (re.compile(r"^v52:bot:disable:([1-9][0-9]{0,17})$"), "bot_disable", "root DM", lambda m: {"bot_id": int(m[1])}),
    (re.compile(r"^v52:bot:reassign:([1-9][0-9]{0,17})$"), "bot_reassign_pick", "root DM", lambda m: {"bot_id": int(m[1])}),
    (re.compile(r"^v52:bot:reassign:([1-9][0-9]{0,17}):(root|[1-9][0-9]{0,17})$"), "bot_reassign", "root DM", lambda m: {"bot_id": int(m[1]), "new_rep": m[2]}),
    (re.compile(r"^v52:bot:del:([1-9][0-9]{0,17})$"), "bot_delete", "root DM", lambda m: {"bot_id": int(m[1])}),
    (re.compile(r"^v52:domains$"), "list_domains", "root", lambda m: {}),
    # v5.2 domain-management surface: list -> detail -> DNS/SSL, lifecycle, delete,
    # and the four-step register wizard (hostname text, then role/panel/primary).
    (re.compile(r"^v52:domainnew$"), "begin_domain_register", "root/same rep", lambda m: {}),
    (re.compile(r"^v52:domainnew:([1-9][0-9]{0,17})$"), "domain_register_rep", "root", lambda m: {"rep_id": int(m[1])}),
    (re.compile(r"^v52:domaindetail:([1-9][0-9]{0,17})$"), "show_domain", "owner/root", lambda m: {"domain_id": int(m[1])}),
    (re.compile(r"^v52:domainrole:(subscription|node)$"), "domain_register_role", "owner/root", lambda m: {"role": m[1]}),
    (re.compile(r"^v52:domainpanel:(none|[1-9][0-9]{0,17})$"), "domain_register_panel", "owner/root", lambda m: {"panel_id": None if m[1] == "none" else int(m[1])}),
    (re.compile(r"^v52:domainprimary:(0|1)$"), "domain_register_primary", "owner/root", lambda m: {"is_primary": m[1] == "1"}),
    (re.compile(r"^v52:domainverify:([1-9][0-9]{0,17})$"), "domain_verify", "owner/root", lambda m: {"domain_id": int(m[1])}),
    (re.compile(r"^v52:domainactivate:([1-9][0-9]{0,17})$"), "domainactivate", "owner/root", lambda m: {"domain_id": int(m[1])}),
    (re.compile(r"^v52:domainlife:([1-9][0-9]{0,17})$"), "preview_domain_lifecycle", "owner/root", lambda m: {"domain_id": int(m[1])}),
    (re.compile(r"^v52:domainlife:([1-9][0-9]{0,17}):(active|drain|disabled)$"), "preview_domain_lifecycle", "owner/root", lambda m: {"domain_id": int(m[1]), "state": m[2]}),
    (re.compile(r"^v52:domainlifeok:([1-9][0-9]{0,17}):(active|drain|disabled)$"), "confirm_domain_lifecycle", "owner/root", lambda m: {"domain_id": int(m[1]), "state": m[2]}),
    (re.compile(r"^v52:domaindel:([1-9][0-9]{0,17})$"), "preview_domain_delete", "owner/root", lambda m: {"domain_id": int(m[1])}),
    (re.compile(r"^v52:domaindelok:([1-9][0-9]{0,17})$"), "confirm_domain_delete", "owner/root", lambda m: {"domain_id": int(m[1])}),
    (re.compile(r"^v52:cards$"), "list_cards", "root", lambda m: {}),
    (re.compile(r"^v52:card:new$"), "begin_bank_card_new", "root DM", lambda m: {}),
    (re.compile(r"^v52:plans$"), "plan_hub", "root", lambda m: {}),
    (re.compile(r"^v52:finance$"), "show_finance", "root", lambda m: {}),
    (re.compile(r"^v52:orders$"), "list_orders", "actor", lambda m: {}),
    (re.compile(r"^v52:account:([1-9][0-9]{0,17})$"), "open_account_profiles", "view_config", lambda m: {"account_id": int(m[1])}),
    (re.compile(r"^v52:subreps:([1-9][0-9]{0,17})$"), "show_sub_reps", "root", lambda m: {"rep_id": int(m[1])}),
    (re.compile(r"^v52:receipt:([0-9a-fA-F-]{16,40})$"), "open_receipt", "payer", lambda m: {"attempt_id": m[1]}),
    (re.compile(r"^v52:life:([1-9][0-9]{0,17}):(active|sold_out|hidden|drain|maintenance)$"), "preview_lifecycle", "root", lambda m: {"panel_id": int(m[1]), "lifecycle": m[2]}),
    (re.compile(r"^v52:life-confirm:([1-9][0-9]{0,17}):(active|sold_out|hidden|drain|maintenance)$"), "confirm_lifecycle", "root", lambda m: {"panel_id": int(m[1]), "lifecycle": m[2]}),
    (re.compile(r"^v52:buy:([A-Za-z0-9][A-Za-z0-9._-]{0,47})$"), "preview_buy", "seller route", lambda m: {"plan_code": m[1]}),
    (re.compile(r"^v52:plan:([A-Za-z0-9][A-Za-z0-9._-]{0,47}):all_explicit$"), "edit_plan_all_explicit", "root", lambda m: {"plan_code": m[1]}),
    (re.compile(r"^v52:plan:([A-Za-z0-9][A-Za-z0-9._-]{0,47})$"), "plan_detail", "root", lambda m: {"plan_code": m[1]}),
    (re.compile(r"^v52:persona:(admin|rep|user)$"), "switch_persona", "actor", lambda m: {"persona": m[1]}),
    (re.compile(r"^v52:renew:([1-9][0-9]{0,17}):([A-Za-z0-9][A-Za-z0-9._-]{0,47})$"), "preview_renew", "manager/owner/seller/root", lambda m: {"account_id": int(m[1]), "plan_code": m[2]}),
    (re.compile(r"^v52:addon:([1-9][0-9]{0,17}):(gib|day)$"), "begin_addon", "owner/seller/root", lambda m: {"account_id": int(m[1]), "addon_type": m[2]}),
    (re.compile(r"^v52:order:([A-Za-z0-9._:-]{1,36})$"), "begin_order", "payer", lambda m: {"quote_id": m[1]}),
    (re.compile(r"^v52:pay:([A-Za-z0-9._:-]{1,36}):(wallet|card)$"), "choose_rail", "payer", lambda m: {"order_id": m[1], "rail": m[2]}),
    (re.compile(r"^v52:receipt:(approve|reject):([A-Za-z0-9._:-]{1,36})$"), "preview_receipt", "reviewer/root", lambda m: {"decision": m[1], "receipt_id": m[2]}),
    (re.compile(r"^v52:shop$"), "show_catalog", "actor", lambda m: {}),
    (re.compile(r"^v52:goods:([1-9][0-9]{0,17})$"), "preview_product", "actor+seller", lambda m: {"product_id": int(m[1])}),
    (re.compile(r"^v52:fulfill:([A-Za-z0-9._:-]{1,36})$"), "prepare_manual_delivery", "assigned seller/root", lambda m: {"order_id": m[1]}),
    # Admin shop-management surface. Every callback emitted by goods_ui's admin
    # pages must resolve here or the button dead-ends in the "W18 bridge" error.
    (re.compile(r"^v52:shop:catalog$"), "view_catalog", "actor", lambda m: {}),
    (re.compile(r"^v52:shop:new$"), "begin_product_wizard", "root", lambda m: {}),
    (re.compile(r"^v52:shop:wiz:(delivery|audience):([a-z_]+)$"), "product_wizard_choice", "root", lambda m: {"field": m[1], "value": m[2]}),
    # The preview card's own controls and the skippable usage-guide step. They reuse
    # the same bridge handler, which already understands confirm/edit/skip.
    (re.compile(r"^v52:shop:wiz:(confirm|edit)$"), "product_wizard_choice", "root", lambda m: {"field": m[1], "value": ""}),
    (re.compile(r"^v52:shop:wiz:skip:([a-z_]+)$"), "product_wizard_choice", "root", lambda m: {"field": "skip", "value": ""}),
    (re.compile(r"^v52:shop:product:([1-9][0-9]{0,17})$"), "admin_product_detail", "root", lambda m: {"product_id": int(m[1])}),
    (re.compile(r"^v52:shop:stock:([1-9][0-9]{0,17})$"), "begin_product_stock", "root", lambda m: {"product_id": int(m[1])}),
    # S60 — the explicit split modes and the structured-item wizard. The bare route
    # above now renders the chooser, so these three carry the operator's decision.
    (re.compile(r"^v52:shop:stock:(line|block):([1-9][0-9]{0,17})$"), "begin_product_stock_mode", "root", lambda m: {"mode": m[1], "product_id": int(m[2])}),
    (re.compile(r"^v52:shop:stock:wiz:([1-9][0-9]{0,17})$"), "begin_structured_item", "root", lambda m: {"product_id": int(m[1])}),
    (re.compile(r"^v52:shop:edit:([1-9][0-9]{0,17})$"), "begin_product_edit", "root", lambda m: {"product_id": int(m[1])}),
    (re.compile(r"^v52:shop:toggle:([1-9][0-9]{0,17})$"), "toggle_product", "root", lambda m: {"product_id": int(m[1])}),
    (re.compile(r"^v52:shop:price:([1-9][0-9]{0,17})$"), "begin_product_price", "root", lambda m: {"product_id": int(m[1])}),
    (re.compile(r"^v52:shop:guide:([1-9][0-9]{0,17})$"), "begin_product_guide", "root DM", lambda m: {"product_id": int(m[1])}),
    (re.compile(r"^v52:shop:guideclear:([1-9][0-9]{0,17})$"), "clear_product_guide", "root DM", lambda m: {"product_id": int(m[1])}),
    (re.compile(r"^v52:shop:delete:([1-9][0-9]{0,17}):confirm$"), "confirm_product_delete", "root", lambda m: {"product_id": int(m[1])}),
    (re.compile(r"^v52:shop:delete:([1-9][0-9]{0,17})$"), "preview_product_delete", "root", lambda m: {"product_id": int(m[1])}),
    # S61 Part 2 — paginated admin catalog, sales report, categories, wizard category choices
    (re.compile(r"^v52:shop:catalog:([1-9][0-9]{0,17})$"), "admin_catalog_page", "root", lambda m: {"page": int(m[1])}),
    (re.compile(r"^v52:shop:categories$"), "shop_categories", "root", lambda m: {}),
    (re.compile(r"^v52:shop:sales:([1-9][0-9]{0,17}):([1-9][0-9]{0,17})$"), "shop_sales_report", "root", lambda m: {"product_id": int(m[1]), "page": int(m[2])}),
    (re.compile(r"^v52:shop:wiz:category:([a-z_0-9]+)$"), "product_wizard_category", "root", lambda m: {"value": m[1]}),
    (re.compile(r"^v52:tickets$"), "list_tickets", "actor", lambda m: {}),
    (re.compile(r"^v52:ticketsettings$"), "ticket_status_settings", "root", lambda m: {}),
    (re.compile(r"^v52:ticketstatus:new$"), "begin_ticket_status_new", "root DM", lambda m: {}),
    (re.compile(r"^v52:ticketstatus:edit:([a-z][a-z0-9_]{1,29})$"), "begin_ticket_status_edit", "root DM", lambda m: {"code": m[1]}),
    (re.compile(r"^v52:ticketstatus:toggle:([a-z][a-z0-9_]{1,29})$"), "toggle_ticket_status", "root", lambda m: {"code": m[1]}),
    (re.compile(r"^v52:ticketstatus:del:([a-z][a-z0-9_]{1,29})$"), "delete_ticket_status", "root", lambda m: {"code": m[1]}),
    (re.compile(r"^v52:ticketstatus:set:([1-9][0-9]{0,17}):([a-z][a-z0-9_]{1,29})$"), "set_ticket_status", "operator", lambda m: {"ticket_id": m[1], "code": m[2]}),
    (re.compile(r"^v52:tiers$"), "list_tiers", "root", lambda m: {}),
    (re.compile(r"^v52:tier:new$"), "begin_tier_new", "root DM", lambda m: {}),
    (re.compile(r"^v52:tier:edit:(rep|end_user):([a-z][a-z0-9_]{1,29})$"), "begin_tier_edit", "root DM", lambda m: {"actor_type": m[1], "code": m[2]}),
    (re.compile(r"^v52:tier:del:(rep|end_user):([a-z][a-z0-9_]{1,29})$"), "delete_tier", "root", lambda m: {"actor_type": m[1], "code": m[2]}),
    # --- S57: unified tariff & category centre ------------------------------ #
    # ``v52:plans`` and ``admin:plans`` both render the same hub; the plan card
    # carries the multi-select checkboxes for categories, panels, audience and
    # tariff kind. ``v52:plan:<code>`` is repointed from the old preview to that
    # card, and the explicit all-panels confirmation keeps its own route.
    (re.compile(r"^v52:plancats$"), "plan_categories", "root", lambda m: {}),
    (re.compile(r"^v52:plancatnew$"), "begin_plan_category_new", "root DM", lambda m: {}),
    (re.compile(r"^v52:plancatedit:([a-z][a-z0-9_]{1,29})$"), "begin_plan_category_edit", "root DM", lambda m: {"category_key": m[1]}),
    (re.compile(r"^v52:plancatdel:([a-z][a-z0-9_]{1,29})$"), "delete_plan_category", "root", lambda m: {"category_key": m[1]}),
    (re.compile(r"^v52:pc:([A-Za-z0-9][A-Za-z0-9._-]{0,47}):([a-z][a-z0-9_]{1,29})$"), "toggle_plan_category", "root", lambda m: {"plan_code": m[1], "category_key": m[2]}),
    (re.compile(r"^v52:pp:([A-Za-z0-9][A-Za-z0-9._-]{0,47}):([1-9][0-9]{0,17})$"), "toggle_plan_panel", "root", lambda m: {"plan_code": m[1], "panel_id": int(m[2])}),
    (re.compile(r"^v52:pa:([A-Za-z0-9][A-Za-z0-9._-]{0,47}):(end_user|rep)$"), "toggle_plan_audience", "root", lambda m: {"plan_code": m[1], "audience": m[2]}),
    (re.compile(r"^v52:pk:([A-Za-z0-9][A-Za-z0-9._-]{0,47}):(fixed|unlimited|custom)$"), "set_plan_kind", "root", lambda m: {"plan_code": m[1], "kind": m[2]}),
    # --- S57: tier-specific plan price matrix ------------------------------- #
    (re.compile(r"^v52:tierprices:(rep|end_user):([a-z][a-z0-9_]{1,29})$"), "tier_price_matrix", "root", lambda m: {"actor_type": m[1], "code": m[2]}),
    (re.compile(r"^v52:tierprice:(rep|end_user):([a-z][a-z0-9_]{1,29}):([A-Za-z0-9][A-Za-z0-9._-]{0,47})$"), "begin_tier_plan_price", "root DM", lambda m: {"actor_type": m[1], "code": m[2], "plan_code": m[3]}),
    (re.compile(r"^v52:tierpriceclear:(rep|end_user):([a-z][a-z0-9_]{1,29}):([A-Za-z0-9][A-Za-z0-9._-]{0,47})$"), "clear_tier_plan_price", "root", lambda m: {"actor_type": m[1], "code": m[2], "plan_code": m[3]}),
    # --- S57: coupons and admin refunds ------------------------------------- #
    (re.compile(r"^v52:coupons$"), "list_coupons", "root", lambda m: {}),
    (re.compile(r"^v52:couponnew$"), "begin_coupon_new", "root DM", lambda m: {}),
    (re.compile(r"^v52:couponedit:([A-Za-z0-9][A-Za-z0-9._-]{0,47})$"), "begin_coupon_edit", "root DM", lambda m: {"code": m[1]}),
    (re.compile(r"^v52:coupontoggle:([A-Za-z0-9][A-Za-z0-9._-]{0,47})$"), "toggle_coupon", "root", lambda m: {"code": m[1]}),
    (re.compile(r"^v52:coupondel:([A-Za-z0-9][A-Za-z0-9._-]{0,47})$"), "delete_coupon", "root", lambda m: {"code": m[1]}),
    (re.compile(r"^v52:refunds$"), "list_refunds", "root", lambda m: {}),
    (re.compile(r"^v52:refundorder:([A-Za-z0-9._:-]{1,36})$"), "preview_order_refund", "root", lambda m: {"order_id": m[1]}),
    (re.compile(r"^v52:refundstart:([A-Za-z0-9._:-]{1,36})$"), "begin_refund_destination", "root DM", lambda m: {"order_id": m[1]}),
    (re.compile(r"^v52:tickets:(open|answered|closed|all)$"), "list_tickets", "actor", lambda m: {"group": m[1]}),
    (re.compile(r"^v52:newticket$"), "begin_new_ticket", "actor", lambda m: {}),
    (re.compile(r"^v52:support:([0-9]{1,18})$"), "begin_ticket", "view authorized", lambda m: {"account_id": int(m[1])}),
    (re.compile(r"^v52:abuse$"), "begin_abuse", "all actors; private root destination", lambda m: {}),
    (re.compile(r"^v52:abuse:list$"), "list_abuse", "root", lambda m: {}),
    (re.compile(r"^v52:close:([A-Za-z0-9_-]{1,32})$"), "close_ticket", "ticket scope", lambda m: {"ticket_id": m[1]}),
    (re.compile(r"^v52:reopen:([A-Za-z0-9_-]{1,32})$"), "reopen_ticket", "ticket scope", lambda m: {"ticket_id": m[1]}),
    (re.compile(r"^v52:ticket:([A-Za-z0-9_-]{1,32})$"), "reply_or_escalate", "ticket scope", lambda m: {"ticket_id": m[1]}),
    (re.compile(r"^v52:escalate:([A-Za-z0-9_-]{1,32})$"), "reply_or_escalate", "assigned seller/root", lambda m: {"ticket_id": m[1], "action": "escalate"}),
    (re.compile(r"^v52:transfer:([1-9][0-9]{0,17})$"), "reply_or_escalate", "root preview", lambda m: {"account_id": int(m[1]), "action": "transfer"}),
    (re.compile(r"^v52:ops$"), "show_operations", "root", lambda m: {}),
    (re.compile(r"^v52:reconcile:([A-Za-z0-9._:-]{1,36})$"), "show_operations", "root", lambda m: {"operation_id": m[1]}),
    (re.compile(r"^v52:refund:([A-Za-z0-9._:-]{1,36})$"), "preview_refund", "payer/root", lambda m: {"refund_id": m[1]}),
    (re.compile(r"^v52:forums:([A-Za-z0-9._:-]{1,36})$"), "sync_topics", "root", lambda m: {"owner_key": m[1]}),
    (re.compile(r"^v52:poolimport:([1-9][0-9]{0,17})$"), "begin_manual_import", "root DM", lambda m: {"panel_id": int(m[1])}),
    (re.compile(r"^v52:inventory:([1-9][0-9]{0,17})$"), "begin_digital_import", "root DM", lambda m: {"product_id": int(m[1])}),
    # Two-step import confirmations. The staged batch is bound to actor+bot+kind
    # and single-use, so the token alone is enough to authorise the insert; the
    # target id is read back from the staged row because it does not fit inside
    # Telegram's 64-byte callback data alongside the token.
    (re.compile(r"^v52:invok:([A-Za-z0-9_-]{24})$"), "confirm_digital_import", "root DM", lambda m: {"token": m[1]}),
    (re.compile(r"^v52:invno:([A-Za-z0-9_-]{24})$"), "cancel_digital_import", "root DM", lambda m: {"token": m[1]}),
    (re.compile(r"^v52:poolok:([A-Za-z0-9_-]{24})$"), "confirm_manual_import", "root DM", lambda m: {"token": m[1]}),
    (re.compile(r"^v52:poolno:([A-Za-z0-9_-]{24})$"), "cancel_manual_import", "root DM", lambda m: {"token": m[1]}),
    (re.compile(r"^v52:q:([A-Za-z0-9_-]{16,40})$"), "resolve_quote_preview", "actor+seller", lambda m: {"nonce": m[1]}),
    (re.compile(r"^v52:ok:([A-Za-z0-9_-]{16,40})$"), "consume_and_dispatch", "same actor/bot/action/revision", lambda m: {"nonce": m[1]}),
    (re.compile(r"^v52:no:([A-Za-z0-9_-]{16,40})$"), "cancel_confirmation", "same actor/bot", lambda m: {"nonce": m[1]}),
    # --- S58: the shared step-by-step wizard controls ------------------------ #
    # One registered namespace for every wizard in the product. The step and choice
    # are positional indexes rather than values, so a callback stays short, ASCII
    # and free of the operator's own text.
    (re.compile(r"^v52:wz:cancel$"), "wizard_cancel", "actor DM", lambda m: {}),
    (re.compile(r"^v52:wz:prev$"), "wizard_prev", "actor DM", lambda m: {}),
    (re.compile(r"^v52:wz:edit$"), "wizard_edit", "actor DM", lambda m: {}),
    (re.compile(r"^v52:wz:confirm$"), "wizard_confirm", "actor DM", lambda m: {}),
    (re.compile(r"^v52:wz:pick:([0-9]{1,2}):([0-9]{1,2})$"), "wizard_pick", "actor DM", lambda m: {"step": int(m[1]), "choice": int(m[2])}),
    (re.compile(r"^v52:wz:done:([0-9]{1,2})$"), "wizard_done", "actor DM", lambda m: {"step": int(m[1])}),
    (re.compile(r"^v52:wz:skip:([0-9]{1,2})$"), "wizard_skip", "actor DM", lambda m: {"step": int(m[1])}),
    (re.compile(r"^v52:wz:gen:([0-9]{1,2})$"), "wizard_generate", "actor DM", lambda m: {"step": int(m[1])}),
    # --- S58: the 360-degree profile hub ------------------------------------- #
    # The profile card's two cross-persona management actions, plus the two
    # contextual drill-downs that carry the card they were opened from. Every one
    # of them is implemented in ``rep_bot._dispatch_v52_callback``: a registry
    # entry without a bridge renders the "not connected to the Bridge" error page
    # instead of the page the operator asked for. The originating surface is an
    # optional group on the two management routes, because a reference long enough
    # to overflow Telegram's 64-byte callback budget is dropped by the emitter
    # rather than sent.
    (re.compile(r"^v52:p360new:([1-9][0-9]{0,17})(?::(tx|rcp|ticket|reg|users|reps|abuse|repnet):([A-Za-z0-9_-]{1,40}))?$"), "p360_issue_service", "root/downstream rep", lambda m: {"user_id": int(m[1]), "origin": m[2] or "", "ref": m[3] or ""}),
    (re.compile(r"^v52:p360own:([1-9][0-9]{0,17})(?::(tx|rcp|ticket|reg|users|reps|abuse|repnet):([A-Za-z0-9_-]{1,40}))?$"), "p360_transfer_ownership", "root/downstream rep", lambda m: {"user_id": int(m[1]), "origin": m[2] or "", "ref": m[3] or ""}),
    (re.compile(r"^v52:p360acc:([1-9][0-9]{0,17}):([1-9][0-9]{0,17}):(tx|rcp|ticket|reg|users|reps|abuse|repnet):([A-Za-z0-9_-]{1,40})$"), "p360_account_card", "root/downstream rep", lambda m: {"account_id": int(m[1]), "user_id": int(m[2]), "origin": m[3], "ref": m[4]}),
    (re.compile(r"^v52:p360tkt:([A-Za-z0-9_-]{1,32}):([1-9][0-9]{0,17}):(tx|rcp|ticket|reg|users|reps|abuse|repnet):([A-Za-z0-9_-]{1,40})$"), "p360_ticket", "root/downstream rep", lambda m: {"ticket_id": m[1], "user_id": int(m[2]), "origin": m[3], "ref": m[4]}),
)


def _positive(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


def _identity(context: Any) -> tuple[int, int, int]:
    bot = _value(context, "bot")
    bot_id = _value(context, "bot_id", _value(bot, "id"))
    actor = _value(context, "actor_tg_id", _value(context, "telegram_id"))
    chat_id = _value(context, "chat_id", actor)
    return (_positive(bot_id, "bot_id"), _positive(actor, "telegram_id"), _positive(chat_id, "chat_id"))


def _value(obj: Any, key: str, default=None):
    return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)


def route_callback(callback_data: str, *, context: Any = None):
    """Strictly resolve a registered callback. Unknown/forged input returns a page, not a crash."""
    if not isinstance(callback_data, str) or not callback_data:
        return render_error("callback خالی یا نامعتبر است.")
    if len(callback_data.encode("utf-8")) > MAX_CALLBACK_BYTES:
        return render_error("callback از سقف ۶۴ بایت بیشتر است.")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in callback_data):
        return render_error("callback دارای نویسه کنترلی است.")
    for pattern, handler, scope, decode in _CALLBACK_PATTERNS:
        match = pattern.fullmatch(callback_data)
        if match:
            return Route(callback_data.split(":", 2)[1], handler, decode(match), scope)
    return render_error("این callback در registry ثبت نشده است.")


class DraftFSM:
    """Small TTL-bound in-memory FSM; callers may persist it in bot/chat-scoped storage."""
    def __init__(self, *, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 1 <= ttl_seconds <= 86_400:
            raise ValueError("ttl_seconds must be 1..86400")
        self.ttl_seconds = ttl_seconds
        self._entries: dict[tuple[int, int, int], FSMEntry] = {}

    def set(self, context: Any, state: str, *, payload: dict[str, Any] | None = None,
            parent_callback: str | None = None, now: int) -> FSMEntry:
        bot_id, tg_id, chat_id = _identity(context)
        selected = str(state or "")
        if selected not in TEXT_STATES:
            raise ValueError("unknown v5.2 FSM state")
        if isinstance(now, bool) or not isinstance(now, int) or now < 0:
            raise ValueError("now must be non-negative epoch seconds")
        entry = FSMEntry(bot_id, tg_id, chat_id, selected, parent_callback,
                         now + self.ttl_seconds, dict(payload or {}))
        self._entries[(bot_id, tg_id, chat_id)] = entry
        return entry

    def peek(self, context: Any, *, now: int) -> FSMEntry | None:
        key = _identity(context)
        entry = self._entries.get(key)
        if entry is None:
            return None
        if isinstance(now, bool) or not isinstance(now, int) or now >= entry.expires_at:
            self._entries.pop(key, None)
            return None
        return entry

    def consume(self, context: Any, *, event: str, now: int):
        entry = self.peek(context, now=now)
        if entry is None:
            return None
        accepted = {"text": TEXT_STATES, "photo": PHOTO_STATES}
        if event not in accepted or entry.state not in accepted[event]:
            return None
        self._entries.pop((entry.bot_id, entry.telegram_id, entry.chat_id), None)
        return entry

    def clear(self, context: Any) -> bool:
        return self._entries.pop(_identity(context), None) is not None


DEFAULT_FSM = DraftFSM()


def route_text(context: Any, text: str, *, now: int, fsm: DraftFSM = DEFAULT_FSM):
    """Consume text only when an unexpired registered draft state owns this identity."""
    if not isinstance(text, str):
        return False
    entry = fsm.consume(context, event="text", now=now)
    if entry is None:
        return False
    return Route(entry.state, entry.state, {"text": text, "payload": dict(entry.payload),
                                           "parent_callback": entry.parent_callback}, "fsm")


def route_photo(context: Any, file_id: str, *, now: int, fsm: DraftFSM = DEFAULT_FSM):
    """Consume photo only for support states that explicitly accept attachments."""
    if not isinstance(file_id, str) or not file_id.strip():
        return False
    entry = fsm.consume(context, event="photo", now=now)
    if entry is None:
        return False
    return Route(entry.state, entry.state, {"file_id": file_id, "payload": dict(entry.payload),
                                           "parent_callback": entry.parent_callback}, "fsm")


__all__ = ["Route", "FSMEntry", "DraftFSM", "DEFAULT_FSM", "PHOTO_STATES",
           "TEXT_STATES", "route_callback", "route_text", "route_photo"]