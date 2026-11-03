import json
from types import SimpleNamespace

import pytest

import sub_manager
import config


class FakeResponse:
    def __init__(self, ok=True, body=None, text=""):
        self.ok = ok
        self._body = body
        self.text = text

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class FakeSession:
    def __init__(self, owner, behavior="success"):
        self.owner = owner
        self.behavior = behavior
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json, timeout))
        settings = jsonlib_loads(json["settings"])
        changed = settings["clients"][0]["subId"]
        if self.behavior in ("success", "malformed_after_apply", "timeout_after_apply"):
            self.owner.client["subId"] = changed
        if self.behavior == "timeout_after_apply":
            raise TimeoutError("simulated timeout after panel accepted the update")
        if self.behavior == "malformed_after_apply":
            return FakeResponse(ok=True, body=ValueError("malformed JSON"))
        if self.behavior == "reject":
            return FakeResponse(ok=True, body={"success": False}, text="rejected")
        return FakeResponse(ok=True, body={"success": True})


def jsonlib_loads(value):
    import json
    return json.loads(value)


class FakeXUI:
    BASE_URL = "https://panel.invalid"

    def __init__(self, behavior="success", second_lookup="state"):
        self.client = {
            "id": "client-001",
            "uuid": "client-uuid",
            "email": "client@example.test",
            "subId": "old-sub-id",
        }
        self.behavior = behavior
        self.second_lookup = second_lookup
        self.lookups = 0
        self.session = FakeSession(self, behavior)

    def get_session(self):
        return self.session

    def find_client_by_code(self, identifier):
        self.lookups += 1
        if self.lookups > 1 and self.second_lookup == "error":
            raise TimeoutError("verification lookup unavailable")
        if self.lookups > 1 and self.second_lookup == "missing":
            return {"found": False}
        return {
            "found": True,
            "data": {
                "email": self.client["email"],
                "inbound_id": 9,
                "raw": dict(self.client),
            },
        }



@pytest.mark.parametrize(("raw_value", "expected"), [("443", 443), ("8443", 8443), ("0", 443), ("65536", 443), ("bad-port", 443)])
def test_config_subscription_port_loader_uses_safe_default(monkeypatch, raw_value, expected):
    monkeypatch.setenv("SUB_PORT", raw_value)
    assert config._read_subscription_port() == expected

def test_subscription_url_requires_valid_public_domain(monkeypatch):
    monkeypatch.setattr(sub_manager.config, "SUB_DOMAIN", "")
    assert sub_manager.get_subscription_url("token-123") == ""


@pytest.mark.parametrize(
    ("domain", "port", "path", "expected"),
    [
        ("sub.example.test", 443, "sub", "https://sub.example.test/sub/token-123"),
        ("sub.example.test", 8443, "/v1/sub/", "https://sub.example.test:8443/v1/sub/token-123"),
        ("sub.example.test", 80, "", "https://sub.example.test:80/token-123"),
    ],
)
def test_subscription_url_builds_configured_host_port_and_path(monkeypatch, domain, port, path, expected):
    monkeypatch.setattr(sub_manager.config, "SUB_DOMAIN", domain)
    monkeypatch.setattr(sub_manager.config, "SUB_PORT", port)
    monkeypatch.setattr(sub_manager.config, "SUB_PATH", path)
    assert sub_manager.get_subscription_url("token-123") == expected


@pytest.mark.parametrize("domain", ["https://example.test/path", "user:pass@example.test", "example.test/path"])
def test_subscription_url_rejects_url_or_path_in_host_setting(monkeypatch, domain):
    monkeypatch.setattr(sub_manager.config, "SUB_DOMAIN", domain)
    monkeypatch.setattr(sub_manager.config, "SUB_PORT", 443)
    monkeypatch.setattr(sub_manager.config, "SUB_PATH", "sub")
    assert sub_manager.get_subscription_url("token-123") == ""


@pytest.mark.parametrize("domain", ["sub example.test", "sub.example.test\\tinvalid"])
def test_subscription_url_rejects_whitespace_or_control_in_host(monkeypatch, domain):
    monkeypatch.setattr(sub_manager.config, "SUB_DOMAIN", domain)
    monkeypatch.setattr(sub_manager.config, "SUB_PORT", 443)
    monkeypatch.setattr(sub_manager.config, "SUB_PATH", "sub")
    assert sub_manager.get_subscription_url("token-123") == ""


@pytest.mark.parametrize("port", [0, -1, 65536, "invalid", None])
def test_subscription_url_rejects_invalid_port(monkeypatch, port):
    monkeypatch.setattr(sub_manager.config, "SUB_DOMAIN", "sub.example.test")
    monkeypatch.setattr(sub_manager.config, "SUB_PORT", port)
    monkeypatch.setattr(sub_manager.config, "SUB_PATH", "sub")
    assert sub_manager.get_subscription_url("token-123") == ""


def test_subscription_url_rejects_empty_token(monkeypatch):
    monkeypatch.setattr(sub_manager.config, "SUB_DOMAIN", "sub.example.test")
    monkeypatch.setattr(sub_manager.config, "SUB_PORT", 443)
    monkeypatch.setattr(sub_manager.config, "SUB_PATH", "sub")
    assert sub_manager.get_subscription_url("") == ""


def test_qr_generation_returns_png_bytes():
    image = sub_manager.generate_qr_code("https://sub.example.test/sub/token")
    assert image.read(8) == b"\x89PNG\r\n\x1a\n"
    assert image.name.endswith(".png")


@pytest.mark.parametrize("payload", ["", "   ", None])
def test_qr_generation_rejects_empty_payload(payload):
    with pytest.raises(ValueError):
        sub_manager.generate_qr_code(payload)


def test_reset_sub_id_confirmed_success_returns_generated_id(monkeypatch):
    monkeypatch.setattr(sub_manager, "generate_secure_sub_id", lambda _length: "new-sub-id")
    xui = FakeXUI("success")

    applied, message, new_sub = sub_manager.reset_client_sub_id(xui, "client@example.test")

    assert applied is True
    assert new_sub == "new-sub-id"
    assert xui.client["subId"] == "new-sub-id"
    assert len(xui.session.calls) == 1


def test_reset_sub_id_malformed_response_is_verified_by_readback(monkeypatch):
    monkeypatch.setattr(sub_manager, "generate_secure_sub_id", lambda _length: "new-sub-id")
    xui = FakeXUI("malformed_after_apply")

    applied, _message, new_sub = sub_manager.reset_client_sub_id(xui, "client@example.test")

    assert applied is True
    assert new_sub == "new-sub-id"
    assert xui.lookups == 2


def test_reset_sub_id_timeout_after_remote_apply_is_verified(monkeypatch):
    monkeypatch.setattr(sub_manager, "generate_secure_sub_id", lambda _length: "new-sub-id")
    xui = FakeXUI("timeout_after_apply")

    applied, _message, new_sub = sub_manager.reset_client_sub_id(xui, "client@example.test")

    assert applied is True
    assert new_sub == "new-sub-id"
    assert xui.lookups == 2


def test_reset_sub_id_explicit_rejection_and_unchanged_remote_state_is_failure(monkeypatch):
    monkeypatch.setattr(sub_manager, "generate_secure_sub_id", lambda _length: "new-sub-id")
    xui = FakeXUI("reject")

    applied, _message, new_sub = sub_manager.reset_client_sub_id(xui, "client@example.test")

    assert applied is False
    assert new_sub == ""
    assert xui.client["subId"] == "old-sub-id"


def test_reset_sub_id_unverifiable_timeout_is_unknown(monkeypatch):
    monkeypatch.setattr(sub_manager, "generate_secure_sub_id", lambda _length: "new-sub-id")
    xui = FakeXUI("timeout_after_apply", second_lookup="error")

    applied, _message, candidate = sub_manager.reset_client_sub_id(xui, "client@example.test")

    assert applied is None
    assert candidate == "new-sub-id"


def test_reset_wrapper_rotates_even_when_public_domain_is_unset(monkeypatch):
    monkeypatch.setattr(sub_manager, "generate_secure_sub_id", lambda _length: "rotated-sub-id")
    monkeypatch.setattr(sub_manager.config, "SUB_DOMAIN", "")
    xui = FakeXUI("success")

    result = sub_manager.reset_client_subscription(xui, "client@example.test")

    assert result["status"] == "partial"
    assert result["new_sub_id"] == "rotated-sub-id"
    assert xui.client["subId"] == "rotated-sub-id"
    assert xui.lookups == 1


def test_reset_wrapper_does_not_claim_success_when_remote_state_is_unknown(monkeypatch):
    monkeypatch.setattr(sub_manager, "generate_secure_sub_id", lambda _length: "candidate-sub")
    monkeypatch.setattr(sub_manager.config, "SUB_DOMAIN", "sub.example.test")
    xui = FakeXUI("timeout_after_apply", second_lookup="error")

    result = sub_manager.reset_client_subscription(xui, "client@example.test")

    assert result["status"] == "unknown"
    assert "new_sub_id" not in result
    assert result["candidate_sub_id"] == "candidate-sub"


def test_reset_wrapper_builds_url_after_confirmed_rotation(monkeypatch):
    monkeypatch.setattr(sub_manager, "generate_secure_sub_id", lambda _length: "rotated-sub-id")
    monkeypatch.setattr(sub_manager.config, "SUB_DOMAIN", "sub.example.test")
    monkeypatch.setattr(sub_manager.config, "SUB_PORT", 443)
    monkeypatch.setattr(sub_manager.config, "SUB_PATH", "sub")
    xui = FakeXUI("success")

    result = sub_manager.reset_client_subscription(xui, "client@example.test")

    assert result["status"] == "ok"
    assert result["new_sub_url"] == "https://sub.example.test/sub/rotated-sub-id"
    assert xui.lookups == 1
