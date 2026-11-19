"""Batch parsing for digital-inventory and manual-pool imports.

A real digital product is rarely one line: an account is an email, a password and
maybe an API key; a config is a URI plus a note. The importer therefore supports
two explicit shapes and never guesses between them silently.

* ``line``  — one item per non-empty line (the historic behaviour).
* ``block`` — a standalone separator line (``---`` or ``###``) ends an item, so
  every block between separators is stored as ONE multi-line item.
* ``auto``  — ``block`` when a separator line is present; otherwise ``block`` when
  the message looks like ONE labelled credential card, else ``line``.

The last rule is the S60 fix. A real account card is routinely pasted without any
separator — ``نام کاربری: x`` / ``رمز عبور: y`` / ``کلید: z`` — and splitting that on
newlines produced three broken items instead of one usable card. A bare list of codes
must keep splitting, so the trigger is a *labelled* field or an emoji-prefixed line,
never merely "there are several lines".

A separator line carries nothing but the marker, so a password that happens to
contain ``---`` inside a line is never mistaken for a boundary.
"""
from __future__ import annotations

import re

MAX_BATCH_ITEMS = 100
MAX_ITEM_BYTES = 8192
BLOCK_SEPARATORS = ("---", "###")
PARSE_MODES = ("auto", "line", "block")


class InventoryParseError(ValueError):
    """A pasted batch cannot be split into valid inventory items."""


def _is_separator(line: str) -> bool:
    return line.strip() in BLOCK_SEPARATORS


#: A Persian field name that labels the line it starts. Persian labels are
#: unambiguous, so one may stand alone on its line (the value follows below).
_FA_LABEL = (
    r"نام\s*کاربری|نام\s*کاربر|یوزر\s*نیم|یوزرنیم|یوزر|کاربری|"
    r"رمز\s*عبور|رمز\s*ورود|رمزعبور|رمز|پسورد|پاسورد|گذرواژه|"
    r"کلید|لایسنس|لیسانس|توکن|کد|"
    r"توضیحات|نکات|راهنما|راه\s*اندازی|"
    r"ایمیل|لینک|آدرس|سرور|پورت|حجم|انقضا|تاریخ|اعتبار"
)

#: An English field name. These are short and collide with real product codes
#: (``KEY-AAAA``), so an English label only counts when it is followed by an
#: explicit separator: ``key: x``, ``key=x``, ``key - x``.
_EN_LABEL = (
    r"username|user\s*name|user|login|email|mail|"
    r"password|passwd|pass|pwd|"
    r"key|licen[cs]e|token|secret|uuid|"
    r"note|notes|guide|hint|url|link|host|port|server|"
    r"sub|subscription|config|remark"
)

#: A hyphen is a valid separator after a Persian label (``رمز عبور - x``) but never
#: after an English one: ``KEY-AAAA`` is a product code, not a labelled field.
_SEPARATOR_FA = r"[:：=\-–—]"
_SEPARATOR_EN = r"[:：=]"

#: ``label: value`` in either language.
_LABELLED_FIELD = re.compile(
    rf"^\s*(?:(?:{_FA_LABEL})\s*{_SEPARATOR_FA}|(?:{_EN_LABEL})\s*{_SEPARATOR_EN})\s*\S",
    re.IGNORECASE)
#: A Persian label alone on its line; the value is on the following line.
_BARE_FA_LABEL = re.compile(rf"^\s*(?:{_FA_LABEL})\s*$")
#: A line the operator labelled with an emoji.
_EMOJI_LINE = re.compile(
    r"^\s*[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2190-\u21FF]")


def looks_like_credential_card(text: str) -> bool:
    """True when a separator-free multi-line message is ONE labelled credential card.

    A bare list of codes must keep splitting into one item per line, so the trigger is
    a *labelled* field (``رمز عبور: x``, ``key=x``, a lone ``توضیحات`` line) or an
    emoji-prefixed line — never merely "the message has several lines". That is why
    ``resolve_mode("a\nb")`` stays ``line``.
    """
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    if len(lines) < 2:
        return False
    return any(
        _LABELLED_FIELD.match(line) or _BARE_FA_LABEL.match(line) or _EMOJI_LINE.match(line)
        for line in lines
    )


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
    if has_separator(text):
        return "block"
    # S60 — one labelled credential card, pasted without separators, is one item.
    return "block" if looks_like_credential_card(text) else "line"


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
    "looks_like_credential_card",
    "parse_batch",
    "resolve_mode",
    "split_blocks",
    "split_lines",
]
