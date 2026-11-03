"""Loading boundary for Hermes/server-local integrations.

The product code remains importable without the server helper directory (useful
for tests and offline maintenance), while a missing integration fails clearly
when an operation actually tries to use it.
"""
import importlib
import os
import sys


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


def load_integration(module_name: str):
    """Load a trusted server integration from the configured directory."""
    configured_path = os.getenv("HSTAGENTBOT_INTEGRATIONS_PATH", "/root/.hermes/scripts").strip()
    if configured_path and configured_path not in sys.path:
        sys.path.insert(0, configured_path)
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        return _UnavailableIntegration(module_name, exc)
