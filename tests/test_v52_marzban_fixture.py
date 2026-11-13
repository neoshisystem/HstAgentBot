"""W21.2 recorded fixture schema and production evidence boundary."""
from test_v52_marzban_contract import fixture_contract

def test_recorded_contract_shape():
    contract=fixture_contract()
    assert contract["schema_version"]=="hstagent-panel-contract/1"
    assert contract["adapter_version"]=="marzban-v1"
    assert contract["production_approved"] is False
    assert contract["live_approval"]=="EVIDENCE_REQUIRED"
    assert contract["synthetic"] is True
    assert isinstance(contract["capability"],dict)
    assert {"request","response","expected"}<=set(contract)
    assert {"success","failure","partial_failure","unknown","unavailable","not_found"}<=set(contract["response"])
    assert contract["response"]["unavailable"]["state"]!=contract["response"]["not_found"]["state"]
    assert contract["expected"]["outcomes"]==["success","failure","unknown"]
    assert contract["secret_markers"]["credential"].startswith("REDACTED:")
    assert "SYNTHETIC" in contract["secret_markers"]["subscription"]
    assert contract["capability"]["disconnect_existing_sessions"]["status"]=="unsupported"
