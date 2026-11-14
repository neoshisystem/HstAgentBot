"""W12.1: domain ownership proof, gateway target allowlist and SSRF protection."""
from contextlib import contextmanager
import importlib.util
from pathlib import Path
import sys

import pytest
from migrations_runner import apply_migrations
import domain_service


@contextmanager
def _prepared(tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("_w12_domains_helpers", Path(__file__).with_name("v52_helpers.py"))
    helpers = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helpers)
    attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(root, monkeypatch, tmp_path)
    monkeypatch.setenv("ADMIN_TELEGRAM_ID", "7001")
    monkeypatch.setattr(sys.modules["config"], "ADMIN_TELEGRAM_ID", 7001)
    with helpers.make_db(database, monkeypatch, tmp_path, filename="w12-domains.sqlite3"):
        with database.get_db() as conn:
            apply_migrations(conn, root / "migrations", now=1800000000)
        helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
        yield database, attempts, sentinel


def test_dns_ownership_and_ssrf_guard(tmp_path, monkeypatch):
    with _prepared(tmp_path, monkeypatch) as (database, attempts, sentinel):
        with database.get_db() as conn:
            domain = domain_service.register_domain(
                conn, 303, 202, "Bücher.example", now=1800000001,
                challenge="w12-random-owner-proof-value-001",
            )
            assert domain.hostname == "xn--bcher-kva.example"
            assert domain.status == "pending"
            assert domain.challenge == "w12-random-owner-proof-value-001"

            records = {
                ("_hst-sub.xn--bcher-kva.example", "TXT"): [domain.challenge],
                ("xn--bcher-kva.example", "CNAME"): ["gateway.example.net."],
                ("xn--bcher-kva.example", "A"): [],
                ("gateway.example.net", "A"): ["8.8.8.8"],
            }
            def resolver(name, kind):
                return records.get((name, kind), [])

            verified = domain_service.verify_domain(
                conn, domain.domain_id, resolver=resolver,
                tls_probe=lambda host, ips: {"ok": True, "server_hostname": host, "peer_ip": "8.8.8.8"},
                gateway_hosts=("gateway.example.net",), now=1800000002,
            )
            assert verified.status == "verified"
            assert verified.verified_at == verified.tls_checked_at == 1800000002
            assert attempts == []
            assert not Path(sentinel).exists()

            with pytest.raises(domain_service.DomainError):
                domain_service.normalize_hostname("https://evil.example/path")
            with pytest.raises(domain_service.DomainError):
                domain_service.normalize_hostname("127.0.0.1")
            with pytest.raises(domain_service.DomainError):
                domain_service.register_domain(conn, 303, 202, "xn--bcher-kva.example", now=1800000003)

            other = domain_service.register_domain(
                conn, 303, 202, "private-target.example", now=1800000004,
                challenge="w12-random-owner-proof-value-002",
            )
            private_records = {
                ("_hst-sub.private-target.example", "TXT"): [other.challenge],
                ("private-target.example", "CNAME"): [],
                ("private-target.example", "A"): ["127.0.0.1"],
                ("gateway.example.net", "A"): ["8.8.8.8"],
            }
            with pytest.raises(domain_service.DomainProofError):
                domain_service.verify_domain(
                    conn, other.domain_id,
                    resolver=lambda name, kind: private_records.get((name, kind), []),
                    tls_probe=lambda *_: True,
                    gateway_hosts=("gateway.example.net",), now=1800000005,
                )
            state = conn.execute(
                "SELECT status FROM sub_domains WHERE domain_id=?", (other.domain_id,)
            ).fetchone()
            assert state["status"] == "pending"
            assert attempts == []
