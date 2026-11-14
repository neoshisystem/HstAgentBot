"""Root reconciliation, manual-refund and forum-repair preview pages."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import config
from ui_v52 import button, render_error, render_page

_SECRET_KEYS = ("secret", "token", "password", "cipher", "credential", "private", "sub_id", "authorization", "api_key")


def _value(obj: Any, key: str, default=None):
    return obj.get(key, default) if isinstance(obj, Mapping) else getattr(obj, key, default)


def _positive(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be positive")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be positive") from exc
    if result <= 0:
        raise ValueError(f"{label} must be positive")
    return result


def _root(ctx: Any) -> bool:
    try:
        actor = _positive(_value(ctx, "actor_tg_id", _value(ctx, "telegram_id")), "actor_tg_id")
        root = _positive(getattr(config, "ADMIN_TELEGRAM_ID", 0) or 0, "ADMIN_TELEGRAM_ID")
        return actor == root
    except (TypeError, ValueError):
        return False


def _safe_fields(row: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in row.items()
            if not any(part in str(key).lower() for part in _SECRET_KEYS)
            and not isinstance(value, (bytes, bytearray))}


def show_operations(
    ctx: Any,
    operations: Sequence[Mapping[str, Any]],
    *,
    parent_callback: str | None = None,
):
    """Show sanitized reconciliation records; unknown remains locked for proof-based review."""
    try:
        if not _root(ctx):
            raise PermissionError("reconciliation queue is root-only")
        rows = []
        lines = []
        for operation in operations:
            if not isinstance(operation, Mapping):
                continue
            safe = _safe_fields(operation)
            operation_id = str(safe.get("operation_id") or "").strip()
            if not operation_id or len(operation_id.encode("utf-8")) > 64:
                continue
            state = str(safe.get("state") or "unknown")
            lines.append(f"عملیات: {operation_id} | وضعیت: {state} | پنل: {safe.get('panel_id', '—')}")
            if state in {"unknown", "needs_reconciliation"}:
                lines.append("اثر نامعلوم است؛ تا دریافت شاهد مثبت/منفی معتبر، قفل حفظ می‌شود و موفقیت فرض نمی‌شود.")
            rows.append([button(f"جزئیات {operation_id[:18]}", f"v52:reconcile:{operation_id}")])
        if not lines:
            lines.append("عملیات معلقی برای تطبیق وجود ندارد.")
        return render_page("صف تطبیق عملیات", "\n".join(lines), persona="admin",
                           page_kind="operations", parent_callback=parent_callback, rows=rows)
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


def preview_refund(
    ctx: Any,
    refund: Mapping[str, Any],
    *,
    amount_toman: int | None = None,
    proof_reference: str | None = None,
    confirm: bool = False,
    parent_callback: str | None = None,
):
    """Preview refund request/proof/confirmation without settling or releasing any hold."""
    try:
        if not isinstance(refund, Mapping):
            raise ValueError("refund request snapshot is required")
        actor = _positive(_value(ctx, "actor_tg_id", _value(ctx, "telegram_id")), "actor_tg_id")
        owner = _positive(refund.get("user_tg_id"), "user_tg_id")
        if not _root(ctx) and actor != owner:
            raise PermissionError("refund details are visible only to payer/root")
        refund_id = str(refund.get("refund_id") or "").strip()
        if not refund_id or len(refund_id.encode("utf-8")) > 64:
            raise ValueError("refund_id is invalid")
        requested = refund.get("amount_toman")
        if isinstance(requested, bool) or not isinstance(requested, int) or requested <= 0:
            raise ValueError("refund amount must be a positive ledger-derived integer")
        if amount_toman is not None and amount_toman != requested:
            raise ValueError("confirmed refund amount must match the held request")
        state = str(refund.get("state") or "processing")
        if state == "unknown":
            text = "وضعیت بازپرداخت UNKNOWN است؛ hold حفظ می‌شود و هیچ settle/retry خودکاری مجاز نیست."
        elif state in {"settled", "rejected"}:
            text = f"این درخواست terminal است: {state}. عملیات دوباره انجام نمی‌شود."
        else:
            text = "پیش‌نمایش درخواست بازپرداخت دستی؛ ثبت proof و تأیید root مرحله جداگانه دارد."
        proof = str(proof_reference or "").strip()
        if confirm and not proof:
            raise ValueError("proof reference is required before root confirmation")
        if confirm and not _root(ctx):
            raise PermissionError("فقط root می‌تواند مرحلهٔ تأیید نهایی را انجام دهد.")
        text += f"\nشناسه: {refund_id}\nمبلغ درخواست: {requested:,} تومان\nوضعیت: {state}"
        if proof:
            text += f"\nشناسه شاهد: {proof[:80]}"
        text += "\nاین UI هیچ posting، reversal یا release مالی انجام نمی‌دهد."
        return render_page("پیش‌نمایش بازپرداخت", text, persona="admin" if _root(ctx) else "user",
                           page_kind="preview", parent_callback=parent_callback,
                           rows=[[button("بازگشت به عملیات", "v52:ops")]])
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin" if _root(ctx) else "user", parent_callback=parent_callback)


def sync_topics(
    ctx: Any,
    group_chat_id: int,
    *,
    topic_key: str,
    thread_id: int | None = None,
    topic_state: str = "unknown",
    permission_snapshot: Mapping[str, Any] | None = None,
    parent_callback: str | None = None,
):
    """Preview a manually identified topic repair; this function never calls Telegram."""
    try:
        if not _root(ctx):
            raise PermissionError("forum-topic repair is root-only")
        if isinstance(group_chat_id, bool) or not isinstance(group_chat_id, int) or group_chat_id == 0:
            raise ValueError("group_chat_id must be a non-zero integer")
        key = str(topic_key or "").strip()
        allowed = {"buyreport", "otherservice", "pendingreceipts", "paymentreport", "reporttest",
                   "supportreport", "errorreport", "reportcron", "reportnight", "backupfile"}
        if key not in allowed:
            raise ValueError("topic key is not in the approved forum registry")
        if key == "abusereport":
            raise ValueError("abuse reports are private-root routes, never group topics")
        state = str(topic_state or "unknown").lower()
        if state in {"unknown", "creating"} and thread_id is None:
            raise ValueError("unknown topic requires a manually supplied thread_id; blind recreate is forbidden")
        selected_thread = None if thread_id is None else _positive(thread_id, "thread_id")
        if permission_snapshot is not None:
            if not isinstance(permission_snapshot, Mapping) or permission_snapshot.get("allowed") is not True:
                raise PermissionError("forum bot permissions have not been verified")
        return render_page(
            "پیش‌نمایش اصلاح تاپیک",
            f"گروه: {group_chat_id}\nکلید: {key}\nوضعیت ثبت‌شده: {state}\n"
            f"thread_id دستی: {selected_thread if selected_thread is not None else 'ثبت نشده'}\n"
            "این فقط پیش‌نمایش repair است؛ تا اجرای جداگانهٔ permission check و API، وضعیت sync ادعا نمی‌شود.",
            persona="admin", page_kind="preview", parent_callback=parent_callback,
            rows=[[button("بازگشت به عملیات", "v52:ops")]],
        )
    except (TypeError, ValueError, PermissionError) as exc:
        return render_error(str(exc), persona="admin", parent_callback=parent_callback)


__all__ = ["show_operations", "preview_refund", "sync_topics"]