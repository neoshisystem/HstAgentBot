"""Isolated, offline-only fixtures for HstAgentBot v5.2 tests.

This module intentionally has no dependency on tests.conftest and performs no
network operations.  It is loaded by file path via importlib in each test.
"""
from __future__ import annotations

from contextlib import contextmanager
import importlib.util
from pathlib import Path
import socket
import sys
import types
import urllib.request
from typing import Any


class PanelSeed:
    __slots__ = ("panel_id", "name", "base_url", "api_secret", "marker")

    def __init__(self, panel_id: int, name: str, base_url: str,
                 api_secret: str, marker: str = "V52_SYNTHETIC_PANEL_MARKER"):
        self.panel_id = panel_id
        self.name = name
        self.base_url = base_url
        self.api_secret = api_secret
        self.marker = marker

    def __repr__(self):
        return (f"PanelSeed(panel_id={self.panel_id!r}, name={self.name!r}, "
                f"base_url={self.base_url!r}, api_secret=<redacted>, marker={self.marker!r})")


def guard_network(monkeypatch) -> list[dict[str, Any]]:
    """Block common stdlib socket/URL entry points and record attempted use."""
    attempts: list[dict[str, Any]] = []

    def blocked(kind: str):
        def _raise(*args, **kwargs):
            attempts.append({"kind": kind, "args": args, "kwargs": kwargs})
            raise AssertionError(f"real network access is forbidden in v5.2 tests: {kind}")
        return _raise

    monkeypatch.setattr(socket, "create_connection", blocked("socket.create_connection"))
    monkeypatch.setattr(socket.socket, "connect", blocked("socket.socket.connect"))
    monkeypatch.setattr(urllib.request, "urlopen", blocked("urllib.request.urlopen"))
    return attempts


def load_database_module(product_root: str | Path, monkeypatch, tmp_path):
    """Load the real Product database.py with harmless local-only dependencies.

    config.DB_PATH deliberately points at a sentinel production-like path.  The
    caller must use make_db before any database operation; that context changes
    database.DB_PATH to a fresh tmp_path file and restores the exact old object.
    """
    root = Path(product_root).resolve()
    source = root / "database.py"
    if not source.is_file():
        raise FileNotFoundError(source)

    sentinel = str((Path(tmp_path) / "DO_NOT_OPEN_PRODUCTION_DB.sqlite3").resolve())
    config = types.ModuleType("config")
    config.DB_PATH = sentinel
    config.REPRESENTATIVES = {}
    config.ADMIN_TELEGRAM_ID = 0
    config.MAIN_GROUP_ID = 0
    config.OPERATIONAL_LOGS_TOPIC_ID = 10
    config.BACKUPS_TOPIC_ID = 20
    config.ADMIN_DM_LOGS_TOPIC_ID = 0

    class OfflineIntegration:
        def __getattr__(self, name):
            raise AssertionError(
                f"server integration access is forbidden in isolated tests: {name}"
            )

        def __repr__(self):
            return "<OfflineIntegration: network disabled>"

    integrations = types.ModuleType("server_integrations")
    integrations.load_integration = lambda module_name: OfflineIntegration()

    # monkeypatch restores pre-existing modules at test teardown.
    monkeypatch.setitem(sys.modules, "config", config)
    monkeypatch.setitem(sys.modules, "server_integrations", integrations)

    spec = importlib.util.spec_from_file_location(
        "_hstagentbot_database_w01_1", source
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"could not load Product database module from {source}")
    database = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(database)
    return database, sentinel


@contextmanager
def make_db(database, monkeypatch, tmp_path, *, filename: str = "v52-isolated.sqlite3"):
    """Initialize a fresh temporary DB and restore database.DB_PATH on exit."""
    original_path = database.DB_PATH
    isolated_path = (Path(tmp_path) / filename).resolve()

    try:
        original_resolved = Path(str(original_path)).expanduser().resolve()
    except (TypeError, OSError):
        original_resolved = None
    if original_resolved == isolated_path:
        raise AssertionError("temporary DB path must differ from the original DB_PATH")
    if isolated_path.exists():
        raise FileExistsError(f"refusing to reuse an existing test DB: {isolated_path}")

    monkeypatch.setattr(database, "DB_PATH", str(isolated_path))
    try:
        database.init_db()
        if not isolated_path.is_file():
            raise AssertionError("database.init_db() did not create the temporary DB")
        yield isolated_path
    finally:
        # Restore the exact pre-context value, including Path-vs-str identity.
        monkeypatch.setattr(database, "DB_PATH", original_path)


def seed_panel(
    *, panel_id: int = 101, name: str = "V52 synthetic panel",
    base_url: str | None = None, api_secret: str = "V52_FAKE_PANEL_SECRET_101",
) -> PanelSeed:
    """Return synthetic panel identity only; never contacts or seeds a real panel."""
    panel_id = int(panel_id)
    if panel_id <= 0:
        raise ValueError("panel_id must be positive")
    return PanelSeed(
        panel_id=panel_id,
        name=str(name),
        base_url=base_url or f"https://panel-{panel_id}.invalid",
        api_secret=str(api_secret),
    )


def seed_actor(
    database, *, actor_tg_id: int = 101, rep_id: int = 202,
    rep_tg_id: int = 303, wallet_balance: int = 0,
):
    """Seed one synthetic representative, actor and Telegram-ID wallet."""
    actor_tg_id, rep_id, rep_tg_id = int(actor_tg_id), int(rep_id), int(rep_tg_id)
    if min(actor_tg_id, rep_id, rep_tg_id) <= 0:
        raise ValueError("fixture identities must be positive")
    with database.get_db() as conn:
        conn.execute(
            """
            INSERT INTO representatives
                (id, rep_code, name, telegram_id, group_id, topic_id,
                 scope_filter, balance_toman, is_active)
            VALUES (?, ?, ?, ?, 0, 0, ?, 0, 1)
            ON CONFLICT(id) DO UPDATE SET
                rep_code=excluded.rep_code, name=excluded.name,
                telegram_id=excluded.telegram_id, scope_filter=excluded.scope_filter,
                is_active=1
            """,
            (rep_id, f"V52-REP-{rep_id}", f"V52 Representative {rep_id}",
             rep_tg_id, f"v52:{actor_tg_id}"),
        )
        conn.execute(
            """
            INSERT INTO bot_users
                (telegram_id, role, rep_id, username, full_name, is_blocked)
            VALUES (?, 'end_user', ?, ?, ?, 0)
            ON CONFLICT(telegram_id) DO UPDATE SET
                role='end_user', rep_id=excluded.rep_id,
                username=excluded.username, full_name=excluded.full_name,
                is_blocked=0
            """,
            (actor_tg_id, rep_id, f"v52_user_{actor_tg_id}",
             f"V52 Synthetic Actor {actor_tg_id}"),
        )
        conn.execute(
            """
            INSERT INTO user_wallets (user_id, balance_toman)
            VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET balance_toman=excluded.balance_toman
            """,
            (actor_tg_id, int(wallet_balance)),
        )
    return {
        "actor_tg_id": actor_tg_id,
        "rep_id": rep_id,
        "rep_tg_id": rep_tg_id,
        "wallet_user_id": actor_tg_id,
        "marker": f"V52_SYNTHETIC_ACTOR_{actor_tg_id}",
    }


def seed_bot(*, bot_id: int = 303, token: str = "V52_FAKE_BOT_TOKEN_303",
             result: Any = None):
    """Return a local fake bot identity; token is always a non-routable marker."""
    bot_id = int(bot_id)
    if bot_id <= 0:
        raise ValueError("bot_id must be positive")
    return {
        "bot_id": bot_id,
        "token": str(token),
        "marker": f"V52_SYNTHETIC_BOT_{bot_id}",
        "telegram": FakeTelegram(result=result, bot_id=bot_id),
    }


def seed_account(
    database, *, account_id: int = 303, rep_id: int = 202,
    owner_tg_id: int = 101, account_code: str | None = None,
    email: str | None = None, sub_id: str = "V52_FAKE_SUBSCRIPTION_MARKER",
):
    """Insert one synthetic account row in the temporary Product database."""
    account_id, rep_id, owner_tg_id = int(account_id), int(rep_id), int(owner_tg_id)
    if min(account_id, rep_id, owner_tg_id) <= 0:
        raise ValueError("fixture identities must be positive")
    code = account_code or f"V52-ACCOUNT-{account_id}"
    address = email or f"v52-account-{account_id}@example.invalid"
    with database.get_db() as conn:
        conn.execute(
            """
            INSERT INTO accounts
                (id, rep_id, account_code, email, customer_name, sub_id,
                 enable, total_gb, expiry_time, owner_tg_id)
            VALUES (?, ?, ?, ?, ?, ?, 1, 10.0, 0, ?)
            """,
            (account_id, rep_id, code, address,
             f"V52 Synthetic Customer {account_id}", str(sub_id), owner_tg_id),
        )
    return {
        "account_id": account_id,
        "rep_id": rep_id,
        "owner_tg_id": owner_tg_id,
        "account_code": code,
        "email": address,
        "sub_id": str(sub_id),
        "marker": f"V52_SYNTHETIC_ACCOUNT_{account_id}",
    }


class FakeAdapter:
    """Pure in-memory panel adapter with injectable results and mutation audit."""
    READ_METHODS = frozenset({
        "verify_connection", "get_client", "list_clients", "get_client_configs",
        "reconcile",
    })
    MUTATION_METHODS = frozenset({
        "create_client", "renew_client", "add_addon", "rotate_subscription_url",
        "rotate_connection_credentials", "disconnect_existing_sessions",
        "set_client_enabled", "set_traffic", "set_expiry", "delete_client",
    })

    def __init__(self, result: Any = None):
        self.result = result
        self.mutation_calls: list[dict[str, Any]] = []
        self.read_calls: list[dict[str, Any]] = []

    def __getattr__(self, name: str):
        if name not in self.READ_METHODS and name not in self.MUTATION_METHODS:
            raise AttributeError(name)

        def _fake_call(*args, **kwargs):
            call = {"method": name, "args": args, "kwargs": kwargs}
            if name in self.MUTATION_METHODS:
                self.mutation_calls.append(call)
            else:
                self.read_calls.append(call)
            return self.result

        return _fake_call


class FakeTelegram:
    """Local Telegram test double; records calls and never owns an HTTP client."""
    def __init__(self, result: Any = None, *, bot_id: int = 303):
        self.result = result
        self.bot_id = int(bot_id)
        self.calls: list[dict[str, Any]] = []

    def __getattr__(self, name: str):
        if not (name.startswith("send_") or name.startswith("edit_")
                or name.startswith("answer_") or name.startswith("delete_")):
            raise AttributeError(name)

        def _fake_call(*args, **kwargs):
            self.calls.append({"method": name, "args": args, "kwargs": kwargs})
            return self.result

        return _fake_call
