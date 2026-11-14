import inspect
from panel_adapters.base import (BasePanelAdapter, CanonicalServerStatus, LookupResult, MutationResult, PanelCapabilities, ReconcileResult, UnsupportedCapability)

def _guard_offline(monkeypatch):
    import importlib.util
    from pathlib import Path
    helper_path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_v52_panel_offline_helpers", helper_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    attempts = module.guard_network(monkeypatch)
    return module, attempts

class InMemoryAdapter(BasePanelAdapter):
    async def verify_connection(self): return CanonicalServerStatus(True)
    async def get_client(self, identifier): return LookupResult("not_found")
    async def list_clients(self, scope_filter=None): return []
    async def get_client_configs(self, identifier): return []
    async def create_client(self, spec): return MutationResult("unknown")
    async def renew_client(self, identifier, target, operation_id): return MutationResult("unknown")
    async def add_addon(self, identifier, target, operation_id): return MutationResult("unknown")
    async def rotate_subscription_url(self, identifier, candidate, operation_id): return MutationResult("unknown")
    async def rotate_connection_credentials(self, identifier, candidate, operation_id): return MutationResult("unknown")
    async def disconnect_existing_sessions(self, identifier, operation_id): return MutationResult("unknown")
    async def set_client_enabled(self, identifier, enabled, operation_id): return MutationResult("unknown")
    async def set_traffic(self, identifier, mode, value, operation_id): return MutationResult("unknown")
    async def set_expiry(self, identifier, expires_at, operation_id): return MutationResult("unknown")
    async def delete_client(self, identifier, operation_id): return MutationResult("unknown")
    async def reconcile(self, operation): return ReconcileResult("still_unknown")

def test_transport_free_contract(monkeypatch):
    _, network_attempts = _guard_offline(monkeypatch)
    assert not inspect.isabstract(InMemoryAdapter)
    adapter = InMemoryAdapter(PanelCapabilities(frozenset({"get_client"})))
    assert adapter.capabilities.supports("get_client")
    try:
        adapter.capabilities.require("disconnect_existing_sessions")
    except UnsupportedCapability:
        pass
    else:
        raise AssertionError("unsupported capability was not rejected")
    import panel_adapters.base as module
    assert "requests" not in module.__dict__
    assert not any(name in module.__dict__ for name in ("GET", "POST", "REST_PATH"))
    assert network_attempts == []
