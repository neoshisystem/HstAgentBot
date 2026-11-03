import pytest

import database


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", str(tmp_path / "explicit-admin-states.sqlite3"))
    database.init_db()
    database.ensure_bot_user(84001, username="state_user", full_name="State User")
    with database.get_db() as conn:
        rep_id = conn.execute(
            """
            INSERT INTO representatives
                (rep_code, name, telegram_id, scope_filter, is_active, balance_toman, credit_limit_toman)
            VALUES ('state-rep', 'State Representative', 84002, 'all', 1, 0, 5_000_000)
            """
        ).lastrowid
    database.add_or_update_plan("state_plan", "State Plan", 10, 30, 100_000, 150_000)
    yield rep_id


def audit_count(action):
    with database.get_db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM audit_logs WHERE action=?", (action,)
        ).fetchone()[0]


def test_explicit_admin_state_setters_are_idempotent_on_replayed_values(isolated_db):
    rep_id = isolated_db

    assert database.set_user_block_status(84001, True, 9000, "Admin") == 1
    assert database.set_user_block_status(84001, True, 9000, "Admin") == 1
    assert database.is_user_blocked(84001) is True
    assert database.set_user_block_status(84001, False, 9000, "Admin") == 0
    assert database.set_user_block_status(84001, False, 9000, "Admin") == 0
    assert database.is_user_blocked(84001) is False

    assert database.set_rep_active_status(rep_id, False, 9000, "Admin") == 0
    assert database.set_rep_active_status(rep_id, False, 9000, "Admin") == 0
    assert database.get_rep_by_id(rep_id)["is_active"] == 0
    assert database.set_rep_active_status(rep_id, True, 9000, "Admin") == 1
    assert database.set_rep_active_status(rep_id, True, 9000, "Admin") == 1
    assert database.get_rep_by_id(rep_id)["is_active"] == 1

    assert database.set_plan_quick_status("state_plan", True, 9000, "Admin") == 1
    assert database.set_plan_quick_status("state_plan", True, 9000, "Admin") == 1
    assert database.get_plan("state_plan")["is_quick"] == 1
    assert database.set_plan_quick_status("state_plan", False, 9000, "Admin") == 0
    assert database.set_plan_quick_status("state_plan", False, 9000, "Admin") == 0
    assert database.get_plan("state_plan")["is_quick"] == 0

    assert database.set_user_first_connect_mode(84001, True) is True
    assert database.set_user_first_connect_mode(84001, True) is True
    assert database.get_user_first_connect_mode(84001) is True
    assert database.set_user_first_connect_mode(84001, False) is False
    assert database.set_user_first_connect_mode(84001, False) is False
    assert database.get_user_first_connect_mode(84001) is False

    assert audit_count("set_user_block_status") == 4
    assert audit_count("set_rep_active_status") == 4
    assert audit_count("set_plan_quick_status") == 4


def test_explicit_admin_state_setters_validate_missing_records(isolated_db):
    with pytest.raises(ValueError, match="Representative not found"):
        database.set_rep_active_status(999999, False)
    with pytest.raises(ValueError, match="Plan not found"):
        database.set_plan_quick_status("missing_plan", True)
    with pytest.raises(ValueError, match="Telegram user ID"):
        database.set_user_block_status(-1, True)
