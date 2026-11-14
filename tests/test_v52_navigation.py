from ui_v52 import render_error, render_page, render_uri, route_home


def _callbacks(page):
    if page.reply_markup is None:
        return []
    return [button.callback_data for row in page.reply_markup.inline_keyboard for button in row]


def test_all_page_kinds_have_exit():
    root = render_page("خانه", "صفحه ریشه", persona="admin", root=True, page_kind="root")
    assert root.reply_markup is None
    assert not any(value.startswith("v52:back:") for value in _callbacks(root))

    page = render_page("حساب", "نام <script>alert(1)</script>\nخط دوم",
                       persona="rep", parent_callback="v52:accounts:rep")
    form = render_page("نام جدید", "متن را ارسال کنید", persona="user",
                       page_kind="form", parent_callback="v52:profile:7")
    error = render_error("خطای <unsafe> و مسیر بازیابی", persona="user",
                         parent_callback="v52:profile:7")
    for rendered in (page, form, error):
        row = rendered.reply_markup.inline_keyboard[-1]
        assert [item.callback_data for item in row] == [
            "v52:profile:7" if rendered is not page else "v52:accounts:rep",
            f"v52:home:{'rep' if rendered is page else 'user'}",
        ]
        assert any(item.startswith("v52:home:") for item in _callbacks(rendered))
    assert "&lt;script&gt;" in page.text
    assert "alert(1)" in page.text
    assert "\n" in page.text and r"\n" not in page.text
    assert "<code>vless://x&amp;y</code>" == render_uri("vless://x&y")

    state = {
        "draft_fsm": {"step": "profile_name"},
        "v52_draft_fsm": {"step": "account_lookup"},
        "accepted_order": {"order_id": "accepted-42", "status": "pending"},
        "payment_state": {"receipt_id": "receipt-9"},
    }
    route = route_home("admin", state)
    assert route.callback_data == "v52:home:admin"
    assert route.legacy_callback == "admin:main"
    assert "draft_fsm" not in state and "v52_draft_fsm" not in state
    assert state["accepted_order"]["order_id"] == "accepted-42"
    assert state["payment_state"]["receipt_id"] == "receipt-9"
