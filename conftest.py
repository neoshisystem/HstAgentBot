"""Server-side pytest bootstrap: stop a local ``.env`` from changing test behaviour.

``config.py`` calls ``load_dotenv(dotenv_path=ENV_PATH)`` at import time, so every
flag in a developer's ``.env`` leaks into the test session. That is not cosmetic:

* ``V52_MIGRATIONS_ENABLED=1`` makes ``database.init_db`` apply the migrations, so the
  explicit ``apply_migrations(...)`` call a test then makes returns ``[]`` and
  ``assert len(...) == 18`` fails. One local run showed **54 failures** for a reason
  that had nothing to do with the code under test.
* ``V52_UI_ENABLED=1`` flips ``_v52_ui_enabled()`` on, which routes the legacy
  ``reset_sub`` callbacks into the v5.2 preview and breaks the tests that pin the
  legacy behaviour.

CI has no ``.env``, so the canonical run sees these flags unset. This module pins them
before ``config`` is first imported; ``load_dotenv`` does not override a variable that
is already present, so the CI behaviour is reproduced exactly. ``setdefault`` is used
deliberately: a flag the caller exported on purpose is left alone, and a test that
needs one of them can still set it with ``monkeypatch.setenv``.
"""
from __future__ import annotations

import os

#: Flags a local ``.env`` may leak into the test session. Empty reproduces CI.
_LEAKED_FLAGS = ("V52_MIGRATIONS_ENABLED", "V52_UI_ENABLED", "BACKUP_ENCRYPTION_KEY")

for _flag in _LEAKED_FLAGS:
    os.environ.setdefault(_flag, "")

# ``config`` parses this one with ``int(...)``, so an empty string would raise; CI sees
# the ``"0"`` default that ``os.getenv("ADMIN_TELEGRAM_ID", "0")`` falls back to.
os.environ.setdefault("ADMIN_TELEGRAM_ID", "0")

del _flag
