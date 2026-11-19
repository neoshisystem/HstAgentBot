"""W19.4: Product integration loads without an external helper directory."""
import importlib
import sys

from server_integrations import load_integration


def test_runs_without_server_helper_directory(monkeypatch, tmp_path):
    monkeypatch.delenv("HSTAGENTBOT_INTEGRATIONS_PATH", raising=False)
    sys.path[:] = [entry for entry in sys.path if str(tmp_path) != entry]
    panel = load_integration("xui_manager")
    reporting = load_integration("backup_and_report")
    assert panel is importlib.import_module("internal_panel_compat")
    assert reporting is importlib.import_module("internal_reporting")
    assert callable(panel.inspect_client)
    assert callable(reporting.send_telegram_document)
    assert not hasattr(panel, "BASE_URL")
    assert not hasattr(panel, "get_session")
    assert not hasattr(reporting, "BOT_TOKEN")
