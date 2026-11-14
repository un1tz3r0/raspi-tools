"""The recipe editor's model: a recipe as a list of rows, and what you can do to it.

Files are read and written with ruamel.yaml in round-trip mode, so comments
and layout of steps you do not touch are kept. A bundled recipe opened as a
template is an untitled copy you save somewhere else.

RecipeModel holds the document, the rows the step list shows (flatten), the
marks, the clipboard and the undo/redo stacks. Every operation takes the
cursor (a row index) and returns what the view needs to show next; views
only draw rows and forms and call these.
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

from .. import recipe_schema as RS
from ..script import VAR_RE, bundled_recipes_dir
from .fields import FieldDef

COMMON = ("name", "when", "ignore_errors")
TYPE_MAP = {"str": "str", "text": "text", "int": "int", "bool": "bool", "mode": "str", "choice": "choice",
            "list": "lines", "map": "map", "yaml": "yaml"}
UNDO_DEPTH = 100


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


def bundled_templates():
    """(path, title) of every bundled recipe."""
    import yaml
    out = []
    for f in sorted(bundled_recipes_dir().glob("*.y*ml")):
        try:
            title = (yaml.safe_load(f.read_text()) or {}).get("name", "")
        except Exception:  # noqa: BLE001
            title = ""
        out.append((f, str(title or "")))
    return out


def find_template(name):
    """A bundled recipe's path for a bare name ('pitft'), or None."""
    if "/" in name or name.endswith((".yaml", ".yml")):
        return None
    for ext in (".yaml", ".yml"):
        q = bundled_recipes_dir() / (name + ext)
        if q.is_file():
            return q
    return None


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
        # the first field that says what the step does: not a flag (live's
        # `network: true`), and a script by its first command
        first = next((k for k in keys if k in arg and k != "content" and not isinstance(arg[k], bool)), None)
        if first == "script" and isinstance(arg[first], str):
            lines = [ln.strip() for ln in arg[first].splitlines()]
            desc = _short(next((ln for ln in lines if ln and not ln.startswith("#")), ""), 50)
        else:
            desc = _short(arg[first], 50) if first else ""
        extra = [k for k in arg if k != first]
        if act in ("config_txt", "cmdline", "systemd"):
            desc = "; ".join(f"{k} {_short(v, 30)}" for k, v in arg.items())
            extra = []
        detail = desc + (f"  (+{', '.join(extra)})" if extra and len(desc) < 40 else "")
    else:
        detail = _short(arg, 60)
    return Text.assemble((act, "bold cyan" if act in RS.ACTIONS else "bold red"), " ", detail)


def used_vars(obj, skip_literal=True):
    """Variables obj uses: {{names}} in its strings (write content / live script
    only with template: true) and `var: {name: ...}` conditions."""
    out = set()

    def walk(o):
        if isinstance(o, str):
            out.update(m.group(1) for m in VAR_RE.finditer(o))
        elif isinstance(o, dict):
            for k, v in o.items():
                if skip_literal and k in ("content", "script") and not o.get("template"):
                    continue
                if k == "var" and isinstance(v, dict) and v.get("name") is not None:
                    out.add(str(v["name"]))
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


# ------------------------------------------------------------- help texts

def action_help(key):
    """Help for an action (or 'if') in the insert dialog, as rich Text."""
    t = Text()
    if key == "if":
        t.append("if (when / then / else)\n\n", "bold magenta")
        t.append(RS.IF_BLOCK_HELP + "\n\nExample condition:\n  ")
        t.append(RS.CONDITION_EXAMPLE, "green")
        return t
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
    return t


def insert_choices():
    """What the insert dialog lists: (category, [(key, summary)]) with 'if' first."""
    out = [("Control", [("if", "when / then / else")])]
    for cat in RS.CATEGORIES:
        acts = [(a.name, a.summary) for a in RS.ACTIONS.values() if a.category == cat]
        if cat == "Control":
            out[0][1].extend(acts)
        elif acts:
            out.append((cat, acts))
    return out


# ------------------------------------------------------------- the model

@dataclass
class Pane:
    """What the form pane shows for a row: a heading (rich Text), fields, and
    a note under them (rich Text) or a message instead of a form."""
    heading: Text
    fields: list
    note: Text = None
    editable: bool = True


class RecipeModel:
    """One recipe being edited. Cursor arguments are row indexes into .rows."""

    def __init__(self, path=None, template=None):
        self.path = Path(path) if path else None
        self.template = Path(template) if template else None
        if self.template:
            self.doc = load_doc(self.template)
        elif self.path and self.path.exists():
            self.doc = load_doc(self.path)
        else:
            self.doc = new_doc(self.path.stem if self.path else "new recipe")
        self.rows = []
        self.marks = set()          # ids of marked step mappings
        self.clipboard = []
        self.undo_stack, self.redo_stack = [], []
        self.edit_key = None        # (id(step), field) being edited, to group undo
        self.dirty = False
        self.error_where = None     # Runner's location of the last run error
        self.refresh()

    # ---- state

    @property
    def declared(self):
        v = self.doc.get("vars") or {}
        return list(v) if isinstance(v, dict) else []

    def title(self):
        name = str(self.path) if self.path else f"untitled (from template {self.template.stem})" \
            if self.template else "untitled"
        return ("● " if self.dirty else "") + name

    def display_name(self):
        return self.path.name if self.path else (f"{self.template.stem} (template)" if self.template else "untitled")

    def refresh(self):
        """Rebuild the rows from the document (after any structural change)."""
        self.rows = flatten(self.doc)
        declared = self.declared
        for r in self.rows:
            r.issues = check_row(r, declared)
        live = {id(r.step) for r in self.rows if r.kind == "step"}
        self.marks &= live
        return self.rows

    def recheck(self, i=None):
        """Re-run the checks on one row (or all)."""
        rows = self.rows if i is None else [self.rows[i]] if 0 <= i < len(self.rows) else []
        for r in rows:
            r.issues = check_row(r, self.declared)

    def clamp(self, i):
        return max(0, min(i or 0, len(self.rows) - 1))

    def index_of(self, step):
        return next((i for i, r in enumerate(self.rows) if r.kind == "step" and r.step is step), None)

    def row_text(self, r):
        """How the step list shows a row, as rich Text."""
        t = Text()
        if r.kind == "meta":
            vars_ = self.declared
            t.append("◆ ", "bold yellow")
            t.append(str(self.doc.get("name") or "(no name)"), "bold")
            t.append(f"   {len(vars_)} variable(s)" + (f": {', '.join(map(str, vars_))}" if vars_ else ""), "dim")
            return t
        t.append("  " * r.depth)
        if r.kind == "branch":
            t.append(f"{r.branch}:", "italic magenta")
            if not r.step.get(r.branch):
                t.append("  (empty)", "dim")
            return t
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
        return t

    def is_marked(self, r):
        return r.kind == "step" and id(r.step) in self.marks

    # ---- marks

    def toggle_mark(self, i):
        r = self.rows[i]
        if r.kind == "step":
            self.marks ^= {id(r.step)}

    def mark_range(self, a, b):
        lo, hi = sorted((self.clamp(a), self.clamp(b)))
        self.marks = {id(r.step) for r in self.rows[lo:hi + 1] if r.kind == "step"}

    def set_marks(self, indexes):
        self.marks = {id(self.rows[i].step) for i in indexes if 0 <= i < len(self.rows)
                      and self.rows[i].kind == "step"}

    def clear_marks(self):
        self.marks.clear()

    def selection(self, cursor):
        """Selected step rows in document order (the marked ones, else the one at
        the cursor), without those inside another selected one."""
        if self.marks:
            rows = [r for r in self.rows if r.kind == "step" and id(r.step) in self.marks]
        else:
            r = self.rows[cursor] if self.rows and 0 <= cursor < len(self.rows) else None
            rows = [r] if r is not None and r.kind == "step" else []
        ids = {id(r.step) for r in rows}
        return [r for r in rows if not ids & set(r.ancestors)]

    # ---- undo

    def snapshot(self):
        self.undo_stack.append(copy.deepcopy(self.doc))
        del self.undo_stack[:-UNDO_DEPTH]
        self.redo_stack.clear()
        self.dirty = True

    def undo(self):
        """Returns False if there was nothing to undo."""
        if not self.undo_stack:
            return False
        self.redo_stack.append(copy.deepcopy(self.doc))
        self.doc = self.undo_stack.pop()
        self._after_history()
        return True

    def redo(self):
        if not self.redo_stack:
            return False
        self.undo_stack.append(copy.deepcopy(self.doc))
        self.doc = self.redo_stack.pop()
        self._after_history()
        return True

    def _after_history(self):
        self.marks.clear()
        self.edit_key = None
        self.dirty = True
        self.refresh()

    # ---- list operations; each returns (row index to show, message)

    def insertion_point(self, cursor):
        """(list, index) where an inserted or pasted step goes: after the cursor's step,
        at the start of a then:/else: branch, or at the top for the recipe row."""
        r = self.rows[cursor] if self.rows and 0 <= cursor < len(self.rows) else Row("meta")
        if r.kind == "step":
            return r.parent, r.index + 1
        if r.kind == "branch":
            if r.step.get(r.branch) is None:
                r.step[r.branch] = CommentedSeq()
            return r.step[r.branch], 0
        return self.doc["steps"], 0

    def insert_steps(self, cursor, steps):
        self.snapshot()
        lst, idx = self.insertion_point(cursor)
        for k, s in enumerate(steps):
            lst.insert(idx + k, s)
        self.refresh()
        return self.index_of(steps[0])

    def insert_new(self, cursor, kind):
        cm = CommentedMap()
        cm.update(RS.new_step(kind))
        return self.insert_steps(cursor, [cm]), f"inserted {kind}"

    def _remove(self, rows):
        by_list = {}
        for r in rows:
            by_list.setdefault(id(r.parent), (r.parent, []))[1].append(r.index)
        for lst, idxs in by_list.values():
            for i in sorted(idxs, reverse=True):
                del lst[i]

    def delete(self, cursor):
        sel = self.selection(cursor)
        if not sel:
            return cursor, "nothing to delete (put the cursor on a step, or mark steps)"
        self.snapshot()
        self._remove(sel)
        self.marks.clear()
        self.refresh()
        return self.clamp(cursor), f"deleted {len(sel)} step(s); undo brings them back"

    def move(self, cursor, step):
        sel = self.selection(cursor)
        if not sel:
            return cursor, None
        if len({id(r.parent) for r in sel}) > 1:
            return cursor, "the marked steps are in different blocks: cut and paste them instead"
        lst = sel[0].parent
        idxs = sorted(r.index for r in sel)
        if (step < 0 and idxs[0] == 0) or (step > 0 and idxs[-1] == len(lst) - 1):
            return cursor, "already at the edge of this block; cut and paste to move it into another block"
        self.snapshot()
        moving = [lst[i] for i in idxs]
        for i in (idxs if step < 0 else reversed(idxs)):
            lst[i], lst[i + step] = lst[i + step], lst[i]
        cur = self.rows[cursor].step
        self.refresh()
        return self.index_of(cur if any(cur is m for m in moving) else moving[0]), None

    def duplicate(self, cursor):
        sel = self.selection(cursor)
        if not sel:
            return cursor, None
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
        self.refresh()
        return self.index_of(first), f"duplicated {len(sel)} step(s)"

    def copy(self, cursor):
        sel = self.selection(cursor)
        self.clipboard = [copy.deepcopy(r.step) for r in sel]
        return cursor, f"copied {len(sel)} step(s); paste puts them after the cursor"

    def cut(self, cursor):
        sel = self.selection(cursor)
        if not sel:
            return cursor, None
        self.clipboard = [copy.deepcopy(r.step) for r in sel]
        self.snapshot()
        self._remove(sel)
        self.marks.clear()
        self.refresh()
        return self.clamp(cursor), f"cut {len(sel)} step(s); paste puts them after the cursor"

    def paste(self, cursor):
        if not self.clipboard:
            return cursor, "the clipboard is empty (copy or cut steps first)"
        n = len(self.clipboard)
        return self.insert_steps(cursor, [copy.deepcopy(s) for s in self.clipboard]), f"pasted {n} step(s)"

    def problems(self):
        """[(row index, where, issue)] for every problem the checks find."""
        return [(i, r.where, issue) for i, r in enumerate(self.rows) for issue in r.issues]

    # ---- the form pane

    def pane(self, r):
        """What the form pane shows for row r."""
        if r.kind == "meta":
            vars_ = self.doc.get("vars")
            used = set()
            for s in self.doc["steps"]:
                used |= used_vars(s)
            undeclared = sorted(used - set(self.declared) - {"boot"})
            unused = sorted(set(map(str, self.declared)) - used)
            note = Text()
            if undeclared:
                note.append(f"Used but not declared: {', '.join(undeclared)}\n", "yellow")
            if unused:
                note.append(f"Declared but not used: {', '.join(unused)}\n", "dim")
            return Pane(Text.assemble(("Recipe\n", "bold"), (RS.VAR_HELP, "dim")), [
                FieldDef("name", "name", "str", False, "the recipe's title, shown when it runs",
                         value=self.doc.get("name") or ""),
                FieldDef("vars", "vars", "yaml", False, "variables (YAML): name: {default: .., choices: [..], "
                         "required: true, help: ..}", value=dump(vars_) if vars_ else ""),
            ], note)
        if r.kind == "branch":
            return Pane(Text.assemble((f"{r.branch}:\n\n", "bold magenta"),
                                      ("The steps under it run when the condition is "
                                       + ("true." if r.branch == "then" else "false.")
                                       + "\n\nWith the cursor here, insert and paste put steps at the "
                                         "start of this branch.", "")), [], editable=False)
        act = RS.step_action(r.step)
        if act is None or (act != "if" and act not in RS.ACTIONS):
            return Pane(Text.assemble(("This step cannot be edited as a form\n", "bold red"),
                                      ("; ".join(r.issues) + "\n\nEdit it as YAML instead.", "")), [],
                        editable=False)
        step = r.step
        if act == "if":
            return Pane(Text.assemble(("if (when / then / else)\n", "bold magenta"), (RS.IF_BLOCK_HELP, "dim")), [
                FieldDef("name", "name", "str", False, RS.COMMON_HELP["name"], value=step.get("name") or ""),
                FieldDef("when", "when", "yaml", True, "the condition (YAML), e.g. " + RS.CONDITION_EXAMPLE,
                         value=dump(step["when"]) if step.get("when") is not None else "")])
        a = RS.ACTIONS[act]
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
        return Pane(Text.assemble((a.name, "bold cyan"), f" — {a.summary}\n", (a.help, "dim")), fields)

    def apply_field(self, r, key, values):
        """A field of row r's form changed. values: every field's current value
        (key -> value). One undo step per field edited, not per keystroke.
        Raises ValueError (shown to the user) if the value cannot be used yet."""
        ek = (id(r.step) if r.step is not None else "meta", key)
        if ek != self.edit_key:
            self.snapshot()
            self.edit_key = ek
        else:
            self.dirty = True
        if r.kind == "meta":
            self._apply_meta(key, values[key])
            self.recheck()
        else:
            self._apply_step(r, key, values)
            r.issues = check_row(r, self.declared)

    def end_edit(self):
        """The form was left or rebuilt: the next change is a new undo step."""
        self.edit_key = None

    def _apply_meta(self, key, v):
        if key == "name":
            self.doc["name"] = v
        elif key == "vars":
            try:
                obj = parse_yaml(v)
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"vars: not valid YAML yet ({str(e).splitlines()[0]})")
            if obj is not None and not isinstance(obj, dict):
                raise ValueError("vars must be a mapping: name: {default: ...}")
            self.doc["vars"] = obj if obj is not None else CommentedMap()

    def _apply_step(self, r, key, values):
        step = r.step
        v = values.get(key)
        if key == "name":
            _set(step, "name", v or None)
        elif key == "when":
            try:
                obj = parse_yaml(v)
            except Exception as e:  # noqa: BLE001
                raise ValueError(f"when: not valid YAML yet ({str(e).splitlines()[0]})")
            if RS.step_action(step) == "if" and obj is None:
                raise ValueError("an if block needs a condition")
            _set(step, "when", obj)
        elif key == "ignore_errors":
            _set(step, "ignore_errors", True if v else None)
        elif key.startswith("arg:"):
            act = RS.step_action(step)
            a = RS.ACTIONS[act]
            new = RS.values_to_arg(act, {af.name: _convert(af, values.get(f"arg:{af.name}")) for af in a.fields})
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

    # ---- YAML

    def yaml_target(self, cursor):
        """(row, title, text) for editing the row at the cursor as YAML: the whole
        recipe on the recipe row, the if-step on a branch row."""
        r = self.rows[cursor]
        if r.kind == "branch":
            r = next(x for x in self.rows if x.kind == "step" and x.step is r.step)
        if r.kind == "meta":
            return r, "The whole recipe as YAML", dump(self.doc)
        return r, f"Step {r.where} as YAML", dump(r.step)

    def replace_yaml(self, r, text):
        """Replace row r (from yaml_target) with parsed YAML text. Returns the row
        index to show. Raises ValueError for text that cannot be used."""
        try:
            obj = parse_yaml(text)
        except Exception as e:  # noqa: BLE001 - YAML errors are shown
            raise ValueError(f"not valid YAML: {e}".replace("\n", " "))
        if r.kind == "meta":
            if not isinstance(obj, dict) or "steps" not in obj:
                raise ValueError("a recipe needs a steps: list")
            if obj["steps"] is None:
                obj["steps"] = CommentedSeq()
            self.snapshot()
            self.doc = obj
            self.refresh()
            return 0
        if not isinstance(obj, dict):
            raise ValueError("a step is a mapping")
        self.snapshot()
        r.parent[r.index] = obj
        self.refresh()
        return self.index_of(obj)

    # ---- files

    def save(self, path=None):
        """Write the recipe (to path, which becomes its file). Raises OSError.
        Returns a note to show, or None."""
        note = None
        if path is not None:
            path = Path(os.path.abspath(os.path.expanduser(str(path))))
            if self.template and self.template.parent != path.parent:
                note = ("note: content_file/copy src paths are relative to the recipe file; "
                        "copy the files they name next to it")
            self.path = path
        if self.path is None:
            raise OSError("no file name to save to")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".part")
        tmp.write_text(dump(self.doc))
        tmp.replace(self.path)
        self.template = None
        self.dirty = False
        return note

    def default_save_path(self):
        return str(self.path or (Path.cwd() / ((self.template.stem + "-custom") if self.template
                                               else "my-recipe")).with_suffix(".yaml"))

    # ---- running

    def run_file(self, cursor, selected_only):
        """Write the recipe (or only the selected steps) to a temporary file next
        to it, so relative paths in it still work. Returns its path, or None if
        selected_only and nothing is selected. The caller deletes it."""
        doc = copy.deepcopy(self.doc)
        if selected_only:
            sel = self.selection(cursor)
            if not sel:
                return None
            doc["steps"] = CommentedSeq(copy.deepcopy(r.step) for r in sel)
        base = (self.path or self.template)
        folder = base.parent if base is not None and os.access(base.parent, os.W_OK) else Path(tempfile.gettempdir())
        fd, tmp = tempfile.mkstemp(prefix=".piimg-edit-", suffix=".yaml", dir=folder)
        with os.fdopen(fd, "w") as f:
            f.write(dump(doc))
        return tmp

    @staticmethod
    def run_argv(image, tmp, dry, vars_):
        return ["run", *(["--dry-run"] if dry else []), *[f"--var={k}={v}" for k, v in vars_.items()],
                "--", image, tmp]

    def run_finished(self, rc, errors, selected_only):
        """A run ended. errors: its `piimg: ...` lines. Marks the failing step
        (whole-recipe runs only) and returns its row index, or None."""
        self.error_where = None
        if rc and errors and not selected_only:
            m = re.search(r"(steps(?:\[\d+\](?:\.(?:then|else))?)+)", errors[-1])
            if m:
                self.error_where = m.group(1)
                return next((i for i, r in enumerate(self.rows) if r.where == self.error_where), None)
        return None


def run_options(image, has_selection, var_spec):
    """The fields of the run-recipe dialog: image, mode, scope, and the variables."""
    from .commands import var_fields
    return [
        FieldDef("image", "image", "str", True, "image, overlay or card to run the recipe on", value=image or "",
                 placeholder="work.qcow2"),
        FieldDef("mode", "mode", "choice", True, "dry run: open the image read-only and show what each "
                 "step would do; apply: make the changes", ("dry run", "apply"), "dry run"),
        FieldDef("scope", "steps", "choice", True, "the whole recipe, or only the marked steps (and the "
                 "one under the cursor if none are marked)", ("all", "selected"),
                 "selected" if has_selection else "all"),
    ], var_fields(var_spec if isinstance(var_spec, dict) else {}, prefix="var:")
