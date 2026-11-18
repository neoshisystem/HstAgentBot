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


#: How a page's own keyboard is recognised as already carrying a back control.
#: Domain pages legitimately ship their own exit row ("بازگشت به حساب",
#: "🔙 بازگشت به مرحله قبل"); the footer must then not add a second one.
_BACK_MARKERS = ("بازگشت", "↩", "🔙")


def _has_back_control(keyboard: Iterable[Sequence[InlineKeyboardButton]]) -> bool:
    return any(marker in str(getattr(item, "text", "") or "")
               for row in keyboard for item in row for marker in _BACK_MARKERS)


def navigation_footer(
    persona: str,
    parent_callback: str | None,
    keyboard: Iterable[Sequence[InlineKeyboardButton]] = (),
) -> list[InlineKeyboardButton]:
    """The navigation controls ``keyboard`` is still missing, in one row.

    Every page ends on the same two exits — back to its parent and home — and a
    page that already carries one of them must not carry it twice: two identical
    exits are noise, and the operator cannot tell which one is the bug.

    * The back button is added only when ``keyboard`` carries no back control at
      all. The page's own, more specific exit ("بازگشت به حساب") is the one that
      stays, so the footer never becomes a second back button.
    * The home button is added unless the exact ``v52:home:{persona}`` callback is
      already on the page, which keeps the global exit on every page without
      duplicating it.

    Only two callbacks are ever emitted here: the caller's ``parent_callback`` and
    the registered ``v52:home:{persona}``. The old ``v52:back:{persona}``
    placeholder is deliberately not used: it was never registered in
    ``v52_router._CALLBACK_PATTERNS``, so a back press on such a page resolved to
    no Route and surfaced the "W18 bridge" error page instead of navigating.
    """
    selected = persona_name(persona)
    home_callback = f"v52:home:{selected}"
    present = {str(getattr(item, "callback_data", "") or "")
               for row in keyboard for item in row}
    row: list[InlineKeyboardButton] = []
    if parent_callback and not _has_back_control(keyboard):
        row.append(button("↩ بازگشت", parent_callback))
    if home_callback not in present:
        row.append(button("⌂ خانه", home_callback))
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
        # The footer closes the page with the exits it is still missing: a page
        # that already ships its own back row gets the home button only, and a
        # page with neither gets both. Never emit an unregistered callback here.
        footer = navigation_footer(selected, parent_callback, keyboard)
        if footer:
            keyboard.append(footer)
    return RenderedPage(content, InlineKeyboardMarkup(keyboard) if keyboard else None,
                         kind=page_kind)


def render_error(
    message: str,
    *,
    persona: str = "user",
    root: bool = False,
    parent_callback: str | None = None,
    rows: Iterable[Sequence[InlineKeyboardButton]] = (),
    body_html: str | None = None,
) -> RenderedPage:
    """An error is still a navigable page; it never traps the actor in a popup.

    ``body_html`` exists for the same reason it does on :func:`render_page`: a
    validation message that quotes the identifier being edited needs a code span,
    and a caller that passes pre-escaped HTML must be able to say so.
    """
    return render_page("خطا", message, body_html=body_html, persona=persona, root=root,
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


# --------------------------------------------------------------------------- #
# Step-by-step wizards with a preview card
#
# Every multi-field operator form used to be one raw text line (space- or
# pipe-separated), which is unforgiving: one wrong field silently reorders the
# rest, and there is no way to see what is about to be written. The engine below
# turns a *declarative* flow description into a step-by-step conversation that
# always ends on a preview card, and nothing reaches the database until the
# operator presses «✅ تایید و ثبت نهایی».
#
# Two properties are non-negotiable and both are load-bearing:
#
# 1. **A flow never writes before confirmation.** ``commit`` is only ever called
#    from :func:`wizard_confirm`.
# 2. **The legacy single-line form still works.** ``WizardFlow.legacy`` parses the
#    old shape; when it recognises the whole input at step 1 the wizard jumps
#    straight to the preview card. Operators who memorised the old format keep it,
#    and the automated tests that drive it keep passing.
# --------------------------------------------------------------------------- #

WIZARD_STATE_KEY = "v52_wizard"
WIZARD_TTL_SECONDS = 900
WIZARD_CALLBACK_PREFIX = "v52:wz:"
WIZARD_MAX_STEPS = 24

WIZARD_CONFIRM_LABEL = "✅ تایید و ثبت نهایی"
WIZARD_EDIT_LABEL = "✏️ بازگشت و اصلاح"
WIZARD_CANCEL_LABEL = "❌ انصراف"
WIZARD_PREV_LABEL = "⬅️ گام قبل"
WIZARD_PREVIEW_TITLE = "🧾 پیش‌نمایش اطلاعات پیش از ثبت"

#: The two checkbox glyphs every multi-select surface in the product uses.
CHECKBOX_ON = "✅"
CHECKBOX_OFF = "☑️"


@dataclass(frozen=True)
class WizardStep:
    """One question in a wizard.

    ``parse`` receives the raw text and returns the stored value, raising
    ``ValueError`` with a Persian explanation when the input is unusable. ``choices``
    renders inline buttons, so a step with choices never forces the operator to
    type a value at all.
    """

    key: str
    title: str
    hint: str = ""
    choices: tuple[tuple[str, str], ...] = ()
    parse: Any = None
    display: Any = None
    skippable: bool = False
    skip_label: str = "⏭ رد کردن"
    skip_value: Any = ""
    generate: Any = None
    generate_label: str = "🎲 تولید خودکار"
    #: A multi-select step toggles its choices and advances only on «پایان انتخاب»,
    #: so the operator can tick several categories or panels in one pass.
    multi: bool = False
    done_label: str = "✅ پایان انتخاب"


@dataclass(frozen=True)
class WizardFlow:
    """A declarative multi-step form.

    ``preview`` renders the confirmation card from the collected values,
    ``commit`` performs the single write and returns the Persian result notice,
    and ``legacy`` optionally recognises the pre-wizard one-line format.
    """

    key: str
    title: str
    steps: tuple[WizardStep, ...]
    preview: Any
    commit: Any
    legacy: Any = None
    persona: str = "admin"
    intro: str = ""


#: Flows registered by the module that owns the domain (``admin_panel``,
#: ``goods_ui``, ``tier_admin_ui``, ``ticket_status_ui``, ``bot_admin_ui``). The
#: engine never imports them, so a module can register its flows at import time
#: without creating a cycle.
WIZARD_FLOWS: dict[str, WizardFlow] = {}


def register_flow(flow: WizardFlow) -> WizardFlow:
    """Publish one flow under its key; a re-registration replaces it."""
    if not isinstance(flow, WizardFlow):
        raise TypeError("flow must be a WizardFlow")
    if not flow.key:
        raise ValueError("flow key is required")
    if not flow.steps:
        raise ValueError("a wizard flow needs at least one step")
    if len(flow.steps) > WIZARD_MAX_STEPS:
        raise ValueError("a wizard flow may not exceed the step budget")
    WIZARD_FLOWS[flow.key] = flow
    return flow


def flow_by_key(key: str) -> WizardFlow | None:
    return WIZARD_FLOWS.get(str(key or "").strip())


def _wizard_now(now: Any = None) -> int:
    if now is None:
        import time as _time
        return int(_time.time())
    return int(now)


def _wizard_display(step: WizardStep, value: Any) -> str:
    if step.display is not None:
        try:
            return str(step.display(value))
        except Exception:
            return str(value)
    if value is None or value == "":
        return "—"
    return str(value)


def start_wizard(
    user_data: Any,
    flow_key: str,
    *,
    return_callback: str,
    origin: str = "",
    ref: str = "",
    seed: dict | None = None,
    message_id: int | None = None,
    chat_id: int | None = None,
    now: Any = None,
) -> dict:
    """Begin a flow and return its fresh state (also stored in ``user_data``)."""
    flow = flow_by_key(flow_key)
    if flow is None:
        raise ValueError("wizard flow is not registered")
    if not isinstance(user_data, dict):
        raise TypeError("user_data must be a dict")
    state = {
        "flow": flow.key,
        "step": 0,
        "values": dict(seed or {}),
        "return_callback": str(return_callback or ""),
        "origin": str(origin or ""),
        "ref": str(ref or ""),
        "persona": flow.persona,
        "started_at": _wizard_now(now),
        "touched_at": _wizard_now(now),
        "prompt_message_id": message_id,
        "prompt_chat_id": chat_id,
        "pending": None,
    }
    user_data[WIZARD_STATE_KEY] = state
    return state


def wizard_state(user_data: Any, *, now: Any = None) -> dict | None:
    """The live wizard state, or ``None`` (an expired one is discarded here)."""
    if not isinstance(user_data, dict):
        return None
    state = user_data.get(WIZARD_STATE_KEY)
    if not isinstance(state, dict):
        return None
    if flow_by_key(state.get("flow")) is None:
        user_data.pop(WIZARD_STATE_KEY, None)
        return None
    if _wizard_now(now) - int(state.get("touched_at") or 0) > WIZARD_TTL_SECONDS:
        user_data.pop(WIZARD_STATE_KEY, None)
        return None
    return state


def clear_wizard(user_data: Any) -> dict | None:
    if not isinstance(user_data, dict):
        return None
    state = user_data.pop(WIZARD_STATE_KEY, None)
    return state if isinstance(state, dict) else None


def current_step(state: dict) -> WizardStep | None:
    flow = flow_by_key(state.get("flow"))
    if flow is None:
        return None
    index = int(state.get("step") or 0)
    if index < 0 or index >= len(flow.steps):
        return None
    return flow.steps[index]


def _step_rows(state: dict) -> list[list[InlineKeyboardButton]]:
    """The control rows for the current step: choices, then navigation."""
    flow = flow_by_key(state["flow"])
    step = current_step(state)
    rows: list[list[InlineKeyboardButton]] = []
    if step is None or flow is None:
        return rows
    index = int(state["step"])
    if step.choices:
        selected = state["values"].get(step.key) if step.multi else None
        selected_set = set(selected) if isinstance(selected, (list, tuple, set)) else set()
        row: list[InlineKeyboardButton] = []
        for position, (label, value) in enumerate(step.choices):
            text = str(label)
            if step.multi:
                text = (f"{CHECKBOX_ON} {text}" if value in selected_set
                        else f"{CHECKBOX_OFF} {text}")
            row.append(button(text, f"{WIZARD_CALLBACK_PREFIX}pick:{index}:{position}"))
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
    extra: list[InlineKeyboardButton] = []
    if step.multi:
        extra.append(button(step.done_label, f"{WIZARD_CALLBACK_PREFIX}done:{index}"))
    if step.generate is not None:
        extra.append(button(step.generate_label, f"{WIZARD_CALLBACK_PREFIX}gen:{index}"))
    if step.skippable:
        extra.append(button(step.skip_label, f"{WIZARD_CALLBACK_PREFIX}skip:{index}"))
    if extra:
        rows.append(extra)
    navigation: list[InlineKeyboardButton] = []
    if index > 0:
        navigation.append(button(WIZARD_PREV_LABEL, f"{WIZARD_CALLBACK_PREFIX}prev"))
    navigation.append(button(WIZARD_CANCEL_LABEL, f"{WIZARD_CALLBACK_PREFIX}cancel"))
    rows.append(navigation)
    # The surface the wizard was started from. A form opened from a 360-degree card
    # must offer the way back to that card, not only a cancel that lands on the menu.
    parent = str(state.get("return_callback") or "")
    if parent:
        rows.append([button("↩ بازگشت", parent)])
    return rows


def _values_summary(state: dict) -> str:
    """The collected-so-far block, so a long wizard stays readable."""
    flow = flow_by_key(state["flow"])
    if flow is None:
        return ""
    lines: list[str] = []
    for step in flow.steps:
        if step.key not in state["values"]:
            continue
        lines.append(
            f"• {escape(str(step.title), quote=True)}: "
            f"<b>{escape(_wizard_display(step, state['values'][step.key]), quote=True)}</b>")
    if not lines:
        return ""
    return "<b>مقادیر ثبت‌شده تا این گام:</b>\n" + "\n".join(lines)


def render_wizard_step(state: dict, *, error: str | None = None,
                       notice: str | None = None) -> RenderedPage:
    """Render the current step as an editable page."""
    flow = flow_by_key(state["flow"])
    step = current_step(state)
    if flow is None or step is None:
        return render_error("این ویزارد دیگر معتبر نیست؛ آن را از منو دوباره آغاز کنید.",
                            persona="admin", parent_callback=state.get("return_callback") or None)
    total = len(flow.steps)
    index = int(state["step"])
    header = (f"🧩 <b>{escape(flow.title, quote=True)}</b> — "
              f"گام {index + 1} از {total}")
    body = [header]
    if flow.intro and index == 0:
        body.append(escape(flow.intro, quote=True))
    body.append(f"<b>{escape(step.title, quote=True)}</b>")
    if step.hint:
        body.append(escape(step.hint, quote=True))
    if error:
        body.append(f"⚠️ <b>{escape(str(error), quote=True)}</b>")
    elif notice:
        body.append(f"ℹ️ {escape(str(notice), quote=True)}")
    summary = _values_summary(state)
    if summary:
        body.append(summary)
    body.append("⏱ برای لغو، «❌ انصراف» را بزنید یا عبارت cancel را بفرستید.")
    return RenderedPage("\n\n".join(body),
                        InlineKeyboardMarkup(_step_rows(state)), kind="wizard")


def render_wizard_preview(state: dict, *, error: str | None = None) -> RenderedPage:
    """Render the confirmation card; the only card that can write."""
    flow = flow_by_key(state["flow"])
    if flow is None:
        return render_error("این ویزارد دیگر معتبر نیست؛ آن را از منو دوباره آغاز کنید.",
                            persona="admin", parent_callback=state.get("return_callback") or None)
    try:
        body = str(flow.preview(dict(state["values"])))
    except Exception as exc:
        return render_error(f"پیش‌نمایش این اطلاعات ممکن نیست: {exc}", persona="admin",
                            parent_callback=state.get("return_callback") or None)
    parts = [f"<b>{escape(WIZARD_PREVIEW_TITLE, quote=True)}</b>",
             f"🧩 {escape(flow.title, quote=True)}",
             body]
    if error:
        parts.append(f"⚠️ <b>{escape(str(error), quote=True)}</b>")
    parts.append("تا زمانی که «✅ تایید و ثبت نهایی» را نزنید، هیچ تغییری در پایگاه داده ثبت نمی‌شود.")
    rows = [
        [button(WIZARD_CONFIRM_LABEL, f"{WIZARD_CALLBACK_PREFIX}confirm")],
        [button(WIZARD_EDIT_LABEL, f"{WIZARD_CALLBACK_PREFIX}edit")],
        [button(WIZARD_CANCEL_LABEL, f"{WIZARD_CALLBACK_PREFIX}cancel")],
    ]
    return RenderedPage("\n\n".join(parts), InlineKeyboardMarkup(rows), kind="wizard")


def _advance(state: dict, *, now: Any = None) -> str:
    """Move to the next step, or to the preview when the flow is complete."""
    flow = flow_by_key(state["flow"])
    state["touched_at"] = _wizard_now(now)
    state["pending"] = None
    if flow is None:
        return "invalid"
    index = int(state["step"]) + 1
    if index >= len(flow.steps):
        state["step"] = len(flow.steps)
        return "preview"
    state["step"] = index
    return "step"


def _record(state: dict, step: WizardStep, value: Any, *, now: Any = None) -> str:
    """Store one answer and advance.

    A ``parse`` that returns a dict contributes several values at once, which is how
    a single question can collect a title *and* the key derived from it without
    asking the operator for the key separately.
    """
    if isinstance(value, dict):
        state["values"].update(value)
    else:
        state["values"][step.key] = value
    return _advance(state, now=now)


def wizard_apply_text(state: dict, text: str, *, now: Any = None) -> tuple[str, str | None]:
    """Feed one typed value to the wizard; returns ``(outcome, error)``.

    ``outcome`` is ``step``, ``preview`` or ``error``. At the first step the flow's
    legacy parser gets a chance to swallow the whole old-format line at once.
    """
    raw = str(text or "").strip()
    flow = flow_by_key(state["flow"])
    if flow is None:
        return "invalid", None
    if not raw:
        return "error", "مقدار ارسالی خالی است؛ لطفاً مقدار این گام را وارد کنید."
    if int(state["step"]) == 0 and flow.legacy is not None:
        try:
            parsed = flow.legacy(raw)
        except Exception:
            parsed = None
        if isinstance(parsed, dict) and parsed:
            state["values"].update(parsed)
            state["step"] = len(flow.steps)
            state["touched_at"] = _wizard_now(now)
            state["pending"] = None
            return "preview", None
    step = current_step(state)
    if step is None:
        return "invalid", None
    value: Any = raw
    if step.parse is not None:
        try:
            value = step.parse(raw)
        except ValueError as exc:
            return "error", str(exc)
    return _record(state, step, value, now=now), None


def wizard_choose(state: dict, step_index: int, choice_index: int, *,
                  now: Any = None) -> tuple[str, str | None]:
    """Apply an inline choice for the current step.

    A multi-select step toggles the choice and stays on the same step; every other
    step advances as soon as one choice is made.
    """
    step = current_step(state)
    if step is None or int(state["step"]) != int(step_index):
        return "error", "این انتخاب با گام جاری هم‌خوان نیست؛ گام را دوباره ببینید."
    try:
        _label, value = step.choices[int(choice_index)]
    except (IndexError, TypeError, ValueError):
        return "error", "این گزینه معتبر نیست."
    if step.multi:
        selected = state["values"].get(step.key)
        current = list(selected) if isinstance(selected, (list, tuple)) else []
        if value in current:
            current = [item for item in current if item != value]
        else:
            current.append(value)
        state["values"][step.key] = current
        state["touched_at"] = _wizard_now(now)
        return "step", None
    # A choice is just another raw answer, so a step that declares a parser must run
    # it here too: the tier wizard's discount buttons carry "10" and the parser turns
    # that into the 1000 basis points the service stores. Skipping the parser for
    # choices silently stored the display value instead of the real one.
    if step.parse is not None:
        try:
            value = step.parse(value)
        except ValueError as exc:
            return "error", str(exc)
    return _record(state, step, value, now=now), None


def wizard_done(state: dict, step_index: int, *, now: Any = None) -> tuple[str, str | None]:
    """Close a multi-select step and advance."""
    step = current_step(state)
    if step is None or int(state["step"]) != int(step_index):
        return "error", "این گام دیگر فعال نیست."
    if not step.multi:
        return "error", "این گام انتخاب چندگانه ندارد."
    selected = state["values"].get(step.key)
    if not isinstance(selected, (list, tuple)):
        state["values"][step.key] = []
    return _advance(state, now=now), None


def wizard_skip(state: dict, step_index: int, *, now: Any = None) -> tuple[str, str | None]:
    step = current_step(state)
    if step is None or int(state["step"]) != int(step_index):
        return "error", "این گام دیگر فعال نیست."
    if not step.skippable:
        return "error", "این گام قابل رد کردن نیست."
    return _record(state, step, step.skip_value, now=now), None


def wizard_generate(state: dict, step_index: int, *, now: Any = None) -> tuple[str, str | None]:
    step = current_step(state)
    if step is None or int(state["step"]) != int(step_index):
        return "error", "این گام دیگر فعال نیست."
    if step.generate is None:
        return "error", "این گام مقدار خودکار ندارد."
    try:
        value = step.generate(dict(state["values"]))
    except ValueError as exc:
        return "error", str(exc)
    return _record(state, step, value, now=now), None


def wizard_back(state: dict, *, now: Any = None) -> str:
    """Step back one question, keeping every value already collected."""
    flow = flow_by_key(state["flow"])
    if flow is None:
        return "invalid"
    index = int(state["step"])
    state["step"] = max(0, min(index, len(flow.steps)) - 1)
    state["touched_at"] = _wizard_now(now)
    state["pending"] = None
    return "step"


def wizard_confirm(state: dict, actor: dict, *, now: Any = None) -> str:
    """Perform the flow's single write and return its Persian notice."""
    flow = flow_by_key(state["flow"])
    if flow is None:
        raise ValueError("wizard flow is not registered")
    if int(state["step"]) < len(flow.steps):
        raise ValueError("wizard is not complete")
    return str(flow.commit(dict(state["values"]), dict(actor or {})))


def wizard_parse_callback(data: str) -> tuple[str, tuple] | None:
    """Split a ``v52:wz:`` callback into ``(action, args)``; ``None`` if foreign."""
    raw = str(data or "")
    if not raw.startswith(WIZARD_CALLBACK_PREFIX):
        return None
    parts = raw[len(WIZARD_CALLBACK_PREFIX):].split(":")
    action = parts[0] if parts else ""
    args: list[Any] = []
    for item in parts[1:]:
        try:
            args.append(int(item))
        except (TypeError, ValueError):
            args.append(item)
    return action, tuple(args)


# --------------------------------------------------------------------------- #
# Transport glue
#
# A wizard must keep the chat on a single screen: the prompt message is edited in
# place for the next step, for a validation error and for the preview card, and
# the operator's own raw text message is removed. Telegram refuses an edit that
# changes nothing (``Message is not modified``) and cannot edit a message that is
# gone, so every edit has a send fallback and only the benign rejection is
# swallowed — a bare ``except`` around an edit is indistinguishable from a dead
# button.
# --------------------------------------------------------------------------- #

async def _wizard_edit_or_send(update: Any, context: Any, page: RenderedPage,
                               state: dict) -> None:
    """Render ``page`` into the wizard's own message, in place.

    The message the button sits on is preferred, because that is the one the legacy
    prompt edited and the one every transport (including the offline harnesses)
    exposes an ``edit_message_text`` for. The bot API is the second choice, for a
    wizard advanced by a plain text message rather than a button. Only the benign
    ``Message is not modified`` rejection is swallowed; anything else falls through
    to the next transport instead of leaving the operator with a dead card.
    """
    query = getattr(update, "callback_query", None)
    if query is not None and hasattr(query, "edit_message_text"):
        try:
            await query.edit_message_text(page.text, parse_mode=page.parse_mode,
                                          reply_markup=page.reply_markup)
            return
        except Exception as exc:
            if "not modified" in str(exc).lower():
                return
    bot = getattr(context, "bot", None)
    message_id = state.get("prompt_message_id")
    chat_id = state.get("prompt_chat_id")
    if bot is not None and message_id and chat_id and hasattr(bot, "edit_message_text"):
        try:
            await bot.edit_message_text(
                chat_id=int(chat_id), message_id=int(message_id), text=page.text,
                parse_mode=page.parse_mode, reply_markup=page.reply_markup)
            return
        except Exception as exc:
            if "not modified" in str(exc).lower():
                return
            # The prompt is gone or uneditable; drop it and send a fresh card so the
            # operator is never left without a live wizard.
            try:
                await bot.delete_message(chat_id=int(chat_id), message_id=int(message_id))
            except Exception:
                pass
    message = getattr(update, "effective_message", None) or getattr(update, "message", None)
    if message is not None and hasattr(message, "reply_text"):
        await message.reply_text(page.text, parse_mode=page.parse_mode,
                                 reply_markup=page.reply_markup)


async def _wizard_drop_user_message(update: Any) -> None:
    """Remove the operator's raw text message so the step stays single-screen."""
    message = getattr(update, "message", None)
    if message is None:
        return
    try:
        await message.delete()
    except Exception:
        pass


def _wizard_actor(update: Any) -> dict:
    user = getattr(update, "effective_user", None)
    if user is None:
        return {}
    return {"id": int(getattr(user, "id", 0) or 0),
            "name": getattr(user, "full_name", None) or getattr(user, "first_name", None) or "",
            "username": getattr(user, "username", None) or ""}


async def _wizard_render(update: Any, context: Any, state: dict, *,
                         error: str | None = None, notice: str | None = None) -> None:
    """Render whichever card the state now calls for."""
    flow = flow_by_key(state["flow"])
    if flow is not None and int(state["step"]) >= len(flow.steps):
        page = render_wizard_preview(state, error=error)
    else:
        page = render_wizard_step(state, error=error, notice=notice)
    await _wizard_edit_or_send(update, context, page, state)


async def wizard_render(update: Any, context: Any, state: dict, *,
                        error: str | None = None, notice: str | None = None) -> None:
    """Public entry point: draw the state's current card into its own message."""
    await _wizard_render(update, context, state, error=error, notice=notice)


async def wizard_handle_text(update: Any, context: Any) -> bool:
    """Consume one typed value for the live wizard; ``False`` when none is live."""
    state = wizard_state(context.user_data)
    if state is None:
        return False
    message = getattr(update, "message", None)
    text = str(getattr(message, "text", "") or "").strip()
    if text.startswith("/"):
        # A command is never a wizard value; drop the wizard so /start and friends
        # keep working instead of being swallowed by a stale form.
        clear_wizard(context.user_data)
        return False
    if text.lower() in ("cancel", "انصراف", "لغو"):
        return await wizard_cancel(update, context)
    outcome, error = wizard_apply_text(state, text)
    await _wizard_drop_user_message(update)
    if outcome == "invalid":
        clear_wizard(context.user_data)
        return False
    await _wizard_render(update, context, state, error=error)
    return True


async def wizard_cancel(update: Any, context: Any) -> bool:
    """Abandon the live wizard and return to the surface that started it."""
    state = clear_wizard(context.user_data)
    if state is None:
        return False
    parent = state.get("return_callback") or None
    page = render_page("لغو شد", "❌ عملیات فرم لغو شد و هیچ تغییری ثبت نشد.",
                       persona=state.get("persona") or "admin", parent_callback=parent)
    await _wizard_edit_or_send(update, context, page, state)
    await _wizard_drop_user_message(update)
    return True


async def wizard_handle_callback(update: Any, context: Any, action: str,
                                 args: tuple = ()) -> bool:
    """Drive the live wizard from its own inline buttons."""
    state = wizard_state(context.user_data)
    if state is None:
        return False
    query = getattr(update, "callback_query", None)
    if query is not None:
        try:
            await query.answer()
        except Exception:
            pass
    if action == "cancel":
        return await wizard_cancel(update, context)
    if action == "prev":
        wizard_back(state)
        await _wizard_render(update, context, state)
        return True
    if action == "edit":
        wizard_back(state)
        await _wizard_render(update, context, state,
                             notice="مقدار مورد نظر را اصلاح کنید.")
        return True
    if action == "confirm":
        try:
            notice = wizard_confirm(state, _wizard_actor(update))
        except Exception as exc:
            # The write failed, so the wizard stays open on its preview card with the
            # reason: the operator can fix a value instead of re-entering everything.
            await _wizard_render(update, context, state, error=str(exc))
            return True
        parent = state.get("return_callback") or None
        clear_wizard(context.user_data)
        page = render_page("ثبت شد", notice, persona=state.get("persona") or "admin",
                          parent_callback=parent)
        await _wizard_edit_or_send(update, context, page, state)
        return True
    if action == "pick" and len(args) >= 2:
        outcome, error = wizard_choose(state, int(args[0]), int(args[1]))
    elif action == "done" and args:
        outcome, error = wizard_done(state, int(args[0]))
    elif action == "skip" and args:
        outcome, error = wizard_skip(state, int(args[0]))
    elif action == "gen" and args:
        outcome, error = wizard_generate(state, int(args[0]))
    else:
        return False
    if outcome == "invalid":
        clear_wizard(context.user_data)
        return False
    await _wizard_render(update, context, state, error=error)
    return True


__all__ = ["RenderedPage", "HomeRoute", "persona_name", "button",
           "navigation_row", "navigation_footer", "render_page", "render_error", "render_uri", "route_home",
           "WizardStep", "WizardFlow", "WIZARD_FLOWS", "WIZARD_STATE_KEY",
           "WIZARD_CALLBACK_PREFIX", "WIZARD_TTL_SECONDS", "WIZARD_CONFIRM_LABEL",
           "WIZARD_EDIT_LABEL", "WIZARD_CANCEL_LABEL", "WIZARD_PREV_LABEL",
           "WIZARD_PREVIEW_TITLE", "register_flow", "flow_by_key", "start_wizard",
           "wizard_state", "clear_wizard", "current_step", "render_wizard_step",
           "render_wizard_preview", "wizard_apply_text", "wizard_choose", "wizard_skip",
           "wizard_generate", "wizard_back", "wizard_confirm", "wizard_parse_callback",
           "wizard_handle_text", "wizard_handle_callback", "wizard_cancel",
           "wizard_done", "wizard_render", "CHECKBOX_ON", "CHECKBOX_OFF"]
