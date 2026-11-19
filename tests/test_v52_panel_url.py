from panel_adapters.url_parser import parse_panel_url

def _guard_offline(monkeypatch):
    import importlib.util
    from pathlib import Path
    helper_path = Path(__file__).with_name("v52_helpers.py")
    spec = importlib.util.spec_from_file_location("_v52_panel_offline_helpers", helper_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    attempts = module.guard_network(monkeypatch)
    return module, attempts


def test_url_parser_preserves_custom_path(monkeypatch):
    _, network_attempts = _guard_offline(monkeypatch)
    endpoint = parse_panel_url("https://example.test:2053/custom/panel/api-docs")
    assert endpoint.base_path == "/custom"
    assert endpoint.api_base_url == "https://example.test:2053/custom/panel/api"
    assert parse_panel_url("https://x.test/panelx").base_path == "/panelx"
    assert parse_panel_url("https://x.test/Custom/Path").base_path == "/Custom/Path"
    assert parse_panel_url("https://[2001:0db8::1]:2053/custom").host == "2001:db8::1"
    assert parse_panel_url("http://x.test").port == 80
    for invalid in ("ftp://x.test", "https://user:pass@x.test", "https://x.test:70000", "https://x.test:0", "https://x.test/path?query=1", "https://x.test/path#frag", "https://x.test:bad"):
        try:
            parse_panel_url(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid URL accepted: {invalid}")
    assert network_attempts == []
