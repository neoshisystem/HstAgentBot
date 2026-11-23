"""Offline acceptance for panel lifecycle and sale eligibility."""

from __future__ import annotations

import sqlite3
import socket
import urllib.request

from eligibility_service import can_renew_existing, can_sell_new


def _disable_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network access is forbidden in this test")
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(urllib.request, "urlopen", blocked)


def test_lifecycle_access_matrix(tmp_path, monkeypatch):
    _disable_network(monkeypatch)
    path = tmp_path / "w06-eligibility.sqlite3"
    conn = sqlite3.connect(path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE panels(
            id INTEGER PRIMARY KEY, lifecycle TEXT NOT NULL, is_hidden INTEGER NOT NULL DEFAULT 0,
            max_clients INTEGER NOT NULL DEFAULT 0, for_end_users INTEGER NOT NULL DEFAULT 1,
            for_reps INTEGER NOT NULL DEFAULT 1,
            access_type TEXT NOT NULL DEFAULT 'public', min_rep_rank INTEGER NOT NULL DEFAULT 0,
            min_user_rank INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE representatives(
            id INTEGER PRIMARY KEY, parent_rep_id INTEGER, tier TEXT NOT NULL DEFAULT 'normal',
            custom_prefix TEXT NOT NULL DEFAULT '', rep_code TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE panel_rep_assignments(
            panel_id INTEGER NOT NULL, rep_id INTEGER NOT NULL, effect TEXT NOT NULL,
            include_sub_reps INTEGER NOT NULL DEFAULT 1, PRIMARY KEY(panel_id,rep_id)
        );
        CREATE TABLE pricing_plans(
            plan_code TEXT PRIMARY KEY, sale_status TEXT NOT NULL, panel_scope TEXT NOT NULL DEFAULT 'selected'
        );
        CREATE TABLE plan_panels(
            plan_code TEXT NOT NULL, panel_id INTEGER NOT NULL, allowed INTEGER NOT NULL,
            PRIMARY KEY(plan_code,panel_id)
        );
        CREATE TABLE tier_policies(
            actor_type TEXT NOT NULL, tier_code TEXT NOT NULL, rank INTEGER NOT NULL,
            PRIMARY KEY(actor_type,tier_code)
        );
        INSERT INTO panels(id,lifecycle,access_type) VALUES(1,'active','public');
        INSERT INTO representatives(id,parent_rep_id,tier,rep_code) VALUES(10,NULL,'gold','PARENT');
        INSERT INTO representatives(id,parent_rep_id,tier,rep_code) VALUES(20,10,'gold','CHILD');
        INSERT INTO tier_policies(actor_type,tier_code,rank) VALUES('rep','gold',5);
        INSERT INTO pricing_plans(plan_code,sale_status,panel_scope) VALUES('plan-a','active','selected');
        INSERT INTO plan_panels(plan_code,panel_id,allowed) VALUES('plan-a',1,1);
    """)

    # An ancestor deny always overrides a child's explicit allow.
    conn.execute("INSERT INTO panel_rep_assignments VALUES(1,10,'deny',1)")
    conn.execute("INSERT INTO panel_rep_assignments VALUES(1,20,'allow',1)")
    assert can_sell_new(
        conn, panel_id=1, plan_code="plan-a", actor_type="rep", rep_id=20
    ) is False
    assert can_renew_existing(
        conn, panel_id=1, actor_type="rep", rep_id=20
    ) is True

    # Four lifecycle values crossed with all three sale visibility states.
    for lifecycle in ("active", "capacity", "drain", "maintenance"):
        conn.execute("UPDATE panels SET lifecycle=? WHERE id=1", (lifecycle,))
        for sale_status in ("active", "sold_out", "hidden"):
            conn.execute(
                "UPDATE pricing_plans SET sale_status=? WHERE plan_code='plan-a'",
                (sale_status,),
            )
            assert can_sell_new(
                conn, panel_id=1, plan_code="plan-a", actor_type="rep", rep_id=20
            ) is False
            can_renew = can_renew_existing(
                conn, panel_id=1, actor_type="rep", rep_id=20
            )
            assert can_renew is (lifecycle in {"active", "capacity"})

    # Existing service renewal ignores deny and hidden/sold-out sale visibility,
    # but an on-hold guard and panel lifecycle guard remain authoritative.
    conn.execute("UPDATE panels SET lifecycle='active' WHERE id=1")
    conn.execute("UPDATE pricing_plans SET sale_status='hidden' WHERE plan_code='plan-a'")
    assert can_renew_existing(conn, panel_id=1, actor_type="rep", rep_id=20)
    assert not can_renew_existing(
        conn, panel_id=1, actor_type="rep", rep_id=20, on_hold=True
    )

    # Private requires an applicable allow for sales; root may bypass privacy only.
    conn.execute("DELETE FROM panel_rep_assignments")
    conn.execute("UPDATE panels SET access_type='private' WHERE id=1")
    conn.execute("UPDATE pricing_plans SET sale_status='active' WHERE plan_code='plan-a'")
    assert not can_sell_new(
        conn, panel_id=1, plan_code="plan-a", actor_type="rep", rep_id=20
    )
    conn.execute("INSERT INTO panel_rep_assignments VALUES(1,20,'allow',0)")
    assert can_sell_new(
        conn, panel_id=1, plan_code="plan-a", actor_type="rep", rep_id=20
    )
    assert can_sell_new(
        conn, panel_id=1, plan_code="plan-a", actor_type="rep", rep_id=20,
        is_root=True, tier_rank=10
    )

    # Tier, audience, and maintenance restrictions still apply to root.
    conn.execute("UPDATE panels SET access_type='tier_restricted',min_rep_rank=6 WHERE id=1")
    assert not can_sell_new(
        conn, panel_id=1, plan_code="plan-a", actor_type="rep", rep_id=20,
        is_root=True, tier_rank=5
    )
    conn.execute("UPDATE panels SET access_type='public',for_reps=0 WHERE id=1")
    assert not can_sell_new(
        conn, panel_id=1, plan_code="plan-a", actor_type="rep", rep_id=20,
        is_root=True, tier_rank=10
    )
    conn.execute("UPDATE panels SET for_reps=1,lifecycle='maintenance' WHERE id=1")
    assert not can_renew_existing(
        conn, panel_id=1, actor_type="rep", rep_id=20,
        is_root=True, tier_rank=10
    )
    conn.close()
