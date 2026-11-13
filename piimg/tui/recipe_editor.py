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
import copy
import io
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from rich.text import Text
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq
from ruamel.yaml.scalarstring import LiteralScalarString
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, Footer, Header, Label, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from .. import recipe_schema as RS
from ..script import VAR_RE, bundled_recipes_dir
from . import run as R
from . import widgets as W
from .widgets import ButtonRow, ConfirmScreen, FieldDef, Form, PromptScreen, StatusLine

COMMON = ("name", "when", "ignore_errors")
TYPE_MAP = {"str": "str", "text": "text", "int": "int", "bool": "bool", "mode": "str", "choice": "choice",
            "list": "lines", "map": "map", "yaml": "yaml"}


# ------------------------------------------------------------- YAML I/O

def _yaml():
    y = YAML(typ="rt")
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    y.width = 4096
    return y


def load_doc(path):
    with open(path) as f:
        doc = _yaml().load(f)
    if doc is None:
        doc = CommentedMap()
    if not isinstance(doc, dict):
        raise ValueError(f"{path}: a recipe is a mapping with name, vars and steps")
    doc.setdefault("steps", CommentedSeq())
    if doc["steps"] is None:
        doc["steps"] = CommentedSeq()
    return doc


def new_doc(name):
    doc = CommentedMap()
    doc["name"] = name
    doc["vars"] = CommentedMap()
    doc["steps"] = CommentedSeq()
    return doc


def dump(obj):
    buf = io.StringIO()
    _yaml().dump(obj, buf)
    text = buf.getvalue()
    return text[:-5] if text.endswith("\n...\n") else text


def parse_yaml(text):
    return _yaml().load(text) if text.strip() else None


def _scalar_from_text(v):
    """'true' -> True, '12' -> 12, 'x' -> 'x' (map values typed into a form)."""
    try:
        val = parse_yaml(v)
    except Exception:  # noqa: BLE001
        return v
    return val if isinstance(val, (bool, int, float)) or val is None else v


# ------------------------------------------------------------- rows

@dataclass
class Row:
    kind: str                 # meta | step | branch
    parent: object = None     # list holding the step / the if-step owning the branch
    index: int = -1
    depth: int = 0
    step: object = None
    branch: str = ""
    where: str = ""           # script.Runner's name for it: steps[2].then[1]
    ancestors: tuple = ()     # ids of enclosing if-steps
    issues: list = field(default_factory=list)


def flatten(doc):
    rows = [Row("meta")]

    def walk(lst, depth, where, ancestors):
        for i, step in enumerate(lst):
            w = f"{where}[{i + 1}]"
            rows.append(Row("step", lst, i, depth, step, "", w, ancestors))
            if RS.step_action(step) == "if":
                for br in ("then", "else"):
                    rows.append(Row("branch", step, -1, depth + 1, step, br, f"{w}.{br}", ancestors + (id(step),)))
                    walk(step.get(br) or [], depth + 2, f"{w}.{br}", ancestors + (id(step),))
    walk(doc["steps"], 0, "steps", ())
    return rows


def _short(v, n=60):
    s = v if isinstance(v, str) else (dump(v).strip().replace("\n", " ") if v is not None else "")
    s = " ".join(s.split())
    return s if len(s) <= n else s[:n - 1] + "…"


def cond_summary(c):
    if isinstance(c, dict) and len(c) == 1:
        (k, v), = c.items()
        if k == "exists":
            return f"exists {v}"
        if k == "grep" and isinstance(v, dict):
            return f"{v.get('path')} ~ /{v.get('pattern')}/"
        if k == "var" and isinstance(v, dict):
            rest = {x: y for x, y in v.items() if x != "name"}
            return f"{{{{{v.get('name')}}}}} " + " ".join(f"{x} {_short(y, 20)}" for x, y in rest.items())
        if k == "not":
            return "not " + cond_summary(v)
        if k in ("all", "any") and isinstance(v, list):
            return f"{k}(" + ", ".join(cond_summary(x) for x in v) + ")"
        return f"{k} {_short(v, 40)}"
    return _short(c, 50)


def step_summary(step):
    act = RS.step_action(step)
    if act is None:
        return Text("invalid step", "red")
    if act == "if":
        return Text.assemble(("if ", "bold magenta"), cond_summary(step.get("when")))
    arg = step[act]
    if isinstance(arg, dict):
        a = RS.ACTIONS.get(act)
        keys = [f.name for f in a.fields] if a else list(arg)
        first = next((k for k in keys if k in arg and k not in ("content", "script")), None)
        desc = _short(arg[first], 50) if first else ""
        extra = [k for k in arg if k != first]
        if act in ("config_txt", "cmdline", "systemd"):
            desc = "; ".join(f"{k} {_short(v, 30)}" for k, v in arg.items())
            extra = []
        detail = desc + (f"  (+{', '.join(extra)})" if extra and len(desc) < 40 else "")
    else:
        detail = _short(arg, 60)
    t = Text.assemble((act, "bold cyan" if act in RS.ACTIONS else "bold red"), " ", detail)
    return t


def used_vars(obj, skip_literal=True):
    """{{names}} used in the strings of obj (write content / live script only with template: true)."""
    out = set()

    def walk(o, ctx=None):
        if isinstance(o, str):
            out.update(m.group(1) for m in VAR_RE.finditer(o))
        elif isinstance(o, dict):
            for k, v in o.items():
                if skip_literal and k in ("content", "script") and not o.get("template"):
                    continue
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(obj)
    return out


def check_row(row, declared):
    issues = []
    if row.kind != "step":
        return issues
    step = row.step
    act = RS.step_action(step)
    if act is None:
        return ["a step is a mapping with exactly one action (plus name / when / ignore_errors)"]
    if act == "if":
        if step.get("when") is None:
            issues.append("then/else need a when: condition")
    elif act not in RS.ACTIONS:
        issues.append(f"unknown action {act!r}")
    else:
        a = RS.ACTIONS[act]
        arg = step[act]
        if isinstance(arg, dict):
            for f in a.fields:
                if f.required and arg.get(f.name) in (None, "", [], {}):
                    if not (act == "write" and f.name == "content"):
                        issues.append(f"{f.name} is required")
            if act == "write" and not (arg.get("content") is not None or arg.get("content_file")):
                issues.append("write needs content or content_file")
            if act == "live" and not (arg.get("script") or arg.get("script_file")):
                issues.append("live needs script or script_file")
        elif arg in (None, "", []) and (a.scalar or a.listform):
            issues.append(f"{a.scalar or a.listform} is empty")
    shallow = {k: v for k, v in step.items() if k not in ("then", "else")}
    missing = sorted(used_vars(shallow) - set(declared) - {"boot"})
    if missing:
        issues.append("undeclared variable(s): " + ", ".join(missing))
    return issues


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
        ol.add_option(Option(Text("Control", "bold underline"), disabled=True))
        self.keys.append(None)
        ol.add_option(Option(Text.assemble(("  if", "bold magenta"), ("  when / then / else", "dim"))))
        self.keys.append("if")
        for cat in RS.CATEGORIES:
            acts = [a for a in RS.ACTIONS.values() if a.category == cat]
            if cat != "Control":
                ol.add_option(Option(Text(cat, "bold underline"), disabled=True))
                self.keys.append(None)
            for a in acts:
                ol.add_option(Option(Text.assemble((f"  {a.name}", "bold cyan"), (f"  {a.summary}", "dim"))))
                self.keys.append(a.name)
        ol.highlighted = 1
        ol.focus()

    @on(OptionList.OptionHighlighted, "#kinds")
    def _hl(self, event):
        key = self.keys[event.option_index]
        t = Text()
        if key == "if":
            t.append("if (when / then / else)\n\n", "bold magenta")
            t.append(RS.IF_BLOCK_HELP + "\n\nExample condition:\n  ")
            t.append(RS.CONDITION_EXAMPLE, "green")
        elif key:
            a = RS.ACTIONS[key]
            t.append(f"{a.name}", "bold cyan")
            t.append(f" — {a.summary}\n\n")
            t.append(a.help + "\n\n")
            t.append("Fields\n", "bold")
            for f in a.fields:
                t.append(f"  {f.name}", "bold")
                t.append(f" ({f.type}{', required' if f.required else ''})", "dim")
                t.append(f"  {f.help}\n")
            if a.example:
                t.append("\nExample\n", "bold")
                t.append(f"  - {a.example}\n", "green")
        self.query_one("#kind-help", Static).update(t)

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
        try:
            obj = parse_yaml(self.query_one("#yaml", TextArea).text)
        except Exception as e:  # noqa: BLE001 - YAML errors are shown
            self.query_one(StatusLine).say(f"not valid YAML: {e}".replace("\n", " "), "bold red")
            return
        self.dismiss(("ok", obj))

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
        import yaml
        ol = self.query_one("#choices", OptionList)
        self.choices = [("new", None), ("open", None)]
        ol.add_option(Option(Text("New empty recipe…", "bold")))
        ol.add_option(Option(Text("Open a recipe file…", "bold")))
        for f in sorted(bundled_recipes_dir().glob("*.y*ml")):
            title = (yaml.safe_load(f.read_text()) or {}).get("name", "")
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
        self.fields = [
            FieldDef("image", "image", "str", True, "image, overlay or card to run the recipe on", value=image or "",
                     placeholder="work.qcow2"),
            FieldDef("mode", "mode", "choice", True, "dry run: open the image read-only and show what each "
                     "step would do; apply: make the changes", ("dry run", "apply"), "dry run"),
            FieldDef("scope", "steps", "choice", True, "the whole recipe, or only the marked steps (and the "
                     "one under the cursor if none are marked)", ("all", "selected"),
                     "selected" if has_selection else "all"),
        ]
        self.var_fields = []
        for name, d in (var_spec or {}).items():
            d = d or {}
            self.var_fields.append(FieldDef(f"var:{name}", f"{{{{{name}}}}}", "choice" if d.get("choices") else "str",
                                            bool(d.get("required")), d.get("help", ""),
                                            tuple(str(c) for c in d.get("choices") or ()), d.get("default")))

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
        self.path = Path(path) if path else None
        self.image = image
        self.template = Path(template) if template else None
        if self.template:
            self.doc = load_doc(self.template)
        elif self.path and self.path.exists():
            self.doc = load_doc(self.path)
        else:
            self.doc = new_doc(self.path.stem if self.path else "new recipe")
        self.rows = []
        self.marks = set()        # ids of marked step mappings
        self.range_anchor = None        # row index where a Shift-range started
        self.clipboard = []
        self.undo_stack, self.redo_stack = [], []
        self.edit_key = None      # (id(step), field) being edited, to group undo
        self.dirty = False
        self.form_row = None      # the Row the right pane shows
        self.form_gen = 0         # bumped each time the form pane is rebuilt
        self.error_where = None   # Runner's location of the last run error

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
        name = str(self.path) if self.path else f"untitled (from template {self.template.stem})" \
            if self.template else "untitled"
        self.app.sub_title = ("● " if self.dirty else "") + name

    # ---- list

    @property
    def declared(self):
        v = self.doc.get("vars") or {}
        return list(v) if isinstance(v, dict) else []

    def refresh_list(self, keep=None):
        ol = self.query_one("#steps", StepList)
        cur = ol.highlighted if keep is None else keep
        self.rows = flatten(self.doc)
        declared = self.declared
        for r in self.rows:
            r.issues = check_row(r, declared)
        ol.clear_options()
        ol.add_options([Option(self._prompt(r)) for r in self.rows])
        if self.rows:
            ol.highlighted = max(0, min(cur or 0, len(self.rows) - 1))
        self._show_form()

    def _prompt(self, r):
        t = Text()
        if r.kind == "meta":
            vars_ = self.declared
            t.append("◆ ", "bold yellow")
            t.append(str(self.doc.get("name") or "(no name)"), "bold")
            t.append(f"   {len(vars_)} variable(s)" + (f": {', '.join(vars_)}" if vars_ else ""), "dim")
            return t
        t.append("  " * r.depth)
        if r.kind == "branch":
            t.append(f"{r.branch}:", "italic magenta")
            if not r.step.get(r.branch):
                t.append("  (empty)", "dim")
            return t
        marked = id(r.step) in self.marks
        t.append("● " if marked else "  ", "bold yellow")
        if r.where == self.error_where:
            t.append("✘ ", "bold red")
        elif r.issues:
            t.append("⚠ ", "yellow")
        if isinstance(r.step, dict) and r.step.get("name"):
            t.append(f"{r.step['name']}: ", "italic")
        t.append_text(step_summary(r.step))
        if isinstance(r.step, dict) and r.step.get("when") is not None and RS.step_action(r.step) != "if":
            t.append(f"   when {cond_summary(r.step['when'])}", "dim magenta")
        if isinstance(r.step, dict) and r.step.get("ignore_errors"):
            t.append("  (errors ignored)", "dim")
        if marked:
            t.stylize("on #303050")
        return t

    def _redraw_row(self, i):
        ol = self.query_one("#steps", StepList)
        if 0 <= i < len(self.rows):
            r = self.rows[i]
            r.issues = check_row(r, self.declared)
            ol.replace_option_prompt_at_index(i, self._prompt(r))

    def _redraw_all(self):
        for i in range(len(self.rows)):
            self._redraw_row(i)

    @property
    def cursor(self):
        return self.query_one("#steps", StepList).highlighted or 0

    @on(OptionList.OptionHighlighted, "#steps")
    def _hl(self, event):
        self._show_form()

    @on(OptionList.OptionSelected, "#steps")
    def _enter(self, event):
        self.action_pane("form")

    # ---- marks

    def toggle_mark(self):
        r = self.rows[self.cursor]
        if r.kind != "step":
            return
        self.marks ^= {id(r.step)}
        self.range_anchor = None
        self._redraw_row(self.cursor)
        self._say_marks()

    def extend_range(self, step):
        ol = self.query_one("#steps", StepList)
        if self.range_anchor is None:
            self.range_anchor = self.cursor
        new = max(0, min(len(self.rows) - 1, self.cursor + step))
        ol.highlighted = new
        lo, hi = sorted((self.range_anchor, new))
        self.marks = {id(r.step) for r in self.rows[lo:hi + 1] if r.kind == "step"}
        self._redraw_all()
        self._say_marks()

    def clear_marks(self):
        self.marks.clear()
        self.range_anchor = None
        self._redraw_all()
        self._say_marks()

    def _say_marks(self):
        n = len(self.marks)
        self.query_one(StatusLine).say(f"{n} step(s) marked" if n else "no marks")

    def selection(self):
        """Selected step rows in document order, without those inside another selected one."""
        if self.marks:
            rows = [r for r in self.rows if r.kind == "step" and id(r.step) in self.marks]
        else:
            r = self.rows[self.cursor] if self.rows else None
            rows = [r] if r is not None and r.kind == "step" else []
        ids = {id(r.step) for r in rows}
        return [r for r in rows if not ids & set(r.ancestors)]

    # ---- undo

    def snapshot(self):
        self.undo_stack.append(copy.deepcopy(self.doc))
        del self.undo_stack[:-100]
        self.redo_stack.clear()
        self._mark_dirty()

    def _mark_dirty(self, dirty=True):
        self.dirty = dirty
        self._update_title()

    def action_undo(self):
        if not self.undo_stack:
            self.query_one(StatusLine).say("nothing to undo")
            return
        self.redo_stack.append(copy.deepcopy(self.doc))
        self.doc = self.undo_stack.pop()
        self.marks.clear()
        self.edit_key = None
        self._mark_dirty()
        self.refresh_list()

    def action_redo(self):
        if not self.redo_stack:
            self.query_one(StatusLine).say("nothing to redo")
            return
        self.undo_stack.append(copy.deepcopy(self.doc))
        self.doc = self.redo_stack.pop()
        self.marks.clear()
        self.edit_key = None
        self._mark_dirty()
        self.refresh_list()

    # ---- list operations

    def insertion_point(self):
        """(list, index) where an inserted or pasted step goes: after the cursor's step,
        at the start of a then:/else: branch, or at the top for the recipe row."""
        r = self.rows[self.cursor] if self.rows else Row("meta")
        if r.kind == "step":
            return r.parent, r.index + 1
        if r.kind == "branch":
            if r.step.get(r.branch) is None:
                r.step[r.branch] = CommentedSeq()
            return r.step[r.branch], 0
        return self.doc["steps"], 0

    def _insert_steps(self, steps):
        lst, idx = self.insertion_point()
        self.snapshot()
        for k, s in enumerate(steps):
            lst.insert(idx + k, s)
        self.refresh_list()
        self._goto_step(steps[0])

    def _goto_step(self, step):
        for i, r in enumerate(self.rows):
            if r.kind == "step" and r.step is step:
                self.query_one("#steps", StepList).highlighted = i
                self._show_form()
                return

    def action_insert(self):
        def chosen(kind):
            if kind:
                step = RS.new_step(kind)
                cm = CommentedMap()
                cm.update(step)
                self._insert_steps([cm])
                self.action_pane("form")
        self.app.push_screen(InsertScreen(), chosen)

    def action_delete(self):
        sel = self.selection()
        if not sel:
            self.query_one(StatusLine).say("nothing to delete (put the cursor on a step, or mark steps)")
            return
        self.snapshot()
        self._remove(sel)
        self.marks.clear()
        self.refresh_list()
        self.query_one(StatusLine).say(f"deleted {len(sel)} step(s); Ctrl+Z undoes")

    def _remove(self, rows):
        by_list = {}
        for r in rows:
            by_list.setdefault(id(r.parent), (r.parent, []))[1].append(r.index)
        for lst, idxs in by_list.values():
            for i in sorted(idxs, reverse=True):
                del lst[i]

    def action_move(self, step):
        sel = self.selection()
        if not sel:
            return
        if len({id(r.parent) for r in sel}) > 1:
            self.query_one(StatusLine).say("marked steps are in different blocks: use cut (x) and paste (v)",
                                           "yellow")
            return
        lst = sel[0].parent
        idxs = sorted(r.index for r in sel)
        if (step < 0 and idxs[0] == 0) or (step > 0 and idxs[-1] == len(lst) - 1):
            self.query_one(StatusLine).say("already at the edge of this block; cut (x) and paste (v) to move "
                                           "it into another block", "yellow")
            return
        self.snapshot()
        moving = [lst[i] for i in idxs]
        for i in (idxs if step < 0 else reversed(idxs)):
            lst[i], lst[i + step] = lst[i + step], lst[i]
        cur = self.rows[self.cursor].step
        self.refresh_list()
        self._goto_step(cur if any(cur is m for m in moving) else moving[0])

    def action_duplicate(self):
        sel = self.selection()
        if not sel:
            return
        self.snapshot()
        groups = {}
        for r in sel:
            groups.setdefault(id(r.parent), (r.parent, []))[1].append(r.index)
        first = None
        for lst, idxs in groups.values():
            copies = [copy.deepcopy(lst[i]) for i in sorted(idxs)]
            at = max(idxs) + 1
            for k, c in enumerate(copies):
                lst.insert(at + k, c)
            first = first or copies[0]
        self.marks.clear()
        self.refresh_list()
        self._goto_step(first)
        self.query_one(StatusLine).say(f"duplicated {len(sel)} step(s)")

    def action_copy(self):
        sel = self.selection()
        self.clipboard = [copy.deepcopy(r.step) for r in sel]
        self.query_one(StatusLine).say(f"copied {len(sel)} step(s); v pastes after the cursor")

    def action_cut(self):
        sel = self.selection()
        if not sel:
            return
        self.clipboard = [copy.deepcopy(r.step) for r in sel]
        self.snapshot()
        self._remove(sel)
        self.marks.clear()
        self.refresh_list()
        self.query_one(StatusLine).say(f"cut {len(sel)} step(s); v pastes after the cursor")

    def action_paste(self):
        if not self.clipboard:
            self.query_one(StatusLine).say("the clipboard is empty (c copies, x cuts)")
            return
        self._insert_steps([copy.deepcopy(s) for s in self.clipboard])

    # ---- the form pane

    def _show_form(self):
        if not self.rows:
            return
        r = self.rows[self.cursor]
        if self.form_row is not None and r.kind == self.form_row.kind and r.step is self.form_row.step \
                and r.branch == self.form_row.branch:
            return
        self.form_row = r
        self.edit_key = None
        pane = self.query_one("#form-pane")
        pane.remove_children()
        widgets = []
        if r.kind == "meta":
            widgets.append(Static(Text.assemble(("Recipe\n", "bold"), (RS.VAR_HELP, "dim"))))
            vars_ = self.doc.get("vars")
            widgets.append(Form([
                FieldDef("name", "name", "str", False, "the recipe's title, shown when it runs",
                         value=self.doc.get("name") or ""),
                FieldDef("vars", "vars", "yaml", False, "variables (YAML): name: {default: .., choices: [..], "
                         "required: true, help: ..}", value=dump(vars_) if vars_ else ""),
            ], classes="pane-form"))
            used = set()
            for s in self.doc["steps"]:
                used |= used_vars(s)
            undeclared = sorted(used - set(self.declared) - {"boot"})
            unused = sorted(set(self.declared) - used)
            info = Text()
            if undeclared:
                info.append(f"\nUsed but not declared: {', '.join(undeclared)}\n", "yellow")
            if unused:
                info.append(f"\nDeclared but not used: {', '.join(unused)}\n", "dim")
            widgets.append(Static(info))
        elif r.kind == "branch":
            widgets.append(Static(Text.assemble((f"{r.branch}:\n\n", "bold magenta"),
                                                ("The steps under it run when the condition is "
                                                 + ("true." if r.branch == "then" else "false.")
                                                 + "\n\nWith the cursor here, insert (a) and paste (v) put "
                                                   "steps at the start of this branch.", ""))))
        else:
            act = RS.step_action(r.step)
            if act is None or (act != "if" and act not in RS.ACTIONS):
                widgets.append(Static(Text.assemble(("This step cannot be edited as a form\n", "bold red"),
                                                    ("; ".join(r.issues) + "\n\nPress F2 to edit it as YAML.",
                                                     ""))))
            else:
                widgets += self._step_widgets(r, act)
        self.form_gen += 1
        pane.mount(Vertical(*widgets, classes=f"form-gen form-gen-{self.form_gen}"))
        if r.issues:
            self.query_one(StatusLine).say("⚠ " + "; ".join(r.issues), "yellow")
        elif r.kind == "step":
            self.query_one(StatusLine).say(f"{r.where} — Enter edits, a inserts after, F2 edits as YAML")

    def _step_widgets(self, r, act):
        step = r.step
        out = []
        if act == "if":
            out.append(Static(Text.assemble(("if (when / then / else)\n", "bold magenta"),
                                            (RS.IF_BLOCK_HELP, "dim"))))
            fields = [FieldDef("name", "name", "str", False, RS.COMMON_HELP["name"], value=step.get("name") or ""),
                      FieldDef("when", "when", "yaml", True, "the condition (YAML), e.g. " + RS.CONDITION_EXAMPLE,
                               value=dump(step["when"]) if step.get("when") is not None else "")]
        else:
            a = RS.ACTIONS[act]
            out.append(Static(Text.assemble((a.name, "bold cyan"), f" — {a.summary}\n", (a.help, "dim"))))
            values = RS.arg_to_values(act, step[act])
            fields = []
            for f in a.fields:
                v = values.get(f.name, f.default if f.type == "bool" else None)
                if f.type == "mode" and isinstance(v, int):
                    v = f"0{v:o}"
                fields.append(FieldDef(f"arg:{f.name}", f.name, TYPE_MAP[f.type], f.required, f.help, f.choices, v))
            fields += [
                FieldDef("name", "name", "str", False, RS.COMMON_HELP["name"], value=step.get("name") or ""),
                FieldDef("when", "when", "yaml", False, RS.COMMON_HELP["when"],
                         value=dump(step["when"]) if step.get("when") is not None else "", data={"short": True}),
                FieldDef("ignore_errors", "ignore_errors", "bool", False, RS.COMMON_HELP["ignore_errors"],
                         value=bool(step.get("ignore_errors"))),
            ]
        out.append(Form(fields, classes="pane-form"))
        return out

    @on(Form.Focused)
    def _field_focus(self, event):
        self.query_one(StatusLine).say(event.field.explain())

    @on(Form.Changed)
    def _field_changed(self, event):
        r = self.form_row
        if r is None:
            return
        form, f = event.form, event.field
        key = (id(r.step) if r.step is not None else "meta", f.key)
        if key != self.edit_key:
            self.snapshot()      # one undo step per field edited, not per keystroke
            self.edit_key = key
        else:
            self._mark_dirty()
        try:
            if r.kind == "meta":
                self._apply_meta(form, f)
            else:
                self._apply_step(r, form, f)
        except ValueError as e:
            self.query_one(StatusLine).say(str(e), "bold red")
            return
        i = self.cursor
        self._redraw_row(i)
        self._redraw_row(0)
        r = self.rows[i]
        self.query_one(StatusLine).say(("⚠ " + "; ".join(r.issues)) if r.issues else f.explain(),
                                       "yellow" if r.issues else "")

    def _apply_meta(self, form, f):
        v = form.value(f.key)
        if f.key == "name":
            self.doc["name"] = v
        elif f.key == "vars":
            try:
                obj = parse_yaml(v)
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"vars: not valid YAML yet ({str(e).splitlines()[0]})")
            if obj is not None and not isinstance(obj, dict):
                raise ValueError("vars must be a mapping: name: {default: ...}")
            self.doc["vars"] = obj if obj is not None else CommentedMap()
            for i, r in enumerate(self.rows):
                r.issues = check_row(r, self.declared)
            self._redraw_all()

    def _apply_step(self, r, form, f):
        step = r.step
        v = form.value(f.key)
        if f.key == "name":
            _set(step, "name", v or None)
        elif f.key == "when":
            try:
                obj = parse_yaml(v)
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"when: not valid YAML yet ({str(e).splitlines()[0]})")
            if RS.step_action(step) == "if" and obj is None:
                raise ValueError("an if block needs a condition")
            _set(step, "when", obj)
        elif f.key == "ignore_errors":
            _set(step, "ignore_errors", True if v else None)
        elif f.key.startswith("arg:"):
            act = RS.step_action(step)
            a = RS.ACTIONS[act]
            values = {}
            for af in a.fields:
                raw = form.value(f"arg:{af.name}")
                values[af.name] = _convert(af, raw)
            new = RS.values_to_arg(act, values)
            old = step[act]
            if isinstance(new, dict) and isinstance(old, dict):
                for k in list(old):
                    if k not in new:
                        del old[k]
                for k, val in new.items():
                    if old.get(k) != val:
                        old[k] = val
            else:
                step[act] = new

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
            self.query_one(StatusLine).say("nothing to edit here")

    def action_yaml(self):
        r = self.rows[self.cursor]
        if r.kind == "branch":
            r = next(x for x in self.rows if x.kind == "step" and x.step is r.step)
        target = self.doc if r.kind == "meta" else r.step
        title = "The whole recipe as YAML" if r.kind == "meta" else f"Step {r.where} as YAML"

        def done(result):
            if not result:
                return
            _, obj = result
            if r.kind == "meta":
                if not isinstance(obj, dict) or "steps" not in obj:
                    self.query_one(StatusLine).say("a recipe needs a steps: list", "bold red")
                    return
                self.snapshot()
                self.doc = obj
            else:
                if not isinstance(obj, dict):
                    self.query_one(StatusLine).say("a step is a mapping", "bold red")
                    return
                self.snapshot()
                r.parent[r.index] = obj
            self.form_row = None
            self.refresh_list()
        self.app.push_screen(YamlEditScreen(title, dump(target)), done)

    def action_check(self):
        problems = [(r.where, issue) for r in self.rows for issue in r.issues]
        if not problems:
            self.query_one(StatusLine).say("✔ no problems found (conditions and files in the image are only "
                                           "checked when it runs: try a dry run, F5)", "green")
            return
        first = next(i for i, r in enumerate(self.rows) if r.issues)
        self.query_one("#steps", StepList).highlighted = first
        self.query_one(StatusLine).say(f"{len(problems)} problem(s); first: {problems[0][0]}: {problems[0][1]}",
                                       "yellow")

    # ---- files

    def action_save(self, then=None):
        if self.path is None:
            self.action_save_as(then)
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".part")
            tmp.write_text(dump(self.doc))
            tmp.replace(self.path)
        except OSError as e:
            self.query_one(StatusLine).say(f"cannot save: {e}", "bold red")
            return
        self.template = None
        self._mark_dirty(False)
        self.query_one(StatusLine).say(f"saved {self.path}", "green")
        if then:
            then()

    def action_save_as(self, then=None):
        default = str(self.path or (Path.cwd() / ((self.template.stem + "-custom") if self.template
                                                  else "my-recipe")).with_suffix(".yaml"))

        def got(p):
            if p:
                self.path = Path(os.path.abspath(os.path.expanduser(p)))
                if self.template and self.template.parent != self.path.parent:
                    self.query_one(StatusLine).say("note: content_file/copy src paths are relative to the "
                                                   "recipe file; copy the files they name next to it", "yellow")
                self.action_save(then)
        self.app.push_screen(PromptScreen("Save recipe as", "File:", default), got)

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
        doc = copy.deepcopy(self.doc)
        if scope == "selected":
            sel = self.selection()
            if not sel:
                self.query_one(StatusLine).say("no step selected", "yellow")
                return
            doc["steps"] = CommentedSeq(copy.deepcopy(r.step) for r in sel)
        base = (self.path or self.template)
        folder = base.parent if base is not None and os.access(base.parent, os.W_OK) else Path(tempfile.gettempdir())
        fd, tmp = tempfile.mkstemp(prefix=".piimg-edit-", suffix=".yaml", dir=folder)
        with os.fdopen(fd, "w") as f:
            f.write(dump(doc))
        argv = ["run", *(["--dry-run"] if dry else []), *[f"--var={k}={v}" for k, v in vars_.items()],
                "--", image, tmp]
        errors = []
        screen = R.RunScreen(f"{'Dry run' if dry else 'Run'}: {doc.get('name') or 'recipe'}"
                             + (" (selected steps)" if scope == "selected" else ""), argv, abort="warn")
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
            self.error_where = None
            if rc and errors and scope == "all":
                m = re.search(r"(steps(?:\[\d+\](?:\.(?:then|else))?)+)", errors[-1])
                if m:
                    where = m.group(1)
                    self.error_where = where
                    for i, r in enumerate(self.rows):
                        if r.where == where:
                            self.query_one("#steps", StepList).highlighted = i
                            self._show_form()  # now, so it doesn't overwrite the error below later
                            break
            self._redraw_all()
            if rc == 0:
                self.query_one(StatusLine).say("dry run finished without errors" if dry else "recipe applied",
                                               "green")
            else:
                self.query_one(StatusLine).say(errors[-1].removeprefix("piimg: ") if errors else
                                               f"failed (exit status {rc})", "bold red")
        self.app.push_screen(screen, done)

    # ---- menu bar

    @on(Button.Pressed, "#menubar Button")
    def _menu(self, event):
        {"m-save": self.action_save, "m-saveas": self.action_save_as, "m-insert": self.action_insert,
         "m-run": self.action_run, "m-check": self.action_check, "m-yaml": self.action_yaml,
         "m-close": self.action_close}[event.button.id]()


def _set(mapping, key, value):
    if value is None:
        mapping.pop(key, None)
    else:
        mapping[key] = value


def _convert(f, raw):
    """A form value -> what goes into the recipe for schema field f (None = leave out)."""
    if f.type in ("str", "mode", "choice"):
        return raw or None
    if f.type == "int":
        return int(raw) if str(raw).strip() not in ("", "-") else None
    if f.type == "bool":
        if f.default is True:
            return None if raw else False
        return True if raw else None
    if f.type == "list":
        return list(raw) or None
    if f.type == "map":
        return {k: _scalar_from_text(v) for k, v in raw.items()} or None
    if f.type == "text":
        if not raw:
            return None
        return LiteralScalarString(raw if raw.endswith("\n") else raw + "\n") if "\n" in raw.rstrip("\n") else raw
    if f.type == "yaml":
        return parse_yaml(raw)
    return raw


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
