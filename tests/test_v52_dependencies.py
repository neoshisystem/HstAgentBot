"""Acceptance coverage for v5.2 crypto runtime dependencies."""
import re
from pathlib import Path


def test_crypto_dependency_declared():
    requirements = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(
        encoding="utf-8"
    )
    declared = set()
    for raw_line in requirements.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        assert match is not None, f"unparseable requirement line: {line!r}"
        declared.add(match.group(1).lower().replace("_", "-"))

    assert {"python-telegram-bot", "requests", "aiohttp", "qrcode", "cryptography"} <= declared
    secret_markers = (
        r"(?i)\bgh[pousr]_[A-Za-z0-9]{20,}",
        r"(?i)\bgithub_pat_[A-Za-z0-9_]{20,}",
        r"(?i)\bAKIA[0-9A-Z]{16}\b",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
        r"(?i)BACKUP_ENCRYPTION_KEY\s*=\s*\S+",
    )
    assert not any(re.search(pattern, requirements) for pattern in secret_markers)
