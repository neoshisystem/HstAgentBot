"""Single-claim, Tehran-period scheduler for non-mutating report jobs."""
from __future__ import annotations

from datetime import date, datetime, time, timezone
import json
import secrets
import sqlite3
from typing import Any, Callable, Iterable, Mapping
from zoneinfo import ZoneInfo

TEHRAN = ZoneInfo("Asia/Tehran")
WATCHDOG_INTERVAL_SECONDS = 300
_NON_MUTATING_KINDS = frozenset({
    "report", "reportcron", "reportnight", "reporttest", "pendingreceipts",
    "paymentreport", "supportreport", "errorreport", "backupfile",
})


class SchedulerError(ValueError):
    """Invalid or unsafe scheduled job request."""


def _positive(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise SchedulerError(f"{label} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError):
        raise SchedulerError(f"{label} must be a positive integer") from None
    if result <= 0:
        raise SchedulerError(f"{label} must be a positive integer")
    return result


def _epoch(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SchedulerError(f"{label} must be a non-negative UTC epoch integer")
    return value


def tehran_day_range(day: date | str) -> tuple[int, int]:
    """Return [start,end) UTC epoch seconds for one Asia/Tehran calendar day."""
    if isinstance(day, str):
        try:
            selected = date.fromisoformat(day)
        except ValueError:
            raise SchedulerError("day must be an ISO calendar date") from None
    elif isinstance(day, date):
        selected = day
    else:
        raise SchedulerError("day must be a date or ISO calendar date")
    local_start = datetime.combine(selected, time.min, tzinfo=TEHRAN)
    local_end = datetime.combine(date.fromordinal(selected.toordinal() + 1),
                                 time.min, tzinfo=TEHRAN)
    return (
        int(local_start.astimezone(timezone.utc).timestamp()),
        int(local_end.astimezone(timezone.utc).timestamp()),
    )


def tehran_period(now: int) -> tuple[str, int, int]:
    """Return Tehran YYYY-MM-DD and its corresponding UTC half-open range."""
    timestamp = _epoch(now, "now")
    local_day = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(TEHRAN).date()
    start_utc, end_utc = tehran_day_range(local_day)
    return local_day.isoformat(), start_utc, end_utc


def claim_period_job(
    conn: sqlite3.Connection,
    *,
    kind: str,
    target: str | int,
    period: str,
    due_at: int,
    now: int,
    lease_seconds: int = WATCHDOG_INTERVAL_SECONDS,
) -> dict[str, Any] | None:
    """Atomically claim one stable kind:target:period key; never steal/replay it."""
    selected_kind = str(kind or "").strip().lower()
    selected_target = str(target).strip()
    selected_period = str(period).strip()
    if not selected_kind or not selected_target or not selected_period:
        raise SchedulerError("kind, target and period are required")
    if len(selected_kind) > 80 or len(selected_target) > 160 or len(selected_period) > 80:
        raise SchedulerError("job key component is too long")
    due = _epoch(due_at, "due_at")
    current = _epoch(now, "now")
    lease = _positive(lease_seconds, "lease_seconds")
    if lease > 3600:
        raise SchedulerError("lease_seconds cannot exceed one hour")
    if due > current:
        return None
    key = f"{selected_kind}:{selected_target}:{selected_period}"
    token = secrets.token_urlsafe(18)
    if conn.in_transaction:
        raise SchedulerError("claim_period_job requires an idle connection")
    conn.execute("BEGIN IMMEDIATE")
    try:
        cursor = conn.execute(
            "INSERT INTO scheduled_jobs(job_key,kind,due_at,state,lease_token,lease_until) "
            "VALUES(?,?,?,'running',?,?) ON CONFLICT(job_key) DO NOTHING",
            (key, selected_kind, due, token, current + lease),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        conn.commit()
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    return {"job_key": key, "kind": selected_kind, "target": selected_target,
            "period": selected_period, "due_at": due, "lease_token": token,
            "lease_until": current + lease}


def run_due_jobs(
    conn: sqlite3.Connection,
    jobs: Iterable[Mapping[str, Any]],
    *,
    handlers: Mapping[str, Callable[[Mapping[str, Any]], Any]],
    now: int,
) -> list[dict[str, Any]]:
    """Run due report handlers after durable claims; operational mutations stay elsewhere."""
    current = _epoch(now, "now")
    outcomes: list[dict[str, Any]] = []
    for spec in jobs:
        kind = str(spec.get("kind", "")).strip().lower()
        if kind not in _NON_MUTATING_KINDS:
            outcomes.append({"kind": kind, "state": "delegated_to_operation_worker"})
            continue
        handler = handlers.get(kind)
        if not callable(handler):
            outcomes.append({"kind": kind, "state": "no_handler"})
            continue
        claim = claim_period_job(
            conn, kind=kind, target=spec.get("target", ""),
            period=spec.get("period", ""), due_at=spec.get("due_at"),
            now=current,
        )
        if claim is None:
            outcomes.append({"kind": kind, "state": "already_claimed_or_not_due"})
            continue
        # No DB transaction is held while a job handler runs.
        try:
            result = handler(dict(spec))
        except Exception as exc:
            conn.execute(
                "UPDATE scheduled_jobs SET state='failed',result_json=? "
                "WHERE job_key=? AND state='running' AND lease_token=?",
                (json.dumps({"error_type": type(exc).__name__}, sort_keys=True),
                 claim["job_key"], claim["lease_token"]),
            )
            conn.commit()
            outcomes.append({"job_key": claim["job_key"], "state": "failed",
                             "error_type": type(exc).__name__})
            continue
        conn.execute(
            "UPDATE scheduled_jobs SET state='complete',result_json=? "
            "WHERE job_key=? AND state='running' AND lease_token=?",
            (json.dumps({"status": "complete"}, sort_keys=True),
             claim["job_key"], claim["lease_token"]),
        )
        conn.commit()
        outcomes.append({"job_key": claim["job_key"], "state": "complete",
                         "result": result})
    return outcomes


__all__ = [
    "SchedulerError", "WATCHDOG_INTERVAL_SECONDS", "tehran_day_range",
    "tehran_period", "claim_period_job", "run_due_jobs",
]
