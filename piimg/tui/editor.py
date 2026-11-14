"""Editors in the TUI: read the target's state, change it, then commit the changes in one go.

The logic is frontend.editors (the GUI draws the same editors); this is one
Textual page for any of them:

    list / review   a table of the items, a mark for each changed one, and
                    below it the highlighted item's details with a form for
                    what can be changed in it (applied as you type)
    text            the whole file in a text box (config.txt)

Ctrl+S commits: the changes become one recipe run with `piimg run` in the
run dialog (F5 previews it with --dry-run); a review page (overlay changes)
runs its own command. Leaving a page with changes asks to commit, discard or
keep editing.
"""
import os
import tempfile

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, ContentSwitcher, DataTable, Footer, Header, Input, Label, Select, Static, TextArea

from ..frontend import commands as C
from ..frontend import editors as E
from ..frontend import proc as P
from ..frontend.fields import FieldDef
from .widgets import ButtonRow, ConfirmScreen, Form, NavSelect, StatusLine

MARKS = {"new": ("+", "bold green"), "removed": ("−", "bold red"), "changed": ("●", "bold yellow")}


def is_editor(c):
    return getattr(c, "is_editor", False)


def editor_help(ed):
    """The main screen's help for an editor."""
    t = Text()
    t.append(f"{ed.title}", "bold")
    t.append("  editor\n", "dim cyan")
    t.append(ed.summary() + "\n\n", "italic")
    t.append(ed.help + "\n\n")
    if ed.layout == "review":
        t.append("Enter opens it. Ctrl+S commits what it shows.", "dim")
    else:
        t.append("Enter opens it: change what you need, then Ctrl+S commits all the changes in one run"
                 + (" (F5 previews them)" if ed.can_preview else "") + ".", "dim")
    if ed.replaces:
        t.append("\nIn place of: " + ", ".join(f"piimg {r}" for r in ed.replaces), "dim")
    return t


def _short(s, n=70):
    s = " ".join(str(s or "").split())
    return s if len(s) <= n else s[:n - 1] + "…"


def _plural(n, word):
    return f"{n} {word}{'s' if n != 1 else ''}"


# ------------------------------------------------------------------ dialogs

class AskDiscardScreen(ModalScreen[str]):
    """The editor has changes: "commit", "discard" or "cancel" (keep editing)."""
    BINDINGS = [Binding("escape", "cancel", "Keep editing")]

    def __init__(self, ed):
        super().__init__()
        self.ed = ed

    def compose(self) -> ComposeResult:
        changes = self.ed.describe_changes()
        shown = changes[:10] + ([f"… and {len(changes) - 10} more"] if len(changes) > 10 else [])
        with Vertical(classes="dialog", id="ask-discard"):
            yield Label(Text(f"Commit the changes to {self.ed.title}?", "bold"), classes="dialog-title")
            yield Static(Text("They are not in the target yet:\n" + "\n".join(f"• {c}" for c in shown)),
                         classes="confirm-msg")
            with ButtonRow(classes="buttons"):
                yield Button("Commit…", id="commit", variant="primary")
                yield Button("Discard", id="discard", variant="error")
                yield Button("Keep editing", id="cancel")

    def on_mount(self):
        self.query_one("#cancel").focus()

    @on(Button.Pressed)
    def _pressed(self, event):
        self.dismiss(event.button.id)

    def action_cancel(self):
        self.dismiss("cancel")


class AddScreen(ModalScreen):
    """The Add form of an editor. try_add(values) returns an error to show, or None when added."""
    BINDINGS = [Binding("escape", "back", "Back")]

    def __init__(self, ed, try_add):
        super().__init__()
        self.ed, self.try_add = ed, try_add
        self.fields = ed.add_fields()

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="add"):
            yield Label(Text(f"{self.ed.add_label}: {self.ed.title}", "bold"), classes="dialog-title")
            yield Form(self.fields, id="add-form")
            yield StatusLine(id="add-status")
            with ButtonRow(classes="buttons"):
                yield Button("Add", id="add-btn", variant="primary")
                yield Button("Back", id="back")

    def on_mount(self):
        first = next(iter(self.query_one(Form).query("Input, Select, Checkbox, TextArea")), None)
        (first or self.query_one("#add-btn")).focus()

    @on(Form.Focused)
    def _focused(self, event):
        self.query_one(StatusLine).say(event.field.explain())

    @on(Input.Submitted)
    @on(Button.Pressed, "#add-btn")
    def action_add(self):
        values = self.query_one(Form).values()
        for f in self.fields:
            if f.required and f.is_empty(values.get(f.key)):
                self.query_one(StatusLine).say(f"{f.label} is required", "bold red")
                return
        error = self.try_add(values)
        if error:
            self.query_one(StatusLine).say(error, "bold red")
            return
        self.dismiss(True)

    @on(Button.Pressed, "#back")
    def action_back(self):
        self.dismiss(False)


# -------------------------------------------------------------------- page

class EditorScreen(Screen):
    """One editor (frontend.editors.Editor) on the target the app has open."""
    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("ctrl+s", "commit", "Commit", priority=True),
        Binding("f5", "preview", "Preview"),
        Binding("space", "cycle", "Change"),
        Binding("a,insert", "add", "Add"),
        Binding("d,delete", "remove", "Remove"),
        Binding("alt+up", "move(-1)", "Move up"),
        Binding("alt+down", "move(1)", "Move down"),
        Binding("u", "revert_item", "Revert"),
        Binding("U", "revert_all", "Revert all", show=False),
        Binding("x", "row_action", "Action"),
        Binding("slash", "filter", "Filter"),
        Binding("f", "next_filter", "Show", show=False),
        Binding("ctrl+r", "reload", "Reload"),
    ]

    def __init__(self, ed):
        super().__init__()
        self.ed = ed
        self.inline = [c for c in ed.columns if c.inline]
        self.form = None
        self.form_key = None
        self.shown = []
        self.state = "loading"   # loading | error | ready

    # ---- layout

    def compose(self) -> ComposeResult:
        ed = self.ed
        yield Header()
        yield Static(id="ed-head")
        if ed.layout != "text":
            with Horizontal(id="ed-bar"):
                yield Input(id="ed-filter", placeholder=f"filter {ed.noun[1]} (/)")
                if len(ed.filters) > 1:
                    yield NavSelect([(label, fid) for fid, label in ed.filters], value=ed.filters[0][0],
                                    allow_blank=False, id="ed-show")
                yield Static(id="ed-count")
        with ContentSwitcher(id="ed-body", initial="ed-msg"):
            yield Static(id="ed-msg")
            if ed.layout == "text":
                yield TextArea(id="ed-text", tab_behavior="focus", soft_wrap=False, show_line_numbers=True)
            else:
                yield DataTable(id="ed-table", cursor_type="row", zebra_stripes=True)
        if ed.layout != "text":
            yield VerticalScroll(id="ed-detail")
        yield StatusLine(id="ed-status")
        yield Footer()

    def on_mount(self):
        self.title = f"piimg — {self.ed.title}"
        self.query_one("#ed-head", Static).update(self._head())
        if self.ed.loaded and self.ed.name not in self.app.stale_editors:
            self.show()
        else:
            self.load()

    def _head(self):
        ed = self.ed
        t = Text()
        t.append(ed.title, "bold")
        if ed.dirty:
            t.append("  ● changed", "bold yellow")
        t.append(f"  {ed.summary()}\n", "dim")
        t.append(ed.help, "dim")
        return t

    def check_action(self, action, parameters):
        ed = self.ed
        listed = ed.layout in ("list", "review") and self.state == "ready"
        editing = ed.layout != "review"
        rules = {
            "commit": self.state == "ready",
            "preview": self.state == "ready" and editing and ed.can_preview and ed.needs_target,
            "cycle": listed and editing and any(c.editable and c.kind in ("choice", "switch") for c in self.inline),
            "add": listed and ed.can_add,
            "remove": listed and editing and ed.can_remove,
            "move": listed and ed.ordered,
            "revert_item": listed and editing,
            "revert_all": self.state == "ready" and editing,
            "row_action": listed and bool(ed.row_actions),
            "filter": listed,
            "next_filter": listed and len(ed.filters) > 1,
        }
        return rules.get(action, True)

    # ---- loading

    def _message(self, text, style=""):
        self.state = "loading" if style == "" else "error"
        self.query_one("#ed-msg", Static).update(Text(text, style))
        self.query_one("#ed-body", ContentSwitcher).current = "ed-msg"
        for d in self.query("#ed-detail"):
            d.remove_children()
        self.refresh_bindings()

    def action_reload(self):
        if self.ed.dirty:
            def answered(ok):
                if ok:
                    self.ed.revert()
                    self.load()
            self.app.push_screen(ConfirmScreen(f"Read {self.ed.title} again?",
                                               ["Your changes are dropped."], ok_label="Read again"), answered)
            return
        self.load()

    @work(exclusive=True, group="query")
    async def load(self):
        ed, app = self.ed, self.app
        target = app.work_path if ed.needs_target else None
        if ed.needs_target and target is None:
            self._message("No card or image is open.", "yellow")
            return
        if ed.needs_overlay and not app.overlay:
            self._message("No overlay is open: open the card or image with an overlay to see what it changes.",
                          "yellow")
            return
        self._message(f"Reading the {ed.noun[1]}…")
        try:
            rc, out, err = await P.run_capture(ed.query_argv(target), timeout=600)
        except Exception as e:  # noqa: BLE001 - shown to the user
            self._message(f"Cannot read the {ed.noun[1]}: {e}\nCtrl+R tries again.", "red")
            return
        if rc:
            msg = P.last_error(err) or (err[-1] if err else f"exit status {rc}")
            self._message(f"Cannot read the {ed.noun[1]}: {msg}\nCtrl+R tries again.", "red")
            return
        try:
            ed.load_output(out)
        except ValueError as e:
            self._message(f"Cannot read the {ed.noun[1]}: {e}\nCtrl+R tries again.", "red")
            return
        app.stale_editors.discard(ed.name)
        self.show()

    def show(self):
        self.state = "ready"
        if self.ed.layout == "text":
            ta = self.query_one("#ed-text", TextArea)
            ta.load_text(self.ed.text)
            self.query_one("#ed-body", ContentSwitcher).current = "ed-text"
            ta.focus()
        else:
            self.query_one("#ed-body", ContentSwitcher).current = "ed-table"
            self.rebuild()
            self.query_one("#ed-table").focus()
        self.changed()

    # ---- the table

    @property
    def needle(self):
        return self.query_one("#ed-filter", Input).value

    @property
    def filter_id(self):
        sel = list(self.query("#ed-show"))
        if not sel or sel[0].value in (None, Select.NULL):
            return self.ed.filters[0][0]
        return sel[0].value

    def _label_header(self):
        first = self.ed.columns[0].label
        return "Name" if any(c.label == first for c in self.inline) else first

    def cells(self, item):
        ed = self.ed
        state = "new" if item.new else "removed" if item.removed else "changed" if item.changed() else None
        mark, mstyle = MARKS.get(state, (" ", ""))
        out = [Text(mark, mstyle), Text(ed.label(item), "strike dim" if item.removed else "bold")]
        for c in self.inline:
            v = item.get(c.key)
            t = Text(ed.show_value(item, c.key, v) if v or c.kind != "text" else "",
                     P.status_style(c.key, v) or ("yellow" if item.changed(c.key) else ""))
            if item.changed(c.key) and not item.new:
                t.append(f"  (was {ed.show_value(item, c.key, item.orig.get(c.key))})", "dim")
            out.append(t)
        out.append(Text(_short(ed.subtitle(item)), "dim"))
        return out

    def rebuild(self, keep=None):
        ed = self.ed
        dt = self.query_one("#ed-table", DataTable)
        keep = keep or self.current_key()
        dt.clear(columns=True)
        dt.add_column(" ", key="mark", width=1)
        dt.add_column(self._label_header(), key="label")
        for c in self.inline:
            dt.add_column(c.label, key=c.key)
        dt.add_column("Details", key="details")
        self.shown = [i for i in ed.items if ed.matches(i, self.needle, self.filter_id)]
        for i in self.shown:
            dt.add_row(*self.cells(i), key=i.key)
        keys = [i.key for i in self.shown]
        if keep in keys:
            dt.move_cursor(row=keys.index(keep))
        self.show_detail()
        self.changed()

    def refresh_row(self, item):
        dt = self.query_one("#ed-table", DataTable)
        if item.key not in dt.rows:
            return
        for col, value in zip(["mark", "label", *[c.key for c in self.inline], "details"], self.cells(item)):
            dt.update_cell(item.key, col, value, update_width=True)

    def current_key(self):
        if self.ed.layout == "text":
            return None
        dt = self.query_one("#ed-table", DataTable)
        if not dt.row_count:
            return None
        try:
            return dt.coordinate_to_cell_key(Coordinate(dt.cursor_row, 0)).row_key.value
        except Exception:  # noqa: BLE001 - no such row any more
            return None

    def current_item(self):
        return self.ed.by_key.get(self.current_key())

    @on(DataTable.RowHighlighted, "#ed-table")
    def _highlighted(self, event):
        self.show_detail()
        item = self.current_item()
        if item is not None:
            self.hint(item)

    @on(DataTable.RowSelected, "#ed-table")
    def _selected(self, event):
        self.action_edit()

    def hint(self, item=None):
        ed = self.ed
        problems = ed.problems() if ed.layout != "review" else []
        status = self.query_one("#ed-status", StatusLine)
        if problems:
            status.say("Cannot commit yet: " + problems[0]
                       + (f" (and {len(problems) - 1} more)" if len(problems) > 1 else ""), "bold red")
            return
        item = item or self.current_item()
        bits = []
        if ed.layout == "text":
            bits.append("edit the text; Ctrl+S commits it")
        elif ed.layout == "review":
            bits.append("Ctrl+S writes all of these into the base")
        elif item is not None:
            if any(c.editable and c.kind in ("choice", "switch") for c in self.inline):
                bits.append("Space changes it")
            if self.form is not None:
                bits.append("Enter edits it")
            if ed.removable(item):
                bits.append(f"d: {ed.remove_label.lower()}" + (" (again: keep)" if item.removed else ""))
        if ed.row_actions and item is not None:
            bits.append(f"x: {ed.row_actions[0][0].lower()}")
        if ed.dirty:
            bits.append("Ctrl+S commits the changes")
        status.say("; ".join(bits))

    # ---- the highlighted item

    def _field(self, item, c):
        ed, v = self.ed, item.get(c.key)
        kind = ed.kind(item, c.key)
        if kind == "choice":
            choices = list(ed.choices(item, c.key))
            if v not in choices:
                choices.insert(0, v)
            return FieldDef(c.key, c.label, "choice", required=True, help=c.help, choices=tuple(choices), value=v)
        if kind == "switch":
            return FieldDef(c.key, c.label, "bool", help=c.help, value=v == "on")
        if kind == "secret":
            return FieldDef(c.key, c.label, "secret", help=c.help, value=v, placeholder="(unchanged)")
        if kind == "lines":
            return FieldDef(c.key, c.label, "lines", help=c.help, value=v, data={"short": True})
        return FieldDef(c.key, c.label, help=c.help, value=v, placeholder=c.placeholder,
                        suggestions=tuple(ed.choices(item, c.key)))

    def show_detail(self):
        boxes = list(self.query("#ed-detail"))
        if not boxes:
            return
        box = boxes[0]
        box.remove_children()
        self.form, self.form_key = None, None
        item = self.current_item()
        if item is None:
            if self.state == "ready":
                box.mount(Static(Text(self.ed.footer or f"No {self.ed.noun[1]} to show.", "dim")))
            return
        ed = self.ed
        info = Text()
        info.append(ed.label(item), "bold")
        if item.new:
            info.append("  new", "green")
        elif item.removed:
            info.append(f"  to {ed.remove_label.lower()}", "red")
        elif item.changed():
            info.append("  changed", "yellow")
        for c in ed.columns[1:]:
            if ed.editable(item, c.key) or c.kind == "secret" or c.key in ed.internal or not item.get(c.key) \
                    or item.get(c.key) == ed.label(item):
                continue
            info.append(f"\n{c.label}: ", "dim")
            info.append(item.get(c.key))
        box.mount(Static(info, classes="ed-info"))
        if item.removed:
            box.mount(Static(Text(f"Marked to {ed.remove_label.lower()} when you commit; d keeps it.", "red")))
            return
        if ed.layout == "review":
            return
        fields = [self._field(item, c) for c in ed.columns if ed.editable(item, c.key)]
        if fields:
            self.form, self.form_key = Form(fields, classes="ed-form"), item.key
            box.mount(self.form)

    def action_edit(self):
        if self.form is None:
            return
        first = next(iter(self.form.query("Input, Select, Checkbox, TextArea")), None)
        if first is not None:
            first.focus()

    @on(Form.Focused)
    def _form_focused(self, event):
        if event.form is self.form:
            self.query_one("#ed-status", StatusLine).say(event.field.explain() + " — Esc returns to the list")

    @on(Form.Changed)
    def _form_changed(self, event):
        if event.form is not self.form or self.form_key not in self.ed.by_key:
            return
        f = event.field
        value = event.form.value(f.key)
        if f.type == "bool":
            value = "on" if value else "off"
        elif f.type == "lines":
            value = "\n".join(value)
        item = self.ed.by_key[self.form_key]
        try:
            self.ed.set(item.key, f.key, value)
        except (KeyError, ValueError) as e:
            self.query_one("#ed-status", StatusLine).say(str(e), "bold red")
            return
        self.refresh_row(item)
        self.changed(item)

    @on(TextArea.Changed, "#ed-text")
    def _text_changed(self, event):
        self.ed.text = event.text_area.text
        self.changed()

    def changed(self, item=None):
        """After any change: the counts, the heading's mark, the problems, the keys."""
        ed = self.ed
        self.query_one("#ed-head", Static).update(self._head())
        for c in self.query("#ed-count"):
            n = len(self.shown)
            total = len([i for i in ed.items if not i.new])
            c.update(Text(ed.count_text() + (f"; {n} shown" if n != len(ed.items) else ""), "dim")
                     if total or ed.items else Text(""))
        if ed.layout == "text":
            self.sub_title = ed.count_text()
        self.hint(item)
        self.refresh_bindings()

    # ---- changing

    def action_cycle(self):
        item = self.current_item()
        col = next((c for c in self.inline if c.editable and c.kind in ("choice", "switch")), None)
        if item is None or col is None or item.removed or not self.ed.editable(item, col.key):
            return
        if self.ed.kind(item, col.key) == "switch":
            value = "off" if item.get(col.key) == "on" else "on"
        else:
            choices = list(self.ed.choices(item, col.key))
            if not choices:
                return
            cur = item.get(col.key)
            value = choices[(choices.index(cur) + 1) % len(choices)] if cur in choices else choices[0]
        self.ed.set(item.key, col.key, value)
        self.refresh_row(item)
        self.show_detail()
        self.changed(item)

    def action_add(self):
        if not self.ed.can_add:
            return
        after = self.current_key()
        added = {}

        def try_add(values):
            try:
                added["item"] = self.ed.add(values, after=after)
            except ValueError as e:
                return str(e)
            return None

        def done(ok):
            if ok and "item" in added:
                self.rebuild(keep=added["item"].key)
        self.app.push_screen(AddScreen(self.ed, try_add), done)

    def action_remove(self):
        item = self.current_item()
        if item is None:
            return
        try:
            kept = self.ed.remove(item.key)
        except ValueError as e:
            self.notify(str(e), severity="warning")
            return
        if kept is None:   # a new item just goes
            self.rebuild()
            return
        self.refresh_row(item)
        self.show_detail()
        self.changed(item)

    def action_move(self, delta):
        key = self.current_key()
        if key is not None and self.ed.move(key, delta):
            self.rebuild(keep=key)

    def action_revert_item(self):
        item = self.current_item()
        if item is None or not item.changed():
            return
        self.ed.revert(item.key)
        if item.new:
            self.rebuild()
            return
        self.refresh_row(item)
        self.show_detail()
        self.changed(item)

    def action_revert_all(self):
        if not self.ed.dirty:
            return
        self.ed.revert()
        if self.ed.layout == "text":
            self.query_one("#ed-text", TextArea).load_text(self.ed.text)
            self.changed()
        else:
            self.rebuild()

    def action_row_action(self):
        item = self.current_item()
        if item is None or not self.ed.row_actions:
            return
        label, _icon, make = self.ed.row_actions[0]
        self.app.run_argv(make(self.app.work_path, item), f"{label}: {self.ed.label(item)}")

    # ---- filtering

    def action_filter(self):
        for f in self.query("#ed-filter"):
            f.focus()

    @on(Input.Changed, "#ed-filter")
    def _filter(self, event):
        if self.state == "ready":
            self.rebuild()

    @on(Input.Submitted, "#ed-filter")
    def _filter_done(self):
        self.query_one("#ed-table").focus()

    @on(Select.Changed, "#ed-show")
    def _show(self, event):
        if self.state == "ready":
            self.rebuild()

    def action_next_filter(self):
        sel = self.query_one("#ed-show", Select)
        ids = [fid for fid, _ in self.ed.filters]
        cur = self.filter_id
        sel.value = ids[(ids.index(cur) + 1) % len(ids)] if cur in ids else ids[0]

    # ---- committing

    def action_preview(self):
        self.commit(dry=True)

    def action_commit(self):
        self.commit()

    def commit(self, dry=False, then=None):
        """Make the changes (dry: a preview); then() after a commit that worked."""
        ed, app = self.ed, self.app
        target = app.work_path
        if self.state != "ready" or (ed.needs_target and target is None):
            return
        if dry and not ed.can_preview:
            return
        if ed.layout != "review" and not ed.dirty:
            self.notify("Nothing to commit: change something first.")
            return
        if ed.layout == "review" and not ed.items:
            self.notify("The overlay changes nothing.")
            return
        problems = ed.problems()
        if problems and not dry:
            self.notify(problems[0], severity="error")
            return
        try:
            argv, text = ed.commit(target, dry)
        except Exception as e:  # noqa: BLE001 - hashing a password, ...: say why
            self.notify(f"Cannot commit: {e}", severity="error")
            return
        cmd = app.command_for(argv)
        try:
            args = C.parse([a if a != E.RECIPE else os.path.join(tempfile.gettempdir(), "x.yaml") for a in argv])
            conf = C.confirmation(cmd, argv, args, app.target if app.direct_card and not dry else None)
        except C.ArgError as e:
            self.notify(str(e), severity="error")
            return

        def start():
            path = ed.write_recipe(text, ed.name) if text is not None else None
            run_argv = [path if a == E.RECIPE else a for a in conf.argv]

            def done(rc):
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
                if dry or rc != 0:
                    return   # on failure the changes stay, to be fixed or tried again
                self.notify(f"{ed.title}: committed")
                if ed.layout != "review":
                    ed.revert()
                app.editors_stale()
                if ed.needs_target:
                    app.load_info()
                if then is not None:
                    then()
                else:
                    self.load()
            app.run_argv(run_argv, f"{'Preview' if dry else 'Commit'}: {ed.title}", done, cmd=cmd)

        if dry:
            start()
            return
        if ed.layout == "review":
            messages = conf.messages or [f"Write the overlay's {_plural(len(ed.items), 'changed file')} into "
                                         f"{app.target}? The overlay is empty afterwards."]
            title = f"Commit the overlay to {os.path.basename(app.target or '')}?"
        else:
            changes = ed.describe_changes()
            shown = changes[:15] + ([f"… and {len(changes) - 15} more"] if len(changes) > 15 else [])
            where = (os.path.basename(app.overlay) if app.overlay else app.target) if ed.needs_target \
                else ed.title.lower()
            messages = [f"{_plural(len(changes), 'change')} to {where}:\n" + "\n".join(f"• {c}" for c in shown),
                        *conf.messages]
            title = f"Commit changes to {ed.title}?"
        danger = app.direct_card or ed.layout == "review" or any(i.removed for i in ed.items)
        self.app.push_screen(ConfirmScreen(title, messages, conf.typed, ok_label="Commit", danger=danger),
                             lambda ok: ok and start())

    # ---- leaving

    def action_back(self):
        focused = self.focused
        table = list(self.query("#ed-table"))
        if table and focused is not None and focused is not table[0]:
            table[0].focus()   # from the form or the filter: back to the list first
            return
        self.leave()

    def leave(self, then=None):
        """Back to the command list (then: instead, e.g. quit); asks first if there are changes."""
        app = self.app

        def go():
            if then is not None:
                then()
            else:
                app.pop_screen()
        if not self.ed.dirty:
            go()
            return

        def answered(choice):
            if choice == "discard":
                self.ed.revert()
                go()
            elif choice == "commit":
                self.commit(then=go)
        app.push_screen(AskDiscardScreen(self.ed), answered)


CSS = """
#ed-head { height: auto; max-height: 6; padding: 0 1; }
#ed-bar { height: 3; }
#ed-filter { width: 1fr; }
#ed-show { width: 30; }
#ed-count { width: auto; min-width: 20; padding: 1 1 0 1; }
#ed-body { height: 1fr; }
#ed-body > * { height: 1fr; }
#ed-msg { padding: 1 2; }
#ed-detail { height: auto; max-height: 45%; border-top: solid $primary-darken-2; padding: 0 1; }
#ed-detail .ed-info { margin-bottom: 1; }
#ask-discard, #add { max-height: 90%; }
"""
