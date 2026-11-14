"""Offline acceptance for atomic capacity reservations."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
import sqlite3
import socket
import urllib.request

import pytest

from capacity_service import (
    CapacityFull,
    CapacityUnavailable,
    release_capacity,
    reserve_capacity,
)


def _disable_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network access is forbidden in this test")
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(urllib.request, "urlopen", blocked)


def test_last_slot_two_connections(tmp_path, monkeypatch):
    _disable_network(monkeypatch)
    path = tmp_path / "w06-capacity.sqlite3"
    setup = sqlite3.connect(path, timeout=10)
    setup.row_factory = sqlite3.Row
    setup.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE panels(id INTEGER PRIMARY KEY,max_clients INTEGER NOT NULL,lifecycle TEXT NOT NULL);
        CREATE TABLE payment_orders(order_id TEXT PRIMARY KEY);
        CREATE TABLE capacity_reservations(
            reservation_id TEXT PRIMARY KEY,order_id TEXT NOT NULL UNIQUE,
            panel_id INTEGER NOT NULL,account_id INTEGER,
            state TEXT NOT NULL CHECK(state IN('active','allocated','released')),
            expires_at INTEGER
        );
        CREATE TABLE panel_operations(
            operation_id TEXT PRIMARY KEY,order_id TEXT,state TEXT NOT NULL
        );
        CREATE TABLE manual_pool_clients(
            id INTEGER PRIMARY KEY,panel_id INTEGER NOT NULL,external_identifier TEXT NOT NULL,
            account_id INTEGER,status TEXT NOT NULL
        );
        INSERT INTO panels VALUES(1,1,'active');
        INSERT INTO panels VALUES(2,0,'active');
        INSERT INTO payment_orders VALUES('order-a');
        INSERT INTO payment_orders VALUES('order-b');
        INSERT INTO payment_orders VALUES('order-pool');
        INSERT INTO payment_orders VALUES('order-unlimited');
        INSERT INTO manual_pool_clients VALUES(1,1,'pool-client-a',101,'allocated');
    """)
    setup.commit()
    setup.close()

    gate = Barrier(2)

    def reserve(reservation_id, order_id):
        conn = sqlite3.connect(path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            gate.wait(timeout=5)
            return reserve_capacity(
                conn, reservation_id=reservation_id, order_id=order_id, panel_id=1,
                snapshot_count=0, snapshot_at=100, now=105,
            )
        except CapacityFull:
            return None
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda pair: reserve(*pair),
            [("res-a", "order-a"), ("res-b", "order-b")],
        ))
    assert sum(result is not None for result in results) == 1

    check = sqlite3.connect(path, timeout=10)
    check.row_factory = sqlite3.Row
    active = check.execute(
        "SELECT reservation_id,order_id,expires_at FROM capacity_reservations WHERE state='active'"
    ).fetchall()
    assert len(active) == 1
    winner = active[0]
    assert winner["expires_at"] is None
    check.execute(
        "INSERT INTO panel_operations(operation_id,order_id,state) "
        "VALUES('unknown-lock',?,'needs_reconciliation')",
        (winner["order_id"],),
    )
    check.commit()
    assert release_capacity(
        check, winner["reservation_id"], outcome="definite_failure"
    ) is False
    assert check.execute(
        "SELECT state FROM capacity_reservations WHERE reservation_id=?",
        (winner["reservation_id"],),
    ).fetchone()["state"] == "active"

    # The snapshot already includes the pool client; it is not added again.
    with pytest.raises(CapacityFull):
        reserve_capacity(
            check, reservation_id="res-pool", order_id="order-pool",
            panel_id=1, snapshot_count=1, snapshot_at=100, now=105,
        )

    check.execute("UPDATE panel_operations SET state='succeeded' WHERE operation_id='unknown-lock'")
    check.commit()
    assert release_capacity(
        check, winner["reservation_id"], outcome="unknown"
    ) is False
    assert release_capacity(
        check, winner["reservation_id"], outcome="definite_failure"
    ) is True
    assert check.execute(
        "SELECT state FROM capacity_reservations WHERE reservation_id=?",
        (winner["reservation_id"],),
    ).fetchone()["state"] == "released"

    # A zero cap means unlimited even when the external snapshot is non-zero.
    result = reserve_capacity(
        check, reservation_id="res-unlimited", order_id="order-unlimited",
        panel_id=2, snapshot_count=500, snapshot_at=100, now=105,
    )
    assert result["state"] == "active" and result["capacity_limit"] == 0

    with pytest.raises(CapacityUnavailable):
        reserve_capacity(
            check, reservation_id="res-stale", order_id="order-unused",
            panel_id=2, snapshot_count=0, snapshot_at=1, now=1000,
        )
    check.close()
