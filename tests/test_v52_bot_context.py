"""Offline primary/dedicated seller routing matrix for W10.2."""
from __future__ import annotations
import sqlite3
from types import SimpleNamespace
import pytest
import config
from bot_context_service import (
    BotContextError, CheckoutRouteDenied, authorize_checkout_route, resolve_bot_context,
)


def _db(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 777, raising=False)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE representatives(
            id INTEGER PRIMARY KEY,telegram_id INTEGER,is_active INTEGER,deleted_at INTEGER
        );
        CREATE TABLE bot_users(
            telegram_id INTEGER PRIMARY KEY,role TEXT,rep_id INTEGER,is_blocked INTEGER
        );
        CREATE TABLE user_settings(user_id INTEGER PRIMARY KEY,active_persona TEXT);
        CREATE TABLE bot_registry(
            bot_id INTEGER PRIMARY KEY,rep_id INTEGER,is_primary INTEGER,status TEXT
        );
        CREATE TABLE accounts(
            id INTEGER PRIMARY KEY,seller_key TEXT,deleted_at INTEGER,enable INTEGER
        );
    """)
    conn.executemany("INSERT INTO representatives VALUES(?,?,1,NULL)", [(202,303),(203,404)])
    conn.executemany("INSERT INTO bot_users VALUES(?,?,?,0)", [
        (101,"end_user",None),(303,"rep",202),(404,"rep",203),(777,"admin",None)])
    conn.executemany("INSERT INTO user_settings VALUES(?,?)", [
        (101,"end_user"),(303,"rep"),(404,"rep"),(777,"end_user")])
    conn.executemany("INSERT INTO bot_registry VALUES(?,?,?,'active')", [
        (9001,None,1),(9002,202,0)])
    conn.executemany("INSERT INTO accounts VALUES(?,?,NULL,1)", [
        (1,"root"),(2,"rep:202"),(3,"root")])
    conn.commit()
    return conn


def _context(bot_id):
    return SimpleNamespace(bot=SimpleNamespace(id=bot_id))


def test_primary_rep_dedicated_sales_matrix(monkeypatch):
    conn = _db(monkeypatch)
    primary, dedicated = _context(9001), _context(9002)

    # Consumer purchase in primary is root-owned even with forged deep links/callbacks.
    result = authorize_checkout_route(
        conn, primary, actor_tg_id=101, action="buy",
        start_parameter="rep_202", callback_data="seller:rep:202")
    assert result["seller_key"] == "root" and result["merchant_key"] == "root"

    # Main-bot renewals must match the account's immutable seller binding.
    assert authorize_checkout_route(
        conn, primary, actor_tg_id=101, action="renew", account_id=1
    )["seller_key"] == "root"
    with pytest.raises(CheckoutRouteDenied):
        authorize_checkout_route(conn, primary, actor_tg_id=101, action="renew", account_id=2)

    # A representative using the primary desk issues at chain price under own identity.
    issued = authorize_checkout_route(
        conn, primary, actor_tg_id=303, action="rep_issue",
        start_parameter="root", callback_data="seller:root")
    assert issued["seller_key"] == "rep:202" and issued["route_mode"] == "chain_price"
    renewed = authorize_checkout_route(
        conn, primary, actor_tg_id=303, action="rep_renew", account_id=2)
    assert renewed["seller_key"] == "rep:202"
    with pytest.raises(CheckoutRouteDenied):
        authorize_checkout_route(conn, primary, actor_tg_id=303, action="rep_renew", account_id=1)

    # Dedicated bot customer sales and renewal always belong to its representative.
    dedicated_buy = authorize_checkout_route(
        conn, dedicated, actor_tg_id=101, action="buy",
        start_parameter="root", callback_data="seller:root")
    assert dedicated_buy["seller_key"] == "rep:202"
    assert dedicated_buy["merchant_rep_id"] == 202
    assert authorize_checkout_route(
        conn, dedicated, actor_tg_id=101, action="renew", account_id=2
    )["seller_key"] == "rep:202"
    with pytest.raises(CheckoutRouteDenied):
        authorize_checkout_route(conn, dedicated, actor_tg_id=101, action="renew", account_id=1)
    with pytest.raises(CheckoutRouteDenied):
        authorize_checkout_route(conn, dedicated, actor_tg_id=404, action="rep_issue")

    with pytest.raises(BotContextError):
        resolve_bot_context(conn, _context(9999))
    with pytest.raises(BotContextError):
        resolve_bot_context(conn, SimpleNamespace(bot=None))
    conn.close()
