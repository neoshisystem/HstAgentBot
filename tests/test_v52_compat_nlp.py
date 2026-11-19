"""W19.1b: Persian terms normalize without rewriting opaque URI tokens."""
import internal_panel_compat as compat


def test_persian_terms_and_uri_preservation():
    uri = "vless://abc123@node.example.invalid:443?flow=xtls-۰۹"
    source = "۵۰ گیگ ۱۴ روز " + uri
    normalized = compat.normalize_persian_digits(source)
    assert normalized.startswith("50 گیگ 14 روز ")
    assert uri in normalized
    assert compat.parse_package_terms(source) == (50.0, 14)
    assert compat.parse_package_terms("بسته اقتصادی ماهانه") == (None, None)
    assert compat.extract_customer_name("نام مشتری: علی رضایی") == "علی رضایی"
    assert compat.extract_account_identifier(uri) is None
