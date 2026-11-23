"""W19.5: Internal contracts change only the external-dependency expectation."""
import types

import pytest

from server_integrations import (
    INTEGRATION_CONTRACTS,
    load_integration,
    validate_integration_contract,
)


def _fake_module(name):
    module = types.SimpleNamespace()
    for function_name in INTEGRATION_CONTRACTS[name]["callables"]:
        setattr(module, function_name, lambda *args, **kwargs: None)
    return module


def test_internal_contract_preserves_negative_tests():
    panel = _fake_module("xui_manager")
    reporting = _fake_module("backup_and_report")
    assert validate_integration_contract("xui_manager", panel) is panel
    assert validate_integration_contract("backup_and_report", reporting) is reporting

    del panel.inspect_client
    with pytest.raises(RuntimeError, match="inspect_client"):
        validate_integration_contract("xui_manager", panel)

    del reporting.send_telegram_document
    with pytest.raises(RuntimeError, match="send_telegram_document"):
        validate_integration_contract("backup_and_report", reporting)

    reporting_with_secret = _fake_module("backup_and_report")
    reporting_with_secret.BOT_TOKEN = "must-not-be-public"
    with pytest.raises(RuntimeError, match="BOT_TOKEN"):
        validate_integration_contract("backup_and_report", reporting_with_secret)

    panel_with_global_url = _fake_module("xui_manager")
    panel_with_global_url.BASE_URL = "https://global.invalid"
    with pytest.raises(RuntimeError, match="BASE_URL"):
        validate_integration_contract("xui_manager", panel_with_global_url)

    # Retain the loader's negative coverage for a missing transitive dependency.
    missing = _Unavailable = "w19_missing_optional_module_for_test"
    assert "UnavailableIntegration" in repr(load_integration(missing))
