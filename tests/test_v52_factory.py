import pytest

from panel_adapters.base import AdapterUnavailable, UnsupportedCapability
from panel_adapters.factory import PanelAdapterFactory


def test_resolver_never_falls_back_on_failure():
    factory = PanelAdapterFactory(
        panel_loader=lambda _panel_id: (_ for _ in ()).throw(RuntimeError("database unavailable")),
        account_loader=lambda _account_id: {"id": 101, "panel_id": 7},
        session_factory=None,
    )
    with pytest.raises(AdapterUnavailable):
        factory.resolve_account_adapter(101)

    unsupported = PanelAdapterFactory(
        panel_loader=lambda _panel_id: {"id": 7, "type": "marzban", "revision": 1},
        account_loader=lambda _account_id: {"id": 101, "panel_id": 7},
        session_factory=None,
    )
    with pytest.raises(UnsupportedCapability):
        unsupported.resolve_account_adapter(101)
