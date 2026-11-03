from server_integrations import load_integration


def test_missing_optional_server_integration_fails_lazily_with_guidance():
    module = load_integration("hstag_test_missing_server_module_123456")
    assert "UnavailableIntegration" in repr(module)
    try:
        module.some_operation()
    except RuntimeError as exc:
        assert "HSTAGENTBOT_INTEGRATIONS_PATH" in str(exc)
    else:
        raise AssertionError("Expected a clear missing-integration error")
