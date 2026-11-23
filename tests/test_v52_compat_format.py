"""W19.1: Pure format/parse compatibility without a global panel URL."""
import internal_panel_compat as compat


def test_roundtrip_fixtures_and_no_global_url():
    fixtures = [
        {"email": "ev02-alpha@example.invalid", "enable": True, "totalGB": 20,
         "expiryTime": 1_800_000_000_000,
         "traffic": {"up": 100, "down": 200}, "subId": "EV02_SUB_ALPHA"},
        {"email": "EV02_beta", "enable": False, "totalGB": 0,
         "expiryTime": 0, "traffic": {"up": 0, "down": 0}},
    ]
    for fixture in fixtures:
        parsed = compat.parse_client_info(fixture)
        assert parsed["state"] == "found"
        assert parsed["email"] == fixture["email"]
        assert parsed["enable"] is fixture["enable"]
        assert parsed["subId"] == fixture.get("subId")
        assert compat.extract_account_identifier(fixture["email"]) == fixture["email"]
    assert not hasattr(compat, "BASE_URL")
    assert not hasattr(compat, "get_session")
    assert "EV02_SUB_ALPHA" not in compat.format_status_report(
        compat.parse_client_info(fixtures[0]), is_admin=False
    )
    assert compat.format_client_deliverable({"email": "a@example.invalid"}, []) \
        .find("قابل تأیید") >= 0
