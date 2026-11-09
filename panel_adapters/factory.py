"""Fail-closed panel adapter factory."""
from __future__ import annotations

from typing import Any, Callable, Mapping

from .base import AdapterUnavailable, UnsupportedCapability
from .manual_pool import ManualPoolAdapter, ManualPoolRepository
from .xui_openapi import XUIOpenAPIAdapter


class PanelAdapterFactory:
    def __init__(self, *, panel_loader: Callable[[int], Mapping[str, Any] | None],
                 account_loader: Callable[[int], Mapping[str, Any] | None],
                 session_factory: Callable[[Mapping[str, Any]], Any] | None,
                 manual_repository: ManualPoolRepository | None = None,
                 decryptor: Callable[[str, str], str] | None = None,
                 manual_decryptor: Callable[[str], str] | None = None,
                 transaction_probe: Callable[[], bool] | None = None,
                 account_id_resolver: Callable[[int, str], int | None] | None = None,
                 operation_preflight: Callable[[str, Any, str], bool] | None = None):
        if not callable(panel_loader) or not callable(account_loader):
            raise TypeError("panel_loader and account_loader are required")
        self.panel_loader, self.account_loader = panel_loader, account_loader
        self.session_factory = session_factory
        self.manual_repository = manual_repository
        self.decryptor, self.manual_decryptor = decryptor, manual_decryptor
        self.transaction_probe = transaction_probe
        self.account_id_resolver = account_id_resolver
        self.operation_preflight = operation_preflight
        self._cache: dict[int, tuple[tuple[Any, ...], Any]] = {}

    def get_adapter(self, panel_id: int):
        if isinstance(panel_id, bool) or not isinstance(panel_id, int) or panel_id <= 0:
            raise ValueError("panel_id must be a positive integer")
        try:
            panel = self.panel_loader(panel_id)
        except Exception:
            raise AdapterUnavailable("panel lookup failed") from None
        if not isinstance(panel, Mapping):
            raise AdapterUnavailable("panel record unavailable")
        stored_id = panel.get("id", panel.get("panel_id"))
        if stored_id != panel_id:
            raise AdapterUnavailable("panel identity mismatch")
        if panel.get("deleted_at") is not None or panel.get("is_active", panel.get("enabled", True)) is False:
            raise AdapterUnavailable("panel is inactive")
        kind = str(panel.get("panel_type") or panel.get("type") or "").strip().lower().replace("_", "-")
        revision = panel.get("revision", panel.get("updated_at"))
        key = (kind, revision, panel.get("base_path"), panel.get("raw_url"), panel.get("auth_cipher"), panel.get("api_token_cipher"))
        if panel_id in self._cache and self._cache[panel_id][0] == key:
            return self._cache[panel_id][1]
        if kind in {"3x-ui", "3xui", "xui"}:
            if self.session_factory is None:
                raise AdapterUnavailable("XUI session factory is not configured")
            try:
                adapter = XUIOpenAPIAdapter(panel, self.session_factory(panel),
                    decryptor=self.decryptor, transaction_probe=self.transaction_probe,
                    account_id_resolver=self.account_id_resolver, operation_preflight=self.operation_preflight)
            except Exception:
                raise AdapterUnavailable("XUI adapter could not be constructed") from None
        elif kind in {"manual", "manual-pool", "pool"}:
            if self.manual_repository is None:
                raise AdapterUnavailable("manual pool repository is not configured")
            adapter = ManualPoolAdapter(panel, self.manual_repository, decryptor=self.manual_decryptor,
                                        account_id_resolver=self.account_id_resolver)
        elif kind == "marzban":
            raise UnsupportedCapability("Marzban adapter is registered but not implemented")
        else:
            raise UnsupportedCapability("unsupported panel type")
        self._cache[panel_id] = (key, adapter)
        return adapter

    def resolve_account_adapter(self, account_id: int):
        if isinstance(account_id, bool) or not isinstance(account_id, int) or account_id <= 0:
            raise ValueError("account_id must be a positive integer")
        try:
            account = self.account_loader(account_id)
        except Exception:
            raise AdapterUnavailable("account lookup failed") from None
        if not isinstance(account, Mapping) or account.get("deleted_at") is not None:
            raise AdapterUnavailable("active account not found")
        panel_id = account.get("panel_id")
        if isinstance(panel_id, bool) or not isinstance(panel_id, int) or panel_id <= 0:
            raise AdapterUnavailable("account has no canonical panel_id")
        return self.get_adapter(panel_id)

    def invalidate(self, panel_id: int | None = None):
        if panel_id is None: self._cache.clear()
        else: self._cache.pop(panel_id, None)
