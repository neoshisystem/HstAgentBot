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



def test_retired_watchdog_refuses_to_start():
    from watchdog_service import WatchdogService

    try:
        WatchdogService(None, None, None)
    except RuntimeError as exc:
        assert "canonical watchdog_worker" in str(exc)
    else:
        raise AssertionError("The retired duplicate watchdog must not be startable")
