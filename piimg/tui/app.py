"""piimg tui: the main screen, the target picker and the parameter dialog.

    piimg tui [TARGET [OVERLAY]]

TARGET is an image, an overlay or a card (/dev/sdX). With OVERLAY, TARGET is
only read: OVERLAY (qcow2) is created on it if missing and every command works
on OVERLAY; `commit` later writes the changes into TARGET, `diff` lists them.
Without OVERLAY, commands change TARGET itself (on a card the TUI asks first).
"""
import os
import sys

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (Button, Checkbox, DataTable, Footer, Header, Input, Label, OptionList, Static)
from textual.widgets.option_list import Option

from ..disks import is_block_device
from ..frontend import commands as C
from ..frontend import target as T
from ..frontend import proc as P
from ..frontend.target import describe_target
from . import run as R
from . import widgets as W
from .recipe_editor import EDITOR_CSS
from .widgets import ButtonRow, ConfirmScreen, Form, StatusLine

# ----------------------------------------------------------------- picker

class OpenScreen(Screen):
    """Pick what to open: a card, an image file, or a path; optionally an overlay."""
    BINDINGS = [Binding("escape", "quit", "Quit"), Binding("ctrl+r", "refresh", "Rescan")]

    def compose(self) -> ComposeResult:
        yield Header()
        with Vertical(id="open"):
            yield Label(Text("Open an SD card or image", "bold"))
            yield DataTable(id="candidates", cursor_type="row", zebra_stripes=True)
            yield Label("Path (image file, .qcow2 overlay or /dev/sdX):")
            yield Input(id="path", placeholder="/dev/sdc or work.qcow2")
            yield Checkbox("Keep the target read-only: write changes to an overlay file", id="use-overlay")
            yield Input(id="overlay", placeholder="changes.qcow2", disabled=True)
            with ButtonRow(classes="buttons"):
                yield Button("Open", id="open-btn", variant="primary")
                yield Button("Quit", id="quit")
        yield StatusLine(id="status")
        yield Footer()

    def on_mount(self):
        self.title = "piimg"
        self.action_refresh()
        self.query_one("#candidates").focus()

    def action_refresh(self):
        dt = self.query_one(DataTable)
        dt.clear(columns=True)
        dt.add_columns("path", "what", "size", "note")
        found, warning = T.candidates()
        if warning:
            self.query_one(StatusLine).say(warning, "yellow")
        self.paths = []
        for c in found:
            note = c.note
            if c.root:
                note += ("; " if note else "") + "needs root (sudo) to open"
            name = c.path if c.kind == "disk" or c.note else c.name
            dt.add_row(name, c.what, c.size, note)
            self.paths.append(c.path)
        if not self.paths:
            self.query_one(StatusLine).say("No card or image found here: type a path below.")

    @on(DataTable.RowHighlighted, "#candidates")
    def _hl(self, event):
        if 0 <= event.cursor_row < len(self.paths):
            p = self.paths[event.cursor_row]
            self.query_one("#path", Input).value = p
            self._suggest_overlay(p)

    def _suggest_overlay(self, p):
        name = T.suggest_overlay(p)
        ov = self.query_one("#overlay", Input)
        if not ov.value or ov.value.endswith("-changes.qcow2"):
            ov.value = name

    @on(DataTable.RowSelected, "#candidates")
    def _sel(self, event):
        self.query_one("#use-overlay").focus()

    @on(Checkbox.Changed, "#use-overlay")
    def _ov(self, event):
        self.query_one("#overlay", Input).disabled = not event.value

    @on(Input.Submitted)
    @on(Button.Pressed, "#open-btn")
    def _open(self):
        path = self.query_one("#path", Input).value.strip()
        overlay = self.query_one("#overlay", Input).value.strip() if self.query_one("#use-overlay", Checkbox).value else None
        if not path:
            self.query_one(StatusLine).say("Choose or type a path first.", "yellow")
            return
        self.app.open_target(path, overlay)

    @on(Button.Pressed, "#quit")
    def action_quit(self):
        self.app.exit()


# ----------------------------------------------------------- parameters

class ParamsScreen(ModalScreen):
    """The form for one command's parameters. Dismisses with (argv, stdin, args) or None."""
    BINDINGS = [Binding("escape", "back", "Back"), Binding("ctrl+r", "run", "Run")]

    def __init__(self, cmd, target, last_values=None):
        super().__init__()
        self.cmd, self.target = cmd, target
        self.fields = C.fields_for(cmd, last_values)
        self.var_fields = []
        self.varform = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog", id="params"):
            yield Label(Text.assemble(("piimg " + self.cmd.name, "bold"), "  ",
                                      (self.cmd.summary(), "dim")), classes="dialog-title")
            plain = [f for f in self.fields if f.type != "vars"]
            yield Form(plain, id="form")
            if any(f.type == "vars" for f in self.fields):
                yield Label(Text("Recipe variables", "bold"))
                yield Vertical(id="vars")
            yield StatusLine(id="status")
            with ButtonRow(classes="buttons"):
                yield Button("Run", id="run", variant="primary")
                yield Button("Back", id="back")

    def on_mount(self):
        form = self.query_one("#form", Form)
        first = next(iter(form.query("Input, Select, Checkbox, TextArea")), None)
        (first or self.query_one("#run")).focus()
        self.query_one(StatusLine).say(self.cmd.help)
        if self.query("#vars"):
            self._refresh_vars()

    @on(Form.Focused)
    def _focused(self, event):
        self.query_one(StatusLine).say(event.field.explain())

    @on(Form.Changed)
    def _changed(self, event):
        if event.field.data.get("kind") == "recipe":
            self._refresh_vars()

    def _refresh_vars(self):
        """For `run`: one field per variable of the chosen recipe."""
        box = self.query_one("#vars")
        box.remove_children()
        self.varform = None
        form = self.query_one("#form", Form)
        name = next((form.value(f.key) for f in form.fields if f.data.get("kind") == "recipe"), "")
        self.var_fields, note = C.recipe_var_fields(name)
        if not self.var_fields:
            box.mount(Static(Text(note, "dim")))
            return
        self.varform = Form(self.var_fields)
        box.mount(self.varform)

    def _values(self):
        values = self.query_one("#form", Form).values()
        if self.var_fields:
            vf = self.varform.values()
            values["var"] = C.var_args(vf)
        return values

    @on(Button.Pressed, "#run")
    def action_run(self):
        values = self._values()
        for f in self.fields:
            if f.required and f.type != "vars" and values.get(f.key) in ("", None, []):
                self.query_one(StatusLine).say(f"{f.label} is required", "bold red")
                try:
                    self.query_one("#form", Form).widget(f.key).focus()
                except KeyError:
                    pass
                return
        for f in self.var_fields:
            if f.required and not self.varform.value(f.key):
                self.query_one(StatusLine).say(f"variable {f.label} is required", "bold red")
                return
        argv, stdin = C.build_argv(self.cmd, values, self.target)
        try:
            args = C.parse(argv)
        except C.ArgError as e:
            self.query_one(StatusLine).say(str(e), "bold red")
            return
        self.app.last_values[self.cmd.name] = values
        self.dismiss((argv, stdin, args))

    @on(Button.Pressed, "#back")
    def action_back(self):
        self.dismiss(None)


# ----------------------------------------------------------------- main

class MainScreen(Screen):
    BINDINGS = [
        Binding("ctrl+left", "pane(-1)", "Pane", show=False, priority=True),
        Binding("ctrl+right", "pane(1)", "Pane", show=False, priority=True),
        Binding("slash", "filter", "Filter"),
        Binding("i", "refresh_info", "Refresh info"),
        Binding("o", "open_other", "Open…"),
        Binding("e", "recipe_editor", "Recipe editor"),
        Binding("q", "app.quit", "Quit"),
    ]

    def __init__(self, cmds):
        super().__init__()
        self.cmds = cmds
        self.shown_cmds = []

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static(id="target")
        with Horizontal(id="main"):
            with Vertical(id="left"):
                yield Input(id="filter", placeholder="filter commands (/)")
                yield OptionList(id="commands")
            with Vertical(id="right"):
                with VerticalScroll(id="details"):
                    yield Static(id="cmd-help")
                yield Label(Text("Target", "bold"))
                yield DataTable(id="info", cursor_type="row", show_header=False)
        yield StatusLine(id="status")
        yield Footer()

    def on_mount(self):
        self._fill("")
        self.show_target()
        self.app.load_info()
        self.query_one("#commands").focus()

    def _fill(self, needle):
        ol = self.query_one("#commands", OptionList)
        ol.clear_options()
        self.shown_cmds = []
        group = None
        has_overlay = bool(self.app.overlay)
        for g, c in C.visible(self.cmds, needle, has_overlay):
            if g != group:
                group = g
                ol.add_option(Option(Text(group, "bold underline"), disabled=True))
                self.shown_cmds.append(None)
            ol.add_option(Option(Text.assemble(("  " + c.name, "bold"),
                                               (" …" if c.has_form() else "", "dim"))))
            self.shown_cmds.append(c)
        first = next((i for i, c in enumerate(self.shown_cmds) if c), None)
        if first is not None:
            ol.highlighted = first

    def show_target(self):
        self.query_one("#target", Static).update(Text(describe_target(self.app.target, self.app.overlay),
                                                      "bold " + ("red" if self.app.direct_card else "green")))

    def show_info(self, rows, error=None):
        dt = self.query_one("#info", DataTable)
        dt.clear(columns=True)
        dt.add_columns("item", "value")
        if error:
            dt.add_row("error", Text(error, "red"))
        for r in rows:
            dt.add_row(Text(r[0], "bold"), r[1])

    @on(Input.Changed, "#filter")
    def _filter(self, event):
        self._fill(event.value)

    @on(Input.Submitted, "#filter")
    def _filter_done(self):
        self.query_one("#commands").focus()

    def action_filter(self):
        self.query_one("#filter").focus()

    @on(OptionList.OptionHighlighted, "#commands")
    def _hl(self, event):
        c = self.shown_cmds[event.option_index] if event.option_index < len(self.shown_cmds) else None
        if c is None:
            return
        help_ = C.command_help(c)
        self.query_one("#cmd-help", Static).update(help_)
        self.query_one(StatusLine).say(f"{c.name}: {c.summary()} — Enter to "
                                       + ("set options and run" if c.has_form() else "run"))

    @on(OptionList.OptionSelected, "#commands")
    def _selected(self, event):
        c = self.shown_cmds[event.option_index]
        if c is not None:
            self.app.start_command(c)

    def action_pane(self, step):
        panes = [self.query_one("#commands"), self.query_one("#details"), self.query_one("#info")]
        focused = self.focused
        idx = next((i for i, p in enumerate(panes) if focused is p or (focused and p in focused.ancestors)), 0)
        panes[(idx + step) % len(panes)].focus()

    def action_refresh_info(self):
        self.app.load_info()

    def action_open_other(self):
        self.app.push_screen(OpenScreen())

    def action_recipe_editor(self):
        self.app.open_recipe_editor()


class PiImgApp(App):
    TITLE = "piimg"
    CSS = W.CSS + R.CSS + EDITOR_CSS + """
#target { height: 1; padding: 0 1; }
#main { height: 1fr; }
#left { width: 34; border-right: solid $primary-darken-2; }
#commands { height: 1fr; border: none; }
#filter { margin: 0 0 0 0; }
#right { padding: 0 1; }
#details { height: 1fr; }
#info { height: 12; }
#open { padding: 1 2; height: 1fr; }
#candidates { height: 1fr; max-height: 20; margin-bottom: 1; }
#params { max-height: 95%; }
#vars { height: auto; }
"""
    BINDINGS = [Binding("ctrl+q", "quit", "Quit", show=False)]

    def __init__(self, target=None, overlay=None):
        super().__init__()
        self.target, self.overlay = target, overlay
        self.cmds = C.load_commands()
        self.last_values = {}
        self.main = None

    @property
    def work_path(self):
        """The path commands operate on."""
        return self.overlay or self.target

    @property
    def direct_card(self):
        return self.target is not None and not self.overlay and is_block_device(self.target)

    def on_mount(self):
        if self.target:
            self.call_after_refresh(self.open_target, self.target, self.overlay)
        else:
            self.push_screen(OpenScreen())

    def open_target(self, path, overlay):
        from ..errors import PiImgError
        from ..image import ensure_overlay
        path = os.path.abspath(os.path.expanduser(path)) if not path.startswith("/dev/") else path
        if not os.path.exists(path):
            self.notify(f"No such file or device: {path}", severity="error")
            return
        if is_block_device(path) and not os.access(path, os.R_OK | (0 if overlay else os.W_OK)):
            self.notify(f"No permission to {'read' if overlay else 'write'} {path}: restart with sudo "
                        f"({' '.join(['sudo', sys.executable, '-m', 'piimg', 'tui', path] + ([overlay] if overlay else []))})",
                        severity="error", timeout=15)
            return
        if overlay:
            try:
                overlay = str(ensure_overlay(path, overlay))
            except PiImgError as e:
                self.notify(str(e), severity="error", timeout=10)
                return
        self.target, self.overlay = path, overlay
        self.sub_title = os.path.basename(path)
        if self.main is None:
            while isinstance(self.screen, OpenScreen):
                self.pop_screen()
            self.main = MainScreen(self.cmds)
            self.push_screen(self.main)  # its on_mount shows the target and loads the info
            return
        while self.screen is not self.main:
            self.pop_screen()
        self.main._fill("")
        self.main.show_target()
        self.load_info()

    @work(exclusive=True, group="info")
    async def load_info(self):
        if self.main is None:
            return
        self.main.show_info([("", "reading…")])
        try:
            rc, out, err = await P.run_capture(["info", self.work_path], timeout=180)
        except Exception as e:  # noqa: BLE001 - shown to the user
            self.main.show_info([], f"{e}")
            return
        table = P.first_table(out)
        if rc or table is None:
            self.main.show_info([], (err[-1] if err else f"exit status {rc}").removeprefix("piimg: "))
        else:
            self.main.show_info(table["rows"])

    # ---- running a command

    def start_command(self, cmd):
        target = self.work_path if cmd.meta.target else None
        if cmd.has_form():
            self.push_screen(ParamsScreen(cmd, target, self.last_values.get(cmd.name)),
                             lambda r: r and self._confirm_and_run(cmd, *r))
        else:
            argv, stdin = C.build_argv(cmd, {}, target)
            try:
                args = C.parse(argv)
            except C.ArgError as e:
                self.notify(str(e), severity="error")
                return
            self._confirm_and_run(cmd, argv, stdin, args)

    def _confirm_and_run(self, cmd, argv, stdin, args):
        writes = cmd.meta.writes_for(args)
        try:
            conf = C.confirmation(cmd, argv, args, self.target if self.direct_card else None)
        except C.ArgError as e:  # e.g. no such device
            self.notify(str(e), severity="error")
            return
        if conf.needed:
            self.push_screen(ConfirmScreen(f"piimg {cmd.name}", conf.messages, conf.typed),
                             lambda ok: ok and self._run(cmd, conf.argv, stdin, writes))
        else:
            self._run(cmd, conf.argv, stdin, writes)

    def _run(self, cmd, argv, stdin, writes):
        # the target summary may still be loading, with the image open read-only: stop it,
        # or a command that writes would find the image locked
        self.workers.cancel_group(self, "info")
        if cmd.meta.output == "terminal":
            import subprocess
            with self.suspend():
                print(f"$ piimg {' '.join(argv)}\n", flush=True)
                rc = subprocess.run([sys.executable, "-m", "piimg", *argv]).returncode
                input(f"\n[exit status {rc}] Press Enter to return to piimg ")
            if writes:
                self.load_info()
            return
        def done(rc):
            if writes and rc == 0:
                self.load_info()
        self.push_screen(R.RunScreen(f"piimg {cmd.name}", argv, stdin, cmd.meta.output, cmd.meta.abort), done)

    def open_recipe_editor(self, path=None):
        from .recipe_editor import RecipeEditorScreen, RecipePickScreen
        if path:
            self.push_screen(RecipeEditorScreen(path, image=self.work_path))
            return

        def picked(choice):
            if choice:
                path, template = choice
                self.push_screen(RecipeEditorScreen(path, image=self.work_path, template=template))
        self.push_screen(RecipePickScreen(), picked)
