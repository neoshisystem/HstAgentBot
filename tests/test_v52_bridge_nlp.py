import nlp_matcher
def test_legacy_reset_is_preview(monkeypatch):
    monkeypatch.setattr(nlp_matcher.database, "get_setting", lambda *a, **k: "1")
    monkeypatch.setattr(nlp_matcher.database, "get_account_by_code_or_email", lambda _c: {"id": 42})
    monkeypatch.setattr(nlp_matcher.database, "get_pricing_plans", lambda **_k: [])
    kb = nlp_matcher.build_account_keyboard("ACC-42", role="rep", pending_res={})
    callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert "confirm_reset_sub:ACC-42" in callbacks
    assert "reset_sub:ACC-42" not in callbacks
    assert "v52:sub:42" in callbacks
    assert all(len(x.encode("utf-8")) <= 64 for x in callbacks)
