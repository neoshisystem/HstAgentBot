"""Offline acceptance for all six username strategies."""

from __future__ import annotations

import sqlite3
import socket
import urllib.request

import pytest

from naming_service import (
    STRATEGIES,
    UsernameCollision,
    UsernameUnknown,
    allocate_username,
    validate_custom_username,
)


def _disable_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network access is forbidden in this test")
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(urllib.request, "urlopen", blocked)


def test_six_strategies_and_collision(tmp_path, monkeypatch):
    _disable_network(monkeypatch)
    path = tmp_path / "w06-naming.sqlite3"
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE panels(
            id INTEGER PRIMARY KEY,username_strategy TEXT NOT NULL,custom_prefix TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE panel_counters(panel_id INTEGER PRIMARY KEY,next_value INTEGER NOT NULL);
        CREATE TABLE representatives(
            id INTEGER PRIMARY KEY,rep_code TEXT NOT NULL,custom_prefix TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE accounts(id INTEGER PRIMARY KEY,panel_id INTEGER NOT NULL,email TEXT NOT NULL);
        CREATE TABLE manual_pool_clients(
            id INTEGER PRIMARY KEY,panel_id INTEGER NOT NULL,external_identifier TEXT NOT NULL,
            account_id INTEGER,status TEXT NOT NULL,allocation_key TEXT UNIQUE,revision INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO panels VALUES(1,'pool_preallocated','');
        INSERT INTO panels VALUES(2,'rep_prefix_sequential','');
        INSERT INTO panels VALUES(3,'rep_prefix_random','');
        INSERT INTO panels VALUES(4,'custom_text_sequential','Team_');
        INSERT INTO panels VALUES(5,'numeric_id_random','');
        INSERT INTO panels VALUES(6,'custom_username','');
        INSERT INTO panels VALUES(7,'rep_prefix_random','');
        INSERT INTO representatives VALUES(20,'REP-CODE','RepX');
        INSERT INTO panel_counters VALUES(2,1);
        INSERT INTO manual_pool_clients(id,panel_id,external_identifier,status)
            VALUES(1,1,'pool-user-001','available');
        INSERT INTO accounts VALUES(900,7,'RepX777777');
        INSERT INTO accounts VALUES(901,6,'foreign.user');
    """)

    pool_name = allocate_username(
        conn, panel_id=1, account_id=101, operation_id="pool-op-1"
    )
    assert pool_name["strategy"] == "pool_preallocated"
    assert pool_name["username"] == "pool-user-001"
    assert conn.execute(
        "SELECT status,account_id,allocation_key FROM manual_pool_clients WHERE id=1"
    ).fetchone()[:] == ("allocated", 101, "pool-op-1")
    replay = allocate_username(
        conn, panel_id=1, account_id=101, operation_id="pool-op-1"
    )
    assert replay["username"] == pool_name["username"] and replay["idempotent_replay"]

    sequential_a = allocate_username(conn, panel_id=2, rep_id=20)
    sequential_b = allocate_username(conn, panel_id=2, rep_id=20)
    assert sequential_a["username"] == "RepX1"
    assert sequential_b["username"] == "RepX2"
    assert sequential_a["username"] != sequential_b["username"]

    random_name = allocate_username(
        conn, panel_id=3, rep_id=20, random_factory=lambda width: "8" * width
    )
    assert random_name["username"] == "RepX888888"

    custom_sequence = allocate_username(conn, panel_id=4)
    assert custom_sequence["username"] == "Team_1"

    numeric = allocate_username(
        conn, panel_id=5, random_factory=lambda width: "7" * width
    )
    assert numeric["username"] == "7" * 10

    custom = allocate_username(
        conn, panel_id=6, custom_username="Good.Name_9", adapter_max_length=20
    )
    assert custom["username"] == "Good.Name_9"
    assert STRATEGIES == {
        "pool_preallocated", "rep_prefix_sequential", "rep_prefix_random",
        "custom_text_sequential", "numeric_id_random", "custom_username",
    }

    with pytest.raises(UsernameCollision):
        allocate_username(conn, panel_id=6, custom_username="foreign.user")
    assert conn.execute(
        "SELECT id FROM accounts WHERE panel_id=6 AND email='foreign.user'"
    ).fetchone()["id"] == 901

    # A fixed colliding random candidate is retried exactly five times.
    attempts = []
    def colliding_digits(width):
        attempts.append(width)
        return "7" * width
    with pytest.raises(UsernameCollision):
        allocate_username(conn, panel_id=7, rep_id=20, random_factory=colliding_digits)
    assert attempts == [6, 6, 6, 6, 6]

    before = conn.execute(
        "SELECT next_value FROM panel_counters WHERE panel_id=2"
    ).fetchone()["next_value"]
    with pytest.raises(UsernameUnknown):
        allocate_username(conn, panel_id=2, rep_id=20, panel_state="unknown")
    after = conn.execute(
        "SELECT next_value FROM panel_counters WHERE panel_id=2"
    ).fetchone()["next_value"]
    assert after == before

    assert validate_custom_username("abc") == "abc"
    with pytest.raises(ValueError):
        validate_custom_username("user name")
    with pytest.raises(ValueError):
        validate_custom_username("éclair")
    with pytest.raises(ValueError):
        validate_custom_username("LongUsername", max_length=8)
    conn.close()
