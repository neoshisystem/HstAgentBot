"""Offline acceptance for reservation activation locks and ledger safety."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
import importlib.util
import sqlite3

import pytest

from migrations_runner import apply_migrations
from reservation_service import (
    ReservationActivationError,
    finish_activation,
    prepare_activation,
)


def _load_helpers():
    helper_path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w06_reservation_helpers", helper_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_activation_restart_and_no_second_charge(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _load_helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w06-reservation.sqlite3"):
        with database.get_db() as conn:
            assert len(apply_migrations(conn, root / "migrations", now=1794237000)) == 18
            conn.execute(
                "INSERT INTO panels(id,panel_code,name,panel_type) "
                "VALUES(1,'W06-PANEL','Synthetic panel','3x-ui')"
            )
        helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
        helpers.seed_account(
            database, account_id=303, rep_id=202, owner_tg_id=101,
            account_code="W06-RESERVED", email="w06-reserved@example.invalid",
        )
        database.add_or_update_plan(
            "w06-reserve", "W06 reserve", 1, 30, 900, 1200, is_quick=1
        )

        def new_reservation(account_id=303, code="W06-RESERVED"):
            if account_id != 303:
                helpers.seed_account(
                    database, account_id=account_id, rep_id=202, owner_tg_id=101,
                    account_code=code, email=f"{code.lower()}@example.invalid",
                )
            reservation_id = database.create_reservation(
                rep_id=202,
                account_code=code,
                email=f"{code.lower()}@example.invalid",
                plan_code="w06-reserve",
                plan_name="W06 reserve",
                gb=1,
                days=30,
                on_first_connect=0,
                charge_amount=900,
                actor_id=303,
                actor_name="W06 synthetic representative",
            )
            with database.get_db() as conn:
                conn.execute(
                    "UPDATE reserved_packages SET account_id=?,panel_id=1 WHERE id=?",
                    (account_id, reservation_id),
                )
            return reservation_id

        first_id = new_reservation()
        assert database.get_rep_balance(202) == 900
        barrier = Barrier(2)
        dispatches = []

        def worker():
            conn = sqlite3.connect(database.DB_PATH, timeout=10)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            try:
                barrier.wait(timeout=5)
                prepared = prepare_activation(
                    conn, account_id=303, reservation_id=first_id, now=1794237001
                )
                assert conn.in_transaction is False
                if prepared is not None:
                    # This fake dispatch represents the external panel boundary.
                    # A successful preparation has already committed every local write.
                    dispatches.append(prepared["reservation_id"])
                return prepared
            finally:
                conn.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: worker(), range(2)))
        winners = [item for item in results if item is not None]
        assert len(winners) == 1
        prepared = winners[0]
        assert prepared["lock_order"] == [
            "account:303", f"reservation-activation:{first_id}"
        ]
        assert prepared["legacy_charge_already_recorded"] is True
        assert dispatches == [first_id]

        # Finish from a fresh connection to model a scheduler worker restart.
        with sqlite3.connect(database.DB_PATH, timeout=10) as conn:
            conn.row_factory = sqlite3.Row
            result = finish_activation(conn, prepared, outcome="success")
            assert conn.in_transaction is False
            assert result["reservation_status"] == "activated"
            replay = finish_activation(conn, prepared, outcome="success")
            assert replay["idempotent_replay"] is True
        assert database.get_reservation_by_id(first_id)["status"] == "activated"
        assert database.get_rep_balance(202) == 900
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_ledger WHERE rep_id=202 AND transaction_type='debit'"
            ).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM operation_locks WHERE lock_key IN (?,?)",
                ("account:303", f"reservation-activation:{first_id}"),
            ).fetchone()[0] == 0
        assert prepare_activation(
            sqlite3.connect(database.DB_PATH), account_id=303,
            reservation_id=first_id, now=1794237002
        ) is None

        # UNKNOWN retains both locks and the already-recorded reservation charge.
        unknown_id = new_reservation(304, "W06-UNKNOWN")
        with database.get_db() as conn:
            unknown_ticket = prepare_activation(
                conn, account_id=304, reservation_id=unknown_id, now=1794237003
            )
        with database.get_db() as conn:
            unknown = finish_activation(conn, unknown_ticket, outcome="unknown")
            assert unknown["locks_retained"] is True
            with pytest.raises(ValueError):
                finish_activation(conn, unknown_ticket, outcome="unclassified")
        assert database.get_reservation_by_id(unknown_id)["status"] == "pending"
        assert database.cancel_reservation(unknown_id, actor_id=9000) is None
        with database.get_db() as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM operation_locks WHERE lock_key IN (?,?)",
                ("account:304", f"reservation-activation:{unknown_id}"),
            ).fetchone()[0] == 2
            assert conn.execute(
                "SELECT COUNT(*) FROM financial_ledger WHERE rep_id=202 AND transaction_type='debit'"
            ).fetchone()[0] == 2
        with database.get_db() as conn:
            assert prepare_activation(
                conn, account_id=304, reservation_id=unknown_id, now=1794237004
            ) is None
        assert database.get_rep_balance(202) == 1800

        # A definite failure releases the exact pair but does not debit or refund.
        failed_id = new_reservation(305, "W06-DEFINITE-FAILURE")
        with database.get_db() as conn:
            failed_ticket = prepare_activation(
                conn, account_id=305, reservation_id=failed_id, now=1794237005
            )
        with database.get_db() as conn:
            failed = finish_activation(conn, failed_ticket, outcome="definite_failure")
            assert failed["reservation_status"] == "pending"
        assert database.get_reservation_by_id(failed_id)["status"] == "pending"
        assert database.cancel_reservation(failed_id, actor_id=9000) is not None
        assert database.get_reservation_by_id(failed_id)["status"] == "cancelled"
        assert database.get_rep_balance(202) == 1800
        assert network_attempts == []
        assert not Path(sentinel).exists()
