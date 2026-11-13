"""Repository-owned compatibility loader for legacy server helper names.

The two named v5.2 integrations are internal Product modules. Optional
third-party integrations remain lazy, but no Product startup requires an
external Hermes helper directory. Secrets and transport internals are not
part of either public compatibility contract.
"""
from __future__ import annotations

import importlib
import os
import sys
from types import ModuleType
from typing import Any


INTEGRATION_CONTRACTS = {
    "xui_manager": {
        "attributes": (),
        "callables": (
            "allocate_pool_client",
            "extract_account_identifier",
            "extract_customer_name",
            "format_audit_report",
            "format_client_deliverable",
            "format_status_report",
            "generate_qr",
            "get_client_links",
            "get_sub_url",
            "inspect_client",
            "normalize_persian_digits",
            "parse_client_info",
            "parse_package_terms",
            "renew_client",
            "set_client_comment",
            "set_client_enable",
            "find_client_by_code",
            "update_client_sub_id",
            "audit_clients_overview",
        ),
    },
    "backup_and_report": {
        "attributes": (),
        "callables": ("send_telegram_message", "send_telegram_document"),
    },
}


class _UnavailableIntegration:
    def __init__(self, module_name: str, cause: Exception):
        self._module_name = module_name
        self._cause = cause

    def __getattr__(self, attribute: str):
        raise RuntimeError(
            f"Optional integration '{self._module_name}' is unavailable. "
            "Set HSTAGENTBOT_INTEGRATIONS_PATH only when deploying an explicit "
            "optional extension; Product panel and reporting helpers are internal."
        ) from self._cause

    def __repr__(self):
        return f"<UnavailableIntegration {self._module_name!r}>"


def validate_integration_contract(module_name: str, module: Any):
    """Validate the consumed API while explicitly forbidding retired secret APIs."""
    contract = INTEGRATION_CONTRACTS.get(module_name)
    if contract is None:
        return module
    missing = [
        name for name in contract["attributes"]
        if not hasattr(module, name)
    ]
    malformed = [
        name for name in contract["callables"]
        if not callable(getattr(module, name, None))
    ]
    forbidden = ("BASE_URL", "get_session") if module_name == "xui_manager" else ("BOT_TOKEN",)
    exposed = [name for name in forbidden if hasattr(module, name)]
    if missing or malformed or exposed:
        details = []
        if missing:
            details.append("missing attributes: " + ", ".join(missing))
        if malformed:
            details.append("missing/non-callable functions: " + ", ".join(malformed))
        if exposed:
            details.append("forbidden public attributes: " + ", ".join(exposed))
        raise RuntimeError(
            f"Internal integration '{module_name}' does not satisfy its Product contract; "
            + "; ".join(details)
        )
    return module


def _load_internal(module_name: str) -> ModuleType:
    module = importlib.import_module(
        "internal_panel_compat" if module_name == "xui_manager" else "internal_reporting"
    )
    return validate_integration_contract(module_name, module)


def load_integration(module_name: str):
    """Resolve named internal integrations first; optional imports remain lazy."""
    if module_name in {"xui_manager", "backup_and_report"}:
        return _load_internal(module_name)

    configured_path = os.getenv("HSTAGENTBOT_INTEGRATIONS_PATH", "").strip()
    if configured_path and configured_path not in sys.path:
        sys.path.insert(0, configured_path)
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        return _UnavailableIntegration(module_name, exc)
    return module


__all__ = ["INTEGRATION_CONTRACTS", "validate_integration_contract", "load_integration"]
