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
