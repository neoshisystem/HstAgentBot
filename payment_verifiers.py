"""Fail-closed payment-verifier interface; no provider is enabled by default."""
from __future__ import annotations

from dataclasses import dataclass
import os
import sqlite3


class VerifierError(ValueError):
    """Payment verification did not produce an acceptable provider proof."""


class VerifierDisabled(VerifierError):
    """The requested payment rail is disabled until a real verifier is connected."""


class VerifierUnknown(VerifierError):
    """Provider outcome is ambiguous; the attempt must not be retried or credited."""


class PaymentMismatch(VerifierError):
    """Provider proof does not match the immutable attempt and expected payment."""


class DuplicateExternalPayment(VerifierError):
    """A provider reference is already bound to another payment attempt."""


@dataclass(frozen=True)
class VerifiedPayment:
    merchant_key: str
    network: str
    asset: str
    destination: str
    atomic_amount: int
    provider_reference: str
    finality: str


class PaymentVerifier:
    """Validate a provider-authenticated result. This class contains no gateway adapter."""

    def __init__(self, *, crypto_enabled: bool | None = None,
                 gateway_enabled: bool | None = None, provider=None):
        self.crypto_enabled = (
            crypto_enabled if crypto_enabled is not None
            else os.environ.get("CRYPTO_PAYMENT_ENABLED", "0") == "1"
        )
        self.gateway_enabled = (
            gateway_enabled if gateway_enabled is not None
            else os.environ.get("PAYMENT_GATEWAY_ENABLED", "0") == "1"
        )
        if not isinstance(self.crypto_enabled, bool) or not isinstance(self.gateway_enabled, bool):
            raise TypeError("payment verifier feature flags must be booleans")
        self.provider = provider

    def verify_attempt(
        self, conn: sqlite3.Connection, *, attempt_id: str, expected_merchant: str,
        expected_network: str, expected_asset: str, expected_destination: str,
        expected_amount_toman: int, expected_atomic_amount: int,
    ) -> VerifiedPayment:
        if not isinstance(conn, sqlite3.Connection):
            raise TypeError("conn must be a sqlite3.Connection")
        if not isinstance(attempt_id, str) or not attempt_id.strip():
            raise VerifierError("attempt_id is required")
        if (
            isinstance(expected_amount_toman, bool) or not isinstance(expected_amount_toman, int)
            or expected_amount_toman <= 0
        ):
            raise VerifierError("expected_amount_toman must be a positive integer")
        if (
            isinstance(expected_atomic_amount, bool) or not isinstance(expected_atomic_amount, int)
            or expected_atomic_amount <= 0
        ):
            raise VerifierError("expected_atomic_amount must be a positive integer")
        attempt = conn.execute(
            "SELECT a.attempt_id,a.rail,a.merchant_key,a.amount_toman,a.state,a.external_payment_key,"
            "o.merchant_key,o.status FROM payment_attempts a JOIN payment_orders o ON o.order_id=a.order_id "
            "WHERE a.attempt_id=?",
            (attempt_id,),
        ).fetchone()
        if attempt is None:
            raise VerifierError("payment attempt does not exist")
        rail, merchant = str(attempt[1]), str(attempt[2])
        if merchant != str(attempt[6]) or merchant != expected_merchant:
            raise PaymentMismatch("attempt merchant does not match the expected merchant")
        if str(attempt[7]) not in {"awaiting_payment", "pending_review"}:
            raise VerifierError("payment order is not open for verification")
        # Toman and provider-native atomic units are different denominations.
        # Bind the invoice amount to the attempt and the provider amount to the
        # separately supplied immutable conversion snapshot; never compare them raw.
        if int(attempt[3]) != expected_amount_toman:
            raise PaymentMismatch("expected Toman amount does not match the immutable attempt")
        enabled = self.crypto_enabled if rail == "crypto_tron" else (
            self.gateway_enabled if rail == "online_gateway" else False
        )
        if rail not in {"crypto_tron", "online_gateway"}:
            raise VerifierError("payment rail is not handled by this verifier")
        if not enabled:
            raise VerifierDisabled(f"{rail} verification is disabled")
        if self.provider is None:
            raise VerifierUnknown("no real provider verifier is connected; no success is inferred")
        verify = getattr(self.provider, "verify", None)
        if not callable(verify):
            raise VerifierUnknown("provider does not implement authenticated verification")
        try:
            result = verify({
                "attempt_id": str(attempt[0]), "rail": rail, "merchant_key": merchant,
                "amount_toman": int(attempt[3]), "expected_network": expected_network,
                "expected_asset": expected_asset, "expected_destination": expected_destination,
                "expected_atomic_amount": expected_atomic_amount, "state": str(attempt[4]),
            })
        except (TimeoutError, ConnectionError) as exc:
            raise VerifierUnknown("provider timeout is UNKNOWN; automatic retry is forbidden") from exc
        except Exception as exc:
            raise VerifierUnknown("provider result is UNKNOWN; automatic retry is forbidden") from exc
        if not isinstance(result, VerifiedPayment):
            raise VerifierUnknown("a hash, callback payload or untyped result is not provider proof")
        if result.finality != "final":
            raise VerifierUnknown("provider has not established finality")
        if (
            result.merchant_key != expected_merchant
            or result.network != expected_network
            or result.asset != expected_asset
            or result.destination != expected_destination
            or isinstance(result.atomic_amount, bool)
            or result.atomic_amount != expected_atomic_amount
            or not isinstance(result.provider_reference, str)
            or not result.provider_reference.strip()
        ):
            raise PaymentMismatch("provider proof does not match merchant/network/asset/destination/amount")
        duplicate = conn.execute(
            "SELECT attempt_id FROM payment_attempts WHERE external_payment_key=? AND attempt_id<>?",
            (result.provider_reference, attempt_id),
        ).fetchone()
        if duplicate is not None:
            raise DuplicateExternalPayment("external_payment_key is already bound to another attempt")
        if attempt[5] is not None and str(attempt[5]) != result.provider_reference:
            raise PaymentMismatch("attempt already has a different external payment key")
        return result


__all__ = [
    "VerifiedPayment", "PaymentVerifier", "VerifierError", "VerifierDisabled",
    "VerifierUnknown", "PaymentMismatch", "DuplicateExternalPayment",
]
