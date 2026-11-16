"""Batch parsing for digital-inventory and manual-pool imports.

A real digital product is rarely one line: an account is an email, a password and
maybe an API key; a config is a URI plus a note. The importer therefore supports
two explicit shapes and never guesses between them silently.

* ``line``  — one item per non-empty line (the historic behaviour).
* ``block`` — a standalone separator line (``---`` or ``###``) ends an item, so
  every block between separators is stored as ONE multi-line item.
* ``auto``  — ``block`` when a separator line is present, else ``line``.

A separator line carries nothing but the marker, so a password that happens to
contain ``---`` inside a line is never mistaken for a boundary.
"""
from __future__ import annotations

MAX_BATCH_ITEMS = 100
MAX_ITEM_BYTES = 8192
BLOCK_SEPARATORS = ("---", "###")
PARSE_MODES = ("auto", "line", "block")


class InventoryParseError(ValueError):
    """A pasted batch cannot be split into valid inventory items."""


def _is_separator(line: str) -> bool:
    return line.strip() in BLOCK_SEPARATORS


def has_separator(text: str) -> bool:
    """True when at least one line is a standalone block separator."""
    return any(_is_separator(line) for line in str(text or "").splitlines())


def split_blocks(text: str) -> list[str]:
    """Split on standalone separator lines, preserving each block's inner newlines.

    Blank lines before and after a block are dropped, but the newlines *inside* a
    block are kept verbatim — that is the whole point of a multi-line item.
    """
    blocks: list[str] = []
    current: list[str] = []
    for line in str(text or "").splitlines():
        if _is_separator(line):
            blocks.append("\n".join(current).strip("\n"))
            current = []
            continue
        current.append(line)
    blocks.append("\n".join(current).strip("\n"))
    return [block.strip() for block in blocks if block.strip()]


def split_lines(text: str) -> list[str]:
    """One item per non-empty line."""
    return [line.strip() for line in str(text or "").splitlines() if line.strip()]


def resolve_mode(text: str, mode: str = "auto") -> str:
    """Return the effective mode for this text; ``auto`` never returns ``auto``."""
    selected = str(mode or "auto").strip().lower()
    if selected not in PARSE_MODES:
        raise InventoryParseError(
            "حالت تقسیم‌بندی نامعتبر است؛ یکی از auto، line یا block را انتخاب کنید."
        )
    if selected != "auto":
        return selected
    return "block" if has_separator(text) else "line"


def parse_batch(text: str, *, mode: str = "auto") -> list[str]:
    """Split a pasted batch into validated items.

    Raises :class:`InventoryParseError` with a Persian message so the caller can
    show it to the operator unchanged.
    """
    if not isinstance(text, str):
        raise InventoryParseError("متن ورودی باید یک رشته باشد.")
    effective = resolve_mode(text, mode)
    items = split_blocks(text) if effective == "block" else split_lines(text)
    if not items:
        raise InventoryParseError("هیچ قلمی در متن ارسالی پیدا نشد.")
    if len(items) > MAX_BATCH_ITEMS:
        raise InventoryParseError(
            f"تعداد اقلام ({len(items)}) از سقف مجاز ({MAX_BATCH_ITEMS}) بیشتر است."
        )
    for index, item in enumerate(items, start=1):
        if len(item.encode("utf-8")) > MAX_ITEM_BYTES:
            raise InventoryParseError(f"قلم شماره {index} از حد مجاز حجم بزرگ‌تر است.")
    return items


def describe_batch(items: list[str]) -> str:
    """A short Persian summary of a parsed batch, safe to show in a preview."""
    if not items:
        return "تعداد اقلام: 0"
    multi = sum(1 for item in items if "\n" in item)
    lines = sum(item.count("\n") + 1 for item in items)
    if multi:
        return (
            f"تعداد اقلام: {len(items)} (از این تعداد {multi} قلم چندخطی)\n"
            f"مجموع خطوط: {lines}"
        )
    return f"تعداد اقلام: {len(items)}"


__all__ = [
    "BLOCK_SEPARATORS",
    "InventoryParseError",
    "MAX_BATCH_ITEMS",
    "MAX_ITEM_BYTES",
    "PARSE_MODES",
    "describe_batch",
    "has_separator",
    "parse_batch",
    "resolve_mode",
    "split_blocks",
    "split_lines",
]
