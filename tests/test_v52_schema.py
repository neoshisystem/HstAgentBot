"""Acceptance coverage for v5.2 migrations W02.1-W02.3."""

import hashlib
import importlib.util
import shutil
import sqlite3
from pathlib import Path

import pytest

from migrations_runner import apply_migrations


def _helpers(root, tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location(
        "_hstagentbot_v52_schema_helpers", Path(__file__).with_name("v52_helpers.py")
    )
    assert spec is not None and spec.loader is not None
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    return helpers, database, sentinel


def _migration_subset(root, tmp_path, names):
    source = root / "migrations"
    target = tmp_path / ("migrations-" + "-".join(names))
    target.mkdir()
    for name in names:
        shutil.copyfile(source / f"{name}.sql", target / f"{name}.sql")
    return target


def _seed_legacy(helpers, database):
    actor = helpers.seed_actor(
        database, actor_tg_id=101, rep_id=202, rep_tg_id=303, wallet_balance=24680
    )
    account = helpers.seed_account(
        database, account_id=303, rep_id=202, owner_tg_id=101,
        account_code="V52-W02-LEGACY-ACCOUNT",
        email="v52-w02-legacy@example.invalid",
        sub_id="V52_W02_LEGACY_SUB_MARKER",
    )
    with database.get_db() as seed:
        seed.execute(
            "UPDATE representatives SET balance_toman=13579 WHERE id=?", (202,)
        )
        seed.execute(
            "INSERT INTO financial_ledger "
            "(rep_id,account_code,email,plan_code,transaction_type,amount,balance_after,description,actor_id,actor_name) "
            "VALUES (?,?,?,'legacy-seed','debit',13579,13579,'W02 legacy fixture',?,'synthetic')",
            (202, account["account_code"], account["email"], actor["actor_tg_id"]),
        )
    return actor, account


def _snapshot_tables(conn, names=None):
    selected = names or [
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    result = {}
    for name in selected:
        ddl = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()[0]
        result[name] = (
            conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid').fetchall(),
            ddl,
        )
    return result


def test_migration_001_policy(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers, database, sentinel = _helpers(root, tmp_path, monkeypatch)

    with helpers.make_db(database, monkeypatch, tmp_path) as db_path:
        assert Path(sentinel) != Path(db_path) and not Path(sentinel).exists()
        actor, account = _seed_legacy(helpers, database)

        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        old_state = _snapshot_tables(conn)
        migration_dir = _migration_subset(root, tmp_path, ("001_policy",))
        assert apply_migrations(conn, migration_dir, now=1794096000) == ["001_policy"]

        assert {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('policy_decisions','feature_flags')"
        )} == {"policy_decisions", "feature_flags"}
        assert [r[1] for r in conn.execute("PRAGMA table_info(policy_decisions)")] == [
            "decision_id", "status", "authority", "approved_date", "policy_version", "decision_text"
        ]
        assert [r[1] for r in conn.execute("PRAGMA table_info(feature_flags)")] == ["name", "enabled"]
        assert conn.execute(
            "SELECT version,checksum,applied_at FROM schema_migrations"
        ).fetchall() == [
            ("001_policy", hashlib.sha256(
                (migration_dir / "001_policy.sql").read_bytes()
            ).hexdigest(), 1794096000)
        ]
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "INSERT INTO feature_flags(name) VALUES ('default-off') RETURNING enabled"
        ).fetchone() == (0,)
        conn.execute("INSERT INTO feature_flags(name,enabled) VALUES ('enabled',1)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO policy_decisions VALUES "
                "('bad','PENDING','hisystem','2026-11-06','v1.0','bad')"
            )
        for invalid in (2, -1):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO feature_flags(name,enabled) VALUES (?,?)",
                    (f"bad-{invalid}", invalid),
                )
        conn.execute(
            "INSERT INTO policy_decisions VALUES "
            "('approved','APPROVED','hisystem','2026-11-06','v1.0','valid')"
        )
        conn.commit()

        applied_state = (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM policy_decisions ORDER BY rowid").fetchall(),
            conn.execute("SELECT * FROM feature_flags ORDER BY rowid").fetchall(),
        )
        assert apply_migrations(conn, migration_dir, now=1794099600) == []
        assert applied_state == (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM policy_decisions ORDER BY rowid").fetchall(),
            conn.execute("SELECT * FROM feature_flags ORDER BY rowid").fetchall(),
        )
        assert _snapshot_tables(conn, old_state.keys()) == old_state
        assert conn.execute(
            "SELECT owner_tg_id FROM accounts WHERE id=?", (account["account_id"],)
        ).fetchone() == (101,)
        assert conn.execute(
            "SELECT amount,description FROM financial_ledger WHERE account_code=?",
            (account["account_code"],),
        ).fetchall() == [(13579, "W02 legacy fixture")]
        conn.close()


def test_migration_002_panels(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers, database, sentinel = _helpers(root, tmp_path, monkeypatch)

    with helpers.make_db(database, monkeypatch, tmp_path) as db_path:
        assert Path(sentinel) != Path(db_path) and not Path(sentinel).exists()
        actor, account = _seed_legacy(helpers, database)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        old_state = _snapshot_tables(conn)
        migration_dir = _migration_subset(root, tmp_path, ("001_policy", "002_panels"))

        assert apply_migrations(conn, migration_dir, now=1794100000) == [
            "001_policy", "002_panels"
        ]
        assert [r[1] for r in conn.execute("PRAGMA table_info(panels)")] == [
            "id", "panel_code", "name", "panel_type", "adapter_version", "raw_url",
            "protocol", "host", "port", "base_path", "auth_type", "auth_cipher",
            "sub_base_url", "lifecycle", "is_default", "is_hidden", "max_clients",
            "for_end_users", "for_reps", "access_type", "min_rep_rank", "min_user_rank",
            "username_strategy", "custom_prefix", "renewal_method", "connect_first_guard",
            "allow_test_account", "test_duration_hours", "test_volume_mib", "allow_addons",
            "extra_gib_rep_price", "extra_gib_user_price", "extra_day_rep_price",
            "extra_day_user_price", "default_inbound_ids_json", "extra_config_json",
            "revision", "created_at",
        ]
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='ux_panels_default'"
        ).fetchone() == ("ux_panels_default",)
        conn.execute(
            "INSERT INTO panels(id,panel_code,name,panel_type,is_default) "
            "VALUES (1,'panel-main','Main','3x-ui',1)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO panels(id,panel_code,name,panel_type,is_default) "
                "VALUES (2,'panel-second','Second','3x-ui',1)"
            )
        for port in (0, 65536):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO panels(id,panel_code,name,panel_type,port) VALUES (?,?,?,?,?)",
                    (10 + port, f"bad-port-{port}", "Bad", "3x-ui", port),
                )
        for lifecycle in ("unknown", "deleted"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO panels(id,panel_code,name,panel_type,lifecycle) VALUES (?,?,?,?,?)",
                    (30, f"bad-life-{lifecycle}", "Bad", "3x-ui", lifecycle),
                )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO panels(id,panel_code,name,panel_type,is_default) VALUES (40,'bad-flag','Bad','3x-ui',2)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO panel_rep_assignments(panel_id,rep_id,effect,assigned_by) "
                "VALUES (1,202,'invalid',101)"
            )
        conn.execute(
            "INSERT INTO panel_rep_assignments(panel_id,rep_id,effect,assigned_by) "
            "VALUES (1,202,'allow',101)"
        )
        conn.execute("INSERT INTO panel_counters(panel_id,next_value) VALUES (1,7)")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.commit()

        applied_state = (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM panels ORDER BY id").fetchall(),
            conn.execute("SELECT * FROM panel_rep_assignments ORDER BY panel_id,rep_id").fetchall(),
            conn.execute("SELECT * FROM panel_counters ORDER BY panel_id").fetchall(),
        )
        assert apply_migrations(conn, migration_dir, now=1794103600) == []
        assert applied_state == (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM panels ORDER BY id").fetchall(),
            conn.execute("SELECT * FROM panel_rep_assignments ORDER BY panel_id,rep_id").fetchall(),
            conn.execute("SELECT * FROM panel_counters ORDER BY panel_id").fetchall(),
        )
        assert _snapshot_tables(conn, old_state.keys()) == old_state
        assert conn.execute(
            "SELECT owner_tg_id FROM accounts WHERE id=?", (account["account_id"],)
        ).fetchone() == (actor["actor_tg_id"],)
        assert conn.execute(
            "SELECT balance_toman FROM representatives WHERE id=202"
        ).fetchone() == (13579,)
        assert conn.execute(
            "SELECT amount,description FROM financial_ledger WHERE account_code=?",
            (account["account_code"],),
        ).fetchall() == [(13579, "W02 legacy fixture")]
        conn.close()


def test_migration_003_accounts(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers, database, sentinel = _helpers(root, tmp_path, monkeypatch)

    with helpers.make_db(database, monkeypatch, tmp_path) as db_path:
        assert Path(sentinel) != Path(db_path) and not Path(sentinel).exists()
        actor, account = _seed_legacy(helpers, database)
        with database.get_db() as seed:
            seed.execute(
                "INSERT INTO reserved_packages "
                "(rep_id,account_code,email,plan_code,plan_name,gb,days,charge_amount,actor_id,actor_name) "
                "VALUES (202,? ,?,'legacy-plan','Legacy Plan',5,30,25000,101,'synthetic')",
                (account["account_code"], account["email"]),
            )
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        reserved_before = conn.execute(
            "SELECT id,rep_id,account_code,email,plan_code,plan_name,gb,days,"
            "on_first_connect,charge_amount,actor_id,actor_name,status,created_at,activated_at "
            "FROM reserved_packages ORDER BY id"
        ).fetchall()
        ledger_before = conn.execute(
            "SELECT rep_id,account_code,email,plan_code,transaction_type,amount,balance_after,"
            "description,actor_id,actor_name FROM financial_ledger ORDER BY id"
        ).fetchall()
        rep_before = conn.execute(
            "SELECT id,rep_code,balance_toman FROM representatives WHERE id=202"
        ).fetchone()
        migration_dir = _migration_subset(
            root, tmp_path, ("001_policy", "002_panels", "003_accounts")
        )
        assert apply_migrations(conn, migration_dir, now=1794110000) == [
            "001_policy", "002_panels", "003_accounts"
        ]
        expected_account_columns = [
            "id", "rep_id", "account_code", "email", "customer_name", "sub_id",
            "enable", "total_gb", "expiry_time", "owner_tg_id", "created_at",
            "updated_at", "first_connect_mode", "rollover_enabled", "panel_id",
            "sub_id_cipher", "seller_key", "revision", "acl_revision", "is_test",
            "is_personal_use", "deleted_at",
        ]
        assert [r[1] for r in conn.execute("PRAGMA table_info(accounts)")] == expected_account_columns
        preserved = conn.execute(
            "SELECT id,rep_id,account_code,email,customer_name,sub_id,enable,total_gb,"
            "expiry_time,owner_tg_id,first_connect_mode,rollover_enabled,panel_id,"
            "sub_id_cipher,seller_key,revision,acl_revision,is_test,is_personal_use,deleted_at "
            "FROM accounts WHERE id=?", (account["account_id"],)
        ).fetchone()
        assert preserved == (
            303, 202, account["account_code"], account["email"], "V52 Synthetic Customer 303",
            account["sub_id"], 1, 10.0, 0, actor["actor_tg_id"], 0, 1, None,
            None, "root", 1, 1, 0, 0, None,
        )
        reserved_after = conn.execute(
            "SELECT id,rep_id,account_code,email,plan_code,plan_name,gb,days,"
            "on_first_connect,charge_amount,actor_id,actor_name,status,created_at,activated_at "
            "FROM reserved_packages ORDER BY id"
        ).fetchall()
        assert reserved_after == reserved_before
        assert conn.execute(
            "SELECT panel_id,account_id FROM reserved_packages"
        ).fetchall() == [(None, None)]
        assert conn.execute(
            "SELECT rep_id,account_code,email,plan_code,transaction_type,amount,balance_after,"
            "description,actor_id,actor_name FROM financial_ledger ORDER BY id"
        ).fetchall() == ledger_before
        assert conn.execute(
            "SELECT id,rep_code,balance_toman FROM representatives WHERE id=202"
        ).fetchone() == rep_before
        conn.execute(
            "INSERT INTO panels(id,panel_code,name,panel_type) VALUES (77,'new-panel','New','3x-ui')"
        )
        conn.execute("UPDATE accounts SET panel_id=77 WHERE id=?", (account["account_id"],))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO accounts(account_code,email,panel_id) VALUES ('DUP','V52-W02-LEGACY@EXAMPLE.INVALID',77)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE accounts SET panel_id=999 WHERE id=?", (account["account_id"],))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE reserved_packages SET account_id=999 WHERE id=1")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.commit()

        state_after = (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM accounts ORDER BY id").fetchall(),
            conn.execute("SELECT * FROM reserved_packages ORDER BY id").fetchall(),
        )
        assert apply_migrations(conn, migration_dir, now=1794113600) == []
        assert state_after == (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM accounts ORDER BY id").fetchall(),
            conn.execute("SELECT * FROM reserved_packages ORDER BY id").fetchall(),
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.close()


def test_migration_004_plans(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers, database, sentinel = _helpers(root, tmp_path, monkeypatch)
    with helpers.make_db(database, monkeypatch, tmp_path) as db_path:
        assert Path(sentinel) != Path(db_path) and not Path(sentinel).exists()
        actor, account = _seed_legacy(helpers, database)
        previous = _migration_subset(root, tmp_path, ("001_policy","002_panels","003_accounts"))
        assert apply_migrations(sqlite3.connect(str(db_path)), previous, now=1794200000) == [
            "001_policy","002_panels","003_accounts"
        ]
        with database.get_db() as seed:
            seed.executemany(
                "INSERT INTO pricing_plans "
                "(plan_code,name,gb,days,rep_price,user_price,is_quick,sort_order,is_active) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                [("W02-PLAN-A","Legacy active",10,30,10000,15000,0,1,1),
                 ("W02-PLAN-H","Legacy hidden",20,60,18000,24000,1,2,0)]
            )
        conn=sqlite3.connect(str(db_path)); conn.execute("PRAGMA foreign_keys=ON")
        prior_tables=[n for n in _snapshot_tables(conn) if n not in {"pricing_plans","schema_migrations"}]
        old_state=_snapshot_tables(conn,prior_tables)
        plans_before=conn.execute(
            "SELECT plan_code,name,gb,days,rep_price,user_price,is_quick,sort_order,is_active "
            "FROM pricing_plans ORDER BY plan_code"
        ).fetchall()
        migration_dir=_migration_subset(root,tmp_path,("001_policy","002_panels","003_accounts","004_plans"))
        assert apply_migrations(conn,migration_dir,now=1794203600)==["004_plans"]
        assert [r[1] for r in conn.execute("PRAGMA table_info(pricing_plans)")] == [
            "plan_code","name","gb","days","rep_price","user_price","is_quick","sort_order",
            "is_active","panel_scope","sale_status","first_purchase_only","revision"
        ]
        plans_after=conn.execute(
            "SELECT plan_code,name,gb,days,rep_price,user_price,is_quick,sort_order,is_active,"
            "panel_scope,sale_status,first_purchase_only,revision FROM pricing_plans ORDER BY plan_code"
        ).fetchall()
        assert [row[:9] for row in plans_after] == plans_before
        assert all(row[10] == ("active" if row[8] == 1 else "hidden") for row in plans_after)
        assert [row[0:1]+row[8:13] for row in plans_after if row[0] in ("W02-PLAN-A","W02-PLAN-H")] == [
            ("W02-PLAN-A",1,"selected","active",0,1),
            ("W02-PLAN-H",0,"selected","hidden",0,1)
        ]
        assert [r[1] for r in conn.execute("PRAGMA table_info(plan_panels)")] == ["plan_code","panel_id","allowed"]
        assert [r[1] for r in conn.execute("PRAGMA table_info(tier_policies)")] == [
            "actor_type","tier_code","title_fa","rank","discount_bps","cashback_bps",
            "daily_test_quota","default_credit_limit","min_monthly_sales_toman"
        ]
        assert [r[1] for r in conn.execute("PRAGMA table_info(rep_custom_prices)")] == [
            "seller_key","buyer_rep_id","plan_code","price_toman","revision"
        ]
        assert _snapshot_tables(conn,prior_tables)==old_state
        conn.execute("INSERT INTO panels(id,panel_code,name,panel_type) VALUES (71,'plans-panel','Plans','3x-ui')")
        conn.execute("INSERT INTO plan_panels(plan_code,panel_id,allowed) VALUES ('W02-PLAN-A',71,1)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO plan_panels(plan_code,panel_id,allowed) VALUES ('W02-PLAN-A',71,2)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO plan_panels(plan_code,panel_id,allowed) VALUES ('W02-PLAN-A',999,1)")
        conn.execute(
            "INSERT INTO tier_policies(actor_type,tier_code,title_fa,rank,discount_bps,cashback_bps) "
            "VALUES ('rep','silver','silver',2,1250,250)"
        )
        for actor_type in ("admin","representative"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO tier_policies(actor_type,tier_code,title_fa,rank) VALUES (?,?,?,1)",
                    (actor_type,"bad-"+actor_type,"invalid")
                )
        for col,val in (("discount_bps",-1),("discount_bps",10001),("cashback_bps",-1),("cashback_bps",10001)):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    f"INSERT INTO tier_policies(actor_type,tier_code,title_fa,rank,{col}) "
                    "VALUES ('end_user',?,'invalid',1,?)",(f"bad-{col}-{val}",val)
                )
        conn.execute(
            "INSERT INTO rep_custom_prices(seller_key,buyer_rep_id,plan_code,price_toman) "
            "VALUES ('rep:202',202,'W02-PLAN-A',9000)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO rep_custom_prices(seller_key,buyer_rep_id,plan_code,price_toman) "
                "VALUES ('rep:202',202,'W02-PLAN-H',-1)"
            )
        assert conn.execute("PRAGMA foreign_key_check").fetchall()==[]
        conn.commit()

        assert conn.execute("SELECT owner_tg_id FROM accounts WHERE id=?",(account["account_id"],)).fetchone()==(actor["actor_tg_id"],)
        assert conn.execute(
            "SELECT amount,description FROM financial_ledger WHERE account_code=?",(account["account_code"],)
        ).fetchall()==[(13579,"W02 legacy fixture")]
        stable=(conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
                conn.execute("SELECT * FROM pricing_plans ORDER BY plan_code").fetchall(),
                conn.execute("SELECT * FROM plan_panels ORDER BY plan_code,panel_id").fetchall(),
                conn.execute("SELECT * FROM tier_policies ORDER BY actor_type,tier_code").fetchall(),
                conn.execute("SELECT * FROM rep_custom_prices ORDER BY seller_key,buyer_rep_id,plan_code").fetchall())
        assert apply_migrations(conn,migration_dir,now=1794207200)==[]
        assert stable==(conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
                        conn.execute("SELECT * FROM pricing_plans ORDER BY plan_code").fetchall(),
                        conn.execute("SELECT * FROM plan_panels ORDER BY plan_code,panel_id").fetchall(),
                        conn.execute("SELECT * FROM tier_policies ORDER BY actor_type,tier_code").fetchall(),
                        conn.execute("SELECT * FROM rep_custom_prices ORDER BY seller_key,buyer_rep_id,plan_code").fetchall())
        assert conn.execute("PRAGMA foreign_key_check").fetchall()==[]
        conn.close()


def test_migration_005_tree(tmp_path, monkeypatch):
    root=Path(__file__).resolve().parents[1]
    helpers,database,sentinel=_helpers(root,tmp_path,monkeypatch)
    with helpers.make_db(database,monkeypatch,tmp_path) as db_path:
        assert Path(sentinel)!=Path(db_path) and not Path(sentinel).exists()
        actor,account=_seed_legacy(helpers,database)
        previous=_migration_subset(root,tmp_path,("001_policy","002_panels","003_accounts","004_plans"))
        conn=sqlite3.connect(str(db_path)); conn.execute("PRAGMA foreign_keys=ON")
        assert apply_migrations(conn,previous,now=1794210000)==["001_policy","002_panels","003_accounts","004_plans"]
        rep_before=conn.execute(
            "SELECT id,rep_code,name,telegram_id,scope_filter,balance_toman,is_active,credit_limit_toman "
            "FROM representatives WHERE id=202"
        ).fetchone()
        ledger_before=conn.execute(
            "SELECT rep_id,account_code,email,plan_code,transaction_type,amount,balance_after,description,actor_id,actor_name "
            "FROM financial_ledger ORDER BY id"
        ).fetchall()
        migration_dir=_migration_subset(root,tmp_path,("001_policy","002_panels","003_accounts","004_plans","005_tree"))
        assert apply_migrations(conn,migration_dir,now=1794213600)==["005_tree"]
        assert [r[1] for r in conn.execute("PRAGMA table_info(representatives)")][-8:]==[
            "parent_rep_id","tier","billing_mode","can_create_sub_reps","max_sub_reps","custom_prefix","markup_bps","deleted_at"
        ]
        assert [r[1] for r in conn.execute("PRAGMA table_info(rep_edges)")] == [
            "id","parent_key","child_rep_id","billing_mode","debt_toman","credit_limit_toman","active","started_at","ended_at"
        ]
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='index' AND name='ux_rep_active_edge'").fetchone()==("ux_rep_active_edge",)
        assert conn.execute(
            "SELECT tier,billing_mode,can_create_sub_reps,max_sub_reps,custom_prefix,markup_bps,parent_rep_id,deleted_at "
            "FROM representatives WHERE id=202"
        ).fetchone()==("normal","credit",0,0,"",0,None,None)
        conn.execute(
            "INSERT INTO representatives(id,rep_code,name,telegram_id,group_id,topic_id,scope_filter) "
            "VALUES (404,'V52-PARENT-404','V52 Parent 404',404,0,0,'v52-parent')"
        )
        conn.execute("UPDATE representatives SET parent_rep_id=404 WHERE id=202")
        with pytest.raises(sqlite3.IntegrityError): conn.execute("UPDATE representatives SET parent_rep_id=999 WHERE id=202")
        with pytest.raises(sqlite3.IntegrityError): conn.execute("UPDATE representatives SET billing_mode='wire' WHERE id=202")
        with pytest.raises(sqlite3.IntegrityError): conn.execute("UPDATE representatives SET max_sub_reps=-1 WHERE id=202")
        with pytest.raises(sqlite3.IntegrityError): conn.execute("UPDATE representatives SET markup_bps=-1 WHERE id=202")
        conn.execute("INSERT INTO rep_edges(id,parent_key,child_rep_id,billing_mode,started_at) VALUES (1,'root',202,'credit',1794213600)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO rep_edges(id,parent_key,child_rep_id,billing_mode,started_at) VALUES (2,'root',202,'prepaid_wallet',1794213601)")
        conn.execute("INSERT INTO rep_edges(id,parent_key,child_rep_id,billing_mode,active,started_at) VALUES (3,'history',202,'prepaid_wallet',0,1794213602)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO rep_edges(id,parent_key,child_rep_id,billing_mode,started_at) VALUES (4,'root',202,'wallet',1794213603)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO rep_edges(id,parent_key,child_rep_id,billing_mode,credit_limit_toman,started_at) VALUES (5,'root',202,'credit',-1,1794213604)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO rep_edges(id,parent_key,child_rep_id,billing_mode,active,started_at) VALUES (6,'root',404,'credit',2,1794213605)")
        assert conn.execute("PRAGMA foreign_key_check").fetchall()==[]
        assert conn.execute(
            "SELECT id,rep_code,name,telegram_id,scope_filter,balance_toman,is_active,credit_limit_toman "
            "FROM representatives WHERE id=202"
        ).fetchone()==rep_before
        assert conn.execute("SELECT owner_tg_id FROM accounts WHERE id=?",(account["account_id"],)).fetchone()==(actor["actor_tg_id"],)
        assert conn.execute(
            "SELECT rep_id,account_code,email,plan_code,transaction_type,amount,balance_after,description,actor_id,actor_name "
            "FROM financial_ledger ORDER BY id"
        ).fetchall()==ledger_before
        conn.commit()
        stable=(conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
                conn.execute("SELECT * FROM rep_edges ORDER BY id").fetchall(),
                conn.execute("SELECT * FROM representatives ORDER BY id").fetchall())
        assert apply_migrations(conn,migration_dir,now=1794217200)==[]
        assert stable==(conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
                        conn.execute("SELECT * FROM rep_edges ORDER BY id").fetchall(),
                        conn.execute("SELECT * FROM representatives ORDER BY id").fetchall())
        assert conn.execute("PRAGMA foreign_key_check").fetchall()==[]
        conn.close()


def test_migration_006_bots(tmp_path, monkeypatch):
    root=Path(__file__).resolve().parents[1]
    helpers,database,sentinel=_helpers(root,tmp_path,monkeypatch)
    with helpers.make_db(database,monkeypatch,tmp_path) as db_path:
        assert Path(sentinel)!=Path(db_path) and not Path(sentinel).exists()
        actor,account=_seed_legacy(helpers,database)
        previous=_migration_subset(root,tmp_path,("001_policy","002_panels","003_accounts","004_plans","005_tree"))
        conn=sqlite3.connect(str(db_path)); conn.execute("PRAGMA foreign_keys=ON")
        assert apply_migrations(conn,previous,now=1794220000)==[
            "001_policy","002_panels","003_accounts","004_plans","005_tree"
        ]
        prior_tables=[n for n in _snapshot_tables(conn) if n!="schema_migrations"]
        old_state=_snapshot_tables(conn,prior_tables)
        rep_before=conn.execute("SELECT id,rep_code,balance_toman FROM representatives WHERE id=202").fetchone()
        ledger_before=conn.execute(
            "SELECT rep_id,account_code,email,plan_code,transaction_type,amount,balance_after,description,actor_id,actor_name "
            "FROM financial_ledger ORDER BY id"
        ).fetchall()
        migration_dir=_migration_subset(root,tmp_path,("001_policy","002_panels","003_accounts","004_plans","005_tree","006_bots"))
        assert apply_migrations(conn,migration_dir,now=1794223600)==["006_bots"]
        assert [r[1] for r in conn.execute("PRAGMA table_info(bot_registry)")] == [
            "bot_id","rep_id","is_primary","token_cipher","username","status","verified_at","revision"
        ]
        assert [r[1] for r in conn.execute("PRAGMA table_info(bot_customers)")] == ["bot_id","user_tg_id","started_at"]
        assert [r[1] for r in conn.execute("PRAGMA table_info(merchant_wallets)")] == ["merchant_key","user_tg_id","balance_toman"]
        indexes={r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name IN ('ux_primary_bot','ux_active_rep_bot')"
        )}
        assert indexes=={"ux_primary_bot","ux_active_rep_bot"}
        conn.execute("INSERT INTO bot_registry(bot_id,rep_id,is_primary,username,status) VALUES (1,202,1,'primary','active')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO bot_registry(bot_id,rep_id,is_primary,username,status) VALUES (2,NULL,1,'second-primary','pending')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO bot_registry(bot_id,rep_id,is_primary,username,status) VALUES (3,202,0,'second-active','active')")
        conn.execute("INSERT INTO bot_registry(bot_id,rep_id,username,status) VALUES (4,202,'pending','pending')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO bot_registry(bot_id,rep_id,username,status) VALUES (5,404,'bad-status','unknown')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO bot_registry(bot_id,rep_id,is_primary,username,status) VALUES (6,404,2,'bad-primary','disabled')")
        conn.execute("INSERT INTO bot_customers(bot_id,user_tg_id,started_at) VALUES (1,101,1794223600)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO bot_customers(bot_id,user_tg_id,started_at) VALUES (999,102,1794223600)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO bot_customers(bot_id,user_tg_id,started_at) VALUES (1,101,1794223601)")
        conn.execute("INSERT INTO merchant_wallets(merchant_key,user_tg_id,balance_toman) VALUES ('merchant:test',101,5000)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO merchant_wallets(merchant_key,user_tg_id,balance_toman) VALUES ('root',101,0)")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO merchant_wallets(merchant_key,user_tg_id,balance_toman) VALUES ('merchant:test',102,-1)")
        assert conn.execute("PRAGMA foreign_key_check").fetchall()==[]
        assert _snapshot_tables(conn,prior_tables)==old_state
        assert conn.execute("SELECT owner_tg_id FROM accounts WHERE id=?",(account["account_id"],)).fetchone()==(actor["actor_tg_id"],)
        assert conn.execute("SELECT id,rep_code,balance_toman FROM representatives WHERE id=202").fetchone()==rep_before
        assert conn.execute(
            "SELECT rep_id,account_code,email,plan_code,transaction_type,amount,balance_after,description,actor_id,actor_name "
            "FROM financial_ledger ORDER BY id"
        ).fetchall()==ledger_before
        conn.commit()
        stable=(conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
                conn.execute("SELECT * FROM bot_registry ORDER BY bot_id").fetchall(),
                conn.execute("SELECT * FROM bot_customers ORDER BY bot_id,user_tg_id").fetchall(),
                conn.execute("SELECT * FROM merchant_wallets ORDER BY merchant_key,user_tg_id").fetchall())
        assert apply_migrations(conn,migration_dir,now=1794227200)==[]
        assert stable==(conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
                        conn.execute("SELECT * FROM bot_registry ORDER BY bot_id").fetchall(),
                        conn.execute("SELECT * FROM bot_customers ORDER BY bot_id,user_tg_id").fetchall(),
                        conn.execute("SELECT * FROM merchant_wallets ORDER BY merchant_key,user_tg_id").fetchall())
        assert conn.execute("PRAGMA foreign_key_check").fetchall()==[]
        conn.close()


def test_migration_007_access(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers, database, sentinel = _helpers(root, tmp_path, monkeypatch)

    with helpers.make_db(database, monkeypatch, tmp_path) as db_path:
        assert Path(sentinel) != Path(db_path) and not Path(sentinel).exists()
        actor, account = _seed_legacy(helpers, database)
        prior_names = (
            "001_policy", "002_panels", "003_accounts", "004_plans",
            "005_tree", "006_bots",
        )
        prior_dir = _migration_subset(root, tmp_path, prior_names)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        assert apply_migrations(conn, prior_dir, now=1794230000) == list(prior_names)

        account_before = conn.execute(
            "SELECT id,owner_tg_id,account_code,email,sub_id,total_gb,expiry_time "
            "FROM accounts WHERE id=?", (account["account_id"],)
        ).fetchone()
        ledger_before = conn.execute(
            "SELECT id,rep_id,user_id,account_code,email,plan_code,transaction_type,"
            "amount,balance_after,description,actor_id,actor_name,created_at "
            "FROM financial_ledger ORDER BY id"
        ).fetchall()
        target_names = prior_names + ("007_access",)
        target_dir = _migration_subset(root, tmp_path, target_names)
        assert apply_migrations(conn, target_dir, now=1794230300) == ["007_access"]

        bot_columns = {row[1]: row for row in conn.execute("PRAGMA table_info(bot_users)")}
        settings_columns = {row[1]: row for row in conn.execute("PRAGMA table_info(user_settings)")}
        assert "tier" in bot_columns and bot_columns["tier"][3] == 1
        assert bot_columns["tier"][4] == "'normal'"
        assert "active_persona" in settings_columns and settings_columns["active_persona"][3] == 1
        assert settings_columns["active_persona"][4] == "'end_user'"
        assert conn.execute(
            "SELECT tier FROM bot_users WHERE telegram_id=?", (actor["actor_tg_id"],)
        ).fetchone() == ("normal",)
        conn.execute(
            "INSERT INTO user_settings(user_id) VALUES (?)", (actor["actor_tg_id"],)
        )
        assert conn.execute(
            "SELECT active_persona FROM user_settings WHERE user_id=?",
            (actor["actor_tg_id"],),
        ).fetchone() == ("end_user",)

        assert [row[1] for row in conn.execute("PRAGMA table_info(account_access_grants)")] == [
            "account_id", "grantee_tg_id", "access_level", "granted_by_tg_id",
            "created_at", "revoked_at", "revision",
        ]
        assert [row[1] for row in conn.execute("PRAGMA table_info(daily_test_usage)")] == [
            "actor_tg_id", "tehran_date", "used",
        ]
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_grants_user'"
        ).fetchone() == ("idx_grants_user",)

        grant = (account["account_id"], 404, "viewer", actor["actor_tg_id"], 1794230300)
        conn.execute(
            "INSERT INTO account_access_grants "
            "(account_id,grantee_tg_id,access_level,granted_by_tg_id,created_at) "
            "VALUES (?,?,?,?,?)", grant
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO account_access_grants "
                "(account_id,grantee_tg_id,access_level,granted_by_tg_id,created_at) "
                "VALUES (?,?,?,?,?)",
                (account["account_id"], 405, "owner", actor["actor_tg_id"], 1794230300),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO account_access_grants "
                "(account_id,grantee_tg_id,access_level,granted_by_tg_id,created_at) "
                "VALUES (?,?,?,?,?)", grant
            )
        conn.execute(
            "INSERT INTO daily_test_usage(actor_tg_id,tehran_date) VALUES (?,?)",
            (actor["actor_tg_id"], "2026-11-08"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO daily_test_usage(actor_tg_id,tehran_date,used) VALUES (?,?,?)",
                (actor["actor_tg_id"], "2026-11-09", -1),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO daily_test_usage(actor_tg_id,tehran_date) VALUES (?,?)",
                (actor["actor_tg_id"], "2026-11-08"),
            )

        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "SELECT id,owner_tg_id,account_code,email,sub_id,total_gb,expiry_time "
            "FROM accounts WHERE id=?", (account["account_id"],)
        ).fetchone() == account_before
        assert conn.execute(
            "SELECT id,rep_id,user_id,account_code,email,plan_code,transaction_type,"
            "amount,balance_after,description,actor_id,actor_name,created_at "
            "FROM financial_ledger ORDER BY id"
        ).fetchall() == ledger_before
        conn.commit()
        stable = (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM account_access_grants ORDER BY account_id,grantee_tg_id").fetchall(),
            conn.execute("SELECT * FROM daily_test_usage ORDER BY actor_tg_id,tehran_date").fetchall(),
        )
        assert apply_migrations(conn, target_dir, now=1794230600) == []
        assert stable == (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM account_access_grants ORDER BY account_id,grantee_tg_id").fetchall(),
            conn.execute("SELECT * FROM daily_test_usage ORDER BY actor_tg_id,tehran_date").fetchall(),
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.close()


def test_migration_008_orders(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers, database, sentinel = _helpers(root, tmp_path, monkeypatch)

    with helpers.make_db(database, monkeypatch, tmp_path) as db_path:
        assert Path(sentinel) != Path(db_path) and not Path(sentinel).exists()
        actor, account = _seed_legacy(helpers, database)
        prior_names = (
            "001_policy", "002_panels", "003_accounts", "004_plans",
            "005_tree", "006_bots", "007_access",
        )
        prior_dir = _migration_subset(root, tmp_path, prior_names)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        assert apply_migrations(conn, prior_dir, now=1794231000) == list(prior_names)

        account_before = conn.execute(
            "SELECT id,owner_tg_id,account_code,email,sub_id,total_gb,expiry_time "
            "FROM accounts WHERE id=?", (account["account_id"],)
        ).fetchone()
        ledger_before = conn.execute(
            "SELECT id,rep_id,user_id,account_code,email,plan_code,transaction_type,"
            "amount,balance_after,description,actor_id,actor_name,created_at "
            "FROM financial_ledger ORDER BY id"
        ).fetchall()
        target_names = prior_names + ("008_orders",)
        target_dir = _migration_subset(root, tmp_path, target_names)
        assert apply_migrations(conn, target_dir, now=1794231300) == ["008_orders"]

        assert [row[1] for row in conn.execute("PRAGMA table_info(quotes)")] == [
            "quote_id", "actor_tg_id", "bot_id", "merchant_key", "purpose",
            "account_id", "plan_code", "product_id", "panel_id", "amount_toman",
            "spec_json", "price_edges_json", "policy_version", "created_at",
            "expires_at", "fingerprint",
        ]
        assert [row[1] for row in conn.execute("PRAGMA table_info(payment_orders)")] == [
            "order_id", "quote_id", "actor_tg_id", "bot_id", "merchant_key",
            "purpose", "payment_rail", "amount_toman", "status", "received_toman",
            "created_at", "expires_at", "revision",
        ]
        assert [row[1] for row in conn.execute("PRAGMA table_info(capacity_reservations)")] == [
            "reservation_id", "order_id", "panel_id", "account_id", "state", "expires_at",
        ]
        conn.execute(
            "INSERT INTO bot_registry(bot_id,rep_id,username,status) VALUES (90908,202,'w02b03','pending')"
        )
        conn.execute(
            "INSERT INTO panels(id,panel_code,name,panel_type) VALUES (90908,'w02b03-panel','W02-B03','3x-ui')"
        )

        def add_quote(quote_id, created_at=1794231300, expires_at=None):
            expires = created_at + 1800 if expires_at is None else expires_at
            conn.execute(
                "INSERT INTO quotes "
                "(quote_id,actor_tg_id,bot_id,merchant_key,purpose,account_id,plan_code,"
                "product_id,panel_id,amount_toman,spec_json,price_edges_json,policy_version,"
                "created_at,expires_at,fingerprint) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    quote_id, actor["actor_tg_id"], 90908, "root", "vpn_buy",
                    account["account_id"], "legacy-seed", None, 90908, 0,
                    "{}", "[]", "v1.0", created_at, expires, "fp-" + quote_id,
                ),
            )

        with pytest.raises(sqlite3.IntegrityError):
            add_quote("bad-expiry", expires_at=1794233099)
        add_quote("quote-1")
        add_quote("quote-2")

        def add_order(order_id, quote_id, purpose="vpn_buy", rail="wallet",
                      status="awaiting_payment", amount=0):
            conn.execute(
                "INSERT INTO payment_orders "
                "(order_id,quote_id,actor_tg_id,bot_id,merchant_key,purpose,payment_rail,"
                "amount_toman,status,created_at,expires_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    order_id, quote_id, actor["actor_tg_id"], 90908, "root",
                    purpose, rail, amount, status, 1794231300, 1794233100,
                ),
            )

        with pytest.raises(sqlite3.IntegrityError):
            add_order("bad-purpose", "quote-1", purpose="unknown")
        with pytest.raises(sqlite3.IntegrityError):
            add_order("bad-rail", "quote-1", rail="cash")
        with pytest.raises(sqlite3.IntegrityError):
            add_order("bad-status", "quote-1", status="paid")
        with pytest.raises(sqlite3.IntegrityError):
            add_order("bad-amount", "quote-1", amount=-1)
        add_order("order-1", "quote-1")
        with pytest.raises(sqlite3.IntegrityError):
            add_order("duplicate-quote", "quote-1")
        add_order("order-2", "quote-2")

        conn.execute(
            "INSERT INTO capacity_reservations(reservation_id,order_id,panel_id,state,expires_at) "
            "VALUES ('reservation-1','order-1',90908,'active',1794233100)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO capacity_reservations(reservation_id,order_id,panel_id,state) "
                "VALUES ('bad-reservation','order-2',90908,'unknown')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO capacity_reservations(reservation_id,order_id,panel_id,state) "
                "VALUES ('duplicate-order-reservation','order-1',90908,'released')"
            )

        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "SELECT id,owner_tg_id,account_code,email,sub_id,total_gb,expiry_time "
            "FROM accounts WHERE id=?", (account["account_id"],)
        ).fetchone() == account_before
        assert conn.execute(
            "SELECT id,rep_id,user_id,account_code,email,plan_code,transaction_type,"
            "amount,balance_after,description,actor_id,actor_name,created_at "
            "FROM financial_ledger ORDER BY id"
        ).fetchall() == ledger_before
        conn.commit()
        stable = (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM quotes ORDER BY quote_id").fetchall(),
            conn.execute("SELECT * FROM payment_orders ORDER BY order_id").fetchall(),
            conn.execute("SELECT * FROM capacity_reservations ORDER BY reservation_id").fetchall(),
        )
        assert apply_migrations(conn, target_dir, now=1794231600) == []
        assert stable == (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM quotes ORDER BY quote_id").fetchall(),
            conn.execute("SELECT * FROM payment_orders ORDER BY order_id").fetchall(),
            conn.execute("SELECT * FROM capacity_reservations ORDER BY reservation_id").fetchall(),
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.close()


def test_migration_009_finance(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers, database, sentinel = _helpers(root, tmp_path, monkeypatch)

    with helpers.make_db(database, monkeypatch, tmp_path) as db_path:
        assert Path(sentinel) != Path(db_path) and not Path(sentinel).exists()
        actor, account = _seed_legacy(helpers, database)
        prior_names = (
            "001_policy", "002_panels", "003_accounts", "004_plans",
            "005_tree", "006_bots", "007_access", "008_orders",
        )
        prior_dir = _migration_subset(root, tmp_path, prior_names)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        assert apply_migrations(conn, prior_dir, now=1794232000) == list(prior_names)

        legacy_columns = (
            "id,rep_id,user_id,account_code,email,plan_code,transaction_type,amount,"
            "balance_after,description,actor_id,actor_name,created_at"
        )
        legacy_ledger_before = conn.execute(
            f"SELECT {legacy_columns} FROM financial_ledger ORDER BY id"
        ).fetchall()
        legacy_count = conn.execute("SELECT COUNT(*) FROM financial_ledger").fetchone()[0]
        target_names = prior_names + ("009_finance",)
        target_dir = _migration_subset(root, tmp_path, target_names)
        assert apply_migrations(conn, target_dir, now=1794232300) == ["009_finance"]

        ledger_columns = {row[1]: row for row in conn.execute("PRAGMA table_info(financial_ledger)")}
        for name in (
            "business_key", "merchant_key", "ledger_scope", "edge_id", "order_id",
            "operation_id", "reversal_of", "policy_version",
        ):
            assert name in ledger_columns
        assert ledger_columns["merchant_key"][4] == "'root'"
        assert ledger_columns["ledger_scope"][4] == "'legacy'"
        assert conn.execute(
            "SELECT merchant_key,ledger_scope,business_key,edge_id,order_id,operation_id,"
            "reversal_of,policy_version FROM financial_ledger ORDER BY id LIMIT 1"
        ).fetchone() == ("root", "legacy", None, None, None, None, None, None)
        index = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_ledger_business_key'"
        ).fetchone()
        assert index is not None and "WHERE business_key IS NOT NULL" in index[0]
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_holds_source'"
        ).fetchone() == ("idx_holds_source",)
        assert [row[1] for row in conn.execute("PRAGMA table_info(financial_holds)")] == [
            "hold_id", "order_id", "source_key", "amount_toman", "state",
            "created_at", "expires_at", "captured_ledger_id",
        ]

        conn.execute(
            "INSERT INTO bot_registry(bot_id,rep_id,username,status) VALUES (90909,202,'w02b03-finance','pending')"
        )
        conn.execute(
            "INSERT INTO quotes "
            "(quote_id,actor_tg_id,bot_id,merchant_key,purpose,amount_toman,spec_json,"
            "price_edges_json,policy_version,created_at,expires_at,fingerprint) "
            "VALUES ('finance-quote',101,90909,'root','wallet_topup',100,'{}','[]','v1.0',"
            "1794232300,1794234100,'finance-fingerprint')"
        )
        conn.execute(
            "INSERT INTO payment_orders "
            "(order_id,quote_id,actor_tg_id,bot_id,merchant_key,purpose,payment_rail,"
            "amount_toman,status,created_at,expires_at) "
            "VALUES ('finance-order','finance-quote',101,90909,'root','wallet_topup','wallet',"
            "100,'awaiting_payment',1794232300,1794234100)"
        )
        conn.execute(
            "INSERT INTO financial_holds(hold_id,order_id,source_key,amount_toman,state,created_at) "
            "VALUES ('hold-1','finance-order','wallet:root:101',50,'active',1794232300)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO financial_holds(hold_id,order_id,source_key,amount_toman,state,created_at) "
                "VALUES ('bad-hold-amount','finance-order','wallet:root:102',0,'active',1794232300)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO financial_holds(hold_id,order_id,source_key,amount_toman,state,created_at) "
                "VALUES ('bad-hold-state','finance-order','wallet:root:103',1,'unknown',1794232300)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO financial_holds(hold_id,order_id,source_key,amount_toman,state,created_at) "
                "VALUES ('duplicate-hold','finance-order','wallet:root:101',50,'active',1794232300)"
            )

        conn.execute(
            "INSERT INTO financial_ledger "
            "(transaction_type,amount,balance_after,description,actor_id,business_key) "
            "VALUES ('credit',1,1,'W02-B03 business key fixture',101,'w02-b03-key-1')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO financial_ledger "
                "(transaction_type,amount,balance_after,description,actor_id,business_key) "
                "VALUES ('credit',2,2,'duplicate business key fixture',101,'w02-b03-key-1')"
            )
        conn.execute(
            "INSERT INTO financial_ledger "
            "(transaction_type,amount,balance_after,description,actor_id) "
            "VALUES ('credit',3,3,'NULL business key fixture A',101)"
        )
        conn.execute(
            "INSERT INTO financial_ledger "
            "(transaction_type,amount,balance_after,description,actor_id) "
            "VALUES ('credit',4,4,'NULL business key fixture B',101)"
        )

        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT COUNT(*) FROM financial_ledger").fetchone()[0] >= legacy_count + 3
        assert conn.execute(
            f"SELECT {legacy_columns} FROM financial_ledger WHERE id IN "
            "(SELECT id FROM financial_ledger ORDER BY id LIMIT ?) ORDER BY id",
            (legacy_count,),
        ).fetchall() == legacy_ledger_before
        conn.commit()
        replay_state = (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM financial_holds ORDER BY hold_id").fetchall(),
            conn.execute("SELECT id,business_key,merchant_key,ledger_scope FROM financial_ledger ORDER BY id").fetchall(),
        )
        assert apply_migrations(conn, target_dir, now=1794232600) == []
        assert replay_state == (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM financial_holds ORDER BY hold_id").fetchall(),
            conn.execute("SELECT id,business_key,merchant_key,ledger_scope FROM financial_ledger ORDER BY id").fetchall(),
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.close()


def test_migration_010_payments(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers, database, sentinel = _helpers(root, tmp_path, monkeypatch)

    with helpers.make_db(database, monkeypatch, tmp_path) as db_path:
        assert Path(sentinel) != Path(db_path) and not Path(sentinel).exists()
        actor, account = _seed_legacy(helpers, database)
        prior_names = (
            "001_policy", "002_panels", "003_accounts", "004_plans",
            "005_tree", "006_bots", "007_access", "008_orders", "009_finance",
        )
        prior_dir = _migration_subset(root, tmp_path, prior_names)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        assert apply_migrations(conn, prior_dir, now=1794233000) == list(prior_names)

        legacy_columns = (
            "id,tracking_code,user_id,role,rep_id,amount_toman,receipt_photo_id,"
            "receipt_note,status,rejection_reason,reviewed_by,reviewed_at,"
            "cancelled_by,cancelled_at,created_at"
        )
        conn.execute(
            "INSERT INTO payment_receipts "
            "(tracking_code,user_id,role,amount_toman,receipt_note,status) "
            "VALUES ('W02-B04-LEGACY-RECEIPT',101,'end_user',2468,'legacy-marker','pending')"
        )
        receipt_before = conn.execute(
            f"SELECT {legacy_columns} FROM payment_receipts ORDER BY id"
        ).fetchall()
        account_before = conn.execute(
            "SELECT id,owner_tg_id,account_code,email,sub_id,total_gb,expiry_time "
            "FROM accounts WHERE id=?", (account["account_id"],)
        ).fetchone()
        ledger_before = conn.execute(
            "SELECT id,rep_id,user_id,account_code,email,plan_code,transaction_type,"
            "amount,balance_after,description,actor_id,actor_name,created_at "
            "FROM financial_ledger ORDER BY id"
        ).fetchall()

        conn.execute(
            "INSERT INTO bot_registry(bot_id,rep_id,username,status) "
            "VALUES (91010,202,'w02b04-payments','pending')"
        )
        conn.execute(
            "INSERT INTO quotes "
            "(quote_id,actor_tg_id,bot_id,merchant_key,purpose,amount_toman,spec_json,"
            "price_edges_json,policy_version,created_at,expires_at,fingerprint) "
            "VALUES ('w02b04-payment-quote',101,91010,'root','wallet_topup',2468,"
            "'{}','[]','v1.0',1794233000,1794234800,'w02b04-payment-fingerprint')"
        )
        conn.execute(
            "INSERT INTO payment_orders "
            "(order_id,quote_id,actor_tg_id,bot_id,merchant_key,purpose,payment_rail,"
            "amount_toman,status,created_at,expires_at) "
            "VALUES ('w02b04-payment-order','w02b04-payment-quote',101,91010,'root',"
            "'wallet_topup','card_to_card',2468,'awaiting_payment',1794233000,1794234800)"
        )

        conn.commit()
        target_names = prior_names + ("010_payments",)
        target_dir = _migration_subset(root, tmp_path, target_names)
        assert apply_migrations(conn, target_dir, now=1794233300) == ["010_payments"]

        assert [row[1] for row in conn.execute("PRAGMA table_info(bank_cards)")] == [
            "id", "merchant_key", "card_number_cipher", "sheba_cipher", "card_holder",
            "bank_name", "target_scope", "priority", "daily_limit_toman", "is_active",
        ]
        assert [row[1] for row in conn.execute("PRAGMA table_info(payment_attempts)")] == [
            "attempt_id", "order_id", "rail", "merchant_key", "assigned_card_id",
            "amount_toman", "state", "external_payment_key", "evidence_cipher",
            "created_at", "verified_at",
        ]
        receipt_columns = [row[1] for row in conn.execute("PRAGMA table_info(payment_receipts)")]
        assert receipt_columns[-6:] == [
            "order_id", "payment_attempt_id", "bot_id", "bank_card_id",
            "merchant_key", "submitted_tehran_date",
        ]
        receipt_info = {
            row[1]: row for row in conn.execute("PRAGMA table_info(payment_receipts)")
        }
        assert receipt_info["merchant_key"][3] == 1
        assert receipt_info["merchant_key"][4] == "'root'"
        assert conn.execute(
            "SELECT merchant_key,order_id,payment_attempt_id,bot_id,bank_card_id,submitted_tehran_date "
            "FROM payment_receipts WHERE tracking_code='W02-B04-LEGACY-RECEIPT'"
        ).fetchone() == ("root", None, None, None, None, None)

        card_index = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='ux_receipt_attempt'"
        ).fetchone()
        assert card_index is not None and "WHERE payment_attempt_id IS NOT NULL" in card_index[0]
        daily_index = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name='idx_receipt_daily_card'"
        ).fetchone()
        assert daily_index is not None and (
            "bank_card_id,submitted_tehran_date,status" in daily_index[0]
        )

        conn.execute(
            "INSERT INTO bank_cards "
            "(id,merchant_key,card_number_cipher,card_holder,bank_name,daily_limit_toman) "
            "VALUES (91010,'root','cipher-card','Synthetic Holder','Synthetic Bank',5000000)"
        )
        conn.execute(
            "INSERT INTO payment_attempts "
            "(attempt_id,order_id,rail,merchant_key,assigned_card_id,amount_toman,state,"
            "external_payment_key,created_at) "
            "VALUES ('w02b04-attempt-1','w02b04-payment-order','card_to_card','root',"
            "91010,2468,'pending','w02b04-external-1',1794233300)"
        )
        conn.execute(
            "UPDATE payment_receipts SET order_id='w02b04-payment-order',"
            "payment_attempt_id='w02b04-attempt-1',bot_id=91010,bank_card_id=91010,"
            "submitted_tehran_date='2026-11-08' "
            "WHERE tracking_code='W02-B04-LEGACY-RECEIPT'"
        )
        assert conn.execute(
            "SELECT order_id,payment_attempt_id,bot_id,bank_card_id,merchant_key,submitted_tehran_date "
            "FROM payment_receipts WHERE tracking_code='W02-B04-LEGACY-RECEIPT'"
        ).fetchone() == (
            "w02b04-payment-order", "w02b04-attempt-1", 91010, 91010,
            "root", "2026-11-08",
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO payment_receipts "
                "(tracking_code,user_id,role,amount_toman,payment_attempt_id) "
                "VALUES ('W02-B04-DUPLICATE-ATTEMPT',102,'end_user',1000,'w02b04-attempt-1')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO bank_cards "
                "(merchant_key,card_number_cipher,card_holder,bank_name,daily_limit_toman) "
                "VALUES ('root','bad','Synthetic Holder','Synthetic Bank',-1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO payment_attempts "
                "(attempt_id,order_id,rail,merchant_key,amount_toman,state,created_at) "
                "VALUES ('w02b04-bad-attempt','w02b04-payment-order','card_to_card',"
                "'root',0,'pending',1794233300)"
            )

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO payment_attempts "
                "(attempt_id,order_id,rail,merchant_key,amount_toman,state,"
                "external_payment_key,created_at) "
                "VALUES ('w02b04-attempt-duplicate-key','w02b04-payment-order',"
                "'card_to_card','root',1000,'pending','w02b04-external-1',1794233301)"
            )

        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            f"SELECT {legacy_columns} FROM payment_receipts ORDER BY id"
        ).fetchall() == receipt_before
        assert conn.execute(
            "SELECT id,owner_tg_id,account_code,email,sub_id,total_gb,expiry_time "
            "FROM accounts WHERE id=?", (account["account_id"],)
        ).fetchone() == account_before
        assert conn.execute(
            "SELECT id,rep_id,user_id,account_code,email,plan_code,transaction_type,"
            "amount,balance_after,description,actor_id,actor_name,created_at "
            "FROM financial_ledger ORDER BY id"
        ).fetchall() == ledger_before

        conn.commit()
        stable = (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM bank_cards ORDER BY id").fetchall(),
            conn.execute("SELECT * FROM payment_attempts ORDER BY attempt_id").fetchall(),
            conn.execute("SELECT * FROM payment_receipts ORDER BY id").fetchall(),
        )
        assert apply_migrations(conn, target_dir, now=1794233600) == []
        assert stable == (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM bank_cards ORDER BY id").fetchall(),
            conn.execute("SELECT * FROM payment_attempts ORDER BY attempt_id").fetchall(),
            conn.execute("SELECT * FROM payment_receipts ORDER BY id").fetchall(),
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.close()


def test_migration_011_operations(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers, database, sentinel = _helpers(root, tmp_path, monkeypatch)

    with helpers.make_db(database, monkeypatch, tmp_path) as db_path:
        assert Path(sentinel) != Path(db_path) and not Path(sentinel).exists()
        actor, account = _seed_legacy(helpers, database)
        prior_names = (
            "001_policy", "002_panels", "003_accounts", "004_plans",
            "005_tree", "006_bots", "007_access", "008_orders", "009_finance",
            "010_payments",
        )
        prior_dir = _migration_subset(root, tmp_path, prior_names)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        assert apply_migrations(conn, prior_dir, now=1794234000) == list(prior_names)

        account_before = conn.execute(
            "SELECT id,owner_tg_id,account_code,email,sub_id,total_gb,expiry_time "
            "FROM accounts WHERE id=?", (account["account_id"],)
        ).fetchone()
        ledger_before = conn.execute(
            "SELECT id,rep_id,user_id,account_code,email,plan_code,transaction_type,"
            "amount,balance_after,description,actor_id,actor_name,created_at "
            "FROM financial_ledger ORDER BY id"
        ).fetchall()
        conn.execute(
            "INSERT INTO bot_registry(bot_id,rep_id,username,status) "
            "VALUES (91111,202,'w02b04-operations','pending')"
        )
        conn.execute(
            "INSERT INTO panels(id,panel_code,name,panel_type) "
            "VALUES (91111,'w02b04-panel','W02-B04 Panel','3x-ui')"
        )
        conn.execute(
            "INSERT INTO quotes "
            "(quote_id,actor_tg_id,bot_id,merchant_key,purpose,account_id,amount_toman,"
            "spec_json,price_edges_json,policy_version,created_at,expires_at,fingerprint) "
            "VALUES ('w02b04-ops-quote',101,91111,'root','vpn_renew',303,1000,"
            "'{}','[]','v1.0',1794234000,1794235800,'w02b04-ops-fingerprint')"
        )
        conn.execute(
            "INSERT INTO payment_orders "
            "(order_id,quote_id,actor_tg_id,bot_id,merchant_key,purpose,payment_rail,"
            "amount_toman,status,created_at,expires_at) "
            "VALUES ('w02b04-ops-order','w02b04-ops-quote',101,91111,'root',"
            "'vpn_renew','wallet',1000,'awaiting_payment',1794234000,1794235800)"
        )

        conn.commit()
        target_names = prior_names + ("011_operations",)
        target_dir = _migration_subset(root, tmp_path, target_names)
        assert apply_migrations(conn, target_dir, now=1794234300) == ["011_operations"]

        assert [row[1] for row in conn.execute("PRAGMA table_info(action_confirmations)")] == [
            "nonce", "actor_tg_id", "bot_id", "action", "target_type", "target_id",
            "expected_revision", "payload_json", "expires_at", "consumed_at", "operation_id",
        ]
        assert [row[1] for row in conn.execute("PRAGMA table_info(panel_operations)")] == [
            "operation_id", "order_id", "actor_tg_id", "bot_id", "account_id", "panel_id",
            "kind", "request_cipher", "expected_effect_json", "state", "lock_key",
            "lock_token", "idempotency_key", "generation", "lease_until", "evidence_json",
            "created_at", "updated_at",
        ]
        assert [row[1] for row in conn.execute("PRAGMA table_info(operation_attempts)")] == [
            "id", "operation_id", "attempt_no", "started_at", "finished_at",
            "outcome", "evidence_json",
        ]
        assert [row[1] for row in conn.execute("PRAGMA table_info(delivery_outbox)")] == [
            "id", "event_key", "bot_id", "chat_id", "thread_id", "purpose", "resource_type",
            "resource_id", "state", "attempts", "next_attempt_at", "lease_token",
            "lease_until", "last_error_code", "message_id",
        ]

        conn.execute(
            "INSERT INTO action_confirmations "
            "(nonce,actor_tg_id,bot_id,action,target_type,target_id,expected_revision,"
            "payload_json,expires_at) "
            "VALUES ('w02b04-nonce',101,91111,'renew','account','303',1,'{}',1794234600)"
        )
        conn.execute(
            "INSERT INTO panel_operations "
            "(operation_id,order_id,actor_tg_id,bot_id,account_id,panel_id,kind,request_cipher,"
            "expected_effect_json,state,lock_key,lock_token,idempotency_key,created_at,updated_at) "
            "VALUES ('w02b04-operation','w02b04-ops-order',101,91111,303,91111,'renew',"
            "'cipher-request','{}','prepared','account:303','lock-token-1',"
            "'w02b04-idempotency-1',1794234300,1794234300)"
        )
        conn.execute(
            "INSERT INTO operation_attempts(operation_id,attempt_no,started_at,outcome) "
            "VALUES ('w02b04-operation',1,1794234300,'dispatching')"
        )
        conn.execute(
            "INSERT INTO delivery_outbox(event_key,bot_id,chat_id,purpose,resource_type,resource_id) "
            "VALUES ('w02b04-event-1',91111,101,'renewal_result','operation','w02b04-operation')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO panel_operations "
                "(operation_id,actor_tg_id,bot_id,panel_id,kind,request_cipher,"
                "expected_effect_json,state,lock_key,lock_token,idempotency_key,created_at,updated_at) "
                "VALUES ('w02b04-invalid-state',101,91111,91111,'renew','cipher','{}',"
                "'unknown','lock:invalid','token-invalid','w02b04-idempotency-invalid',"
                "1794234301,1794234301)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO panel_operations "
                "(operation_id,actor_tg_id,bot_id,panel_id,kind,request_cipher,"
                "expected_effect_json,state,lock_key,lock_token,idempotency_key,created_at,updated_at) "
                "VALUES ('w02b04-duplicate-idempotency',101,91111,91111,'renew','cipher','{}',"
                "'prepared','lock:duplicate','token-duplicate','w02b04-idempotency-1',"
                "1794234302,1794234302)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO operation_attempts(operation_id,attempt_no,started_at) "
                "VALUES ('w02b04-operation',1,1794234301)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO delivery_outbox(event_key,bot_id,chat_id,purpose,resource_type,resource_id) "
                "VALUES ('w02b04-event-1',91111,101,'renewal_result','operation','duplicate')"
            )

        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "SELECT id,owner_tg_id,account_code,email,sub_id,total_gb,expiry_time "
            "FROM accounts WHERE id=?", (account["account_id"],)
        ).fetchone() == account_before
        assert conn.execute(
            "SELECT id,rep_id,user_id,account_code,email,plan_code,transaction_type,"
            "amount,balance_after,description,actor_id,actor_name,created_at "
            "FROM financial_ledger ORDER BY id"
        ).fetchall() == ledger_before
        conn.commit()
        stable = (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM action_confirmations ORDER BY nonce").fetchall(),
            conn.execute("SELECT * FROM panel_operations ORDER BY operation_id").fetchall(),
            conn.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall(),
            conn.execute("SELECT * FROM delivery_outbox ORDER BY id").fetchall(),
        )
        assert apply_migrations(conn, target_dir, now=1794234600) == []
        assert stable == (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM action_confirmations ORDER BY nonce").fetchall(),
            conn.execute("SELECT * FROM panel_operations ORDER BY operation_id").fetchall(),
            conn.execute("SELECT * FROM operation_attempts ORDER BY id").fetchall(),
            conn.execute("SELECT * FROM delivery_outbox ORDER BY id").fetchall(),
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.close()


def test_migration_012_goods(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    helpers, database, sentinel = _helpers(root, tmp_path, monkeypatch)

    with helpers.make_db(database, monkeypatch, tmp_path) as db_path:
        assert Path(sentinel) != Path(db_path) and not Path(sentinel).exists()
        actor, account = _seed_legacy(helpers, database)
        prior_names = (
            "001_policy", "002_panels", "003_accounts", "004_plans",
            "005_tree", "006_bots", "007_access", "008_orders", "009_finance",
            "010_payments", "011_operations",
        )
        prior_dir = _migration_subset(root, tmp_path, prior_names)
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        assert apply_migrations(conn, prior_dir, now=1794235000) == list(prior_names)

        account_before = conn.execute(
            "SELECT id,owner_tg_id,account_code,email,sub_id,total_gb,expiry_time "
            "FROM accounts WHERE id=?", (account["account_id"],)
        ).fetchone()
        ledger_before = conn.execute(
            "SELECT id,rep_id,user_id,account_code,email,plan_code,transaction_type,"
            "amount,balance_after,description,actor_id,actor_name,created_at "
            "FROM financial_ledger ORDER BY id"
        ).fetchall()
        conn.execute(
            "INSERT INTO bot_registry(bot_id,rep_id,username,status) "
            "VALUES (91212,202,'w02b04-goods','pending')"
        )

        conn.commit()
        target_names = prior_names + ("012_goods",)
        target_dir = _migration_subset(root, tmp_path, target_names)
        assert apply_migrations(conn, target_dir, now=1794235300) == ["012_goods"]

        assert [row[1] for row in conn.execute("PRAGMA table_info(digital_categories)")] == [
            "id", "title", "parent_id", "description", "sort_order", "is_active",
        ]
        assert [row[1] for row in conn.execute("PRAGMA table_info(digital_products)")] == [
            "id", "product_code", "category_id", "title", "description", "delivery_type",
            "rep_price", "user_price", "file_telegram_id", "file_bot_id",
            "stock_warning_threshold", "is_active", "sort_order",
        ]
        assert [row[1] for row in conn.execute("PRAGMA table_info(digital_inventory)")] == [
            "id", "product_id", "secret_cipher", "notes", "status", "order_id",
            "sold_to_tg_id", "reservation_until", "exposed_at", "sold_at", "created_at",
        ]

        conn.execute(
            "INSERT INTO digital_categories(id,title) VALUES (91212,'W02-B04 root category')"
        )
        conn.execute(
            "INSERT INTO digital_categories(id,title,parent_id) "
            "VALUES (91213,'W02-B04 child category',91212)"
        )
        conn.execute(
            "INSERT INTO digital_products "
            "(id,product_code,category_id,title,delivery_type,rep_price,user_price,file_bot_id) "
            "VALUES (91212,'W02B04-AUTO',91212,'Auto inventory','auto_inventory',100,150,NULL)"
        )
        conn.execute(
            "INSERT INTO digital_products "
            "(id,product_code,category_id,title,delivery_type,rep_price,user_price,"
            "file_telegram_id,file_bot_id) "
            "VALUES (91213,'W02B04-FILE',91213,'File document','file_document',200,250,"
            "'synthetic-telegram-file-id',91212)"
        )
        conn.execute(
            "INSERT INTO digital_products "
            "(id,product_code,category_id,title,delivery_type,rep_price,user_price) "
            "VALUES (91214,'W02B04-MANUAL',91213,'Manual fulfill','manual_fulfill',0,0)"
        )
        conn.execute(
            "INSERT INTO digital_inventory(id,product_id,secret_cipher,status,created_at) "
            "VALUES (91212,91212,'cipher-inventory-1','available',1794235300)"
        )
        conn.execute(
            "INSERT INTO digital_inventory(id,product_id,secret_cipher,status,created_at) "
            "VALUES (91213,91212,'cipher-inventory-2','reserved',1794235300)"
        )
        conn.execute(
            "INSERT INTO digital_inventory(id,product_id,secret_cipher,status,created_at) "
            "VALUES (91214,91212,'cipher-inventory-3','sold',1794235300)"
        )
        conn.execute(
            "INSERT INTO digital_inventory(id,product_id,secret_cipher,status,created_at) "
            "VALUES (91215,91212,'cipher-inventory-4','defective',1794235300)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO digital_products "
                "(product_code,category_id,title,delivery_type,rep_price,user_price) "
                "VALUES ('W02B04-BAD-DELIVERY',91212,'Bad','external',1,1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO digital_products "
                "(product_code,category_id,title,delivery_type,rep_price,user_price) "
                "VALUES ('W02B04-BAD-PRICE',91212,'Bad','manual_fulfill',-1,1)"
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO digital_inventory(product_id,secret_cipher,status,created_at) "
                "VALUES (91212,'cipher-invalid','in_transit',1794235300)"
            )

        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute(
            "SELECT id,owner_tg_id,account_code,email,sub_id,total_gb,expiry_time "
            "FROM accounts WHERE id=?", (account["account_id"],)
        ).fetchone() == account_before
        assert conn.execute(
            "SELECT id,rep_id,user_id,account_code,email,plan_code,transaction_type,"
            "amount,balance_after,description,actor_id,actor_name,created_at "
            "FROM financial_ledger ORDER BY id"
        ).fetchall() == ledger_before
        conn.commit()
        stable = (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM digital_categories ORDER BY id").fetchall(),
            conn.execute("SELECT * FROM digital_products ORDER BY id").fetchall(),
            conn.execute("SELECT * FROM digital_inventory ORDER BY id").fetchall(),
        )
        assert apply_migrations(conn, target_dir, now=1794235600) == []
        assert stable == (
            conn.execute("SELECT * FROM schema_migrations ORDER BY version").fetchall(),
            conn.execute("SELECT * FROM digital_categories ORDER BY id").fetchall(),
            conn.execute("SELECT * FROM digital_products ORDER BY id").fetchall(),
            conn.execute("SELECT * FROM digital_inventory ORDER BY id").fetchall(),
        )
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        conn.close()

# W02-B05-FINAL — isolated acceptance coverage for migrations 013–018.
from contextlib import contextmanager


_W02_PREFIX_MIGRATIONS = (
    "001_policy", "002_panels", "003_accounts", "004_plans",
    "005_tree", "006_bots", "007_access", "008_orders", "009_finance",
    "010_payments", "011_operations", "012_goods",
)
_W02_FINAL_MIGRATIONS = (
    "013_profiles", "014_domains", "015_support",
    "016_refunds_promos", "017_fulfillment_audit", "018_manual_pool",
)
_W02_FINAL_BOT_ID = 91300
_W02_FINAL_PANEL_ID = 91300


@contextmanager
def _prepared_w02_final_database(root, tmp_path, monkeypatch):
    helpers, database, sentinel = _helpers(root, tmp_path, monkeypatch)
    with helpers.make_db(database, monkeypatch, tmp_path) as db_path:
        assert Path(sentinel) != Path(db_path) and not Path(sentinel).exists()
        _seed_legacy(helpers, database)

        prefix_dir = _migration_subset(root, tmp_path, _W02_PREFIX_MIGRATIONS)
        prefix_conn = sqlite3.connect(str(db_path))
        prefix_conn.execute("PRAGMA foreign_keys=ON")
        assert apply_migrations(prefix_conn, prefix_dir, now=1794235700) == list(
            _W02_PREFIX_MIGRATIONS
        )
        prefix_conn.close()

        # Seed all FK parents through the configured DB context; it commits on exit.
        with database.get_db() as seed:
            seed.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,username,status) "
                "VALUES (?,?,?,'pending')",
                (_W02_FINAL_BOT_ID, 202, "w02-final-fixture"),
            )
            seed.execute(
                "INSERT INTO panels(id,panel_code,name,panel_type) VALUES (?,?,?,?)",
                (_W02_FINAL_PANEL_ID, "W02-FINAL-PANEL", "W02 final fixture", "synthetic"),
            )

        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield helpers, database, sentinel, db_path, conn
        finally:
            conn.close()


def _apply_w02_final_target(root, tmp_path, conn, target, now):
    target_index = _W02_FINAL_MIGRATIONS.index(target)
    names = _W02_PREFIX_MIGRATIONS + _W02_FINAL_MIGRATIONS[:target_index + 1]
    migration_dir = _migration_subset(root, tmp_path, names)
    assert not conn.in_transaction, "fixture DML must be committed before apply_migrations"
    expected_new_migrations = _W02_FINAL_MIGRATIONS[:target_index + 1]
    assert apply_migrations(conn, migration_dir, now=now) == list(expected_new_migrations)
    return migration_dir


def _seed_w02_quote_and_order(conn):
    conn.execute(
        "INSERT INTO quotes "
        "(quote_id,actor_tg_id,bot_id,merchant_key,purpose,amount_toman,spec_json,"
        "price_edges_json,policy_version,created_at,expires_at,fingerprint) "
        "VALUES ('w02-final-quote',101,?,'root','wallet_topup',100,'{}','[]',"
        "'v5.2',1000,2800,'w02-final-fingerprint')",
        (_W02_FINAL_BOT_ID,),
    )
    conn.execute(
        "INSERT INTO payment_orders "
        "(order_id,quote_id,actor_tg_id,bot_id,merchant_key,purpose,payment_rail,"
        "amount_toman,status,created_at,expires_at) "
        "VALUES ('w02-final-order','w02-final-quote',101,?,'root','wallet_topup',"
        "'card_to_card',100,'awaiting_payment',1000,2800)",
        (_W02_FINAL_BOT_ID,),
    )


def test_migration_013_profiles(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    with _prepared_w02_final_database(root, tmp_path, monkeypatch) as (
        helpers, database, sentinel, db_path, conn
    ):
        migration_dir = _apply_w02_final_target(
            root, tmp_path, conn, "013_profiles", 1794236000
        )
        assert [row[1] for row in conn.execute(
            "PRAGMA table_info(subscription_profiles)"
        )] == [
            "profile_id", "owner_tg_id", "name", "is_default", "enabled",
            "brand_rep_id", "created_bot_id", "token_hash", "token_cipher",
            "token_version", "revision", "deleted_at", "created_at",
        ]
        assert [row[1] for row in conn.execute(
            "PRAGMA table_info(subscription_profile_accounts)"
        )] == [
            "profile_id", "account_id", "added_by_tg_id", "sort_order", "added_at",
        ]
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='ux_default_profile'"
        ).fetchone() == (1,)
        conn.execute(
            "INSERT INTO subscription_profiles "
            "(profile_id,owner_tg_id,name,is_default,created_bot_id,token_hash,token_cipher,created_at) "
            "VALUES (91300,101,'Default',1,?, 'w02-default-hash','cipher-default',1794236000)",
            (_W02_FINAL_BOT_ID,),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="default_profile_not_deletable"):
            conn.execute("DELETE FROM subscription_profiles WHERE profile_id=91300")
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="default_profile_not_deletable"):
            conn.execute(
                "UPDATE subscription_profiles SET is_default=0 WHERE profile_id=91300"
            )
        conn.rollback()
        assert conn.execute(
            "SELECT is_default,deleted_at,owner_tg_id FROM subscription_profiles "
            "WHERE profile_id=91300"
        ).fetchone() == (1, None, 101)
        conn.commit()
        assert apply_migrations(conn, migration_dir, now=1794236100) == []
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_014_domains(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    with _prepared_w02_final_database(root, tmp_path, monkeypatch) as (
        helpers, database, sentinel, db_path, conn
    ):
        migration_dir = _apply_w02_final_target(
            root, tmp_path, conn, "014_domains", 1794236200
        )
        assert [row[1] for row in conn.execute(
            "PRAGMA table_info(sub_domains)"
        )] == [
            "domain_id", "rep_id", "hostname", "challenge", "status",
            "verified_at", "tls_checked_at", "created_at",
        ]
        indexes = {
            row[1]: row[2] for row in conn.execute("PRAGMA index_list(sub_domains)")
        }
        assert indexes["ux_active_rep_domain"] == 1
        conn.execute(
            "INSERT INTO sub_domains(domain_id,rep_id,hostname,challenge,status,created_at) "
            "VALUES (91300,202,'active-1.example.invalid','challenge-1','active',1794236200)"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO sub_domains(domain_id,rep_id,hostname,challenge,status,created_at) "
                "VALUES (91301,202,'active-2.example.invalid','challenge-2','active',1794236200)"
            )
        conn.rollback()
        conn.execute(
            "INSERT INTO sub_domains(domain_id,rep_id,hostname,challenge,status,created_at) "
            "VALUES (91302,202,'pending.example.invalid','challenge-3','pending',1794236200)"
        )
        conn.commit()
        assert apply_migrations(conn, migration_dir, now=1794236300) == []
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_015_support(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    with _prepared_w02_final_database(root, tmp_path, monkeypatch) as (
        helpers, database, sentinel, db_path, conn
    ):
        migration_dir = _apply_w02_final_target(
            root, tmp_path, conn, "015_support", 1794236400
        )
        assert [row[1] for row in conn.execute(
            "PRAGMA table_info(support_tickets)"
        )] == [
            "ticket_id", "sender_tg_id", "bot_id", "account_id", "rep_id",
            "ticket_type", "assigned_key", "status", "snapshot_cipher",
            "created_at", "updated_at",
        ]
        assert [row[1] for row in conn.execute(
            "PRAGMA table_info(support_messages)"
        )] == [
            "id", "ticket_id", "actor_tg_id", "visibility", "body_cipher",
            "attachment_file_id", "attachment_bot_id", "created_at",
        ]
        assert [row[1] for row in conn.execute(
            "PRAGMA table_info(forum_topics)"
        )] == [
            "id", "owner_key", "bot_id", "group_chat_id", "generation",
            "topic_key", "title_fa", "thread_id", "state", "updated_at",
        ]
        conn.execute(
            "INSERT INTO support_tickets "
            "(ticket_id,sender_tg_id,bot_id,account_id,rep_id,ticket_type,assigned_key,"
            "status,created_at,updated_at) "
            "VALUES ('TKT-W02-FINAL-01',101,?,303,202,'general','root','open',1794236400,1794236400)",
            (_W02_FINAL_BOT_ID,),
        )
        conn.execute(
            "INSERT INTO support_messages "
            "(ticket_id,actor_tg_id,visibility,body_cipher,created_at) "
            "VALUES ('TKT-W02-FINAL-01',101,'customer','cipher-message',1794236400)"
        )
        conn.execute(
            "INSERT INTO forum_topics "
            "(owner_key,bot_id,group_chat_id,generation,topic_key,title_fa,state,updated_at) "
            "VALUES ('rep:202',?, -10091300,1,'support','پشتیبانی','active',1794236400)",
            (_W02_FINAL_BOT_ID,),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO support_tickets "
                "(ticket_id,sender_tg_id,bot_id,ticket_type,assigned_key,status,created_at,updated_at) "
                "VALUES ('TKT-W02-FINAL-BAD',101,?,'unknown','root','open',1794236400,1794236400)",
                (_W02_FINAL_BOT_ID,),
            )
        conn.rollback()
        assert conn.execute(
            "SELECT COUNT(*) FROM support_tickets WHERE ticket_id='TKT-W02-FINAL-01'"
        ).fetchone() == (1,)
        conn.commit()
        assert apply_migrations(conn, migration_dir, now=1794236500) == []
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_016_refunds_promos(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    with _prepared_w02_final_database(root, tmp_path, monkeypatch) as (
        helpers, database, sentinel, db_path, conn
    ):
        migration_dir = _apply_w02_final_target(
            root, tmp_path, conn, "016_refunds_promos", 1794236600
        )
        assert [row[1] for row in conn.execute(
            "PRAGMA table_info(refund_requests)"
        )] == [
            "refund_id", "order_id", "merchant_key", "user_tg_id", "amount_toman",
            "state", "wallet_hold_key", "bank_details_cipher", "evidence_cipher",
            "reviewer_tg_id", "created_at", "settled_at",
        ]
        assert [row[1] for row in conn.execute(
            "PRAGMA table_info(wallet_refund_holds)"
        )] == [
            "hold_key", "merchant_key", "user_tg_id", "amount_toman",
            "state", "created_at",
        ]
        assert [row[1] for row in conn.execute(
            "PRAGMA table_info(coupons)"
        )] == [
            "code", "issuer_key", "discount_toman", "budget_toman", "used_toman",
            "max_uses", "expires_at", "is_active",
        ]
        assert [row[1] for row in conn.execute(
            "PRAGMA table_info(coupon_uses)"
        )] == ["code", "order_id", "amount_toman", "state"]
        assert [row[1] for row in conn.execute(
            "PRAGMA table_info(scheduled_jobs)"
        )] == [
            "job_key", "kind", "due_at", "state", "lease_token",
            "lease_until", "result_json",
        ]
        _seed_w02_quote_and_order(conn)
        conn.execute(
            "INSERT INTO refund_requests "
            "(refund_id,merchant_key,user_tg_id,amount_toman,state,wallet_hold_key,"
            "bank_details_cipher,created_at) "
            "VALUES ('refund-w02-final','root',101,500,'requested','hold-refund-w02',"
            "'cipher-bank-details',1794236600)"
        )
        conn.execute(
            "INSERT INTO wallet_refund_holds "
            "(hold_key,merchant_key,user_tg_id,amount_toman,state,created_at) "
            "VALUES ('hold-refund-w02','root',101,500,'active',1794236600)"
        )
        conn.execute(
            "INSERT INTO coupons(code,issuer_key,discount_toman,budget_toman,expires_at) "
            "VALUES ('W02FINAL','root',100,0,1794323000)"
        )
        conn.execute(
            "INSERT INTO coupon_uses(code,order_id,amount_toman,state) "
            "VALUES ('W02FINAL','w02-final-order',100,'held')"
        )
        conn.execute(
            "INSERT INTO scheduled_jobs(job_key,kind,due_at,state) "
            "VALUES ('w02-final-job','refund_reconcile',1794236600,'pending')"
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO refund_requests "
                "(refund_id,merchant_key,user_tg_id,amount_toman,state,wallet_hold_key,"
                "bank_details_cipher,created_at) "
                "VALUES ('refund-w02-zero','root',101,0,'requested','hold-zero',"
                "'cipher',1794236600)"
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO coupons(code,issuer_key,discount_toman,budget_toman,expires_at) "
                "VALUES ('W02BADZERO','root',0,0,1794323000)"
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO coupons(code,issuer_key,discount_toman,budget_toman,expires_at) "
                "VALUES ('W02BADBUDGET','root',1,-1,1794323000)"
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO coupon_uses(code,order_id,amount_toman,state) "
                "VALUES ('W02FINAL','w02-final-order','100','invalid')"
            )
        conn.rollback()
        conn.commit()
        assert apply_migrations(conn, migration_dir, now=1794236700) == []
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_017_fulfillment_audit(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    with _prepared_w02_final_database(root, tmp_path, monkeypatch) as (
        helpers, database, sentinel, db_path, conn
    ):
        migration_dir = _apply_w02_final_target(
            root, tmp_path, conn, "017_fulfillment_audit", 1794236800
        )
        assert "details_cipher" in {
            row[1] for row in conn.execute("PRAGMA table_info(audit_logs)")
        }
        assert [row[1] for row in conn.execute(
            "PRAGMA table_info(goods_fulfillments)"
        )] == [
            "order_id", "state", "inventory_id", "payload_cipher",
            "claim_token", "lease_until", "exposed_at", "revision",
        ]
        _seed_w02_quote_and_order(conn)
        conn.execute(
            "INSERT INTO goods_fulfillments(order_id,state,payload_cipher) "
            "VALUES ('w02-final-order','awaiting_manual','cipher-fulfillment')"
        )
        ledger_id = conn.execute(
            "SELECT id FROM financial_ledger ORDER BY id LIMIT 1"
        ).fetchone()[0]
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="ledger_append_only"):
            conn.execute(
                "UPDATE financial_ledger SET description=description WHERE id=?",
                (ledger_id,),
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="ledger_append_only"):
            conn.execute(
                "DELETE FROM financial_ledger WHERE id=?", (ledger_id,)
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="quote_immutable"):
            conn.execute(
                "UPDATE quotes SET amount_toman=amount_toman WHERE quote_id='w02-final-quote'"
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError, match="quote_immutable"):
            conn.execute(
                "DELETE FROM quotes WHERE quote_id='w02-final-quote'"
            )
        conn.rollback()
        conn.commit()
        assert apply_migrations(conn, migration_dir, now=1794236900) == []
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_migration_018_manual_pool(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    with _prepared_w02_final_database(root, tmp_path, monkeypatch) as (
        helpers, database, sentinel, db_path, conn
    ):
        migration_dir = _apply_w02_final_target(
            root, tmp_path, conn, "018_manual_pool", 1794237000
        )
        assert [row[1] for row in conn.execute(
            "PRAGMA table_info(manual_pool_clients)"
        )] == [
            "id", "panel_id", "external_identifier", "account_id", "status",
            "config_cipher", "quota_mode", "quota_bytes", "used_bytes",
            "time_mode", "expires_at", "enabled", "allocation_key", "revision",
            "created_at",
        ]
        indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(manual_pool_clients)")
        }
        assert "idx_manual_pool_available" in indexes
        conn.execute(
            "INSERT INTO manual_pool_clients "
            "(id,panel_id,external_identifier,config_cipher,quota_mode,quota_bytes,"
            "used_bytes,time_mode,expires_at,created_at) "
            "VALUES (91300,?,'manual-available-1','cipher-config','finite',1000,0,"
            "'dated',1794323400,1794237000)",
            (_W02_FINAL_PANEL_ID,),
        )
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO manual_pool_clients "
                "(id,panel_id,external_identifier,config_cipher,quota_mode,quota_bytes,"
                "used_bytes,time_mode,created_at) "
                "VALUES (91301,?,'manual-negative-quota','cipher','finite',-1,0,'dated',1794237000)",
                (_W02_FINAL_PANEL_ID,),
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO manual_pool_clients "
                "(id,panel_id,external_identifier,config_cipher,quota_mode,quota_bytes,"
                "used_bytes,time_mode,created_at) "
                "VALUES (91302,?,'manual-negative-used','cipher','finite',1000,-1,'dated',1794237000)",
                (_W02_FINAL_PANEL_ID,),
            )
        conn.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO manual_pool_clients "
                "(id,panel_id,external_identifier,config_cipher,quota_mode,quota_bytes,"
                "used_bytes,time_mode,created_at) "
                "VALUES (91303,?,'manual-invalid-time','cipher','finite',1000,0,'forever',1794237000)",
                (_W02_FINAL_PANEL_ID,),
            )
        conn.rollback()
        conn.commit()
        assert apply_migrations(conn, migration_dir, now=1794237100) == []
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
