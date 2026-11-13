"""W21.8 versioned factory registration with independent family contracts."""
from copy import deepcopy
import pytest
from panel_adapters.base import UnsupportedCapability
from panel_adapters.factory import PanelAdapterFactory
from panel_adapters.marzban_adapter import MarzbanAdapter
from test_v52_marzban_contract import FakeTransport, fixture_contract

def _approved_contract():
    contract=deepcopy(fixture_contract())
    contract["production_approved"]=True
    contract["live_approval"]="APPROVED"
    for spec in contract["capability"].values():
        if spec.get("status")=="recorded": spec["status"]="verified"
    return contract

def test_versioned_registration_without_family_assumption():
    panels={
        1:{"id":1,"panel_type":"marzban","adapter_version":"marzban-v1"},
        2:{"id":2,"panel_type":"marzneshin","adapter_version":"marzban-v1"},
        3:{"id":3,"panel_type":"pasarguard","adapter_version":"marzban-v1"},
        4:{"id":4,"panel_type":"marzban","adapter_version":"marzban-v2"},
    }
    contract=_approved_contract()
    transport=FakeTransport()
    factory=PanelAdapterFactory(
        panel_loader=lambda pid:panels.get(pid),account_loader=lambda aid:{"panel_id":1},
        session_factory=None,account_id_resolver=lambda pid,ident:9001,
        operation_preflight=lambda op,candidate,cap:True,marzban_feature_enabled=True,
        marzban_contract_loader=lambda panel:contract,marzban_transport_factory=lambda panel:transport,
        marzban_contract_gate=lambda panel,value:panel.get("panel_type")=="marzban" and value.get("production_approved") is True)
    adapter=factory.get_adapter(1)
    assert isinstance(adapter,MarzbanAdapter) and "get_client" in adapter.capabilities.methods
    disabled=PanelAdapterFactory(panel_loader=lambda pid:panels.get(pid),
        account_loader=lambda aid:{"panel_id":1},session_factory=None)
    with pytest.raises(UnsupportedCapability,match="disabled"): disabled.get_adapter(1)
    for pid in (2,3,4):
        with pytest.raises(UnsupportedCapability): factory.get_adapter(pid)
    assert transport.calls==[]
