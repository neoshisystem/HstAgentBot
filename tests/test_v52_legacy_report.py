"""W19.3b: The legacy report entry point cannot reach a helper token or mirror abuse."""
import ast
from pathlib import Path

import internal_reporting as reporting


def test_no_helper_token_and_no_abuse_mirror(monkeypatch):
    root = Path(__file__).resolve().parents[1]
    source = (root / "rep_bot.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    assert not any(
        isinstance(node, ast.Attribute)
        and node.attr == "BOT_TOKEN"
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "backup_and_report"
        for node in ast.walk(tree)
    )
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                    and node.name == "send_admin_log")
    fragment = ast.get_source_segment(source, function)
    assert "urllib.request" not in fragment
    assert "requests." not in fragment

    routed = []
    monkeypatch.setattr(
        reporting, "_queue",
        lambda logical_key, **kwargs: routed.append((logical_key, kwargs)) or {
            "ok": True, "queued": True, "state": "pending", "delivery_id": 7,
        },
    )
    result = reporting.send_telegram_message(
        -100999, "ABUSE_REPORT private case", parse_mode="HTML",
    )
    assert result["queued"] is True
    assert len(routed) == 1
    assert routed[0][0] == "abusereport"
    assert routed[0][1]["purpose"] == "abusereport"
