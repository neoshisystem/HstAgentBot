import inspect
import admin_panel
def test_root_all_scopes_no_hardcoded_url():
    source = inspect.getsource(admin_panel)
    assert "xui_manager.BASE_URL" not in source
    assert "panel_adapter_factory" in source
    assert "list_clients(scope_filter=" in source
