"""Loading boundary and explicit contracts for Hermes/server-local integrations.

The product code remains importable without the server helper directory (useful
for tests and offline maintenance), while a missing integration fails clearly
when an operation actually tries to use it.
"""
import importlib
import os
import sys


INTEGRATION_CONTRACTS = {
    "xui_manager": {
        "attributes": ("BASE_URL",),
        "callables": (
            "allocate_pool_client",
            "extract_account_identifier",
            "format_client_deliverable",
            "format_status_report",
            "generate_qr",
            "get_client_links",
            "get_session",
            "get_sub_url",
            "inspect_client",
            "parse_client_info",
            "renew_client",
            "set_client_comment",
            "set_client_enable",
            "audit_clients_overview",
        ),
    },
    "backup_and_report": {
        "attributes": ("BOT_TOKEN",),
        "callables": ("send_telegram_message", "send_telegram_document"),
    },
}


class _UnavailableIntegration:
    def __init__(self, module_name: str, cause: Exception):
        self._module_name = module_name
        self._cause = cause

    def __getattr__(self, attribute: str):
        raise RuntimeError(
            f"Required server integration '{self._module_name}' is unavailable. "
            "Set HSTAGENTBOT_INTEGRATIONS_PATH to the directory containing the "
            "Hermes server helper modules."
        ) from self._cause

    def __repr__(self):
        return f"<UnavailableIntegration {self._module_name!r}>"


def validate_integration_contract(module_name: str, module):
    """Validate only the public attributes actually consumed by Product code."""
    contract = INTEGRATION_CONTRACTS.get(module_name)
    if not contract:
        return module

    missing = [
        name for name in contract["attributes"]
        if not hasattr(module, name)
    ]
    malformed = [
        name for name in contract["callables"]
        if not callable(getattr(module, name, None))
    ]
    if missing or malformed:
        details = []
        if missing:
            details.append("missing attributes: " + ", ".join(missing))
        if malformed:
            details.append("missing/non-callable functions: " + ", ".join(malformed))
        raise RuntimeError(
            f"Server integration '{module_name}' does not satisfy its Product contract; "
            + "; ".join(details)
        )
    return module


def load_integration(module_name: str):
    """Load a trusted server integration and validate its repository-facing API."""
    configured_path = os.getenv(
        "HSTAGENTBOT_INTEGRATIONS_PATH", "/root/.hermes/scripts"
    ).strip()
    if configured_path and configured_path not in sys.path:
        sys.path.insert(0, configured_path)
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        return _UnavailableIntegration(module_name, exc)
    return validate_integration_contract(module_name, module)
