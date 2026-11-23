from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from migrations_runner import apply_migrations
from payment_verifiers import (
    DuplicateExternalPayment, PaymentMismatch, PaymentVerifier, VerifiedPayment,
    VerifierDisabled, VerifierUnknown,
)


def _helpers():
    path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_w086_verifier_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeProvider:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def verify(self, attempt):
        self.calls += 1
        return self.result


class _TimeoutProvider:
    def verify(self, attempt):
        raise TimeoutError("synthetic provider timeout")


def test_disabled_and_mismatched_payment(tmp_path, monkeypatch):
    helpers = _helpers()
    network_attempts = helpers.guard_network(monkeypatch)
    database, sentinel = helpers.load_database_module(Path(__file__).resolve().parents[1], monkeypatch, tmp_path)
    now = 1794237000
    attempt_id = "00000000-0000-4000-8000-000000000086"

    with helpers.make_db(database, monkeypatch, tmp_path, filename="w086-verifier.sqlite3"):
        with database.get_db() as conn:
            assert len(apply_migrations(conn, Path(__file__).resolve().parents[1] / "migrations", now=now)) == 18
            helpers.seed_actor(database, actor_tg_id=101, rep_id=202, rep_tg_id=303)
            conn.execute(
                "INSERT INTO bot_registry(bot_id,rep_id,is_primary,token_cipher,username,status,verified_at,revision) "
                "VALUES(9001,NULL,1,NULL,'w086-root','active',?,1)", (now,),
            )
            conn.execute(
                "INSERT INTO quotes(quote_id,actor_tg_id,bot_id,merchant_key,purpose,amount_toman,"
                "spec_json,price_edges_json,policy_version,created_at,expires_at,fingerprint) "
                "VALUES('quote-w086',101,9001,'root','wallet_topup',250,'{}','[]','v1.0',?,?, 'fp-w086')",
                (now, now + 1800),
            )
            conn.execute(
                "INSERT INTO payment_orders(order_id,quote_id,actor_tg_id,bot_id,merchant_key,purpose,"
                "payment_rail,amount_toman,status,received_toman,created_at,expires_at,revision) "
                "VALUES('order-w086','quote-w086',101,9001,'root','wallet_topup','crypto_tron',250,"
                "'awaiting_payment',0,?,?,1)", (now, now + 1800),
            )
            conn.execute(
                "INSERT INTO payment_attempts(attempt_id,order_id,rail,merchant_key,assigned_card_id,"
                "amount_toman,state,external_payment_key,evidence_cipher,created_at,verified_at) "
                "VALUES(?,'order-w086','crypto_tron','root',NULL,250,'pending',NULL,NULL,?,NULL)",
                (attempt_id, now),
            )

        expected = dict(
            attempt_id=attempt_id, expected_merchant="root", expected_network="TRON",
            expected_asset="USDT", expected_destination="TQ_DESTINATION_086",
            expected_amount_toman=250, expected_atomic_amount=2500,
        )
        with database.get_db() as conn:
            disabled = PaymentVerifier()
            with pytest.raises(VerifierDisabled):
                disabled.verify_attempt(conn, **expected)

            valid = VerifiedPayment(
                merchant_key="root", network="TRON", asset="USDT",
                destination="TQ_DESTINATION_086", atomic_amount=2500,
                provider_reference="provider-ref-086", finality="final",
            )
            provider = _FakeProvider(valid)
            verifier = PaymentVerifier(crypto_enabled=True, provider=provider)
            assert verifier.verify_attempt(conn, **expected) == valid
            assert provider.calls == 1

            with pytest.raises(PaymentMismatch):
                verifier.verify_attempt(conn, **{**expected, "expected_merchant": "rep:202"})
            with pytest.raises(PaymentMismatch):
                verifier.verify_attempt(conn, **{**expected, "expected_amount_toman": 251})
            with pytest.raises(PaymentMismatch):
                verifier.verify_attempt(conn, **{**expected, "expected_atomic_amount": 3000})
            with pytest.raises(PaymentMismatch):
                verifier.verify_attempt(conn, **{**expected, "expected_network": "ETH"})

            conn.execute(
                "INSERT INTO payment_attempts(attempt_id,order_id,rail,merchant_key,assigned_card_id,"
                "amount_toman,state,external_payment_key,evidence_cipher,created_at,verified_at) "
                "VALUES('00000000-0000-4000-8000-000000000087','order-w086','crypto_tron','root',NULL,"
                "250,'succeeded','provider-ref-086',NULL,?,?)", (now, now),
            )
            with pytest.raises(DuplicateExternalPayment):
                verifier.verify_attempt(conn, **expected)

            with pytest.raises(VerifierUnknown):
                PaymentVerifier(crypto_enabled=True, provider=_TimeoutProvider()).verify_attempt(
                    conn, **expected,
                )
            with pytest.raises(VerifierUnknown):
                PaymentVerifier(crypto_enabled=True, provider=_FakeProvider({"tx_hash": "provider-ref-086"})).verify_attempt(
                    conn, **expected,
                )

            assert conn.execute("SELECT COUNT(*) FROM financial_ledger").fetchone()[0] == 0
            assert conn.execute("SELECT external_payment_key FROM payment_attempts WHERE attempt_id=?", (attempt_id,)).fetchone()[0] is None

    assert sentinel != str((tmp_path / "w086-verifier.sqlite3").resolve())
    assert network_attempts == []
