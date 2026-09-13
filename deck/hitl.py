"""Answering a parked turn from the deck (#3470): questions, forms, approvals.

A member parks a turn (``input-required``) when the agent calls ``ask_human`` (a plain
question), ``request_user_input`` (a JSON-schema form, one or more steps), or a gated tool
asks for approval (``run_command``, a permanent delete). The prompt rides the status
message as a ``hitl-v1`` DataPart; the console renders it as a card
(``apps/web/src/chat/HitlForm.tsx``) and this module renders it as a modal.

The answer goes back the way the console sends it: a ``SendStreamingMessage`` on the parked
task with ``metadata.hitl_resume = true`` and the answer as the message text — the raw
answer for a question, ``"approved"`` / ``"denied"`` for an approval, the answers object as
JSON for a form (``tools/lg_tools.py`` reads the string back). Dismissing sends the same
sentinel the console does (:data:`deck.a2a.DISMISS_SENTINEL`) so the task cannot stay
parked forever. A plugin composer-form (``plugin_callback_id``) is NOT a graph interrupt: its
answers are redeemed on ``POST /api/chat/commands/submit`` and a returned form is the next
wizard step.

The form rules are the console's (``apps/web/src/chat/hitl-form.ts``), ported function for
function so a form renders and gates the same in both places: fields from the step's
JSON schema, ``oneOf`` / ``enum`` options, ``type: array`` multi-select, ``showWhen``
visibility, ``default`` as an answer, required-gating per step.
"""

from __future__ import annotations

import json
import re
from typing import Any

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from deck.modal import OnceModal
from textual.widgets import Button, Checkbox, Input, Label, Markdown, Select, SelectionList, Static, TextArea

# ── the prompt ────────────────────────────────────────────────────────────────


def kind_of(hitl: dict | None) -> str:
    """``question`` (ask_human) · ``form`` (request_user_input, or a plugin form) ·
    ``approval`` (a gated tool). Anything malformed is a question with whatever text it has."""
    if not isinstance(hitl, dict):
        return "question"
    if hitl.get("kind") == "approval":
        return "approval"
    if hitl.get("kind") == "form" and isinstance(hitl.get("steps"), list) and hitl["steps"]:
        return "form"
    if hitl.get("plugin_callback_id") and isinstance(hitl.get("steps"), list):
        return "form"  # a plugin form with no fields is still redeemed as a form ({}), never typed at as a question
    return "question"


def prompt_of(hitl: dict | None) -> str:
    if not isinstance(hitl, dict):
        return "input required"
    return str(hitl.get("question") or hitl.get("title") or "input required")


# ── form rules (apps/web/src/chat/hitl-form.ts) ───────────────────────────────


def fields_of(step: dict | None) -> list[tuple[str, dict, bool]]:
    """``(key, schema, required)`` for every property a step's JSON schema declares."""
    schema = (step or {}).get("schema") if isinstance((step or {}).get("schema"), dict) else {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = set(schema.get("required") or []) if isinstance(schema.get("required"), list) else set()
    return [(k, fs if isinstance(fs, dict) else {}, k in required) for k, fs in props.items()]


def is_multi(schema: dict) -> bool:
    return schema.get("type") == "array"


def _option_source(schema: dict) -> dict:
    if is_multi(schema):
        return schema.get("items") if isinstance(schema.get("items"), dict) else {}
    return schema


def options_of(schema: dict) -> list[tuple[str, str, str]]:
    """``(value, label, description)`` from ``oneOf`` [{const,title,description}] (preferred)
    or a bare ``enum``; empty when the field is not a choice."""
    src = _option_source(schema)
    if isinstance(src.get("oneOf"), list):
        out = []
        for o in src["oneOf"]:
            if not isinstance(o, dict):
                continue
            value = o.get("const", o.get("title", ""))
            out.append((str(value if value is not None else ""), str(o.get("title", value) if o.get("title") is not None else value), str(o.get("description") or "")))
        return out
    if isinstance(src.get("enum"), list):
        return [(str(v), str(v), "") for v in src["enum"]]
    return []


def has_value(schema: dict, value: Any) -> bool:
    if is_multi(schema):
        return isinstance(value, list) and len(value) > 0
    if schema.get("type") == "boolean":
        return True  # unchecked is a valid False
    return value is not None and value != ""


def is_visible(schema: dict, values: dict) -> bool:
    cond = schema.get("showWhen")
    if not isinstance(cond, dict) or not cond.get("field"):
        return True
    v = values.get(str(cond["field"]))
    if isinstance(cond.get("in"), list):
        return any(x == v for x in cond["in"])
    if "equals" in cond:
        return v == cond["equals"]
    return bool(v)


def visible_fields_of(step: dict | None, values: dict) -> list[tuple[str, dict, bool]]:
    return [f for f in fields_of(step) if is_visible(f[1], values)]


def seed_defaults(steps: list[dict]) -> dict:
    """A ``default`` IS an answer (#1978): every field carrying one starts answered."""
    values: dict = {}
    for step in steps or []:
        for key, schema, _ in fields_of(step):
            if "default" in schema and schema["default"] is not None:
                values[key] = schema["default"]
    return values


def missing_in_step(step: dict | None, values: dict) -> list[str]:
    return [k for k, s, req in fields_of(step) if req and is_visible(s, values) and not has_value(s, values.get(k))]


def any_step_missing(steps: list[dict], values: dict) -> bool:
    return any(missing_in_step(s, values) for s in steps or [])


def coerce(schema: dict, raw: Any) -> Any:
    """A typed answer from what a widget holds: numbers parse (an unparsable number is
    ``None`` → unanswered), booleans stay, arrays stay lists, everything else is text."""
    t = schema.get("type")
    if is_multi(schema):
        return list(raw) if isinstance(raw, (list, tuple, set)) else ([] if raw in (None, "") else [raw])
    if t == "boolean":
        return bool(raw)
    if t in ("number", "integer"):
        if raw is None or str(raw).strip() == "":
            return None
        try:
            return int(str(raw).strip()) if t == "integer" else float(str(raw).strip())
        except ValueError:
            return None
    return raw if raw is None else str(raw)


def answer_text(kind: str, answer: Any) -> str:
    """What goes on the wire (the message text): a form's answers as JSON, else the text."""
    if kind == "form" and not isinstance(answer, str):
        return json.dumps(answer, ensure_ascii=False)
    return str(answer)


# ── the modals ────────────────────────────────────────────────────────────────

HITL_CSS = """
.hitl-modal { align: center middle; }
.hitl-box { width: 80%; max-width: 100; height: auto; max-height: 90%; border: round $accent; background: $surface; padding: 1 2; }
.hitl-title { text-style: bold; height: 1; }
.hitl-sub { color: $text-muted; }
.hitl-detail { height: auto; max-height: 12; border: solid $surface-lighten-2; padding: 0 1; }
.hitl-field { height: auto; margin-top: 1; }
.hitl-field Label { color: $text-muted; }
.hitl-field Input { width: 100%; }
.hitl-field TextArea { height: 5; }
.hitl-field SelectionList { height: auto; max-height: 8; }
.hitl-buttons { height: 3; margin-top: 1; align-horizontal: right; }
.hitl-buttons Button { margin-left: 1; min-width: 12; }
.hitl-hint { color: $text-muted; height: 1; }
"""


class ApprovalModal(OnceModal[str | None]):
    """Approve / deny a gated tool call. Returns ``"approved"``, ``"denied"``, the dismiss
    marker ``"__dismiss__"``, or ``None`` (closed — the turn stays parked)."""

    BINDINGS = [
        Binding("escape", "close", "leave it parked", show=True, priority=True),
        Binding("a", "approve", "approve", show=True),
        Binding("d", "deny", "deny", show=True),
        Binding("ctrl+d", "dismiss_request", "dismiss the request", show=True, priority=True),
    ]

    def __init__(self, member: str, hitl: dict) -> None:
        super().__init__(classes="hitl-modal")
        self.member = member
        self.hitl = hitl

    def compose(self) -> ComposeResult:
        with Vertical(classes="hitl-box"):
            yield Static(Text(f"⚑ {self.member} asks for approval"), classes="hitl-title")
            yield Static(Text(str(self.hitl.get("title") or "Approve?")), classes="hitl-sub")  # member-authored: text, never markup
            detail = str(self.hitl.get("detail") or "")
            if detail:
                with VerticalScroll(classes="hitl-detail"):
                    yield Static(Text(detail))
            if self.hitl.get("project"):
                yield Static(Text(f"project: {self.hitl['project']}"), classes="hitl-sub")
            with Horizontal(classes="hitl-buttons"):
                yield Button("Approve (a)", id="approve", variant="success")
                yield Button("Deny (d)", id="deny", variant="error")
                yield Button("Dismiss (ctrl+d)", id="dismiss")
            yield Static("esc leaves the turn parked · dismiss tells the member to go on without an answer", classes="hitl-hint")

    def on_mount(self) -> None:
        self.query_one("#approve", Button).focus()

    def action_approve(self) -> None:
        self.dismiss("approved")

    def action_deny(self) -> None:
        self.dismiss("denied")

    def action_dismiss_request(self) -> None:
        self.dismiss("__dismiss__")

    def action_close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss({"approve": "approved", "deny": "denied", "dismiss": "__dismiss__"}.get(event.button.id or "", None))


class QuestionModal(OnceModal[str | None]):
    """A free-text answer to ``ask_human``. Returns the text, ``"__dismiss__"``, or ``None``."""

    BINDINGS = [
        Binding("escape", "close", "leave it parked", show=True, priority=True),
        Binding("ctrl+d", "dismiss_request", "dismiss the question", show=True, priority=True),
    ]

    def __init__(self, member: str, hitl: dict, *, draft: str = "") -> None:
        super().__init__(classes="hitl-modal")
        self.member = member
        self.hitl = hitl
        self.draft = draft

    def compose(self) -> ComposeResult:
        with Vertical(classes="hitl-box"):
            yield Static(Text(f"⚑ {self.member} asks"), classes="hitl-title")
            yield Markdown(prompt_of(self.hitl))
            yield Input(value=self.draft, placeholder="your answer… (enter sends)", id="answer", select_on_focus=False)
            with Horizontal(classes="hitl-buttons"):
                yield Button("Answer", id="send", variant="primary")
                yield Button("Dismiss (ctrl+d)", id="dismiss")
            yield Static("esc leaves the turn parked", classes="hitl-hint")

    def on_mount(self) -> None:
        self.query_one("#answer", Input).focus()

    def _send(self) -> None:
        text = self.query_one("#answer", Input).value.strip()
        if text:
            self.dismiss(text)

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        self._send()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "send":
            self._send()
        elif event.button.id == "dismiss":
            self.dismiss("__dismiss__")

    def action_dismiss_request(self) -> None:
        self.dismiss("__dismiss__")

    def action_close(self) -> None:
        self.dismiss(None)


class FormModal(OnceModal[dict | str | None]):
    """``request_user_input`` (or a plugin form): a stepped wizard, one step per screen with
    Back / Next and a step indicator; every step's answers submit together. Returns the
    answers dict, ``"__dismiss__"``, or ``None``."""

    BINDINGS = [
        Binding("escape", "close", "leave it parked", show=True, priority=True),
        Binding("ctrl+d", "dismiss_request", "dismiss the form", show=True, priority=True),
        Binding("ctrl+s", "submit", "submit", show=True, priority=True),
        Binding("ctrl+right", "next", "next step", show=False, priority=True),
        Binding("ctrl+left", "back", "previous step", show=False, priority=True),
    ]

    def __init__(self, member: str, hitl: dict) -> None:
        super().__init__(classes="hitl-modal")
        self.member = member
        self.hitl = hitl
        self.steps: list[dict] = [s for s in (hitl.get("steps") or []) if isinstance(s, dict)]
        self.values: dict = seed_defaults(self.steps)
        self.current = 0
        self._plugin = bool(hitl.get("plugin_callback_id"))
        # a schema key is any string; a Textual id is [A-Za-z_][A-Za-z0-9_-]*: every key gets
        # a stable safe slot (readable when the key already is one) and a reverse lookup
        self._slot_of: dict[str, str] = {}
        self._key_of: dict[str, str] = {}
        for step in self.steps:
            for key, _, _ in fields_of(step):
                if key not in self._slot_of:
                    base = re.sub(r"[^A-Za-z0-9_-]", "_", key) or "field"
                    if not re.match(r"[A-Za-z_]", base):
                        base = f"f_{base}"
                    slot, n = base, 1
                    while slot in self._key_of:
                        n += 1
                        slot = f"{base}-{n}"
                    self._slot_of[key], self._key_of[slot] = slot, key

    # ── layout ──

    def compose(self) -> ComposeResult:
        with Vertical(classes="hitl-box"):
            yield Static(Text(f"⚑ {self.member} needs input"), classes="hitl-title")
            yield Static(Text(str(self.hitl.get("title") or "")), classes="hitl-sub", id="form-title")
            if self.hitl.get("description"):
                yield Markdown(str(self.hitl["description"]))
            yield Static("", id="form-step-head", classes="hitl-sub")
            yield VerticalScroll(id="form-fields")
            with Horizontal(classes="hitl-buttons"):
                yield Button("Back", id="back")
                yield Button("Next", id="next")
                yield Button("Submit (ctrl+s)", id="submit", variant="primary")
                yield Button("Dismiss", id="dismiss")
            yield Static("", id="form-hint", classes="hitl-hint")

    async def on_mount(self) -> None:
        await self._render_step(focus=True)

    @property
    def step(self) -> dict:
        return self.steps[self.current] if self.steps else {}

    async def _render_step(self, *, focus: bool = False) -> None:
        box = self.query_one("#form-fields", VerticalScroll)
        await box.remove_children()  # the new fields reuse the ids — the old ones must be gone first
        n = len(self.steps)
        head = self.query_one("#form-step-head", Static)
        title = str(self.step.get("title") or "")
        head.update(Text((f"step {self.current + 1} / {n}" + (f" · {title}" if title else "")) if n > 1 else title))
        widgets: list = []
        if self.step.get("description"):
            widgets.append(Static(Text(str(self.step["description"])), classes="hitl-sub"))
        for key, schema, required in visible_fields_of(self.step, self.values):
            widgets.append(self._field_widget(key, schema, required))
        await box.mount_all(widgets)
        self._render_buttons()
        if focus:
            self._focus_first()

    def _focus_first(self) -> None:
        for w in self.query("#form-fields Input, #form-fields TextArea, #form-fields Select, #form-fields SelectionList, #form-fields Checkbox"):
            w.focus()
            return

    def _field_widget(self, key: str, schema: dict, required: bool) -> Vertical:
        label = f"{schema.get('title') or key}{' *' if required else ''}"
        value = self.values.get(key)
        opts = options_of(schema)
        slot = self._slot_of.get(key, key)
        children: list = [] if schema.get("type") == "boolean" else [Label(Text(label))]  # a checkbox carries its own label
        if schema.get("description"):
            children.append(Static(Text(str(schema["description"])), classes="hitl-sub"))
        if is_multi(schema) and opts:
            chosen = set(value) if isinstance(value, list) else set()
            children.append(SelectionList[str](*[(Text(lab + (f" — {desc}" if desc else "")), val, val in chosen) for val, lab, desc in opts], id=f"in-{slot}"))  # option labels are member text too
        elif opts:
            known = {v for v, _, _ in opts}
            children.append(Select[str]([(Text(lab + (f" — {desc}" if desc else "")), val) for val, lab, desc in opts], value=str(value) if value is not None and str(value) in known else Select.NULL, allow_blank=True, id=f"in-{slot}"))
        elif schema.get("type") == "boolean":
            children.append(Checkbox(Text(label), value=bool(value), id=f"in-{slot}"))  # Textual 8.2.8 builds a str label with Content.from_text, which PARSES markup
        elif schema.get("format") == "textarea":
            children.append(TextArea(str(value) if value is not None else "", id=f"in-{slot}"))
        else:
            hint = "number" if schema.get("type") in ("number", "integer") else ""
            # select_on_focus=False: a visibility re-render re-focuses the successor Input, and
            # Textual's focus-selects-all would make the next keystroke REPLACE what was typed
            children.append(Input(value=str(value) if value is not None else "", placeholder=hint, id=f"in-{slot}", select_on_focus=False))
        return Vertical(*children, classes="hitl-field", id=f"field-{slot}")

    def _render_buttons(self) -> None:
        n = len(self.steps)
        last = self.current >= n - 1
        self.query_one("#back", Button).display = n > 1
        self.query_one("#back", Button).disabled = self.current == 0
        self.query_one("#next", Button).display = n > 1 and not last
        self.query_one("#submit", Button).display = last
        self.query_one("#next", Button).disabled = bool(missing_in_step(self.step, self.values))
        missing = [k for s in self.steps for k in missing_in_step(s, self.values)]
        self.query_one("#submit", Button).disabled = bool(missing)
        hint = "esc leaves the turn parked"
        if missing:
            hint = f"required: {', '.join(missing)}  ·  " + hint
        if n > 1:
            hint = "ctrl+← / ctrl+→ steps  ·  " + hint
        self.query_one("#form-hint", Static).update(Text(hint))  # names member-authored field keys

    # ── values ──

    def _schema_for(self, key: str) -> dict:
        for s in self.steps:
            for k, schema, _ in fields_of(s):
                if k == key:
                    return schema
        return {}

    async def _set(self, key: str, raw: Any) -> None:
        schema = self._schema_for(key)
        value = coerce(schema, raw)
        if value is None or value == "" or (is_multi(schema) and not value):
            self.values.pop(key, None)
        else:
            self.values[key] = value
        # a sibling's visibility may depend on this answer
        visible_now = {k for k, _, _ in visible_fields_of(self.step, self.values)}
        shown = {self._key_of.get(w.id[6:], w.id[6:]) for w in self.query(".hitl-field") if w.id}
        if visible_now != shown:
            # the re-render replaces the widget being edited: put focus (and the caret)
            # back on its successor of the same id, or the operator's next keystrokes go
            # to the scroll container
            focused_id = getattr(self.focused, "id", None)
            await self._render_step()
            if focused_id:
                for w in self.query(f"#{focused_id}"):
                    w.focus()
                    if isinstance(w, Input):
                        w.cursor_position = len(w.value)
                    break
        else:
            self._render_buttons()

    @on(Input.Changed)
    async def _input_changed(self, event: Input.Changed) -> None:
        if event.input.id and event.input.id.startswith("in-"):
            await self._set(self._key_of.get(event.input.id[3:], event.input.id[3:]), event.value)

    @on(TextArea.Changed)
    async def _textarea_changed(self, event: TextArea.Changed) -> None:
        if event.text_area.id and event.text_area.id.startswith("in-"):
            await self._set(self._key_of.get(event.text_area.id[3:], event.text_area.id[3:]), event.text_area.text)

    @on(Checkbox.Changed)
    async def _checkbox_changed(self, event: Checkbox.Changed) -> None:
        if event.checkbox.id and event.checkbox.id.startswith("in-"):
            await self._set(self._key_of.get(event.checkbox.id[3:], event.checkbox.id[3:]), event.value)

    @on(Select.Changed)
    async def _select_changed(self, event: Select.Changed) -> None:
        if event.select.id and event.select.id.startswith("in-"):
            await self._set(self._key_of.get(event.select.id[3:], event.select.id[3:]), None if event.value is Select.NULL else event.value)

    @on(SelectionList.SelectedChanged)
    async def _selection_changed(self, event: SelectionList.SelectedChanged) -> None:
        sl = event.selection_list
        if sl.id and sl.id.startswith("in-"):
            await self._set(self._key_of.get(sl.id[3:], sl.id[3:]), list(sl.selected))

    @on(Input.Submitted)
    async def _input_submitted(self, event: Input.Submitted) -> None:
        # enter in a field: next step, or submit on the last one
        if self.current < len(self.steps) - 1:
            await self.action_next()
        else:
            self.action_submit()

    # ── navigation ──

    async def action_next(self) -> None:
        if self.current < len(self.steps) - 1 and not missing_in_step(self.step, self.values):
            self.current += 1
            await self._render_step(focus=True)

    async def action_back(self) -> None:
        if self.current > 0:
            self.current -= 1
            await self._render_step(focus=True)

    def action_submit(self) -> None:
        if any_step_missing(self.steps, self.values):
            self._render_buttons()
            return
        self.dismiss(dict(self.values))

    def action_dismiss_request(self) -> None:
        self.dismiss("__dismiss__")

    def action_close(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed)
    async def _pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid == "back":
            await self.action_back()
        elif bid == "next":
            await self.action_next()
        elif bid == "submit":
            self.action_submit()
        elif bid == "dismiss":
            self.action_dismiss_request()


def open_prompt(app, member: str, hitl: dict, callback, *, draft: str = "") -> None:
    """Push the modal for this prompt's kind; ``callback(result)`` gets the answer (see each
    modal for the shapes), ``"__dismiss__"``, or ``None``."""
    kind = kind_of(hitl)
    if kind == "approval":
        app.push_screen(ApprovalModal(member, hitl), callback)
    elif kind == "form":
        app.push_screen(FormModal(member, hitl), callback)
    else:
        app.push_screen(QuestionModal(member, hitl, draft=draft), callback)
