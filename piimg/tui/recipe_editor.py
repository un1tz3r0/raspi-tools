"""The recipe editor: a recipe as a list of steps (left) and the selected step's
arguments (right).

    piimg recipe-edit [FILE | BUNDLED-NAME] [--image IMG]     (or `e` in piimg tui)

A bundled recipe's name opens it as a template: an untitled copy you save
somewhere else. Files are read and written with ruamel.yaml in round-trip mode,
so comments and layout of steps you do not touch are kept.

Keys in the step list (see the footer):
  Up/Down, PgUp/PgDn, Home/End   move          Space         mark / unmark a step
  Shift+Up/Down                  mark a range  Esc           clear the marks
  a / Insert                     insert a step after the cursor (a dialog)
  d / Delete                     delete marked steps (or the one under the cursor)
  Alt+Up / Alt+Down              move them     Ctrl+D        duplicate them
  c / x / v                      copy / cut / paste after the cursor
  Ctrl+Z / Ctrl+Y                undo / redo   F2            edit the step as YAML
  Enter                          edit the step's fields (right pane)
  Ctrl+S save, F5 run / dry run, Ctrl+Left/Right list <-> form, Ctrl+Up menu bar
"""
import os
from pathlib import Path

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, Label, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from ..frontend import recipe as M
from ..frontend.recipe import parse_yaml
from . import run as R
from . import widgets as W
from .widgets import ButtonRow, ConfirmScreen, Form, PromptScreen, StatusLine


# ------------------------------------------------------------- widgets

class StepList(OptionList):
    """The left pane. Marks are kept by the editor; this widget only draws."""
    BINDINGS = [
        Binding("space", "toggle_mark", "Mark", show=False),
        Binding("shift+up", "extend(-1)", "Range", show=False),
        Binding("shift+down", "extend(1)", "Range", show=False),
        Binding("escape", "clear_marks", "Unmark", show=False),
    ]

    def action_toggle_mark(self):
        self.screen.toggle_mark()

    def action_extend(self, step):
        self.screen.extend_range(step)

    def action_clear_marks(self):
        self.screen.clear_marks()


class InsertScreen(ModalScreen):
    """Pick the kind of step to insert; help for the highlighted one on the right."""
    BINDINGS = [Binding("escape", "back", "Back"), Binding("ctrl+right", "focus_buttons", show=False, priority=True)]

    def __init__(self):
        super().__init__()
        self.keys = []

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide", id="insert"):
            yield Label(Text("Insert a step", "bold"), classes="dialog-title")
            with Horizontal(id="insert-body"):
                yield OptionList(id="kinds")
                with VerticalScroll(id="kind-help-box"):
                    yield Static(id="kind-help")
            with ButtonRow(classes="buttons"):
                yield Button("Insert", id="insert-btn", variant="primary")
                yield Button("Back", id="back")

    def on_mount(self):
        ol = self.query_one("#kinds", OptionList)
        for cat, acts in M.insert_choices():
            ol.add_option(Option(Text(cat, "bold underline"), disabled=True))
            self.keys.append(None)
            for key, summary in acts:
                ol.add_option(Option(Text.assemble((f"  {key}", "bold magenta" if key == "if" else "bold cyan"),
                                                   (f"  {summary}", "dim"))))
                self.keys.append(key)
        ol.highlighted = 1
        ol.focus()

    @on(OptionList.OptionHighlighted, "#kinds")
    def _hl(self, event):
        key = self.keys[event.option_index]
        self.query_one("#kind-help", Static).update(M.action_help(key) if key else Text())

    @on(OptionList.OptionSelected, "#kinds")
    @on(Button.Pressed, "#insert-btn")
    def _insert(self):
        idx = self.query_one("#kinds", OptionList).highlighted
        if idx is not None and self.keys[idx]:
            self.dismiss(self.keys[idx])

    def action_focus_buttons(self):
        self.query_one("#insert-btn").focus()

    @on(Button.Pressed, "#back")
    def action_back(self):
        self.dismiss(None)


class YamlEditScreen(ModalScreen):
    """Edit one step (or anything) as YAML text. Dismisses with the parsed object or None."""
    BINDINGS = [Binding("escape", "back", "Back"), Binding("ctrl+s", "apply", "Apply", priority=True)]

    def __init__(self, title, text):
        super().__init__()
        self.title_text, self.text = title, text

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog wide", id="yaml-edit"):
            yield Label(Text(self.title_text, "bold"), classes="dialog-title")
            yield TextArea(self.text, id="yaml", show_line_numbers=True, tab_behavior="indent")
            yield StatusLine(id="ystatus")
            with ButtonRow(classes="buttons"):
                yield Button("Apply", id="apply", variant="primary")
                yield Button("Back", id="back")

    def on_mount(self):
        self.query_one("#yaml").focus()
        self.query_one(StatusLine).say("Ctrl+S or Apply to use this; Tab indents; Esc discards")

    @on(Button.Pressed, "#apply")
    def action_apply(self):
        text = self.query_one("#yaml", TextArea).text
        try:
            parse_yaml(text)
        except Exception as e:  # noqa: BLE001 - YAML errors are shown
            self.query_one(StatusLine).say(f"not valid YAML: {e}".replace("\n", " "), "bold red")
            return
        self.dismiss(("ok", text))

    @on(Button.Pressed, "#back")
    def action_back(self):
        self.dismiss(None)


class RecipePickScreen(ModalScreen):
    """New recipe, open a file, or start from a bundled recipe (a template)."""
    BINDINGS = [Binding("escape", "back", "Back")]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(Text("Recipe editor", "bold"), classes="dialog-title")
            yield OptionList(id="choices")
            yield StatusLine(id="pstatus")
            with ButtonRow(classes="buttons"):
                yield Button("Open", id="open", variant="primary")
                yield Button("Back", id="back")

    def on_mount(self):
        ol = self.query_one("#choices", OptionList)
        self.choices = [("new", None), ("open", None)]
        ol.add_option(Option(Text("New empty recipe…", "bold")))
        ol.add_option(Option(Text("Open a recipe file…", "bold")))
        for f, title in M.bundled_templates():
            ol.add_option(Option(Text.assemble(("Template: ", "dim"), (f.stem, "bold cyan"), f"  {title}")))
            self.choices.append(("template", f))
        ol.highlighted = 0
        ol.focus()
        self.query_one(StatusLine).say("A template is a bundled recipe: you edit a copy and save it elsewhere.")

    @on(OptionList.OptionSelected)
    @on(Button.Pressed, "#open")
    def _go(self):
        kind, f = self.choices[self.query_one("#choices", OptionList).highlighted or 0]
        if kind == "template":
            self.dismiss((None, f))
            return

        def got(path):
            if path:
                self.dismiss((os.path.abspath(os.path.expanduser(path)), None))
        if kind == "new":
            self.app.push_screen(PromptScreen("New recipe", "Save it as:", "my-recipe.yaml",
                                              help="Created when you first save."), got)
        else:
            files = [str(p) for p in Path.cwd().glob("**/*.y*ml") if ".venv" not in p.parts][:200]
            self.app.push_screen(PromptScreen("Open recipe", "Recipe file:", "", suggestions=files), got)

    @on(Button.Pressed, "#back")
    def action_back(self):
        self.dismiss(None)


class RunRecipeScreen(ModalScreen):
    """Options for running the edited recipe; dismisses with (image, scope, dry, vars) or None."""
    BINDINGS = [Binding("escape", "back", "Back"), Binding("ctrl+r", "run", "Run")]

    def __init__(self, image, has_selection, var_spec):
        super().__init__()
        self.fields, self.var_fields = M.run_options(image, has_selection, var_spec)

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(Text("Run recipe", "bold"), classes="dialog-title")
            yield Form(self.fields + self.var_fields, id="rform")
            yield StatusLine(id="rstatus")
            with ButtonRow(classes="buttons"):
                yield Button("Run", id="run", variant="primary")
                yield Button("Back", id="back")

    def on_mount(self):
        self.query_one("#rform").query("Input, Select").first().focus()

    @on(Form.Focused)
    def _f(self, event):
        self.query_one(StatusLine).say(event.field.explain())

    @on(Button.Pressed, "#run")
    def action_run(self):
        v = self.query_one("#rform", Form).values()
        if not v["image"]:
            self.query_one(StatusLine).say("image is required", "bold red")
            return
        vars_ = {k[4:]: val for k, val in v.items() if k.startswith("var:") and val not in ("", None)}
        for f in self.var_fields:
            if f.required and f.key[4:] not in vars_:
                self.query_one(StatusLine).say(f"{f.label} is required", "bold red")
                return
        self.dismiss((v["image"], v["scope"], v["mode"] == "dry run", vars_))

    @on(Button.Pressed, "#back")
    def action_back(self):
        self.dismiss(None)


# ------------------------------------------------------------- the editor

class RecipeEditorScreen(Screen):
    """A view over frontend.recipe.RecipeModel."""
    BINDINGS = [
        Binding("ctrl+left", "pane('list')", "List", show=False, priority=True),
        Binding("ctrl+right", "pane('form')", "Form", show=False, priority=True),
        Binding("ctrl+up", "pane('menu')", "Menu", show=False, priority=True),
        Binding("ctrl+down", "pane('list')", show=False, priority=True),
        Binding("a,insert", "insert", "Insert"),
        Binding("d,delete", "delete", "Delete"),
        Binding("alt+up", "move(-1)", "Move up"),
        Binding("alt+down", "move(1)", "Move down"),
        Binding("ctrl+d", "duplicate", "Duplicate"),
        Binding("c", "copy", "Copy", show=False),
        Binding("x", "cut", "Cut", show=False),
        Binding("v", "paste", "Paste", show=False),
        Binding("ctrl+z", "undo", "Undo", priority=True),
        Binding("ctrl+y", "redo", "Redo", priority=True),
        Binding("f2", "yaml", "YAML"),
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("f5", "run", "Run…"),
        Binding("ctrl+w", "close", "Close", priority=True),
    ]

    def __init__(self, path=None, image=None, template=None):
        super().__init__()
        self.model = M.RecipeModel(path, template)
        self.image = image
        self.range_anchor = None  # row index where a Shift-range started
        self.form_row = None      # the Row the right pane shows
        self.form_gen = 0         # bumped each time the form pane is rebuilt

    # the model's state, for code that reads it from the screen
    @property
    def doc(self):
        return self.model.doc

    @property
    def rows(self):
        return self.model.rows

    @property
    def marks(self):
        return self.model.marks

    @property
    def dirty(self):
        return self.model.dirty

    # ---- layout

    def compose(self) -> ComposeResult:
        yield Header()
        with ButtonRow(id="menubar"):
            yield Button("Save", id="m-save")
            yield Button("Save as…", id="m-saveas")
            yield Button("Insert…", id="m-insert")
            yield Button("Run…", id="m-run")
            yield Button("Check", id="m-check")
            yield Button("YAML", id="m-yaml")
            yield Button("Close", id="m-close")
        with Horizontal(id="editor"):
            yield StepList(id="steps")
            yield VerticalScroll(id="form-pane")
        yield StatusLine(id="status")
        yield Footer()

    def on_mount(self):
        self._update_title()
        self.refresh_list(0)
        self.query_one("#steps").focus()

    def _update_title(self):
        self.app.sub_title = self.model.title()

    def say(self, text, style=""):
        if text:
            self.query_one(StatusLine).say(text, style)

    # ---- list

    def refresh_list(self, keep=None, rebuild=True):
        """Redraw the list from the model (rebuild: re-flatten the document first)."""
        ol = self.query_one("#steps", StepList)
        cur = ol.highlighted if keep is None else keep
        if rebuild:
            self.model.refresh()
        ol.clear_options()
        ol.add_options([Option(self._prompt(r)) for r in self.rows])
        if self.rows:
            ol.highlighted = self.model.clamp(cur)
        self._update_title()
        self._show_form()

    def _prompt(self, r):
        t = Text()
        if r.kind == "step":
            t.append("● " if self.model.is_marked(r) else "  ", "bold yellow")
        t.append_text(self.model.row_text(r))
        if self.model.is_marked(r):
            t.stylize("on #303050")
        return t

    def _redraw_row(self, i):
        ol = self.query_one("#steps", StepList)
        if 0 <= i < len(self.rows):
            self.model.recheck(i)
            ol.replace_option_prompt_at_index(i, self._prompt(self.rows[i]))

    def _redraw_all(self):
        for i in range(len(self.rows)):
            self._redraw_row(i)

    @property
    def cursor(self):
        return self.query_one("#steps", StepList).highlighted or 0

    def _goto(self, i):
        if i is not None:
            self.query_one("#steps", StepList).highlighted = self.model.clamp(i)
            self._show_form()

    @on(OptionList.OptionHighlighted, "#steps")
    def _hl(self, event):
        self._show_form()

    @on(OptionList.OptionSelected, "#steps")
    def _enter(self, event):
        self.action_pane("form")

    # ---- marks

    def toggle_mark(self):
        self.model.toggle_mark(self.cursor)
        self.range_anchor = None
        self._redraw_row(self.cursor)
        self._say_marks()

    def extend_range(self, step):
        if self.range_anchor is None:
            self.range_anchor = self.cursor
        new = self.model.clamp(self.cursor + step)
        self.query_one("#steps", StepList).highlighted = new
        self.model.mark_range(self.range_anchor, new)
        self._redraw_all()
        self._say_marks()

    def clear_marks(self):
        self.model.clear_marks()
        self.range_anchor = None
        self._redraw_all()
        self._say_marks()

    def _say_marks(self):
        n = len(self.marks)
        self.say(f"{n} step(s) marked" if n else "no marks")

    def selection(self):
        return self.model.selection(self.cursor)

    # ---- undo

    def action_undo(self):
        if not self.model.undo():
            self.say("nothing to undo")
            return
        self.form_row = None
        self.refresh_list(rebuild=False)

    def action_redo(self):
        if not self.model.redo():
            self.say("nothing to redo")
            return
        self.form_row = None
        self.refresh_list(rebuild=False)

    # ---- list operations

    def _after(self, result, style=""):
        i, msg = result
        self.form_row = None
        self.refresh_list(i, rebuild=False)
        self.say(msg, style)

    def action_insert(self):
        def chosen(kind):
            if kind:
                self._after(self.model.insert_new(self.cursor, kind))
                self.action_pane("form")
        self.app.push_screen(InsertScreen(), chosen)

    def action_delete(self):
        self._after(self.model.delete(self.cursor))

    def action_move(self, step):
        i, msg = self.model.move(self.cursor, step)
        if msg:
            self.say(msg, "yellow")
            return
        self._after((i, None))

    def action_duplicate(self):
        self._after(self.model.duplicate(self.cursor))

    def action_copy(self):
        self.say(self.model.copy(self.cursor)[1])

    def action_cut(self):
        self._after(self.model.cut(self.cursor))

    def action_paste(self):
        self._after(self.model.paste(self.cursor))

    # ---- the form pane

    def _show_form(self):
        if not self.rows:
            return
        r = self.rows[self.cursor]
        if self.form_row is not None and r.kind == self.form_row.kind and r.step is self.form_row.step \
                and r.branch == self.form_row.branch:
            return
        self.form_row = r
        self.model.end_edit()
        pane = self.query_one("#form-pane")
        pane.remove_children()
        p = self.model.pane(r)
        widgets = [Static(p.heading)]
        if p.fields:
            widgets.append(Form(p.fields, classes="pane-form"))
        if p.note:
            widgets.append(Static(p.note))
        self.form_gen += 1
        pane.mount(Vertical(*widgets, classes=f"form-gen form-gen-{self.form_gen}"))
        if r.issues:
            self.say("⚠ " + "; ".join(r.issues), "yellow")
        elif r.kind == "step":
            self.say(f"{r.where} — Enter edits, a inserts after, F2 edits as YAML")

    @on(Form.Focused)
    def _field_focus(self, event):
        self.say(event.field.explain())

    @on(Form.Changed)
    def _field_changed(self, event):
        r = self.form_row
        if r is None:
            return
        form, f = event.form, event.field
        try:
            self.model.apply_field(r, f.key, form.values())
        except ValueError as e:
            self.say(str(e), "bold red")
            return
        self._update_title()
        i = self.cursor
        if r.kind == "meta":
            self._redraw_all()
        else:
            self._redraw_row(i)
            self._redraw_row(0)
        r = self.rows[i]
        self.say(("⚠ " + "; ".join(r.issues)) if r.issues else f.explain(), "yellow" if r.issues else "")

    # ---- panes, YAML, check

    def action_pane(self, which):
        if which == "list":
            self.query_one("#steps").focus()
        elif which == "menu":
            self.query_one("#m-save").focus()
        else:
            self._focus_form(5)

    def _focus_form(self, tries):
        """Focus the first field of the form pane; the form may still be mounting."""
        gen = f".form-gen-{self.form_gen}"
        w = next(iter(self.query_one("#form-pane").query(f"{gen} Input, {gen} Select, {gen} Checkbox, {gen} TextArea")), None)
        if w is not None:
            w.focus()
        elif tries > 0:
            self.set_timer(0.05, lambda: self._focus_form(tries - 1))
        else:
            self.say("nothing to edit here")

    def action_yaml(self):
        r, title, text = self.model.yaml_target(self.cursor)

        def done(result):
            if not result:
                return
            try:
                i = self.model.replace_yaml(r, result[1])
            except ValueError as e:
                self.say(str(e), "bold red")
                return
            self.form_row = None
            self.refresh_list(i, rebuild=False)
        self.app.push_screen(YamlEditScreen(title, text), done)

    def action_check(self):
        problems = self.model.problems()
        if not problems:
            self.say("✔ no problems found (conditions and files in the image are only "
                     "checked when it runs: try a dry run, F5)", "green")
            return
        i, where, issue = problems[0]
        self._goto(i)
        self.say(f"{len(problems)} problem(s); first: {where}: {issue}", "yellow")

    # ---- files

    def action_save(self, then=None, path=None):
        if self.model.path is None and path is None:
            self.action_save_as(then)
            return
        try:
            note = self.model.save(path)
        except OSError as e:
            self.say(f"cannot save: {e}", "bold red")
            return
        self._update_title()
        self.say(note or f"saved {self.model.path}", "yellow" if note else "green")
        if then:
            then()

    def action_save_as(self, then=None):
        def got(p):
            if p:
                self.action_save(then, path=p)
        self.app.push_screen(PromptScreen("Save recipe as", "File:", self.model.default_save_path()), got)

    def action_close(self):
        if not self.dirty:
            self._leave()
            return
        self.app.push_screen(ConfirmScreen("Unsaved changes", ["Close without saving?"], ok_label="Discard"),
                             lambda ok: ok and self._leave())

    def _leave(self):
        self.app.sub_title = ""
        if len(self.app.screen_stack) > 2:
            self.app.pop_screen()
        else:
            self.app.exit()

    # ---- running

    def action_run(self):
        def chosen(res):
            if res:
                self._run(*res)
        self.app.push_screen(RunRecipeScreen(self.image, bool(self.marks), self.doc.get("vars")), chosen)

    def _run(self, image, scope, dry, vars_):
        self.image = image
        selected = scope == "selected"
        tmp = self.model.run_file(self.cursor, selected)
        if tmp is None:
            self.say("no step selected", "yellow")
            return
        errors = []
        screen = R.RunScreen(f"{'Dry run' if dry else 'Run'}: {self.doc.get('name') or 'recipe'}"
                             + (" (selected steps)" if selected else ""),
                             self.model.run_argv(image, tmp, dry, vars_), abort="warn")
        orig_err = screen._err

        def err(line):
            if line.startswith("piimg: "):
                errors.append(line)
            orig_err(line)
        screen._err = err

        def done(rc):
            try:
                os.unlink(tmp)
            except OSError:
                pass
            i = self.model.run_finished(rc, errors, selected)
            if i is not None:
                self.query_one("#steps", StepList).highlighted = i
                self._show_form()  # now, so it doesn't overwrite the error below later
            self._redraw_all()
            if rc == 0:
                self.say("dry run finished without errors" if dry else "recipe applied", "green")
            else:
                self.say(errors[-1].removeprefix("piimg: ") if errors else f"failed (exit status {rc})", "bold red")
        self.app.push_screen(screen, done)

    # ---- menu bar

    @on(Button.Pressed, "#menubar Button")
    def _menu(self, event):
        {"m-save": self.action_save, "m-saveas": self.action_save_as, "m-insert": self.action_insert,
         "m-run": self.action_run, "m-check": self.action_check, "m-yaml": self.action_yaml,
         "m-close": self.action_close}[event.button.id]()


EDITOR_CSS = """
#menubar { height: 3; background: $boost; }
#menubar Button { min-width: 8; margin: 0 1 0 0; height: 3; }
#editor { height: 1fr; }
.form-gen { height: auto; }
#steps { width: 55%; height: 1fr; border: round $primary-darken-2; }
#steps:focus-within { border: round $accent; }
#form-pane { width: 45%; border: round $primary-darken-2; padding: 0 1; }
#form-pane:focus-within { border: round $accent; }
#insert { height: 85%; }
#insert-body { height: 1fr; }
#kinds { width: 40%; }
#kind-help-box { width: 60%; padding: 0 1; }
#yaml-edit { height: 85%; }
#yaml { height: 1fr; }
"""


class RecipeEditorApp(App):
    TITLE = "piimg recipe editor"
    CSS = W.CSS + R.CSS + EDITOR_CSS

    def __init__(self, path=None, image=None, template=None, pick=False):
        super().__init__()
        self.args = (path, image, template)
        self.pick = pick

    def on_mount(self):
        path, image, template = self.args
        if self.pick:
            def picked(choice):
                if not choice:
                    self.exit()
                    return
                self.push_screen(RecipeEditorScreen(choice[0], image, choice[1]))
            self.push_screen(RecipePickScreen(), picked)
        else:
            self.push_screen(RecipeEditorScreen(path, image, template))
