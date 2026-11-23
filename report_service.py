"""Financial and nightly reporting projections without ledger double counting."""
from __future__ import annotations

from collections import defaultdict
import json
import sqlite3
from typing import Any


class ReportServiceError(ValueError):
    """A report request or the persisted accounting projection is invalid."""


def _epoch(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReportServiceError(f"{name} must be a non-negative UTC epoch integer")
    return value


def _mapping_rows(conn: sqlite3.Connection, sql: str, params=()) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(sql, params).fetchall()]


def _run_with_connection(conn, callback):
    if conn is not None:
        return callback(conn)
    import database
    with database.get_db() as managed:
        return callback(managed)


def _nightly_metrics(conn: sqlite3.Connection, start_utc: int, end_utc: int) -> dict[str, Any]:
    if end_utc <= start_utc:
        raise ReportServiceError("report period must be a non-empty [start,end) interval")

    # Sales gross is counted from completed customer orders exactly once.
    # Representative price edges and settlement/anchor ledger rows are not sales.
    gross_rows = _mapping_rows(
        conn,
        "SELECT merchant_key,COUNT(*) AS order_count,SUM(amount_toman) AS gross_toman "
        "FROM payment_orders WHERE status='completed' "
        "AND purpose IN ('vpn_buy','vpn_renew','vpn_addon','digital_product_buy') "
        "AND created_at>=? AND created_at<? GROUP BY merchant_key ORDER BY merchant_key",
        (start_utc, end_utc),
    )
    gross_by_merchant = {
        str(row["merchant_key"]): {
            "order_count": int(row["order_count"]),
            "gross_toman": int(row["gross_toman"] or 0),
        }
        for row in gross_rows
    }
    gross_total = sum(item["gross_toman"] for item in gross_by_merchant.values())

    refund_rows = _mapping_rows(
        conn,
        "SELECT merchant_key,COUNT(*) AS refund_count,SUM(amount_toman) AS refund_toman "
        "FROM refund_requests WHERE state='settled' AND settled_at>=? AND settled_at<? "
        "GROUP BY merchant_key ORDER BY merchant_key",
        (start_utc, end_utc),
    )
    refunds_by_merchant = {
        str(row["merchant_key"]): {
            "refund_count": int(row["refund_count"]),
            "refund_toman": int(row["refund_toman"] or 0),
        }
        for row in refund_rows
    }
    refunds_total = sum(item["refund_toman"] for item in refunds_by_merchant.values())

    # Cost edges are disclosed in their own projection; they never inflate sales.
    edge_rows = _mapping_rows(
        conn,
        "SELECT fl.merchant_key,fl.edge_id,COUNT(*) AS posting_count,"
        "SUM(fl.amount) AS edge_cost_toman "
        "FROM financial_ledger fl JOIN payment_orders po ON po.order_id=fl.order_id "
        "WHERE fl.edge_id IS NOT NULL AND po.status='completed' "
        "AND po.purpose IN ('vpn_buy','vpn_renew','vpn_addon','digital_product_buy') "
        "AND po.created_at>=? AND po.created_at<? "
        "GROUP BY fl.merchant_key,fl.edge_id ORDER BY fl.merchant_key,fl.edge_id",
        (start_utc, end_utc),
    )
    edge_total = sum(int(row["edge_cost_toman"] or 0) for row in edge_rows)

    panel_rows = _mapping_rows(
        conn,
        "SELECT COALESCE(q.panel_id,a.panel_id) AS panel_id,COUNT(*) AS order_count,"
        "SUM(po.amount_toman) AS gross_toman "
        "FROM payment_orders po LEFT JOIN quotes q ON q.quote_id=po.quote_id "
        "LEFT JOIN accounts a ON a.id=q.account_id "
        "WHERE po.status='completed' "
        "AND po.purpose IN ('vpn_buy','vpn_renew','vpn_addon','digital_product_buy') "
        "AND po.created_at>=? AND po.created_at<? "
        "GROUP BY COALESCE(q.panel_id,a.panel_id) ORDER BY panel_id",
        (start_utc, end_utc),
    )

    rep_rows = _mapping_rows(
        conn,
        "SELECT e.parent_key AS representative_key,COUNT(*) AS edge_postings,"
        "SUM(fl.amount) AS edge_cost_toman "
        "FROM financial_ledger fl JOIN payment_orders po ON po.order_id=fl.order_id "
        "JOIN rep_edges e ON e.id=fl.edge_id "
        "WHERE po.status='completed' "
        "AND po.purpose IN ('vpn_buy','vpn_renew','vpn_addon','digital_product_buy') "
        "AND po.created_at>=? AND po.created_at<? "
        "GROUP BY e.parent_key ORDER BY SUM(fl.amount) DESC,e.parent_key LIMIT 3",
        (start_utc, end_utc),
    )

    return {
        "period_utc": {"start": start_utc, "end": end_utc},
        "gross_sales_by_merchant": gross_by_merchant,
        "gross_sales_toman": gross_total,
        "refunds_by_merchant": refunds_by_merchant,
        "refunds_toman": refunds_total,
        "net_sales_toman": gross_total - refunds_total,
        "panel_metrics": panel_rows,
        "top_3_representatives": rep_rows,
        "wholesale_costs_by_edge": edge_rows,
        "wholesale_cost_toman": edge_total,
    }


def nightly_metrics(start_utc: int, end_utc: int, *, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Build a Tehran-bounded report from a caller-supplied UTC half-open range."""
    start = _epoch(start_utc, "start_utc")
    end = _epoch(end_utc, "end_utc")
    if end <= start:
        raise ReportServiceError("report period must be a non-empty [start,end) interval")
    return _run_with_connection(conn, lambda db: _nightly_metrics(db, start, end))


def _reconcile_balances(conn: sqlite3.Connection) -> dict[str, Any]:
    representatives = _mapping_rows(
        conn, "SELECT id,balance_toman FROM representatives ORDER BY id"
    )
    alerts: list[dict[str, Any]] = []
    projections: list[dict[str, Any]] = []
    for rep in representatives:
        rep_id = int(rep["id"])
        rows = _mapping_rows(
            conn,
            "SELECT id,transaction_type,amount,balance_after,ledger_scope "
            "FROM financial_ledger WHERE rep_id=? ORDER BY id",
            (rep_id,),
        )
        anchors = [
            row for row in rows
            if str(row.get("ledger_scope") or "") in {"opening_anchor", "legacy_anchor"}
            or str(row.get("transaction_type") or "") == "opening_anchor"
        ]
        cached = int(rep["balance_toman"] or 0)
        if not anchors:
            alert = {
                "rep_id": rep_id, "state": "anchor_missing",
                "cached_balance_toman": cached, "projected_balance_toman": None,
            }
            alerts.append(alert)
            projections.append(alert)
            continue

        anchor = anchors[-1]
        anchor_id = int(anchor["id"])
        projected = int(anchor["balance_after"])
        unknown: list[str] = []
        for row in rows:
            if int(row["id"]) <= anchor_id:
                continue
            tx_type = str(row["transaction_type"] or "").lower()
            amount = int(row["amount"] or 0)
            if tx_type == "debit":
                projected += amount
            elif tx_type == "credit":
                projected -= amount
            elif tx_type == "adjust":
                projected += amount
            else:
                unknown.append(tx_type or "unknown")
        if unknown:
            alert = {
                "rep_id": rep_id, "state": "unknown_ledger_operation",
                "unknown_types": sorted(set(unknown)),
                "cached_balance_toman": cached, "projected_balance_toman": None,
                "anchor_id": anchor_id,
            }
        elif projected != cached:
            alert = {
                "rep_id": rep_id, "state": "discrepancy",
                "cached_balance_toman": cached, "projected_balance_toman": projected,
                "difference_toman": cached - projected, "anchor_id": anchor_id,
            }
        else:
            alert = {
                "rep_id": rep_id, "state": "matched",
                "cached_balance_toman": cached, "projected_balance_toman": projected,
                "anchor_id": anchor_id,
            }
        projections.append(alert)
        if alert["state"] != "matched":
            alerts.append(alert)

    return {
        "state": "alert" if alerts else "matched",
        "checked_representatives": len(representatives),
        "alerts": alerts,
        "projections": projections,
        "mutated": False,
    }


def reconcile_balances(*, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Compare cache to append-only ledger suffix after opening anchor; never auto-fix."""
    return _run_with_connection(conn, _reconcile_balances)


__all__ = ["ReportServiceError", "nightly_metrics", "reconcile_balances"]
