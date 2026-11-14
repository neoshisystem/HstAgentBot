"""Standalone aiohttp read-only subscription HTTP endpoint; never starts bot workers."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import os
import re
import time
from collections import defaultdict, deque
from contextlib import contextmanager
from typing import Any

from aiohttp import web

import config
import database
import gateway_service

_LOG = logging.getLogger("hstagentbot.subscription_gateway")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{40,64}$")


class _RateLimiter:
    def __init__(self, limit: int = 60, window: int = 60):
        self.limit, self.window = int(limit), int(window)
        self.events: dict[tuple[str, str], deque[float]] = defaultdict(deque)

    def allow(self, ip: str, token_digest: str, now: float | None = None) -> bool:
        stamp = time.monotonic() if now is None else float(now)
        key = (ip, token_digest)
        queue = self.events[key]
        while queue and queue[0] <= stamp - self.window:
            queue.popleft()
        if len(queue) >= self.limit:
            return False
        queue.append(stamp)
        # Bound attacker-controlled key growth without retaining request secrets.
        if len(self.events) > 4096:
            for old_key in tuple(self.events)[:1024]:
                if not self.events[old_key]:
                    self.events.pop(old_key, None)
        return True


def _default_adapter_factory():
    """Build read-only adapters from the canonical DB registry; secrets stay encrypted at rest."""
    import requests
    from crypto_store import decrypt_secret
    from panel_adapters.factory import PanelAdapterFactory
    from panel_adapters.manual_pool import ManualPoolRepository

    def panel_loader(panel_id):
        with database.get_db() as conn:
            row = conn.execute("SELECT * FROM panels WHERE id=?", (int(panel_id),)).fetchone()
            return dict(row) if row else None

    def account_loader(account_id):
        with database.get_db() as conn:
            row = conn.execute("SELECT * FROM accounts WHERE id=?", (int(account_id),)).fetchone()
            return dict(row) if row else None

    def session_factory(_panel):
        session = requests.Session()
        session.trust_env = False
        session.verify = True
        return session

    def account_id_resolver(panel_id, email):
        with database.get_db() as conn:
            row = conn.execute(
                "SELECT id FROM accounts WHERE panel_id=? AND email=? AND deleted_at IS NULL",
                (int(panel_id), str(email)),
            ).fetchone()
            return int(row["id"]) if row else None

    return PanelAdapterFactory(
        panel_loader=panel_loader,
        account_loader=account_loader,
        session_factory=session_factory,
        manual_repository=ManualPoolRepository(config.DB_PATH),
        decryptor=decrypt_secret,
        manual_decryptor=lambda cipher: decrypt_secret(cipher, "hstagent/columns/v1"),
        transaction_probe=lambda: False,
        account_id_resolver=account_id_resolver,
    )


def create_app(
    *, db_factory=None, adapter_factory=None, canonical_host: str | None = None,
    trusted_proxy_cidrs=(), rate_limit: int = 60, max_body_bytes: int = 1024,
) -> web.Application:
    app = web.Application(client_max_size=int(max_body_bytes))
    app["db_factory"] = db_factory or database.get_db
    app["adapter_factory"] = adapter_factory if adapter_factory is not None else _default_adapter_factory()
    app["canonical_host"] = canonical_host if canonical_host is not None else getattr(config, "SUB_DOMAIN", "")
    app["trusted_proxy_cidrs"] = tuple(trusted_proxy_cidrs)
    app["rate_limiter"] = _RateLimiter(rate_limit)
    app["max_body_bytes"] = int(max_body_bytes)
    app.router.add_get("/s/{token}", handle_subscription)
    return app


def _remote_ip(request: web.Request) -> str:
    remote = request.remote or ""
    try:
        return str(ipaddress.ip_address(remote))
    except ValueError:
        return "unknown"


@contextmanager
def _connection(factory):
    value = factory()
    if hasattr(value, "__enter__"):
        with value as conn:
            yield conn
    else:
        try:
            yield value
        finally:
            close = getattr(value, "close", None)
            if callable(close):
                close()


async def handle_subscription(request: web.Request) -> web.Response:
    token = request.match_info.get("token", "")
    no_store = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    if not _TOKEN_RE.fullmatch(token):
        return web.Response(status=404, body=b"", headers=no_store, content_type="text/plain", charset="utf-8")
    if request.content_length is not None and request.content_length > request.app["max_body_bytes"]:
        return web.Response(status=400, body=b"", headers=no_store, content_type="text/plain", charset="utf-8")
    selected_format = request.query.get("format", "base64")
    if selected_format not in {"raw", "base64"}:
        return web.Response(status=400, body=b"", headers=no_store, content_type="text/plain", charset="utf-8")
    ip = _remote_ip(request)
    digest = hashlib.sha256(token.encode("ascii")).hexdigest()
    if not request.app["rate_limiter"].allow(ip, digest):
        return web.Response(status=429, body=b"", headers={**no_store, "Retry-After": "60"},
                            content_type="text/plain", charset="utf-8")
    forwarded = request.headers.get("X-Forwarded-Host")
    try:
        with _connection(request.app["db_factory"]) as conn:
            result = await gateway_service.resolve_subscription_request(
                conn, token, request.host,
                canonical_host=request.app["canonical_host"],
                forwarded_host=forwarded, remote_ip=ip,
                trusted_proxy_cidrs=request.app["trusted_proxy_cidrs"],
                adapter_factory=request.app["adapter_factory"],
            )
    except Exception:
        _LOG.warning("subscription request failed status=503")
        return web.Response(status=503, body=b"", headers=no_store, content_type="text/plain", charset="utf-8")
    if result.status != 200:
        _LOG.info("subscription request status=%s", result.status)
        return web.Response(status=result.status, body=b"", headers=no_store,
                            content_type="text/plain", charset="utf-8")
    try:
        body = gateway_service.encode_subscription(result.body, selected_format)
    except ValueError:
        return web.Response(status=400, body=b"", headers=no_store, content_type="text/plain", charset="utf-8")
    _LOG.info("subscription request status=200 bytes=%d", len(body))
    return web.Response(status=200, body=body, headers=no_store,
                        content_type="text/plain", charset="utf-8")


async def _serve() -> None:
    app = create_app()
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    host = os.getenv("SUB_BIND_HOST", "127.0.0.1")
    site = web.TCPSite(runner, host=host, port=int(getattr(config, "SUB_PORT", 8080)))
    await site.start()
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
