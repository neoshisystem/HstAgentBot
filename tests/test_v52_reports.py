"""W15.4 acceptance: one gross order, separate edge cost and read-only reconcile."""
from __future__ import annotations

import sqlite3

from report_service import nightly_metrics, reconcile_balances


def test_no_double_count_cascade():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE payment_orders(
            order_id TEXT PRIMARY KEY,quote_id TEXT NOT NULL,merchant_key TEXT NOT NULL,
            purpose TEXT NOT NULL,status TEXT NOT NULL,amount_toman INTEGER NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE quotes(quote_id TEXT PRIMARY KEY,panel_id INTEGER,account_id INTEGER);
        CREATE TABLE accounts(id INTEGER PRIMARY KEY,panel_id INTEGER);
        CREATE TABLE refund_requests(
            refund_id TEXT PRIMARY KEY,order_id TEXT,merchant_key TEXT NOT NULL,
            amount_toman INTEGER NOT NULL,state TEXT NOT NULL,settled_at INTEGER
        );
        CREATE TABLE representatives(id INTEGER PRIMARY KEY,balance_toman INTEGER);
        CREATE TABLE rep_edges(id INTEGER PRIMARY KEY,parent_key TEXT,child_rep_id INTEGER);
        CREATE TABLE financial_ledger(
            id INTEGER PRIMARY KEY,rep_id INTEGER,edge_id INTEGER,order_id TEXT,
            transaction_type TEXT NOT NULL,amount INTEGER NOT NULL,balance_after INTEGER,
            ledger_scope TEXT,merchant_key TEXT,business_key TEXT
        );
    """)
    try:
        conn.execute("INSERT INTO quotes VALUES('q-1',11,1)")
        conn.execute("INSERT INTO accounts VALUES(1,11)")
        conn.execute(
            "INSERT INTO payment_orders VALUES('o-1','q-1','root','vpn_buy','completed',1000,150)"
        )
        conn.execute(
            "INSERT INTO refund_requests VALUES('r-1','o-1','root',100,'settled',175)"
        )
        conn.executemany(
            "INSERT INTO representatives VALUES(?,?)", [(201,50),(202,25)]
        )
        conn.executemany(
            "INSERT INTO rep_edges VALUES(?,?,?)", [(1,'rep:201',201),(2,'rep:202',202)]
        )
        # These edge rows are wholesale allocation records, not customer sales.
        conn.executemany(
            "INSERT INTO financial_ledger(id,rep_id,edge_id,order_id,transaction_type,"
            "amount,balance_after,ledger_scope,merchant_key) "
            "VALUES(?,NULL,?,'o-1','edge_cost',?,NULL,'rep_edge','root')",
            [(1,1,900),(2,2,950)],
        )
        # An anchored suffix projects 20 + 10 - 5 = 25, while cache is deliberately 50.
        conn.executemany(
            "INSERT INTO financial_ledger(id,rep_id,edge_id,order_id,transaction_type,"
            "amount,balance_after,ledger_scope,merchant_key) "
            "VALUES(?,?,NULL,NULL,?,?,? ,?, 'root')",
            [
                (3,201,"opening_anchor",0,20,"opening_anchor"),
                (4,201,"debit",10,30,"rep_edge"),
                (5,201,"credit",5,25,"rep_edge"),
                (6,202,"opening_anchor",0,25,"opening_anchor"),
            ],
        )
        before_balances = [tuple(row) for row in conn.execute(
            "SELECT id,balance_toman FROM representatives ORDER BY id"
        )]
        before_ledger_count = conn.execute("SELECT COUNT(*) FROM financial_ledger").fetchone()[0]

        metrics = nightly_metrics(100,200,conn=conn)
        assert metrics["gross_sales_toman"] == 1000  # not 1000 + 900 + 950.
        assert metrics["refunds_toman"] == 100
        assert metrics["net_sales_toman"] == 900
        assert metrics["wholesale_cost_toman"] == 1850
        assert metrics["panel_metrics"][0]["panel_id"] == 11
        assert metrics["panel_metrics"][0]["gross_toman"] == 1000
        assert {row["representative_key"] for row in metrics["top_3_representatives"]} == {
            "rep:201", "rep:202"
        }

        reconciliation = reconcile_balances(conn=conn)
        rep201 = next(item for item in reconciliation["alerts"] if item["rep_id"] == 201)
        assert rep201["state"] == "discrepancy"
        assert rep201["projected_balance_toman"] == 25
        assert rep201["cached_balance_toman"] == 50
        assert reconciliation["mutated"] is False
        assert [tuple(row) for row in conn.execute(
            "SELECT id,balance_toman FROM representatives ORDER BY id"
        )] == before_balances
        assert conn.execute("SELECT COUNT(*) FROM financial_ledger").fetchone()[0] == before_ledger_count
    finally:
        conn.close()
