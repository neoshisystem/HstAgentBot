from __future__ import annotations

import config
import panel_admin_ui


def test_lifecycle_single_truth(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_TELEGRAM_ID", 9901)
    root = {"actor_tg_id": 9901}
    page = panel_admin_ui.preview_lifecycle(
        root, {"id": 42, "lifecycle": "drain", "is_active": True,
               "allow_new_sales": True, "allow_renewals": True}
    )
    assert page.kind == "preview"
    assert "Lifecycle (منبع حقیقت): drain" in page.text
    assert "فروش جدید: مسدود" in page.text
    assert "تمدید: مسدود" in page.text
    assert "پرچم‌های نمایشی از lifecycle محاسبه شده‌اند" in page.text
    flags = panel_admin_ui.lifecycle_flags("sold_out")
    assert flags["new_sales"] is False
    assert flags["renewals"] is True
