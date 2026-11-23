"""W19.2: Legacy mutations preserve UNKNOWN and evidence-gated readiness."""
import internal_panel_compat as compat
from panel_adapters.base import MutationResult


class _Adapter:
    async def renew_client(self, identifier, target, operation_id):
        return MutationResult("unknown", {"stage": "test_partial_effect"})

    async def set_client_enabled(self, identifier, enabled, operation_id):
        return MutationResult("unknown", {"stage": "test_readback_unavailable"})


class _Factory:
    def resolve_account_adapter(self, account_id):
        assert account_id == 41
        return _Adapter()


class _Connection:
    def __init__(self, renewal_method):
        self.renewal_method = renewal_method

    def execute(self, _sql, _params):
        return self

    def fetchone(self):
        return {"renewal_method": self.renewal_method}


class _Database:
    def __init__(self):
        self.renewal_method = "manual"

    def get_db(self):
        from contextlib import contextmanager

        @contextmanager
        def connection():
            yield _Connection(self.renewal_method)

        return connection()


def test_smart_characterization_and_unknown(monkeypatch):
    monkeypatch.setattr(
        compat, "_resolve_account",
        lambda *_a, **_k: {"id": 41, "panel_id": 7, "email": "ev02@example.invalid"},
    )
    monkeypatch.setattr(compat, "_factory", lambda: _Factory())
    monkeypatch.setattr(compat, "_operation_matches", lambda *_a, **_k: True)
    monkeypatch.setattr(
        compat, "_dispatch_operation",
        lambda *_a, **_k: {"dispatch_outcome": "unknown", "operation_state": "needs_reconciliation"},
    )
    db = _Database()
    monkeypatch.setattr(compat, "_database", lambda: db)
    renewal = compat.renew_client(
        "ev02@example.invalid", gb=5, days=14,
        account_ref={"account_id": 41, "panel_id": 7}, operation_id="prepared-op",
    )
    toggled = compat.set_client_enable(
        "ev02@example.invalid", False,
        account_ref={"account_id": 41, "panel_id": 7}, operation_id="prepared-op",
    )
    comment = compat.set_client_comment(
        "ev02@example.invalid", "comment",
        account_ref={"account_id": 41, "panel_id": 7},
    )
    assert renewal["state"] == "unknown"
    assert toggled["state"] == "unknown"
    assert comment["state"] == "unknown"
    assert "EVIDENCE_REQUIRED" in comment["reason"]
    db.renewal_method = "smart_rollover"
    smart = compat.renew_client(
        "ev02@example.invalid", gb=5, days=14,
        account_ref={"account_id": 41, "panel_id": 7}, operation_id="prepared-op",
    )
    assert smart["state"] == "unknown"
    assert "EV-02" in smart["reason"]
    assert compat.renew_client("legacy-only", gb=5, days=14)["state"] == "unknown"
