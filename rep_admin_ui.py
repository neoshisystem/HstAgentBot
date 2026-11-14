"""Transport-neutral v5.2 representative-tree administration pages."""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

import config
import database
import rep_tree_service
from ui_v52 import button, render_error, render_page


_BILLING_MODES = {"credit", "prepaid_wallet"}
_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def _actor(ctx) -> tuple[int | None, int | None]:
    if isinstance(ctx, Mapping):
        tg_id = ctx.get("actor_tg_id", ctx.get("telegram_id"))
        rep_id = ctx.get("rep_id")
    else:
        tg_id = getattr(ctx, "actor_tg_id", getattr(ctx, "telegram_id", None))
        rep_id = getattr(ctx, "rep_id", None)
    try:
        tg_id = int(tg_id)
        if tg_id <= 0:
            tg_id = None
    except (TypeError, ValueError):
        tg_id = None
    try:
        rep_id = int(rep_id)
        if rep_id <= 0:
            rep_id = None
    except (TypeError, ValueError):
        rep_id = None
    return tg_id, rep_id


def _is_root(ctx) -> bool:
    tg_id, _ = _actor(ctx)
    try:
        root_id = int(getattr(config, "ADMIN_TELEGRAM_ID", 0) or 0)
    except (TypeError, ValueError):
        root_id = 0
    return root_id > 0 and tg_id == root_id


def _positive(value, name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if result <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return result


def _may_manage_parent(ctx, parent_rep_id: int) -> bool:
    if _is_root(ctx):
        return True
    _, actor_rep_id = _actor(ctx)
    return actor_rep_id == parent_rep_id


def _safe_child(item: Mapping) -> dict:
    """Allow aggregate representative fields only; strip customer/config data."""
    allowed = (
        "id", "rep_code", "name", "is_active", "tier", "billing_mode",
        "debt_toman", "credit_limit_toman", "account_count", "child_count",
    )
    return {key: item.get(key) for key in allowed if key in item}


def _load_child_summaries(parent_rep_id: int) -> list[dict]:
    with database.get_db() as conn:
        rows = conn.execute(
            """
            SELECT child.id,child.rep_code,child.name,child.is_active,child.tier,
                   edge.billing_mode,edge.debt_toman,edge.credit_limit_toman,
                   COUNT(DISTINCT account.id) AS account_count
            FROM representatives child
            LEFT JOIN rep_edges edge ON edge.child_rep_id=child.id AND edge.active=1
            LEFT JOIN accounts account ON account.rep_id=child.id AND account.deleted_at IS NULL
            WHERE child.parent_rep_id=? AND child.deleted_at IS NULL
            GROUP BY child.id
            ORDER BY child.id
            """,
            (parent_rep_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def show_sub_reps(
    ctx,
    parent_rep_id: int,
    children: Sequence[Mapping] | None = None,
    *,
    persona: str = "rep",
    parent_callback: str | None = None,
):
    """Show direct child summaries only; never query or render child customer configs."""
    try:
        parent_id = _positive(parent_rep_id, "parent_rep_id")
        if not _may_manage_parent(ctx, parent_id):
            raise PermissionError("only the parent representative or root may view this tree")
        source = _load_child_summaries(parent_id) if children is None else children
        safe = [_safe_child(item) for item in source if isinstance(item, Mapping)]
        lines = [f"نمایندهٔ والد: {parent_id}", f"تعداد زیرمجموعهٔ مستقیم: {len(safe)}"]
        rows = []
        for item in safe:
            child_id = _positive(item.get("id"), "child rep id")
            mode = str(item.get("billing_mode") or "نامشخص")
            if mode not in _BILLING_MODES:
                mode = "نامشخص"
            debt = item.get("debt_toman", 0)
            limit = item.get("credit_limit_toman", 0)
            count = item.get("account_count", 0)
            child_name = str(item.get("name") or item.get("rep_code") or child_id)
            lines.append(
                f"{child_name} (#{child_id}) — حساب‌های مشتری: {count}؛ "
                f"مدل مالی: {mode}؛ بدهی/مصرف اعتبار: {debt}؛ سقف اعتبار: {limit}"
            )
            if _is_root(ctx):
                rows.append([button(f"مدیریت نماینده {child_id}", f"v52:repedit:{child_id}")])
        if not safe:
            lines.append("زیرنمایندهٔ مستقیمی ثبت نشده است.")
        if _is_root(ctx):
            rows.append([button("ساخت نماینده", f"v52:repnew:{parent_id}")])
        return render_page("زیرنمایندگان", "\n".join(lines),
                           persona="admin" if _is_root(ctx) else persona,
                           page_kind="aggregate", parent_callback=parent_callback, rows=rows)
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona=persona, parent_callback=parent_callback)


def begin_sub_rep(
    ctx, parent_rep_id: int, *, parent: Mapping | None = None,
    persona: str = "rep", parent_callback: str | None = None,
):
    """Open a child-representative draft only for root or its own eligible parent."""
    try:
        parent_id = _positive(parent_rep_id, "parent_rep_id")
        if not _may_manage_parent(ctx, parent_id):
            raise PermissionError("representative may create children only under its own identity")
        record = parent
        if record is None:
            record = database.get_rep_by_id(parent_id)
        if record is None:
            raise ValueError("parent representative does not exist")
        if not bool(record.get("is_active", 1)) or record.get("deleted_at") is not None:
            raise ValueError("inactive parent cannot create children")
        if not bool(record.get("can_create_sub_reps", 0)) or int(record.get("max_sub_reps") or 0) <= 0:
            raise ValueError("max_sub_reps=0 or can_create_sub_reps=false forbids child creation")
        _, actor_rep_id = _actor(ctx)
        if not _is_root(ctx) and int(record.get("telegram_id") or 0) != _actor(ctx)[0]:
            raise PermissionError("actor does not own the parent representative")
        return render_page(
            "ساخت زیرنماینده",
            f"والد: {parent_id}\nمدل مالی را صریح انتخاب کنید: credit (بدهی/سقف اعتبار) یا "
            "prepaid_wallet (کیف‌پول پیش‌پرداخت). parent_shared پشتیبانی نمی‌شود. "
            "max_sub_reps=0 به معنی ممنوعیت است، نه نامحدود.",
            persona="admin" if _is_root(ctx) else persona, page_kind="form",
            parent_callback=parent_callback,
            rows=[[button("Credit", f"v52:repnew:{parent_id}:credit"),
                   button("Prepaid wallet", f"v52:repnew:{parent_id}:prepaid_wallet")]],
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin" if _is_root(ctx) else persona,
                            parent_callback=parent_callback)


def preview_rep_change(
    ctx,
    rep_id: int | None = None,
    *,
    parent_rep_id: int | None = None,
    changes: Mapping | None = None,
    action: str = "create",
    persona: str = "admin",
    parent_callback: str | None = None,
):
    """Validate a proposed representative/tree/financial change without writing it."""
    try:
        selected_action = str(action or "create").strip().lower()
        payload = dict(changes or {})
        if selected_action in {"create", "new", "create_child"}:
            if parent_rep_id is None:
                raise ValueError("parent_rep_id is required")
            parent_id = _positive(parent_rep_id, "parent_rep_id")
            if not _may_manage_parent(ctx, parent_id):
                raise PermissionError("actor cannot create a child under this parent")
            mode = str(payload.get("billing_mode") or "credit").strip().lower()
            if mode not in _BILLING_MODES:
                raise ValueError("billing_mode must be credit or prepaid_wallet")
            code = str(payload.get("rep_code") or "").strip()
            name = str(payload.get("name") or "").strip()
            if not code or code.lower() == "root_admin_rep" or not name:
                raise ValueError("rep_code and name are required; root_admin_rep is reserved")
            telegram_id = _positive(payload.get("telegram_id"), "telegram_id")
            can_create = payload.get("can_create_sub_reps", False)
            if not isinstance(can_create, bool):
                raise ValueError("can_create_sub_reps must be boolean")
            max_children = payload.get("max_sub_reps", 0)
            if isinstance(max_children, bool) or not isinstance(max_children, int) or max_children < 0:
                raise ValueError("max_sub_reps must be a non-negative integer; zero forbids children")
            if can_create and max_children == 0:
                raise ValueError("can_create_sub_reps requires max_sub_reps greater than zero")
            credit_limit = payload.get("credit_limit_toman", 0)
            if isinstance(credit_limit, bool) or not isinstance(credit_limit, int) or credit_limit < 0:
                raise ValueError("credit_limit_toman must be a non-negative integer")
            prefix = str(payload.get("custom_prefix") or "")
            if prefix and not _PREFIX_RE.fullmatch(prefix):
                raise ValueError("custom_prefix must be 1..32 lowercase letters, digits, _ or -")
            custom_prices = payload.get("custom_prices", {})
            if not isinstance(custom_prices, Mapping):
                raise ValueError("custom_prices must be a mapping")
            for plan_code, price in custom_prices.items():
                if not str(plan_code).strip() or isinstance(price, bool) or not isinstance(price, int) or price < 0:
                    raise ValueError("custom finite prices must be non-negative integer toman")
            with database.get_db() as conn:
                checked = rep_tree_service.validate_parent_change(conn, parent_id)
            if checked["depth"] > rep_tree_service.MAX_REP_DEPTH:
                raise ValueError("representative tree exceeds the approved depth")
            body = (
                f"والد: {parent_id}\nکد: {code}\nنام: {name}\nTelegram ID: {telegram_id}\n"
                f"عمق: {checked['depth']} از {rep_tree_service.MAX_REP_DEPTH}\n"
                f"مدل مالی: {mode}\n"
                f"سقف اعتبار: {credit_limit}\n"
                f"امکان ساخت فرزند: {'بله' if can_create else 'خیر'}؛ سقف فرزند: {max_children}\n"
                f"پیشوند سفارشی: {prefix or 'بدون تغییر'}\nقیمت‌های سفارشی: {len(custom_prices)} مورد\n"
                "ثبت این تغییر فقط با تأیید نهایی سرویس rep_tree_service انجام می‌شود."
            )
            return render_page("پیش‌نمایش ساخت زیرنماینده", body,
                               persona="admin" if _is_root(ctx) else "rep",
                               page_kind="preview", parent_callback=parent_callback,
                               rows=[[button("تأیید و ساخت", f"v52:repnew:{parent_id}")]])
        if not _is_root(ctx):
            raise PermissionError("status, delete, credit-limit and tier changes require root")
        target_id = _positive(rep_id, "rep_id")
        if selected_action == "tier":
            tier = str(payload.get("tier") or "").strip().lower()
            if not tier:
                raise ValueError("tier is required")
            detail = f"تغییر tier نماینده {target_id} به {tier}"
        elif selected_action in {"delete", "status", "credit_limit", "edit"}:
            detail = f"عملیات {selected_action} برای نماینده {target_id}"
            if "credit_limit_toman" in payload:
                limit = payload["credit_limit_toman"]
                if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
                    raise ValueError("credit_limit_toman must be non-negative integer toman")
                detail += f"؛ سقف اعتبار: {limit}"
            if "is_active" in payload and not isinstance(payload["is_active"], bool):
                raise ValueError("is_active must be boolean")
            if "custom_prices" in payload:
                for value in payload["custom_prices"].values():
                    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                        raise ValueError("custom prices must be non-negative integer toman")
        else:
            raise ValueError("unsupported representative change action")
        return render_page(
            "پیش‌نمایش تغییر نماینده",
            detail + "\nوضعیت مالی و اثر روی فرزندان باید قبل از تأیید نهایی بازبینی شود.",
            persona="admin", page_kind="preview", parent_callback=parent_callback,
            rows=[[button("بازگشت به نماینده", f"v52:repedit:{target_id}")]],
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin" if _is_root(ctx) else persona,
                            parent_callback=parent_callback)


__all__ = ["show_sub_reps", "begin_sub_rep", "preview_rep_change"]
