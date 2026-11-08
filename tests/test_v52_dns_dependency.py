"""Verify dnspython is declared and its resolver imports without DNS traffic."""
from __future__ import annotations

import importlib
import re
from pathlib import Path


def test_dns_dependency_declared():
    root = Path(__file__).resolve().parents[1]
    requirements = (root / "requirements.txt").read_text(encoding="utf-8")
    declared = {}
    for raw_line in requirements.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)\s*(.*)", line)
        assert match is not None, f"unparseable requirement line: {line!r}"
        declared[match.group(1).lower().replace("_", "-")] = match.group(2).strip()

    retained = {
        "python-telegram-bot", "python-dotenv", "requests", "aiohttp",
        "tabulate", "qrcode", "cryptography",
    }
    assert retained <= declared.keys()
    assert re.fullmatch(r">=\s*2\.6\.0", declared.get("dnspython", ""))

    # Import only; constructing a resolver performs no external DNS query.
    resolver = importlib.import_module("dns.resolver")
    assert callable(resolver.Resolver)
