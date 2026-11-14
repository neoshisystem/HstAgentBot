import importlib
import sys
import types

import pytest

from server_integrations import (
    INTEGRATION_CONTRACTS,
    load_integration,
    validate_integration_contract,
)


def _fake_module(name):
    module = types.SimpleNamespace()
    contract = INTEGRATION_CONTRACTS[name]
    for attribute in contract["attributes"]:
        setattr(module, attribute, "https://panel.example.test" if attribute == "BASE_URL" else "test-token")
    for function_name in contract["callables"]:
        setattr(module, function_name, lambda *args, **kwargs: None)
    return module


def test_missing_optional_server_integration_fails_lazily_with_guidance(monkeypatch, tmp_path):
    monkeypatch.setenv("HSTAGENTBOT_INTEGRATIONS_PATH", str(tmp_path))
    module = load_integration("hstag_test_missing_server_module_123456")
    assert "UnavailableIntegration" in repr(module)
    with pytest.raises(RuntimeError, match="HSTAGENTBOT_INTEGRATIONS_PATH"):
        module.some_operation()


def test_xui_manager_contract_accepts_repository_api_surface():
    module = _fake_module("xui_manager")
    assert validate_integration_contract("xui_manager", module) is module


def test_xui_manager_contract_rejects_missing_or_non_callable_method():
    module = _fake_module("xui_manager")
    module.renew_client = None
    del module.inspect_client
    with pytest.raises(RuntimeError) as error:
        validate_integration_contract("xui_manager", module)
    assert "inspect_client" in str(error.value)
    assert "renew_client" in str(error.value)


def test_backup_report_contract_requires_token_and_both_delivery_methods():
    module = _fake_module("backup_and_report")
    assert validate_integration_contract("backup_and_report", module) is module
    assert not hasattr(module, "BOT_TOKEN")
    module.send_telegram_document = "not-callable"
    with pytest.raises(RuntimeError) as error:
        validate_integration_contract("backup_and_report", module)
    assert "send_telegram_document" in str(error.value)

    exposed_secret = _fake_module("backup_and_report")
    exposed_secret.BOT_TOKEN = "PRIVATE_TOKEN_MARKER"
    with pytest.raises(RuntimeError, match="BOT_TOKEN"):
        validate_integration_contract("backup_and_report", exposed_secret)


def test_loader_does_not_mask_missing_internal_dependency(monkeypatch, tmp_path):
    module_name = "hstag_t11_adapter_with_missing_dependency"
    dependency_name = "hstag_t11_internal_dependency_absent"
    (tmp_path / f"{module_name}.py").write_text(
        f"import {dependency_name}\n", encoding="utf-8"
    )
    monkeypatch.setenv("HSTAGENTBOT_INTEGRATIONS_PATH", str(tmp_path))
    sys.modules.pop(module_name, None)
    try:
        with pytest.raises(ModuleNotFoundError) as error:
            load_integration(module_name)
        assert error.value.name == dependency_name
    finally:
        sys.modules.pop(module_name, None)


def test_unknown_integration_names_remain_backward_compatible():
    module = types.SimpleNamespace(run=lambda: "ok")
    assert validate_integration_contract("unregistered_helper", module) is module


def test_retired_watchdog_refuses_to_start():
    from watchdog_service import WatchdogService

    with pytest.raises(RuntimeError, match="canonical watchdog_worker"):
        WatchdogService(None, None, None)
