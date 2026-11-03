import json

import pytest

import config
import sub_manager


class FakeResponse:
    def __init__(self, ok=True, body=None):
        self.ok = ok
        self.body = body

    def json(self):
        if isinstance(self.body, Exception):
            raise self.body
        return self.body


class FakeSession:
    def __init__(self, owner, behavior="success"):
        self.owner = owner
        self.behavior = behavior
        self.calls = []

    def post(self, url, json=None, timeout=None):
        self.calls.append((url, json, timeout))
        changed = jsonlib.loads(json["settings"])["clients"][0]["subId"]
        if self.behavior in ("success", "malformed", "timeout"):
            self.owner.client["subId"] = changed
        if self.behavior == "timeout":
            raise TimeoutError("accepted request then timeout")
        if self.behavior == "malformed":
            return FakeResponse(body=ValueError("malformed response"))
        if self.behavior == "reject":
            return FakeResponse(body={"success": False})
        return FakeResponse(body={"success": True})


jsonlib = json


class FakeXUI:
    BASE_URL = "https://panel.invalid"

    def __init__(self, behavior="success", verify="state"):
        self.client = {
            "id": "client-001",
            "email": "client@example.test",
            "subId": "old-sub-id",
        }
        self.behavior = behavior
        self.verify = verify
        self.lookups = 0
        self.session = FakeSession(self, behavior)

    def get_session(self):
        return self.session

    def find_client_by_code(self, _identifier):
        self.lookups += 1
        if self.lookups > 1 and self.verify == "error":
            raise TimeoutError("verification read unavailable")
        if self.lookups > 1 and self.verify == "missing":
            return {"found": False}
        return {
            "found": True,
            "data": {
                "email": self.client["email"],
                "inbound_id": 9,
                "raw": dict(self.client),
            },
        }


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("443", 443), ("8443", 8443), ("0", 443), ("65536", 443), ("bad", 443)],
)
def test_config_port_uses_safe_value(monkeypatch, raw, expected):
    monkeypatch.setenv("SUB_PORT", raw)
    assert config._read_subscription_port() == expected


def test_url_requires_a_configured_public_domain(monkeypatch):
    monkeypatch.setattr(sub_manager.config, "SUB_DOMAIN", "")
    assert sub_manager.get_subscription_url("token-123") == ""


@pytest.mark.parametrize(
    ("host", "port", "path", "expected"),
    [
        ("sub.example.test", 443, "sub", "https://sub.example.test/sub/token-123"),
        ("sub.example.test", 8443, "/v1/sub/", "https://sub.example.test:8443/v1/sub/token-123"),
        ("sub.example.test", 80, "", "https://sub.example.test:80/token-123"),
    ],
)
def test_url_respects_host_port_and_path(monkeypatch, host, port, path, expected):
    monkeypatch.setattr(sub_manager.config, "SUB_DOMAIN", host)
    monkeypatch.setattr(sub_manager.config, "SUB_PORT", port)
    monkeypatch.setattr(sub_manager.config, "SUB_PATH", path)
    assert sub_manager.get_subscription_url("token-123") == expected


@pytest.mark.parametrize("host", ["https://example.test/path", "user:pass@example.test", "example.test/path"])
def test_url_rejects_scheme_credentials_or_path_in_host(monkeypatch, host):
    monkeypatch.setattr(sub_manager.config, "SUB_DOMAIN", host)
    monkeypatch.setattr(sub_manager.config, "SUB_PORT", 443)
    monkeypatch.setattr(sub_manager.config, "SUB_PATH", "sub")
    assert sub_manager.get_subscription_url("token-123") == ""


@pytest.mark.parametrize("port", [0, -1, 65536, "bad", None])
def test_url_rejects_invalid_port(monkeypatch, port):
    monkeypatch.setattr(sub_manager.config, "SUB_DOMAIN", "sub.example.test")
    monkeypatch.setattr(sub_manager.config, "SUB_PORT", port)
    monkeypatch.setattr(sub_manager.config, "SUB_PATH", "sub")
    assert sub_manager.get_subscription_url("token-123") == ""


def test_url_rejects_empty_token(monkeypatch):
    monkeypatch.setattr(sub_manager.config, "SUB_DOMAIN", "sub.example.test")
    monkeypatch.setattr(sub_manager.config, "SUB_PORT", 443)
    monkeypatch.setattr(sub_manager.config, "SUB_PATH", "sub")
    assert sub_manager.get_subscription_url("") == ""


def test_qr_returns_a_png(monkeypatch):
    image = sub_manager.generate_qr_code("https://sub.example.test/sub/token")
    assert image.read(8) == b"\x89PNG\r\n\x1a\n"
    assert image.name.endswith(".png")


@pytest.mark.parametrize("payload", ["", "   ", None])
def test_qr_rejects_empty_payload(payload):
    with pytest.raises(ValueError):
        sub_manager.generate_qr_code(payload)


def test_reset_success_uses_remote_confirmation(monkeypatch):
    monkeypatch.setattr(sub_manager, "generate_secure_sub_id", lambda _length: "new-sub-id")
    xui = FakeXUI()
    applied, _message, new_id = sub_manager.reset_client_sub_id(xui, "client@example.test")
    assert applied is True
    assert new_id == "new-sub-id"
    assert xui.client["subId"] == new_id
    assert len(xui.session.calls) == 1


@pytest.mark.parametrize("behavior", ["malformed", "timeout"])
def test_reset_ambiguous_response_is_verified_by_readback(monkeypatch, behavior):
    monkeypatch.setattr(sub_manager, "generate_secure_sub_id", lambda _length: "new-sub-id")
    xui = FakeXUI(behavior)
    applied, _message, new_id = sub_manager.reset_client_sub_id(xui, "client@example.test")
    assert applied is True
    assert new_id == "new-sub-id"
    assert xui.lookups == 2


def test_reset_explicit_rejection_with_old_id_unchanged_is_failure(monkeypatch):
    monkeypatch.setattr(sub_manager, "generate_secure_sub_id", lambda _length: "new-sub-id")
    xui = FakeXUI("reject")
    applied, _message, new_id = sub_manager.reset_client_sub_id(xui, "client@example.test")
    assert applied is False
    assert new_id == ""
    assert xui.client["subId"] == "old-sub-id"


def test_reset_unverifiable_remote_result_is_unknown(monkeypatch):
    monkeypatch.setattr(sub_manager, "generate_secure_sub_id", lambda _length: "new-sub-id")
    xui = FakeXUI("timeout", verify="error")
    applied, _message, candidate = sub_manager.reset_client_sub_id(xui, "client@example.test")
    assert applied is None
    assert candidate == "new-sub-id"


def test_reset_without_public_url_returns_partial_and_keeps_new_id(monkeypatch):
    monkeypatch.setattr(sub_manager, "generate_secure_sub_id", lambda _length: "rotated-id")
    monkeypatch.setattr(sub_manager.config, "SUB_DOMAIN", "")
    xui = FakeXUI()
    result = sub_manager.reset_client_subscription(xui, "client@example.test")
    assert result["status"] == "partial"
    assert result["new_sub_id"] == "rotated-id"
    assert xui.client["subId"] == "rotated-id"
    assert xui.lookups == 1


def test_reset_unknown_outcome_never_returns_persistable_new_id(monkeypatch):
    monkeypatch.setattr(sub_manager, "generate_secure_sub_id", lambda _length: "candidate-id")
    monkeypatch.setattr(sub_manager.config, "SUB_DOMAIN", "sub.example.test")
    xui = FakeXUI("timeout", verify="error")
    result = sub_manager.reset_client_subscription(xui, "client@example.test")
    assert result["status"] == "unknown"
    assert "new_sub_id" not in result
    assert result["candidate_sub_id"] == "candidate-id"


def test_reset_confirmed_rotation_builds_public_url(monkeypatch):
    monkeypatch.setattr(sub_manager, "generate_secure_sub_id", lambda _length: "rotated-id")
    monkeypatch.setattr(sub_manager.config, "SUB_DOMAIN", "sub.example.test")
    monkeypatch.setattr(sub_manager.config, "SUB_PORT", 443)
    monkeypatch.setattr(sub_manager.config, "SUB_PATH", "sub")
    result = sub_manager.reset_client_subscription(FakeXUI(), "client@example.test")
    assert result["status"] == "ok"
    assert result["new_sub_url"] == "https://sub.example.test/sub/rotated-id"
