"""Managing the fleet from the deck (#3471): create, rename, delete, remotes, order.

Every mutation goes through the hub's own routes (the console's bodies, by immutable id —
display names are editable, ids are not); the modals here only collect what the operator
means. Two are guarded on purpose: deleting a member wants its name TYPED (and purge is a
separate checkbox — both irreversible, and the modal says so), and a remote's bearer token
is entered masked, sent once, never shown again.
"""

from __future__ import annotations

from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Select, Static

MANAGE_CSS = """
.manage-modal { align: center middle; }
.manage-box { width: 76; max-width: 96%; height: auto; border: round $accent; background: $surface; padding: 1 2; }
.manage-title { text-style: bold; height: 1; }
.manage-sub { color: $text-muted; }
.manage-danger { color: $error; }
.manage-field { height: auto; margin-top: 1; }
.manage-field Label { color: $text-muted; }
.manage-field Input { width: 100%; }
.manage-buttons { height: 3; margin-top: 1; align-horizontal: right; }
.manage-buttons Button { margin-left: 1; min-width: 12; }
.manage-hint { color: $text-muted; height: 1; }
"""


def _field(label: str, widget) -> Vertical:
    return Vertical(Label(label), widget, classes="manage-field")


class NewAgentModal(ModalScreen[dict | None]):
    """Name + archetype (the built-in Basic and every installed archetype), "inherit the
    hub's model connections" and "start after create". Returns the ``POST /api/fleet``
    body, or None."""

    BINDINGS = [
        Binding("escape", "cancel", "cancel", show=True, priority=True),
        Binding("ctrl+s", "submit", "create", show=True, priority=True),
    ]

    def __init__(self, archetypes: list[dict]) -> None:
        super().__init__(classes="manage-modal")
        self.archetypes = [a for a in archetypes if a.get("id")]
        if not any(str(a.get("id")) == "basic" or not a.get("bundle") for a in self.archetypes):
            self.archetypes.insert(0, {"id": "basic", "label": "Basic", "blurb": "a blank agent", "bundle": None, "soul": ""})

    def compose(self) -> ComposeResult:
        with Vertical(classes="manage-box"):
            yield Static("new member", classes="manage-title")
            yield _field("name", Input(placeholder="letters, digits, - and _", id="name", select_on_focus=False))
            options = [(f"{a.get('label') or a['id']}" + (f" — {a['blurb']}" if a.get("blurb") else ""), str(a["id"])) for a in self.archetypes]
            yield _field("archetype", Select[str](options, value=str(self.archetypes[0]["id"]), allow_blank=False, id="archetype"))
            yield Static("", id="archetype-note", classes="manage-sub")
            yield Checkbox("inherit the hub's model connections and credentials", value=True, id="inherit")
            yield Checkbox("start after create", value=True, id="start")
            yield _field("port (optional)", Input(placeholder="next free", id="port", select_on_focus=False))
            with Horizontal(classes="manage-buttons"):
                yield Button("Create (ctrl+s)", id="submit", variant="primary")
                yield Button("Cancel (esc)", id="cancel")
            yield Static("", id="hint", classes="manage-hint")

    def on_mount(self) -> None:
        self.query_one("#name", Input).focus()
        self._note()

    def _picked(self) -> dict:
        aid = self.query_one("#archetype", Select).value
        return next((a for a in self.archetypes if str(a.get("id")) == str(aid)), self.archetypes[0])

    def _note(self) -> None:
        a = self._picked()
        bits = []
        if a.get("bundle"):
            bits.append(f"installs {a['bundle']}")
        if a.get("requires_tools"):
            bits.append("needs " + ", ".join(str(t) for t in a["requires_tools"]))
        if a.get("requires"):
            bits.append("requires " + ", ".join(str(r) for r in a["requires"]))
        self.query_one("#archetype-note", Static).update("  ·  ".join(bits) if bits else "a blank agent with the persona you give it")

    @on(Select.Changed)
    def _changed(self, event: Select.Changed) -> None:
        self._note()

    def _body(self) -> dict | None:
        name = self.query_one("#name", Input).value.strip()
        if not name:
            self.query_one("#hint", Static).update("a name is required")
            self.query_one("#name", Input).focus()
            return None
        if name.lower() == "host":
            self.query_one("#hint", Static).update("'host' is reserved — it is how the fleet addresses the hub")
            return None
        port_raw = self.query_one("#port", Input).value.strip()
        port: int | None = None
        if port_raw:
            try:
                port = int(port_raw)
            except ValueError:
                self.query_one("#hint", Static).update("port must be a number")
                return None
        a = self._picked()
        body: dict = {
            "name": name,
            "bundle": a.get("bundle") or None,
            "inherit_config": self.query_one("#inherit", Checkbox).value,
            "start": self.query_one("#start", Checkbox).value,
        }
        if a.get("soul"):
            body["soul"] = a["soul"]
        if a.get("requires_tools"):
            body["requires_tools"] = list(a["requires_tools"])
        if port is not None:
            body["port"] = port
        return body

    def action_submit(self) -> None:
        body = self._body()
        if body is not None:
            self.dismiss(body)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted)
    def _enter(self, event: Input.Submitted) -> None:
        self.action_submit()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "submit":
            self.action_submit()
        else:
            self.action_cancel()


class RenameModal(ModalScreen[str | None]):
    """A display-name change: the id, URL slug and data scope never move."""

    BINDINGS = [Binding("escape", "cancel", "cancel", show=True, priority=True)]

    def __init__(self, current: str) -> None:
        super().__init__(classes="manage-modal")
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(classes="manage-box"):
            yield Static(f"rename {self.current}", classes="manage-title")
            yield Static("display name only — the id, the URL slug and the member's data never change; a running member re-reads it on restart", classes="manage-sub")
            yield _field("new name", Input(value=self.current, id="name", select_on_focus=False))
            with Horizontal(classes="manage-buttons"):
                yield Button("Rename", id="submit", variant="primary")
                yield Button("Cancel (esc)", id="cancel")
            yield Static("", id="hint", classes="manage-hint")

    def on_mount(self) -> None:
        inp = self.query_one("#name", Input)
        inp.focus()
        inp.cursor_position = len(inp.value)

    def _submit(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        if not name:
            self.query_one("#hint", Static).update("a name is required")
            return
        if name == self.current:
            self.dismiss(None)
            return
        self.dismiss(name)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted)
    def _enter(self, event: Input.Submitted) -> None:
        self._submit()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self._submit() if event.button.id == "submit" else self.action_cancel()


class DeleteModal(ModalScreen[dict | None]):
    """Type the member's name to delete it; purge (its workspace and data, gone for good) is
    a separate box. For a remote member this only unregisters it. Returns ``{"purge": bool}``
    or None."""

    BINDINGS = [Binding("escape", "cancel", "cancel", show=True, priority=True)]

    def __init__(self, name: str, *, remote: bool = False) -> None:
        super().__init__(classes="manage-modal")
        self.target = name  # (Screen.name is Textual's own, read-only)
        self.remote = remote

    def compose(self) -> ComposeResult:
        with Vertical(classes="manage-box"):
            if self.remote:
                yield Static(f"remove remote {self.target}", classes="manage-title")
                yield Static("unregisters it from this fleet — the remote agent itself is untouched", classes="manage-sub")
            else:
                yield Static(f"delete {self.target}", classes="manage-title")
                yield Static("stops it and takes it out of the fleet. Its data is kept unless you also purge — both are irreversible.", classes="manage-danger")
            yield _field(f"type {self.target} to confirm", Input(placeholder=self.target, id="confirm", select_on_focus=False))
            if not self.remote:
                yield Checkbox("also purge its workspace and data (cannot be undone)", value=False, id="purge")
            with Horizontal(classes="manage-buttons"):
                yield Button("Remove" if self.remote else "Delete", id="submit", variant="error", disabled=True)
                yield Button("Cancel (esc)", id="cancel")
            yield Static("", id="hint", classes="manage-hint")

    def on_mount(self) -> None:
        self.query_one("#confirm", Input).focus()

    def _matches(self) -> bool:
        return self.query_one("#confirm", Input).value.strip() == self.target

    @on(Input.Changed)
    def _typed(self, event: Input.Changed) -> None:
        self.query_one("#submit", Button).disabled = not self._matches()

    def _submit(self) -> None:
        if not self._matches():
            self.query_one("#hint", Static).update(f"type the name exactly: {self.target}")
            return
        purge = bool(self.query_one("#purge", Checkbox).value) if not self.remote else False
        self.dismiss({"purge": purge})

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted)
    def _enter(self, event: Input.Submitted) -> None:
        self._submit()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self._submit() if event.button.id == "submit" else self.action_cancel()


class RemoteModal(ModalScreen[dict | None]):
    """Register a remote protoAgent (name, URL, optional bearer entered masked and never
    shown again), or edit one in place. Returns ``{name, url, token?}`` — for an edit only the
    changed fields, ``token: ""`` meaning "clear the stored bearer" — or None."""

    BINDINGS = [Binding("escape", "cancel", "cancel", show=True, priority=True)]

    def __init__(self, existing: dict | None = None) -> None:
        super().__init__(classes="manage-modal")
        self.existing = existing

    def compose(self) -> ComposeResult:
        ex = self.existing or {}
        with Vertical(classes="manage-box"):
            yield Static(f"edit remote {ex.get('name')}" if ex else "add a remote member", classes="manage-title")
            yield Static("the hub proxies its console and A2A under a slug window, like a local member; an unreachable peer is registered anyway and reads 'unreachable' until it answers", classes="manage-sub")
            yield _field("name", Input(value=str(ex.get("name") or ""), placeholder="ava", id="name", select_on_focus=False))
            yield _field("url", Input(value=str(ex.get("url") or ""), placeholder="https://ava.tail:7870", id="url", select_on_focus=False))
            yield _field("bearer token" + (" (blank keeps the stored one)" if ex else " (optional)"), Input(password=True, placeholder="entered once, never shown again", id="token", select_on_focus=False))
            if ex:
                yield Checkbox("clear the stored token", value=False, id="clear")
            with Horizontal(classes="manage-buttons"):
                yield Button("Save" if ex else "Add", id="submit", variant="primary")
                yield Button("Cancel (esc)", id="cancel")
            yield Static("", id="hint", classes="manage-hint")

    def on_mount(self) -> None:
        self.query_one("#name" if not self.existing else "#url", Input).focus()

    def _submit(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        url = self.query_one("#url", Input).value.strip()
        token = self.query_one("#token", Input).value
        if self.existing is None:
            if not name or not url:
                self.query_one("#hint", Static).update("a name and a URL are required")
                return
            out: dict = {"name": name, "url": url}
            if token:
                out["token"] = token
            self.dismiss(out)
            return
        out = {}
        if name and name != str(self.existing.get("name") or ""):
            out["name"] = name
        if url and url != str(self.existing.get("url") or ""):
            out["url"] = url
        if self.query_one("#clear", Checkbox).value:
            out["token"] = ""
        elif token:
            out["token"] = token
        self.dismiss(out or None)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Input.Submitted)
    def _enter(self, event: Input.Submitted) -> None:
        self._submit()

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self._submit() if event.button.id == "submit" else self.action_cancel()
