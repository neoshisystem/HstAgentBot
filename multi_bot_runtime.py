"""Isolated multi-Application Telegram runtime for W10.3."""
from __future__ import annotations
import inspect
import sqlite3
from typing import Any, Callable
from telegram.ext import Application
from crypto_store import CryptoStoreError, decrypt_secret


class MultiBotRuntimeError(RuntimeError):
    """A registered bot could not be started or stopped safely."""


_ACTIVE: dict[int, dict[str, Any]] = {}


async def _await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def build_bot_application(
    token: str, *, bot_id: int, database: Any, scheduler: Any,
    handler_factory: Callable[..., Any],
    application_factory: Callable[[str], Any] | None = None,
):
    """Construct a bot-scoped PTB Application with injected shared singletons."""
    if not isinstance(token, str) or not token:
        raise MultiBotRuntimeError("decrypted bot token is empty")
    if isinstance(bot_id, bool) or not isinstance(bot_id, int) or bot_id <= 0:
        raise MultiBotRuntimeError("bot_id must be a positive integer")
    application = (
        Application.builder().token(token).build()
        if application_factory is None else application_factory(token)
    )
    bot_data = getattr(application, "bot_data", None)
    if not isinstance(bot_data, dict):
        raise MultiBotRuntimeError("Application has no independent bot_data mapping")
    bot_data["bot_id"] = bot_id
    bot_data["database"] = database
    bot_data["scheduler"] = scheduler

    handlers = handler_factory(application, bot_id)
    if handlers is not None:
        selected = handlers if isinstance(handlers, (list, tuple, set)) else (handlers,)
        add_handler = getattr(application, "add_handler", None)
        if not callable(add_handler):
            raise MultiBotRuntimeError("Application does not support handler registration")
        for handler in selected:
            add_handler(handler)
    return application


async def _cleanup_failed(application: Any, flags: dict[str, bool]) -> None:
    updater = getattr(application, "updater", None)
    if flags.get("polling") and updater is not None:
        try:
            await _await(updater.stop())
        except Exception:
            pass
        flags["polling"] = False
    if flags.get("started"):
        try:
            await _await(application.stop())
        except Exception:
            pass
        flags["started"] = False
    if flags.get("initialized"):
        try:
            await _await(application.shutdown())
        except Exception:
            pass
        flags["initialized"] = False


async def run_registered_bots(
    conn: sqlite3.Connection, *, database: Any, scheduler: Any,
    handler_factory: Callable[..., Any],
    application_factory: Callable[[str], Any] | None = None,
) -> dict[str, Any]:
    """Start every active bot independently; a failed bot cannot stop another."""
    rows = conn.execute(
        "SELECT bot_id,token_cipher FROM bot_registry WHERE status='active' ORDER BY bot_id"
    ).fetchall()
    runtime: dict[str, Any] = {
        "applications": {}, "records": {}, "started_bot_ids": [],
        "failed_bot_ids": [], "failures": {}, "scheduler": scheduler,
        "database": database, "stopped": False,
    }
    application_ids: set[int] = set()
    for row in rows:
        bot_id = int(row["bot_id"])
        current = _ACTIVE.get(bot_id)
        if current is not None and not current.get("stopped", False):
            runtime["applications"][bot_id] = current["application"]
            runtime["records"][bot_id] = current
            runtime["started_bot_ids"].append(bot_id)
            continue

        application = None
        flags = {"initialized": False, "started": False, "polling": False}
        try:
            cipher = row["token_cipher"]
            if not isinstance(cipher, str) or not cipher:
                raise MultiBotRuntimeError("registered bot has no encrypted token")
            try:
                token = decrypt_secret(cipher, "hstagent/columns/v1")
            except CryptoStoreError:
                raise MultiBotRuntimeError("registered bot credential could not be decrypted") from None
            application = build_bot_application(
                token, bot_id=bot_id, database=database, scheduler=scheduler,
                handler_factory=handler_factory, application_factory=application_factory,
            )
            if id(application) in application_ids:
                raise MultiBotRuntimeError("application factory reused an Application instance")
            application_ids.add(id(application))
            if getattr(application, "user_data", None) is None:
                raise MultiBotRuntimeError("Application has no bot-scoped user_data mapping")
            updater = getattr(application, "updater", None)
            if updater is None:
                raise MultiBotRuntimeError("Application has no polling updater")

            await _await(application.initialize())
            flags["initialized"] = True
            await _await(application.start())
            flags["started"] = True
            # Pending updates are intentionally retained at startup.
            await _await(updater.start_polling(drop_pending_updates=False))
            flags["polling"] = True

            record = {"application": application, "flags": flags, "stopped": False}
            _ACTIVE[bot_id] = record
            runtime["applications"][bot_id] = application
            runtime["records"][bot_id] = record
            runtime["started_bot_ids"].append(bot_id)
        except Exception as exc:
            if application is not None:
                await _cleanup_failed(application, flags)
            runtime["failed_bot_ids"].append(bot_id)
            # Keep exception messages private: only a safe type name is exposed.
            runtime["failures"][bot_id] = type(exc).__name__
    return runtime


async def stop_registered_bots(runtime: dict[str, Any]) -> dict[str, Any]:
    """Stop each bot's updater and Application once; repeated calls are no-ops."""
    applications = runtime.get("applications", {})
    records = runtime.setdefault("records", {})
    stopped: list[int] = []
    failed: dict[int, str] = {}
    replayed = True
    for raw_bot_id, application in list(applications.items()):
        bot_id = int(raw_bot_id)
        record = records.get(bot_id) or _ACTIVE.get(bot_id)
        if record is None:
            record = {
                "application": application,
                "flags": {"initialized": True, "started": True, "polling": True},
                "stopped": False,
            }
            records[bot_id] = record
        if record.get("stopped", False):
            stopped.append(bot_id)
            continue
        replayed = False
        flags = record["flags"]
        try:
            updater = getattr(application, "updater", None)
            if flags.get("polling") and updater is not None:
                await _await(updater.stop())
                flags["polling"] = False
            if flags.get("started"):
                await _await(application.stop())
                flags["started"] = False
            if flags.get("initialized"):
                await _await(application.shutdown())
                flags["initialized"] = False
            record["stopped"] = True
            if _ACTIVE.get(bot_id) is record:
                _ACTIVE.pop(bot_id, None)
            stopped.append(bot_id)
        except Exception as exc:
            failed[bot_id] = type(exc).__name__
    runtime["stopped"] = not failed and all(
        record.get("stopped", False) for record in records.values()
    )
    return {"stopped_bot_ids": stopped, "failed": failed, "replayed": replayed}


__all__ = [
    "MultiBotRuntimeError", "build_bot_application",
    "run_registered_bots", "stop_registered_bots",
]
