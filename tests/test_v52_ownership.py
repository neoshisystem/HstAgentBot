"""W14.4 offline acceptance: root preview, transfer CAS, ACL revocation and debt preservation."""
from __future__ import annotations

import base64
import importlib.util
import json
from pathlib import Path
import sys

from crypto_store import decrypt_secret
from migrations_runner import apply_migrations


def _load(path, name, monkeypatch):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def _seed_user(conn, telegram_id, rep_id=None):
    conn.execute(
        "INSERT INTO bot_users(telegram_id,role,rep_id,username,is_blocked) "
        "VALUES(?,'end_user',?,?,0)",
        (telegram_id, rep_id, f"w14o_{telegram_id}"),
    )
    conn.execute(
        "INSERT INTO user_settings(user_id,first_connect_mode,active_persona) "
        "VALUES(?,0,'end_user')",
        (telegram_id,),
    )
    conn.execute(
        "INSERT INTO user_wallets(user_id,balance_toman) VALUES(?,9000)",
        (telegram_id,),
    )


def test_transfer_preserves_debt_and_revokes_access(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers = _load(Path(__file__).with_name("v52_helpers.py"), "_w14_ownership_helpers", monkeypatch)
    attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setitem(sys.modules, "database", database)
    config = sys.modules["config"]
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9090)
    monkeypatch.setenv(
        "BACKUP_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    )
    access = _load(root / "access_service.py", "access_service", monkeypatch)
    ownership = _load(root / "ownership_service.py", "ownership_service", monkeypatch)

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w14-ownership.sqlite3"):
        with database.get_db() as conn:
            apply_migrations(conn, root / "migrations", now=1794400000)
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,parent_rep_id) "
                "VALUES(550,'W14O-SELLER','W14O seller',303,'',NULL)"
            )
            for telegram_id, rep_id in ((101, None), (202, None), (303, 550), (9090, None)):
                _seed_user(conn, telegram_id, rep_id)
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,username,status,verified_at,revision) "
                "VALUES(9001,NULL,1,'w14o_primary','active',1794400000,1)"
            )
        helpers.seed_account(
            database, account_id=705, rep_id=550, owner_tg_id=101,
            account_code="W14O-ACCOUNT-A", email="w14o-account@example.invalid",
        )
        with database.get_db() as conn:
            conn.execute(
                "INSERT INTO account_access_grants(account_id,grantee_tg_id,access_level,"
                "granted_by_tg_id,created_at,revoked_at,revision) "
                "VALUES(705,202,'viewer',101,1794400010,NULL,1)"
            )
            conn.execute(
                "INSERT INTO rep_edges(parent_key,child_rep_id,billing_mode,debt_toman,"
                "credit_limit_toman,active,started_at,ended_at) "
                "VALUES('root',550,'credit',765432,2000000,1,1794400000,NULL)"
            )
            conn.execute(
                "INSERT INTO financial_ledger(rep_id,account_code,transaction_type,amount,"
                "balance_after,description,actor_id) "
                "VALUES(550,'W14O-ACCOUNT-A','debit',765432,765432,'Preserve transfer debt',303)"
            )
            conn.execute(
                "INSERT INTO subscription_profiles(profile_id,owner_tg_id,name,is_default,enabled,"
                "created_bot_id,token_hash,token_cipher,token_version,revision,created_at) "
                "VALUES(1805,101,'W14 preserved profile',0,1,9001,'w14-profile-hash',"
                "'synthetic-profile-cipher',1,1,1794400000)"
            )
            conn.execute(
                "INSERT INTO subscription_profile_accounts(profile_id,account_id,added_by_tg_id,"
                "sort_order,added_at) VALUES(1805,705,101,0,1794400001)"
            )
            debt_before = tuple(conn.execute(
                "SELECT debt_toman,credit_limit_toman,active FROM rep_edges WHERE child_rep_id=550"
            ).fetchone())
            ledger_before = tuple(conn.execute(
                "SELECT rep_id,account_code,transaction_type,amount,balance_after "
                "FROM financial_ledger WHERE account_code='W14O-ACCOUNT-A'"
            ).fetchone())

        root_actor = {"actor_tg_id": 9090, "bot_id": 9001}
        preview = ownership.preview_transfer(
            root_actor, 705, 202,
            reason="Verified claimant and approved support transfer",
            seller_mode="root_direct",
            now=1794400100,
        )
        assert preview["before"]["owner_tg_id"] == 101
        assert preview["after"]["owner_tg_id"] == 202
        assert preview["after"]["seller_key"] == "root"
        assert preview["after"]["rep_id"] is None
        assert len(preview["nonce"]) == 24
        assert preview["expires_at"] == 1794400400
        with database.get_db() as conn:
            unchanged = conn.execute(
                "SELECT owner_tg_id,seller_key,rep_id,revision FROM accounts WHERE id=705"
            ).fetchone()
            assert tuple(unchanged) == (101, "root", 550, 1)

        result = ownership.execute_transfer(
            root_actor, 705, 202, preview["nonce"], now=1794400101,
        )
        assert result["owner_tg_id"] == 202
        assert result["seller_key"] == "root"
        assert result["rep_id"] is None
        assert result["revoked_grants"] == 1
        assert result["credentials_rotated"] is False
        assert result["replayed"] is False
        assert access.authorize_account({"actor_tg_id": 202}, 705, "view_config") is True
        assert access.authorize_account({"actor_tg_id": 101}, 705, "view_config") is False

        replay = ownership.execute_transfer(
            root_actor, 705, 202, preview["nonce"], now=1794400200,
        )
        assert replay["replayed"] is True
        with database.get_db() as conn:
            account = conn.execute(
                "SELECT owner_tg_id,seller_key,rep_id,revision,acl_revision "
                "FROM accounts WHERE id=705"
            ).fetchone()
            grant = conn.execute(
                "SELECT revoked_at,revision FROM account_access_grants "
                "WHERE account_id=705 AND grantee_tg_id=202"
            ).fetchone()
            debt_after = tuple(conn.execute(
                "SELECT debt_toman,credit_limit_toman,active FROM rep_edges WHERE child_rep_id=550"
            ).fetchone())
            ledger_after = tuple(conn.execute(
                "SELECT rep_id,account_code,transaction_type,amount,balance_after "
                "FROM financial_ledger WHERE account_code='W14O-ACCOUNT-A'"
            ).fetchone())
            membership_count = conn.execute(
                "SELECT COUNT(*) FROM subscription_profile_accounts "
                "WHERE profile_id=1805 AND account_id=705"
            ).fetchone()[0]
            audit = conn.execute(
                "SELECT details_json FROM audit_logs WHERE action='ownership_transfer'"
            ).fetchone()
            confirmation = conn.execute(
                "SELECT consumed_at,operation_id FROM action_confirmations WHERE nonce=?",
                (preview["nonce"],),
            ).fetchone()
        assert tuple(account) == (202, "root", None, 2, 2)
        assert tuple(grant) == (1794400101, 2)
        assert debt_after == debt_before
        assert ledger_after == ledger_before
        assert membership_count == 1
        assert audit is not None
        details = json.loads(decrypt_secret(audit["details_json"], "hstagent/columns/v1"))
        assert details["reason"] == "Verified claimant and approved support transfer"
        assert details["nonce"] == preview["nonce"]
        assert details["before"]["owner_tg_id"] == 101
        assert details["after"]["owner_tg_id"] == 202
        assert details["credentials_rotated"] is False
        assert confirmation["operation_id"] == result["operation_id"]
        try:
            ownership.preview_transfer(
                {"actor_tg_id": 101, "bot_id": 9001, "is_root": True},
                705, 101, reason="spoofed root attempt", now=1794400201,
            )
        except ownership.OwnershipPermissionError:
            pass
        else:
            raise AssertionError("a claimed root flag must not authorize ownership transfer")
        assert attempts == []
        assert not Path(sentinel).exists()
