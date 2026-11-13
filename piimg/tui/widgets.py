"""Building blocks shared by the TUI's screens.

Keys, everywhere:
  Tab / Shift+Tab           next / previous widget
  Ctrl+Left/Right/Up/Down   between panes (screen-specific; see each screen)
  Up / Down                 within a pane: between the fields of a form, the
                            rows of a list; in a multi-line box they move the
                            cursor and leave the box at its first/last line
  Left / Right              between buttons in a button row
  Enter / Space             open a drop-down, toggle a checkbox, press a button
"""
from dataclasses import dataclass, field
from typing import Any, Optional

from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import Button, Checkbox, Input, Label, Select, Static, TextArea


class NavSelect(Select, inherit_bindings=False):
    """A Select that leaves Up/Down to the form (Enter/Space open it)."""
    BINDINGS = [Binding("enter,space", "show_overlay", "Choose", show=False)]


class NavTextArea(TextArea):
    """A TextArea whose Up on the first line / Down on the last line moves to the
    previous / next field, so arrow keys can walk through a whole form."""

    def action_cursor_up(self, select: bool = False) -> None:
        if not select and self.cursor_location[0] == 0:
            self.screen.focus_previous()
        else:
            super().action_cursor_up(select)

    def action_cursor_down(self, select: bool = False) -> None:
        if not select and self.cursor_location[0] >= self.document.line_count - 1:
            self.screen.focus_next()
        else:
            super().action_cursor_down(select)


class ButtonRow(Horizontal):
    """Buttons; Left/Right move between them."""
    BINDINGS = [Binding("left", "app.focus_previous", show=False), Binding("right", "app.focus_next", show=False)]


class StatusLine(Static):
    """One line of explanation at the bottom of a screen or dialog."""

    def say(self, text, style=""):
        self.update(Text(text or "", style=style))


# ------------------------------------------------------------------- forms

@dataclass
class FieldDef:
    """One form field.

    type: str, secret, int, bool, choice, lines (one value per line), map
          (KEY=VALUE per line), text (multi-line), yaml (multi-line YAML)
    """
    key: str
    label: str
    type: str = "str"
    required: bool = False
    help: str = ""
    choices: tuple = ()
    value: Any = None
    placeholder: str = ""
    suggestions: tuple = ()
    data: dict = field(default_factory=dict)  # caller's own bookkeeping

    def explain(self):
        bits = [f"{self.label}{' (required)' if self.required else ''}: {self.help or ''}".rstrip(": ")]
        if self.type in ("lines", "map"):
            bits.append("one per line" if self.type == "lines" else "KEY=VALUE, one per line")
        return " — ".join(b for b in bits if b)


class Form(VerticalScroll):
    """A column of labelled fields. Posts Form.Focused(field) when a field gets
    focus (for the status line) and Form.Changed(field) when a value changes."""

    class Focused(Message):
        def __init__(self, form, field):
            self.form, self.field = form, field
            super().__init__()

    class Changed(Message):
        def __init__(self, form, field):
            self.form, self.field = form, field
            super().__init__()

    BINDINGS = [Binding("up", "app.focus_previous", show=False), Binding("down", "app.focus_next", show=False)]

    def __init__(self, fields, **kw):
        super().__init__(**kw)
        self.fields = list(fields)
        self._widgets = {}
        self._loading = True

    def compose(self) -> ComposeResult:
        for f in self.fields:
            with Vertical(classes="field"):
                if f.type != "bool":
                    yield Label(Text.assemble((f.label, "bold"), (" *" if f.required else "", "red")),
                                classes="field-label")
                w = self._make(f)
                w.form_field = f
                self._widgets[f.key] = w
                yield w

    def on_mount(self):
        self.call_after_refresh(setattr, self, "_loading", False)

    def _make(self, f):
        v = f.value
        if f.type == "bool":
            return Checkbox(Text.assemble((f.label, "bold"), " ", (f.help or "", "dim")), value=bool(v))
        if f.type == "choice":
            opts = [(str(c), str(c)) for c in f.choices]
            return NavSelect(opts, value=str(v) if v not in (None, "") and str(v) in map(str, f.choices)
                             else Select.NULL, allow_blank=not f.required, prompt=f.placeholder or "(not set)")
        if f.type in ("lines", "map", "text", "yaml"):
            if isinstance(v, (list, tuple)):
                v = "\n".join(map(str, v))
            elif isinstance(v, dict):
                v = "\n".join(f"{k}={_scalar(x)}" for k, x in v.items())
            ta = NavTextArea(str(v or ""), soft_wrap=f.type == "text", show_line_numbers=f.type in ("text", "yaml"),
                             tab_behavior="focus")
            ta.add_class("tall" if f.type in ("text", "yaml") and not f.data.get("short") else "short")
            return ta
        inp = Input(value="" if v is None else str(v), placeholder=f.placeholder,
                    password=f.type == "secret", type="integer" if f.type == "int" else "text",
                    suggester=SuggestFromList(f.suggestions, case_sensitive=False) if f.suggestions else None)
        return inp

    def widget(self, key):
        return self._widgets[key]

    def value(self, key):
        """The field's current value: str, bool, list (lines) or dict (map); '' if empty."""
        f = next(x for x in self.fields if x.key == key)
        w = self._widgets[key]
        if isinstance(w, Checkbox):
            return w.value
        if isinstance(w, Select):
            return "" if w.value is Select.NULL or w.value is None else str(w.value)
        if isinstance(w, TextArea):
            text = w.text
            if f.type == "lines":
                return [l.strip() for l in text.splitlines() if l.strip()]
            if f.type == "map":
                out = {}
                for l in text.splitlines():
                    if l.strip():
                        k, _, val = l.partition("=")
                        out[k.strip()] = val.strip()
                return out
            return text
        return w.value

    def values(self):
        return {f.key: self.value(f.key) for f in self.fields}

    def on_descendant_focus(self, event):
        f = getattr(event.widget, "form_field", None)
        if f is not None:
            self.post_message(self.Focused(self, f))

    def _changed(self, widget):
        f = getattr(widget, "form_field", None)
        if f is not None and not self._loading:
            self.post_message(self.Changed(self, f))

    @on(Input.Changed)
    @on(Checkbox.Changed)
    @on(Select.Changed)
    @on(TextArea.Changed)
    def _any_change(self, event):
        self._changed(event.control if hasattr(event, "control") else event.text_area)


def _scalar(v):
    if v is True:
        return "true"
    if v is False:
        return "false"
    return "" if v is None else str(v)


# ----------------------------------------------------------------- dialogs

class ConfirmScreen(ModalScreen[bool]):
    """Warnings to confirm, optionally by typing a word (e.g. a device name)."""
    BINDINGS = [Binding("escape", "cancel", "Back")]

    def __init__(self, title, messages, typed=None, ok_label="Run", danger=True):
        super().__init__()
        self.title_text, self.messages, self.typed, self.ok_label, self.danger = \
            title, messages, typed, ok_label, danger

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm", classes="dialog"):
            yield Label(Text(self.title_text, "bold"), classes="dialog-title")
            for m in self.messages:
                yield Static(Text(m), classes="confirm-msg")
            if self.typed:
                yield Label(f"Type {self.typed} to confirm:")
                yield Input(id="typed", placeholder=self.typed)
            with ButtonRow(classes="buttons"):
                yield Button(self.ok_label, id="ok", variant="error" if self.danger else "primary",
                             disabled=bool(self.typed))
                yield Button("Back", id="back")

    def on_mount(self):
        self.query_one("#typed" if self.typed else "#back").focus()

    @on(Input.Changed, "#typed")
    def _typed(self, event):
        self.query_one("#ok", Button).disabled = event.value.strip() != self.typed

    @on(Input.Submitted, "#typed")
    def _submitted(self, event):
        if event.value.strip() == self.typed:
            self.dismiss(True)

    @on(Button.Pressed, "#ok")
    def _ok(self):
        self.dismiss(True)

    @on(Button.Pressed, "#back")
    def action_cancel(self):
        self.dismiss(False)


class PromptScreen(ModalScreen[Optional[str]]):
    """Ask for one line of text (a path, a name)."""
    BINDINGS = [Binding("escape", "cancel", "Back")]

    def __init__(self, title, label, value="", ok_label="OK", help="", suggestions=()):
        super().__init__()
        self.title_text, self.label, self.value, self.ok_label, self.help = title, label, value, ok_label, help
        self.suggestions = suggestions

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(Text(self.title_text, "bold"), classes="dialog-title")
            yield Label(self.label)
            yield Input(self.value, id="value", suggester=SuggestFromList(self.suggestions)
                        if self.suggestions else None)
            if self.help:
                yield Static(Text(self.help, "dim"))
            with ButtonRow(classes="buttons"):
                yield Button(self.ok_label, id="ok", variant="primary")
                yield Button("Back", id="back")

    def on_mount(self):
        self.query_one("#value").focus()

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def _ok(self):
        v = self.query_one("#value", Input).value.strip()
        if v:
            self.dismiss(v)

    @on(Button.Pressed, "#back")
    def action_cancel(self):
        self.dismiss(None)


class MessageScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape,enter", "close", "Close")]

    def __init__(self, title, text):
        super().__init__()
        self.title_text, self.text = title, text

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide"):
            yield Label(Text(self.title_text, "bold"), classes="dialog-title")
            with VerticalScroll(classes="message-body"):
                yield Static(self.text if not isinstance(self.text, str) else Text(self.text))
            with ButtonRow(classes="buttons"):
                yield Button("Close", id="close", variant="primary")

    def on_mount(self):
        self.query_one("#close").focus()

    @on(Button.Pressed, "#close")
    def action_close(self):
        self.dismiss(None)


CSS = """
Screen { background: $surface; }
.dialog { width: 80%; max-width: 110; height: auto; max-height: 90%; border: thick $primary;
          background: $panel; padding: 1 2; }
.dialog.wide { width: 95%; max-width: 160; }
ModalScreen { align: center middle; }
.dialog-title { width: 100%; content-align: center middle; margin-bottom: 1; }
.buttons { height: auto; align-horizontal: center; margin-top: 1; }
.buttons Button { margin: 0 1; }
.confirm-msg { margin-bottom: 1; color: $warning; }
.message-body { height: auto; max-height: 30; }
Form { height: auto; max-height: 100%; }
Form .field { height: auto; margin-bottom: 1; }
.field-label { color: $text-muted; }
NavTextArea.short { height: 5; }
NavTextArea.tall { height: 12; }
StatusLine { height: auto; min-height: 1; max-height: 3; background: $boost; color: $text; padding: 0 1; }
Input.-invalid { border: tall $error; }
"""
