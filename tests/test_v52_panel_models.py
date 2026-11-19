from dataclasses import FrozenInstanceError
from panel_adapters.models import NodeConfig, expiry_seconds_to_panel_ms, normalize_expiry, normalize_volume

def _guard_offline(monkeypatch):
    import importlib.util
    from pathlib import Path
    helper_path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_v52_panel_offline_helpers", helper_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    attempts = module.guard_network(monkeypatch)
    return module, attempts

def test_units_unlimited_and_onhold(monkeypatch):
    _, network_attempts = _guard_offline(monkeypatch)
    assert normalize_volume(1024**3) == ("finite", 1024**3)
    assert normalize_volume(0) == ("unknown", None)
    assert normalize_volume(0, zero_is_unlimited=True) == ("unlimited", None)
    assert normalize_volume(None) == ("unknown", None)
    assert normalize_expiry(1_800_000_123_000) == ("dated", 1_800_000_123)
    assert normalize_expiry(0) == ("unknown", None)
    assert normalize_expiry(0, zero_is_unlimited=True) == ("unlimited", None)
    assert normalize_expiry(-1) == ("on_hold", None)
    assert expiry_seconds_to_panel_ms(1_800_000_123) == 1_800_000_123_000
    node = NodeConfig("n1", "mobile", "vless", "vless://uuid:secret@example.test:443#private")
    assert "secret" not in repr(node)
    try:
        node.label = "changed"
    except FrozenInstanceError:
        pass
    else:
        raise AssertionError("NodeConfig must be frozen")
    assert network_attempts == []
