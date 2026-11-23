"""W15.3 acceptance: Tehran midnight, UTC period and a single durable claim."""
from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from zoneinfo import ZoneInfo

from scheduler_service import (
    WATCHDOG_INTERVAL_SECONDS, claim_period_job, run_due_jobs,
    tehran_day_range, tehran_period,
)


def test_tehran_midnight_single_claim(tmp_path):
    path = tmp_path / "scheduler.sqlite3"
    first = sqlite3.connect(path, timeout=5)
    first.row_factory = sqlite3.Row
    first.execute(
        "CREATE TABLE scheduled_jobs("
        "job_key TEXT PRIMARY KEY,kind TEXT NOT NULL,due_at INTEGER NOT NULL,"
        "state TEXT NOT NULL,lease_token TEXT,lease_until INTEGER,result_json TEXT)"
    )
    first.commit()
    second = sqlite3.connect(path, timeout=5)
    second.row_factory = sqlite3.Row
    try:
        local_midnight = datetime(2026,11,11,0,0,tzinfo=ZoneInfo("Asia/Tehran"))
        now = int(local_midnight.timestamp())
        period, start_utc, end_utc = tehran_period(now)
        assert period == "2026-11-11"
        assert start_utc == now
        assert end_utc - start_utc == 86400
        assert tehran_day_range(period) == (start_utc,end_utc)

        wrong_utc_midnight = int(datetime(2026,11,11,0,0,tzinfo=timezone.utc).timestamp())
        assert wrong_utc_midnight != start_utc
        assert datetime.fromtimestamp(start_utc, timezone.utc).hour == 20
        assert WATCHDOG_INTERVAL_SECONDS == 300

        claim1 = claim_period_job(
            first, kind="reportnight", target="primary", period=period,
            due_at=now, now=now,
        )
        claim2 = claim_period_job(
            second, kind="reportnight", target="primary", period=period,
            due_at=now, now=now,
        )
        assert claim1 is not None and claim1["lease_token"]
        assert claim2 is None

        calls = []
        outcomes = run_due_jobs(
            first,
            [
                {"kind":"reportcron","target":"primary","period":"2026-11-11T00:00","due_at":now},
                {"kind":"reservation_activation","target":"account:42","period":period,"due_at":now},
            ],
            handlers={"reportcron": lambda spec: calls.append(spec["kind"]) or {"ok": True}},
            now=now,
        )
        assert calls == ["reportcron"]
        assert outcomes[0]["state"] == "complete"
        assert outcomes[1]["state"] == "delegated_to_operation_worker"
        replay = run_due_jobs(
            first,
            [{"kind":"reportcron","target":"primary","period":"2026-11-11T00:00","due_at":now}],
            handlers={"reportcron": lambda spec: calls.append("duplicate")},
            now=now,
        )
        assert replay[0]["state"] == "already_claimed_or_not_due"
        assert calls == ["reportcron"]
    finally:
        second.close()
        first.close()
