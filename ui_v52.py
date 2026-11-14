"""Small, transport-neutral Telegram page primitives for v5.2."""
from __future__ import annotations

from dataclasses import dataclass
from html import escape
from typing import Any, Iterable, Sequence

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

_PERSONAS = {"admin": "admin", "rep": "rep", "representative": "rep",
              "user": "user", "end_user": "user"}


@dataclass(frozen=True)
class RenderedPage:
    text: str
    reply_markup: InlineKeyboardMarkup | None
    parse_mode: str = "HTML"
    kind: str = "page"


@dataclass(frozen=True)
class HomeRoute:
    persona: str
    callback_data: str
    legacy_callback: str


def persona_name(value: str) -> str:
    selected = str(value or "").strip().lower()
    if selected not in _PERSONAS:
        raise ValueError("persona must be admin, rep or user")
    return _PERSONAS[selected]


def button(text: str, callback_data: str) -> InlineKeyboardButton:
    if not isinstance(callback_data, str) or not callback_data:
        raise ValueError("callback_data is required")
    if len(callback_data.encode("utf-8")) > 64:
        raise ValueError("callback_data exceeds Telegram's 64-byte limit")
    return InlineKeyboardButton(str(text), callback_data=callback_data)


def navigation_row(persona: str, parent_callback: str | None) -> list[InlineKeyboardButton]:
    persona = persona_name(persona)
    row = []
    if parent_callback:
        row.append(button("↩ بازگشت", parent_callback))
    row.append(button("⌂ خانه", f"v52:home:{persona}"))
    return row


def render_page(
    title: str,
    body: str = "",
    *,
    body_html: str | None = None,
    persona: str = "user",
    root: bool = False,
    page_kind: str = "page",
    parent_callback: str | None = None,
    rows: Iterable[Sequence[InlineKeyboardButton]] = (),
) -> RenderedPage:
    """Render plain text safely; explicitly trusted body_html is for escaped code values."""
    selected = persona_name(persona)
    content = f"<b>{escape(str(title), quote=True)}</b>"
    if body_html is not None:
        if body_html:
            content += "\n\n" + str(body_html)
    elif body:
        content += "\n\n" + escape(str(body), quote=True)
    keyboard = [list(row) for row in rows if row]
    if not root:
        # A page without an explicit parent gets the persona home button only.
        # The previous ``v52:back:{persona}`` placeholder was never registered in
        # ``v52_router._CALLBACK_PATTERNS``, so every back press on such a page
        # resolved to no Route and surfaced the "W18 bridge" error page instead of
        # navigating. Never emit an unregistered callback from a navigation row.
        if parent_callback:
            keyboard.append(navigation_row(selected, parent_callback))
        else:
            keyboard.append([button("⌂ خانه", f"v52:home:{selected}")])
    return RenderedPage(content, InlineKeyboardMarkup(keyboard) if keyboard else None,
                         kind=page_kind)


def render_error(
    message: str,
    *,
    persona: str = "user",
    root: bool = False,
    parent_callback: str | None = None,
    rows: Iterable[Sequence[InlineKeyboardButton]] = (),
) -> RenderedPage:
    """An error is still a navigable page; it never traps the actor in a popup."""
    return render_page("خطا", message, persona=persona, root=root,
                       page_kind="error", parent_callback=parent_callback, rows=rows)


def render_uri(uri: str) -> str:
    """Render a URI as an escaped HTML code value, never as callback data."""
    return f"<code>{escape(str(uri), quote=True)}</code>"


def route_home(persona: str, user_data: Any = None) -> HomeRoute:
    """Discard draft-only FSM state; accepted order and payment state are retained."""
    selected = persona_name(persona)
    state = user_data
    if state is not None and not isinstance(state, dict):
        state = getattr(state, "user_data", None)
    if isinstance(state, dict):
        state.pop("draft_fsm", None)
        state.pop("v52_draft_fsm", None)
    legacy = {"admin": "admin:main", "rep": "rep:main", "user": "user:main"}[selected]
    return HomeRoute(selected, f"v52:home:{selected}", legacy)


__all__ = ["RenderedPage", "HomeRoute", "persona_name", "button",
           "navigation_row", "render_page", "render_error", "render_uri", "route_home"]
