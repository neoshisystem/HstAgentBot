from __future__ import annotations

import rep_admin_ui


def test_parent_stats_not_configs():
    parent = {"actor_tg_id": 2020, "rep_id": 202}
    children = [{
        "id": 303, "rep_code": "CHILD-303", "name": "Child 303",
        "is_active": 1, "tier": "normal", "billing_mode": "prepaid_wallet",
        "debt_toman": 0, "credit_limit_toman": 0, "account_count": 4,
        "account_configs": ["vless://private-secret"],
        "customer_email": "private@example.invalid",
    }]
    page = rep_admin_ui.show_sub_reps(parent, 202, children=children)
    assert page.kind == "aggregate"
    assert "Child 303" in page.text
    assert "حساب‌های مشتری: 4" in page.text
    assert "prepaid_wallet" in page.text
    assert "vless://private-secret" not in page.text
    assert "private@example.invalid" not in page.text
