"""Offline migration/backup/restore rehearsal against a disposable DB copy."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _connect_read_only(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=30)


def _digest_query(conn: sqlite3.Connection, query: str) -> str:
    rows = [tuple(row) for row in conn.execute(query).fetchall()]
    return hashlib.sha256(
        json.dumps(rows, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _snapshot(conn: sqlite3.Connection) -> dict[str, Any]:
    conn.row_factory = sqlite3.Row
    tables = {
        str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    result: dict[str, Any] = {}
    for table in (
        "accounts", "financial_ledger", "financial_holds", "panel_operations",
        "delivery_outbox", "payment_orders", "payment_attempts", "payment_receipts",
        "subscription_profiles", "user_wallets", "merchant_wallets", "rep_edges",
    ):
        if table not in tables:
            result[table] = {"present": False, "count": 0}
            continue
        count = int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        result[table] = {"present": True, "count": count}
        columns = {
            str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')
        }
        if table == "accounts" and "owner_tg_id" in columns:
            result[table]["owned_count"] = int(conn.execute(
                'SELECT COUNT(*) FROM accounts WHERE owner_tg_id IS NOT NULL'
            ).fetchone()[0])
        if table == "financial_ledger" and "amount" in columns:
            result[table]["amount_sum"] = int(conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM financial_ledger"
            ).fetchone()[0])
            ledger_columns = {
                "merchant_key", "ledger_scope", "user_id", "edge_id",
                "transaction_type", "amount",
            }
            if ledger_columns <= columns:
                balances = [
                    tuple(row) for row in conn.execute(
                        "SELECT merchant_key,ledger_scope,user_id,edge_id,"
                        "SUM(CASE transaction_type WHEN 'credit' THEN amount "
                        "WHEN 'debit' THEN -amount ELSE 0 END) "
                        "FROM financial_ledger GROUP BY merchant_key,ledger_scope,user_id,edge_id "
                        "ORDER BY merchant_key,ledger_scope,user_id,edge_id"
                    ).fetchall()
                ]
                result[table]["balance_digest"] = hashlib.sha256(
                    json.dumps(balances, separators=(",", ":"), default=str).encode("utf-8")
                ).hexdigest()
        if table == "accounts" and {
            "id", "owner_tg_id", "rep_id", "panel_id", "deleted_at"
        } <= columns:
            result[table]["ownership_digest"] = _digest_query(
                conn, "SELECT id,owner_tg_id,rep_id,panel_id,deleted_at "
                "FROM accounts ORDER BY id"
            )
        if table == "financial_holds" and {
            "hold_id", "order_id", "source_key", "amount_toman", "state", "captured_ledger_id"
        } <= columns:
            result[table]["hold_digest"] = _digest_query(
                conn, "SELECT hold_id,order_id,source_key,amount_toman,state,captured_ledger_id "
                "FROM financial_holds ORDER BY hold_id"
            )
        if table == "panel_operations" and {
            "operation_id", "state", "lock_key", "lock_token"
        } <= columns:
            result[table]["lock_digest"] = _digest_query(
                conn, "SELECT operation_id,state,lock_key,lock_token "
                "FROM panel_operations ORDER BY operation_id"
            )
        if table == "delivery_outbox" and {
            "id", "event_key", "bot_id", "chat_id", "purpose", "state", "attempts"
        } <= columns:
            result[table]["delivery_digest"] = _digest_query(
                conn, "SELECT id,event_key,bot_id,chat_id,purpose,state,attempts "
                "FROM delivery_outbox ORDER BY id"
            )
        if table == "payment_orders" and {
            "order_id", "status", "amount_toman", "received_toman"
        } <= columns:
            result[table]["order_digest"] = _digest_query(
                conn, "SELECT order_id,status,amount_toman,received_toman "
                "FROM payment_orders ORDER BY order_id"
            )
        if table == "payment_attempts" and {
            "attempt_id", "order_id", "state", "amount_toman"
        } <= columns:
            result[table]["attempt_digest"] = _digest_query(
                conn, "SELECT attempt_id,order_id,state,amount_toman "
                "FROM payment_attempts ORDER BY attempt_id"
            )
        if table == "payment_receipts" and {
            "id", "order_id", "status", "amount_toman"
        } <= columns:
            result[table]["receipt_digest"] = _digest_query(
                conn, "SELECT id,order_id,status,amount_toman "
                "FROM payment_receipts ORDER BY id"
            )
        if table == "subscription_profiles" and {
            "profile_id", "owner_tg_id", "enabled", "token_version", "revision", "deleted_at"
        } <= columns:
            result[table]["profile_digest"] = _digest_query(
                conn, "SELECT profile_id,owner_tg_id,enabled,token_version,revision,deleted_at "
                "FROM subscription_profiles ORDER BY profile_id"
            )
        if table == "user_wallets" and {"user_id", "balance_toman"} <= columns:
            result[table]["wallet_digest"] = _digest_query(
                conn, "SELECT user_id,balance_toman FROM user_wallets ORDER BY user_id"
            )
        if table == "merchant_wallets" and {
            "merchant_key", "user_tg_id", "balance_toman"
        } <= columns:
            result[table]["merchant_wallet_digest"] = _digest_query(
                conn, "SELECT merchant_key,user_tg_id,balance_toman "
                "FROM merchant_wallets ORDER BY merchant_key,user_tg_id"
            )
        if table == "rep_edges" and {
            "id", "parent_key", "child_rep_id", "billing_mode", "debt_toman",
            "credit_limit_toman", "active"
        } <= columns:
            result[table]["edge_digest"] = _digest_query(
                conn, "SELECT id,parent_key,child_rep_id,billing_mode,debt_toman,"
                "credit_limit_toman,active FROM rep_edges ORDER BY id"
            )
        if table == "panel_operations" and "state" in columns:
            result[table]["unresolved_count"] = int(conn.execute(
                "SELECT COUNT(*) FROM panel_operations "
                "WHERE state IN ('dispatching','needs_reconciliation')"
            ).fetchone()[0])
            if "lock_token" in columns:
                result[table]["owned_lock_count"] = int(conn.execute(
                    "SELECT COUNT(*) FROM panel_operations "
                    "WHERE state IN ('dispatching','needs_reconciliation') "
                    "AND lock_token IS NOT NULL AND lock_token<>''"
                ).fetchone()[0])
        if table == "delivery_outbox" and "state" in columns:
            result[table]["pending_count"] = int(conn.execute(
                "SELECT COUNT(*) FROM delivery_outbox "
                "WHERE state IN ('pending','sending','delivery_unknown')"
            ).fetchone()[0])
    return result


def _check_sqlite(path: Path) -> tuple[str, int]:
    conn = sqlite3.connect(path)
    try:
        integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
        foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
        return ("ok" if integrity == ["ok"] else "failed", len(foreign_keys))
    finally:
        conn.close()


def _migration_invariant_mismatches(
    before: dict[str, Any], after: dict[str, Any],
) -> list[str]:
    """Compare stable data invariants for every table present on both sides."""
    stable_fields = {
        "accounts": ("count", "owned_count", "ownership_digest"),
        "financial_ledger": ("count", "amount_sum", "balance_digest"),
        "financial_holds": ("count", "hold_digest"),
        "panel_operations": ("count", "unresolved_count", "owned_lock_count", "lock_digest"),
        "delivery_outbox": ("count", "pending_count", "delivery_digest"),
        "payment_orders": ("count", "order_digest"),
        "payment_attempts": ("count", "attempt_digest"),
        "payment_receipts": ("count", "receipt_digest"),
        "subscription_profiles": ("count", "profile_digest"),
        "user_wallets": ("count", "wallet_digest"),
        "merchant_wallets": ("count", "merchant_wallet_digest"),
        "rep_edges": ("count", "edge_digest"),
    }
    failures = []
    for table, fields in stable_fields.items():
        left, right = before.get(table, {}), after.get(table, {})
        if not left.get("present") or not right.get("present"):
            continue
        for field in fields:
            if field in left and field in right and left[field] != right[field]:
                failures.append(f"{table}.{field}")
    return failures


def rehearse_migration_restore(
    source_db: str | Path,
    output_dir: str | Path,
    migration_dir: str | Path,
) -> dict[str, Any]:
    """Copy, migrate, encrypt-backup, restore and compare without writing source_db."""
    source = Path(source_db).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    migrations = Path(migration_dir).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError("source database does not exist")
    if not migrations.is_dir():
        raise FileNotFoundError("migration directory does not exist")
    if output == source.parent or output in source.parents or source in output.parents:
        raise ValueError("output directory and source database must be disjoint")
    if output.exists() and any(output.iterdir()):
        raise FileExistsError("rehearsal output directory must be empty")
    if not os.environ.get("BACKUP_ENCRYPTION_KEY"):
        raise RuntimeError("BACKUP_ENCRYPTION_KEY is required for encrypted rehearsal")

    product_root = Path(__file__).resolve().parents[1]
    if str(product_root) not in sys.path:
        sys.path.insert(0, str(product_root))

    import crypto_store
    crypto_store.load_master_key()
    output.mkdir(parents=True, exist_ok=True)
    work_dir = output / "working"
    work_dir.mkdir()
    copy_path = work_dir / "database-copy.sqlite3"
    source_sha_before = _sha256(source)

    src = _connect_read_only(source)
    dst = sqlite3.connect(copy_path)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from migrations_runner import apply_migrations

    pre_conn = sqlite3.connect(copy_path, timeout=30)
    try:
        pre_conn.execute("PRAGMA foreign_keys=ON")
        pre_migration_snapshot = _snapshot(pre_conn)
    finally:
        pre_conn.close()

    migrated_conn = sqlite3.connect(copy_path, timeout=30)
    try:
        migrated_conn.execute("PRAGMA foreign_keys=ON")
        migrated_versions = apply_migrations(migrated_conn, migrations, now=1794500000)
        post_migration_snapshot = _snapshot(migrated_conn)
    finally:
        migrated_conn.close()

    migration_invariant_mismatches = _migration_invariant_mismatches(
        pre_migration_snapshot, post_migration_snapshot
    )
    if migration_invariant_mismatches:
        raise RuntimeError(
            "migration changed preserved data invariants: "
            + ",".join(migration_invariant_mismatches)
        )
    before_snapshot = post_migration_snapshot

    integrity, foreign_key_violations = _check_sqlite(copy_path)
    if integrity != "ok" or foreign_key_violations:
        raise RuntimeError("copied database failed SQLite integrity checks")

    import backup
    old_values = {
        "DB_PATH": backup.DB_PATH,
        "BACKUP_DIR": backup.BACKUP_DIR,
        "BACKUPS_TOPIC_ID": backup.BACKUPS_TOPIC_ID,
    }
    archive_dir = output / "archives"
    restored_dir = output / "restored"
    try:
        backup.DB_PATH = str(copy_path)
        backup.BACKUP_DIR = str(archive_dir)
        backup.BACKUPS_TOPIC_ID = None
        backup_result = backup.run_backup()
        archive_path = Path(str(backup_result["archive"]))
        verification = backup.verify_backup_archive(str(archive_path))
        restore_result = backup.restore_backup_archive(
            str(archive_path), str(restored_dir)
        )
    finally:
        backup.DB_PATH = old_values["DB_PATH"]
        backup.BACKUP_DIR = old_values["BACKUP_DIR"]
        backup.BACKUPS_TOPIC_ID = old_values["BACKUPS_TOPIC_ID"]

    restored_db = Path(restore_result["destination"]) / "agent_bot.db"
    restored_conn = _connect_read_only(restored_db)
    try:
        restored_snapshot = _snapshot(restored_conn)
    finally:
        restored_conn.close()
    restored_integrity, restored_fk_violations = _check_sqlite(restored_db)
    source_sha_after = _sha256(source)
    if source_sha_before != source_sha_after:
        raise RuntimeError("source database SHA-256 changed during rehearsal")
    if before_snapshot != restored_snapshot:
        raise RuntimeError("restored record, balance, owner, lock or outbox snapshot differs")
    if restored_integrity != "ok" or restored_fk_violations:
        raise RuntimeError("restored database failed SQLite integrity checks")
    if verification.get("sqlite_integrity") != "ok" or not backup_result.get("encrypted"):
        raise RuntimeError("backup encryption or integrity verification failed")

    report = {
        "status": "PASS",
        "source_sha256_before": source_sha_before,
        "source_sha256_after": source_sha_after,
        "source_unchanged": True,
        "copy_sha256": _sha256(copy_path),
        "migrations_applied": migrated_versions,
        "pre_migration_snapshot": pre_migration_snapshot,
        "post_migration_snapshot": post_migration_snapshot,
        "migration_invariants_preserved": True,
        "migration_runner_checksum": "verified",
        "copied_sqlite_integrity": integrity,
        "copied_foreign_key_violations": foreign_key_violations,
        "backup_sha256": verification["sha256"],
        "backup_encrypted": True,
        "restored_sqlite_integrity": restored_integrity,
        "restored_foreign_key_violations": restored_fk_violations,
        "snapshot": before_snapshot,
        "restored_snapshot": restored_snapshot,
        "unknown_operations_and_outbox_preserved": before_snapshot == restored_snapshot,
        "network_used": False,
    }
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-db", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--migration-dir", default=str(Path(__file__).resolve().parents[1] / "migrations"))
    args = parser.parse_args()
    try:
        report = rehearse_migration_restore(
            args.source_db, args.output_dir, args.migration_dir
        )
    except Exception as exc:
        print(json.dumps({
            "status": "FAIL", "error_type": type(exc).__name__,
            "error": str(exc),
        }, ensure_ascii=False, sort_keys=True))
        return 1
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
