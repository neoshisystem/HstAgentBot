"""Fail-closed validation and secret-redaction tests for v5.2 settings."""
from __future__ import annotations

import base64
import importlib.util
from pathlib import Path
import sys

import pytest


def _load_config(monkeypatch, bot_token):
    monkeypatch.setenv("BOT_TOKEN", bot_token)
    source = Path(__file__).resolve().parents[1] / "config.py"
    spec = importlib.util.spec_from_file_location(
        "_hstagentbot_v52_config_under_test", source
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def test_settings_fail_closed_and_defaults(monkeypatch):
    bot_token = "V52_BOT_TOKEN_MUST_NOT_LEAK"
    config = _load_config(monkeypatch, bot_token)

    defaults = config.read_v52_settings(environ={})
    assert defaults.sub_gateway_bind == "127.0.0.1:8088"
    assert defaults.sub_gateway_host == "127.0.0.1"
    assert defaults.sub_gateway_port == 8088
    assert defaults.migrations_enabled is False
    assert defaults.sub_gateway_enabled is False

    # A rollout request cannot expose the public gateway without its public URL.
    no_public_origin = config.read_v52_settings(
        environ={"V52_SUB_GATEWAY_ENABLED": "true"}
    )
    assert no_public_origin.sub_gateway_rollout_enabled is True
    assert no_public_origin.sub_gateway_enabled is False

    for invalid_bind in ("bad_host:8088", "127.0.0.1:70000", "localhost:abc"):
        with pytest.raises(ValueError):
            config.read_v52_settings(environ={"SUB_GATEWAY_BIND": invalid_bind})

    for invalid_key in ("not-a-key", "A" * 42, "!" * 44):
        with pytest.raises(ValueError):
            config.read_v52_settings(
                environ={"BACKUP_ENCRYPTION_KEY": invalid_key}
            )

    with pytest.raises(ValueError):
        config.read_v52_settings(
            environ={"TRUSTED_PROXY_CIDRS": "127.0.0.1/32,not-a-cidr"}
        )
    with pytest.raises(ValueError):
        config.read_v52_settings(
            environ={"SUB_GATEWAY_BASE_URL": "http://subs.example.test"}
        )

    key = base64.urlsafe_b64encode(b"V" * 32).decode("ascii").rstrip("=")
    valid = config.read_v52_settings(
        environ={
            "BACKUP_ENCRYPTION_KEY": key,
            "V52_BACKUP_ENCRYPTION_ENABLED": "true",
            "SUB_GATEWAY_BIND": "127.0.0.1:8088",
            "SUB_GATEWAY_BASE_URL": "https://subs.example.test/base/",
            "V52_SUB_GATEWAY_ENABLED": "true",
            "V52_MIGRATIONS_ENABLED": "true",
            "V52_DNS_VERIFICATION_ENABLED": "false",
            "TRUSTED_PROXY_CIDRS": "127.0.0.1/32,2001:db8::/32",
        }
    )
    assert valid.backup_encryption_key == key
    assert valid.backup_encryption_enabled is True
    assert valid.sub_gateway_enabled is True
    assert valid.sub_gateway_base_url == "https://subs.example.test/base"
    assert valid.trusted_proxy_cidrs == ("127.0.0.1/32", "2001:db8::/32")
    assert valid.migrations_enabled is True
    assert valid.dns_verification_enabled is False

    rendered = repr(valid)
    assert str(valid) == rendered
    assert bot_token not in rendered
    assert key not in rendered
    assert "<redacted>" in rendered
