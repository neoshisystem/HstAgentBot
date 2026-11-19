"""W12.2: explicit activation confirmation and representative host isolation."""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys

import pytest
from migrations_runner import apply_migrations
from confirmation_service import create_confirmation
import domain_service


@contextmanager
def _prepared(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("_w12_domain_host_helpers", Path(__file__).with_name("v52_helpers.py"))
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setenv("ADMIN_TELEGRAM_ID", "7001")
    monkeypatch.setattr(sys.modules["config"], "ADMIN_TELEGRAM_ID", 7001)
    with helpers.make_db(database, monkeypatch, tmp_path, filename="w12-domain-host.sqlite3"):
        with database.get_db() as conn:
            apply_migrations(conn, root / "migrations", now=1800000100)
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision) "
                "VALUES(9001,NULL,1,'synthetic','w12_primary','active',1800000000,2)"
            )
        helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
        with database.get_db() as conn:
            conn.execute(
                "INSERT INTO representatives(id,rep_code,name,telegram_id,scope_filter,is_active) "
                "VALUES(203,'W12-REP-B','Rep B',404,'w12:b',1)"
            )
        yield database, attempts, sentinel


def _confirmation(database, actor, action, domain_id, revision, now):
    with database.get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        return create_confirmation(
            conn, actor_tg_id=actor, bot_id=9001, action=action,
            target_type="sub_domain", target_id=domain_id,
            expected_revision=revision, payload={"domain_id": domain_id}, now=now,
        )


def test_brand_isolation_and_disable(tmp_path, monkeypatch):
    with _prepared(tmp_path, monkeypatch) as (database, attempts, sentinel):
        # Commit a pending domain separately so the expected rejection cannot roll
        # back the unrelated verified domains and profile fixtures below.
        with database.get_db() as conn:
            pending = conn.execute(
                "INSERT INTO sub_domains(rep_id,hostname,challenge,status,created_at) "
                "VALUES(202,'pending.example','no-proof','pending',1800000000)"
            ).lastrowid
        with database.get_db() as conn:
            with pytest.raises(domain_service.DomainProofError):
                domain_service.activate_domain(
                    conn, 7001, pending, nonce="x" * 24, bot_id=9001, now=1800000101
                )

        with database.get_db() as conn:
            cur = conn.execute(
                "INSERT INTO sub_domains(rep_id,hostname,challenge,status,verified_at,tls_checked_at,created_at) "
                "VALUES(202,'brand-a.example','proof-a','verified',1800000100,1800000100,1800000000)"
            )
            domain_a = cur.lastrowid
            conn.execute(
                "INSERT INTO sub_domains(rep_id,hostname,challenge,status,verified_at,tls_checked_at,created_at) "
                "VALUES(203,'brand-b.example','proof-b','active',1800000100,1800000100,1800000000)"
            )
            conn.execute(
                "INSERT INTO subscription_profiles(profile_id,owner_tg_id,name,is_default,enabled,brand_rep_id,"
                "created_bot_id,token_hash,token_cipher,created_at) VALUES"
                "(1,101,'Default',1,1,NULL,9001,'hash-default','cipher',1800000000),"
                "(2,303,'Brand A',0,1,202,9001,'hash-a','cipher',1800000000),"
                "(3,404,'Brand B',0,1,203,9001,'hash-b','cipher',1800000000)"
            )

        activate = _confirmation(database, 303, "domain.activate", domain_a, 1800000100, 1800000102)
        with database.get_db() as conn:
            active = domain_service.activate_domain(
                conn, 303, domain_a, nonce=activate["nonce"], bot_id=9001, now=1800000102
            )
            assert active.status == "active"
            profiles = {
                row["profile_id"]: row for row in conn.execute(
                    "SELECT * FROM subscription_profiles WHERE profile_id IN (1,2,3)"
                ).fetchall()
            }
            assert domain_service.resolve_profile_host(conn, profiles[1], "canonical.example", "canonical.example")
            assert domain_service.resolve_profile_host(conn, profiles[2], "brand-a.example", "canonical.example")
            assert not domain_service.resolve_profile_host(conn, profiles[2], "brand-b.example", "canonical.example")
            assert not domain_service.resolve_profile_host(conn, profiles[3], "brand-a.example", "canonical.example")
            assert domain_service.resolve_profile_host(conn, profiles[2], "canonical.example", "canonical.example")

        disable = _confirmation(database, 303, "domain.disable", domain_a, 1800000102, 1800000103)
        with database.get_db() as conn:
            disabled = domain_service.disable_domain(
                conn, 303, domain_a, nonce=disable["nonce"], bot_id=9001, now=1800000103
            )
            assert disabled.status == "disabled"
            profile = conn.execute("SELECT * FROM subscription_profiles WHERE profile_id=2").fetchone()
            assert not domain_service.resolve_profile_host(conn, profile, "brand-a.example", "canonical.example")
            assert domain_service.resolve_profile_host(conn, profile, "canonical.example", "canonical.example")
        assert attempts == []
        assert not Path(sentinel).exists()
