"""W19.2b: The facade exposes only scoped, canonical lookup helpers."""
import internal_panel_compat as compat
from panel_adapters.base import LookupResult


class _Adapter:
    async def get_client(self, identifier):
        return LookupResult("not_found")

    async def get_client_configs(self, identifier):
        return []


class _Factory:
    def resolve_account_adapter(self, account_id):
        assert account_id == 52
        return _Adapter()


def test_consumed_helpers_present_and_scoped(monkeypatch):
    names = (
        "inspect_client", "get_client_links", "get_sub_url", "find_client_by_code",
        "update_client_sub_id", "audit_clients_overview", "allocate_pool_client",
    )
    assert all(callable(getattr(compat, name, None)) for name in names)
    assert not hasattr(compat, "BASE_URL")
    assert not hasattr(compat, "get_session")
    monkeypatch.setattr(
        compat, "_resolve_account",
        lambda *_a, **_k: {"id": 52, "panel_id": 9, "email": "known@example.invalid"},
    )
    monkeypatch.setattr(compat, "_factory", lambda: _Factory())
    monkeypatch.setattr(compat, "_account_for_sub_id", lambda _sub: None)
    absent = compat.inspect_client("known@example.invalid")
    assert absent["state"] == "not_found"
    monkeypatch.setattr(compat, "_resolve_account", lambda *_a, **_k: None)
    ambiguous = compat.inspect_client("ambiguous-key")
    assert ambiguous["state"] == "unknown"
    assert compat.get_sub_url("unbound-sub-id") == ""
