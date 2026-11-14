"""The recipe editor window: steps on the left, the selected step's form on the right.

A view over frontend.recipe.RecipeModel, like the TUI's editor. Each recipe
has its own window, with its own file, undo history and unsaved state. Run
and dry run use the main window's target unless you pick another.

Selection: click, Ctrl+click and Shift+click (or Shift+arrows, Ctrl+Space)
select steps; list operations work on the selected steps, and insert and
paste put steps after the last one clicked.
"""
import os
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ..frontend import proc as P
from ..frontend import recipe as M
from .dialogs import help_bar, say
from .forms import Form, buffer_text, source_view
from .markup import to_markup
from .run import RunDialog

LIST_KEYS = {
    "win.delete": ["Delete", "KP_Delete"],
    "win.move-up": ["<Alt>Up"],
    "win.move-down": ["<Alt>Down"],
    "win.duplicate": ["<Control>d"],
    "win.copy": ["<Control>c"],
    "win.cut": ["<Control>x"],
    "win.paste": ["<Control>v"],
    "win.undo": ["<Control>z"],
    "win.redo": ["<Control><Shift>z", "<Control>y"],
    "win.unmark": ["Escape"],
}


# ------------------------------------------------------------- dialogs

class RecipePickDialog(Adw.Dialog):
    """New recipe, open a file, or start from a bundled recipe (a template).
    on_pick(path, template)."""

    def __init__(self, on_pick):
        super().__init__(title="Recipe Editor", content_width=560, content_height=620)
        self.on_pick = on_pick
        page = Adw.PreferencesPage()
        start = Adw.PreferencesGroup()
        new = Adw.ActionRow(title="New Empty Recipe", activatable=True)
        new.add_prefix(Gtk.Image(icon_name="document-new-symbolic"))
        new.connect("activated", lambda _r: self._pick(None, None))
        opn = Adw.ActionRow(title="Open a Recipe File…", activatable=True)
        opn.add_prefix(Gtk.Image(icon_name="document-open-symbolic"))
        opn.connect("activated", self._open_file)
        start.add(new)
        start.add(opn)
        page.add(start)
        tg = Adw.PreferencesGroup(title="Templates",
                                  description="A template is a bundled recipe: you edit a copy and save it elsewhere.")
        for f, title in M.bundled_templates():
            row = Adw.ActionRow(title=GLib.markup_escape_text(f.stem), subtitle=GLib.markup_escape_text(title),
                                activatable=True)
            row.add_prefix(Gtk.Image(icon_name="text-x-generic-symbolic"))
            row.add_suffix(Gtk.Image(icon_name="go-next-symbolic"))
            row.connect("activated", lambda _r, f=f: self._pick(None, f))
            tg.add(row)
        page.add(tg)
        tv = Adw.ToolbarView(content=page)
        tv.add_top_bar(Adw.HeaderBar())
        self.set_child(tv)

    def _pick(self, path, template):
        self.close()
        self.on_pick(path, template)

    def _open_file(self, _r):
        open_recipe_file(self.get_root(), lambda p: self._pick(p, None))


def _yaml_filters():
    f = Gtk.FileFilter(name="Recipes (YAML)")
    f.add_pattern("*.yaml")
    f.add_pattern("*.yml")
    store = Gio.ListStore(item_type=Gtk.FileFilter)
    store.append(f)
    return store, f


def open_recipe_file(parent, on_path):
    filters, f = _yaml_filters()
    d = Gtk.FileDialog(title="Open a recipe", filters=filters, default_filter=f)

    def done(dlg, res):
        try:
            g = dlg.open_finish(res)
        except GLib.Error:
            return
        if g and g.get_path():
            on_path(g.get_path())
    d.open(parent, None, done)


class InsertDialog(Adw.Dialog):
    """Pick the kind of step to insert, with help for the highlighted one. on_insert(key)."""

    def __init__(self, on_insert):
        super().__init__(title="Insert a Step", content_width=900, content_height=640)
        self.on_insert = on_insert
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.close())
        self.ok = Gtk.Button(label="Insert", css_classes=["suggested-action"])
        self.ok.connect("clicked", lambda _b: self._insert())
        header.pack_start(cancel)
        header.pack_end(self.ok)
        self.search = Gtk.SearchEntry(placeholder_text="Filter actions", hexpand=True)
        header.set_title_widget(self.search)
        self.list = Gtk.ListBox(css_classes=["navigation-sidebar"])
        self.list.connect("row-selected", self._selected)
        self.list.connect("row-activated", lambda *_: self._insert())
        for cat, acts in M.insert_choices():
            for i, (key, summary) in enumerate(acts):
                row = Gtk.ListBoxRow()
                row.key, row.text = key, f"{key} {summary}".lower()
                row.group_title = cat if i == 0 else None
                b = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=3, margin_bottom=3)
                n = Gtk.Label(xalign=0)
                n.set_markup(f"<b>{GLib.markup_escape_text(key)}</b>")
                s = Gtk.Label(label=summary, xalign=0, css_classes=["dim-label", "caption"],
                              ellipsize=Pango.EllipsizeMode.END)
                b.append(n)
                b.append(s)
                row.set_child(b)
                self.list.append(row)
        self.list.set_header_func(_group_header)
        self.list.set_filter_func(lambda row: not self.search.get_text()
                                  or self.search.get_text().lower() in row.text)
        self.search.connect("search-changed", lambda _e: self._filtered())
        self.search.connect("activate", lambda _e: self._insert())
        self.help = Gtk.Label(xalign=0, yalign=0, wrap=True, selectable=True, css_classes=["help-pane"])
        self.help.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        paned = Gtk.Paned(start_child=Gtk.ScrolledWindow(child=self.list, hscrollbar_policy=Gtk.PolicyType.NEVER),
                          end_child=Gtk.ScrolledWindow(child=self.help), position=330,
                          shrink_start_child=False, resize_start_child=False)
        tv = Adw.ToolbarView(content=paned)
        tv.add_top_bar(header)
        self.set_child(tv)
        self.list.select_row(self.list.get_row_at_index(0))
        GLib.idle_add(lambda: self.search.grab_focus() and False)
        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._search_keys)
        self.search.add_controller(key)

    def _search_keys(self, _c, keyval, _code, _state):
        from gi.repository import Gdk
        if keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            rows = [r for r in _rows(self.list) if r.get_child_visible()]
            cur = self.list.get_selected_row()
            if rows:
                i = rows.index(cur) if cur in rows else -1
                i = max(0, min(len(rows) - 1, i + (1 if keyval == Gdk.KEY_Down else -1)))
                self.list.select_row(rows[i])
            return True
        return False

    def _filtered(self):
        self.list.invalidate_filter()
        cur = self.list.get_selected_row()
        if cur is None or not cur.get_child_visible():
            first = next((r for r in _rows(self.list) if r.get_child_visible()), None)
            if first is not None:
                self.list.select_row(first)

    def _selected(self, _l, row):
        if row is not None:
            self.help.set_markup(to_markup(M.action_help(row.key)))

    def _insert(self):
        row = self.list.get_selected_row()
        if row is not None:
            self.close()
            self.on_insert(row.key)


def _rows(listbox):
    out, i = [], 0
    while (r := listbox.get_row_at_index(i)) is not None:
        out.append(r)
        i += 1
    return out


def _group_header(row, before):
    title = getattr(row, "group_title", None)
    if title:
        row.set_header(Gtk.Label(label=title, xalign=0, margin_top=10, margin_start=10, margin_bottom=2,
                                 css_classes=["heading", "dim-label"]))
    else:
        row.set_header(None)


class YamlDialog(Adw.Dialog):
    """Edit text as YAML. on_apply(text) returns an error message, or None when done."""

    def __init__(self, title, text, on_apply):
        super().__init__(title=title, content_width=820, content_height=640)
        self.on_apply = on_apply
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.close())
        apply = Gtk.Button(label="Apply", css_classes=["suggested-action"], tooltip_text="Apply (Ctrl+S)")
        apply.connect("clicked", lambda _b: self.apply())
        header.pack_start(cancel)
        header.pack_end(apply)
        self.view = source_view(text, "yaml")
        self.view.set_show_line_numbers(True)
        self.view.set_highlight_current_line(True)
        self.status = help_bar()
        say(self.status, "Ctrl+S or Apply to use this; Escape discards")
        tv = Adw.ToolbarView(content=Gtk.ScrolledWindow(child=self.view, vexpand=True))
        tv.add_top_bar(header)
        tv.add_bottom_bar(self.status)
        self.set_child(tv)
        sc = Gtk.ShortcutController()
        for accel in ("<Control>s", "<Control>Return"):
            sc.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string(accel),
                                         action=Gtk.CallbackAction.new(lambda *_: self.apply() or True)))
        self.add_controller(sc)
        GLib.idle_add(lambda: self.view.grab_focus() and False)

    def apply(self):
        err = self.on_apply(buffer_text(self.view.get_buffer()))
        if err:
            say(self.status, err, "error")
        else:
            self.close()


class RunRecipeDialog(Adw.Dialog):
    """Options for running the edited recipe. on_run(image, scope, dry, vars)."""

    def __init__(self, image, has_selection, var_spec, on_run):
        super().__init__(title="Run Recipe", content_width=560, content_height=560)
        self.on_run = on_run
        fields, var_fields = M.run_options(image, has_selection, var_spec)
        fields[0].data["kind"] = "image"
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: self.close())
        run = Gtk.Button(label="Run", css_classes=["suggested-action"])
        run.connect("clicked", lambda _b: self.run())
        header.pack_start(cancel)
        header.pack_end(run)
        self.help = help_bar()
        page = Adw.PreferencesPage()
        self.form = Form(fields, on_focus=lambda f: say(self.help, f.explain()))
        page.add(self.form.widget)
        self.varform = None
        if var_fields:
            self.varform = Form(var_fields, on_focus=lambda f: say(self.help, f.explain()), title="Variables")
            page.add(self.varform.widget)
        tv = Adw.ToolbarView(content=page)
        tv.add_top_bar(header)
        tv.add_bottom_bar(self.help)
        self.set_child(tv)

    def run(self):
        v = self.form.values()
        if not v["image"]:
            say(self.help, "image is required", "error")
            self.form.focus("image")
            return
        vars_ = {}
        if self.varform is not None:
            missing = self.varform.check_required()
            if missing:
                say(self.help, f"{missing.label} is required", "error")
                return
            vars_ = {k[4:]: val for k, val in self.varform.values().items() if val not in ("", None)}
        self.close()
        self.on_run(v["image"], v["scope"], v["mode"] == "dry run", vars_)


# ------------------------------------------------------------- the window

class RecipeWindow(Adw.ApplicationWindow):
    def __init__(self, app, path=None, template=None):
        self.model = M.RecipeModel(path, template)   # raises OSError / ValueError for a bad file
        super().__init__(application=app, default_width=1200, default_height=760)
        self.set_size_request(360, 360)
        self.app = app
        self.image = None
        self.cursor = 0
        self.form = None
        self.form_row = None
        self.row_widgets = []
        self._rebuilding = False
        self._build()
        self._actions()
        self.refresh_list(0)
        self.connect("close-request", self._close_request)

    # ---- layout

    def _build(self):
        header = Adw.HeaderBar()
        self.wtitle = Adw.WindowTitle()
        header.set_title_widget(self.wtitle)
        ins = Gtk.Button(icon_name="list-add-symbolic", tooltip_text="Insert a step (Ctrl+I)", action_name="win.insert")
        dele = Gtk.Button(icon_name="list-remove-symbolic", tooltip_text="Delete the selected steps (Delete)",
                          action_name="win.delete")
        up = Gtk.Button(icon_name="go-up-symbolic", tooltip_text="Move up (Alt+Up)", action_name="win.move-up")
        down = Gtk.Button(icon_name="go-down-symbolic", tooltip_text="Move down (Alt+Down)",
                          action_name="win.move-down")
        undo = Gtk.Button(icon_name="edit-undo-symbolic", tooltip_text="Undo (Ctrl+Z)", action_name="win.undo")
        redo = Gtk.Button(icon_name="edit-redo-symbolic", tooltip_text="Redo (Ctrl+Shift+Z)", action_name="win.redo")
        edit_box = Gtk.Box(css_classes=["linked"])
        for b in (ins, dele):
            edit_box.append(b)
        move_box = Gtk.Box(css_classes=["linked"])
        for b in (up, down):
            move_box.append(b)
        hist = Gtk.Box(css_classes=["linked"])
        for b in (undo, redo):
            hist.append(b)
        header.pack_start(edit_box)
        header.pack_start(move_box)
        header.pack_start(hist)

        menu = Gio.Menu()
        sec = Gio.Menu()
        sec.append("Save", "win.save")
        sec.append("Save As…", "win.save-as")
        sec.append("Open…", "win.open")
        sec.append("New Recipe Window", "win.new")
        menu.append_section(None, sec)
        sec = Gio.Menu()
        for label, action in (("Duplicate", "win.duplicate"), ("Copy Steps", "win.copy"),
                              ("Cut Steps", "win.cut"), ("Paste Steps", "win.paste")):
            item = Gio.MenuItem.new(label, action)
            accel = LIST_KEYS.get(action)
            if accel:
                item.set_attribute_value("accel", GLib.Variant.new_string(accel[0]))
            sec.append_item(item)
        sec.append("Edit as YAML…", "win.yaml")
        sec.append("Check", "win.check")
        menu.append_section(None, sec)
        sec = Gio.Menu()
        sec.append("Keyboard Shortcuts", "app.shortcuts")
        sec.append("Close", "win.close")
        menu.append_section(None, sec)
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True,
                                       tooltip_text="Menu"))
        run = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Run or dry run (F5)",
                         action_name="win.run")
        header.pack_end(run)
        save = Gtk.Button(label="Save", action_name="win.save", tooltip_text="Save (Ctrl+S)")
        header.pack_end(save)

        self.list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.MULTIPLE, css_classes=["step-list"],
                                activate_on_single_click=False)
        self.list.connect("selected-rows-changed", self._selection_changed)
        self.list.connect("row-activated", lambda _l, row: self._focus_form())
        sc = Gtk.ShortcutController(scope=Gtk.ShortcutScope.LOCAL)
        for action, accels in LIST_KEYS.items():
            for a in accels:
                sc.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string(a),
                                             action=Gtk.NamedAction.new(action)))
        self.list.add_controller(sc)
        left = Gtk.ScrolledWindow(child=self.list, hscrollbar_policy=Gtk.PolicyType.AUTOMATIC, vexpand=True)

        self.pane_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12,
                                margin_bottom=18, margin_start=12, margin_end=12)
        right = Gtk.ScrolledWindow(child=self.pane_box, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.paned = Gtk.Paned(start_child=left, end_child=right, position=620, wide_handle=True,
                               shrink_start_child=False, shrink_end_child=False)
        self.status = help_bar()
        tv = Adw.ToolbarView(content=self.paned)
        tv.add_top_bar(header)
        tv.add_bottom_bar(self.status)
        self.toasts = Adw.ToastOverlay(child=tv)
        self.set_content(self.toasts)

    def _actions(self):
        acts = {
            "save": lambda: self.save(), "save-as": lambda: self.save_as(), "open": self.open_other,
            "new": lambda: self.app.recipe_window(), "insert": self.insert, "delete": self.delete,
            "move-up": lambda: self.move(-1), "move-down": lambda: self.move(1), "duplicate": self.duplicate,
            "copy": self.copy, "cut": self.cut, "paste": self.paste, "undo": self.undo, "redo": self.redo,
            "unmark": self.unmark, "yaml": self.edit_yaml, "check": self.check, "run": self.run,
            "close": self.close,
        }
        for name, cb in acts.items():
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", lambda _a, _p, cb=cb: cb())
            self.add_action(a)

    def say(self, text, kind=""):
        if text:
            say(self.status, text, kind)

    def update_title(self):
        m = self.model
        self.wtitle.set_title(("● " if m.dirty else "") + m.display_name())
        self.wtitle.set_subtitle(str(m.path.parent) if m.path else "not saved yet")
        self.set_title(f"{m.display_name()} — piimg recipe editor")
        self.lookup_action("undo").set_enabled(bool(m.undo_stack))
        self.lookup_action("redo").set_enabled(bool(m.redo_stack))

    # ---- the list

    def _row_markup(self, r):
        t = self.model.row_text(r)
        plain = t.plain
        lead = len(plain) - len(plain.lstrip(" "))
        if lead:
            t = t[lead:]
        return to_markup(t)

    def refresh_list(self, cursor=None, marks=None):
        """Rebuild the list from the model's rows; select `cursor` (and the marks)."""
        had_focus = self.list.get_focus_child() is not None
        self._rebuilding = True
        self.list.remove_all()
        self.row_widgets = []
        for i, r in enumerate(self.model.rows):
            row = Gtk.ListBoxRow()
            lb = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, margin_start=8 + 18 * r.depth,
                           margin_top=4, margin_bottom=4)
            lb.set_markup(self._row_markup(r))
            row.set_child(lb)
            row.index = i
            if r.kind == "branch" or r.kind == "meta":
                row.add_css_class("dim-label" if r.kind == "branch" else "heading")
            self.list.append(row)
            self.row_widgets.append(row)
        cursor = self.model.clamp(self.cursor if cursor is None else cursor)
        self.cursor = cursor
        for i in (marks or ()):
            if 0 <= i < len(self.row_widgets):
                self.list.select_row(self.row_widgets[i])
        if self.row_widgets:
            self.list.select_row(self.row_widgets[cursor])
        self._rebuilding = False
        self._sync_marks()
        self.form_row = None
        self.show_form()
        self.redraw_labels()
        self.update_title()
        if had_focus and self.row_widgets:
            self.row_widgets[cursor].grab_focus()

    def redraw_labels(self, indexes=None):
        rows = self.model.rows
        for i in (range(len(rows)) if indexes is None else indexes):
            if 0 <= i < len(self.row_widgets):
                r = rows[i]
                w = self.row_widgets[i]
                w.get_child().set_markup(self._row_markup(r))
                (w.add_css_class if r.where == self.model.error_where and r.kind == "step"
                 else w.remove_css_class)("error-step")
                w.set_tooltip_text("; ".join(r.issues) if r.issues else None)

    def selected_indexes(self):
        return sorted(r.get_index() for r in self.list.get_selected_rows())

    def _selection_changed(self, _l):
        if self._rebuilding:
            return
        sel = self.selected_indexes()
        focus = self.list.get_focus_child()
        if len(sel) == 1:
            self.cursor = sel[0]
        elif sel:
            fi = focus.get_index() if isinstance(focus, Gtk.ListBoxRow) else -1
            self.cursor = fi if fi in sel else sel[-1]
        self._sync_marks()
        self.show_form()

    def _sync_marks(self):
        sel = self.selected_indexes()
        if len(sel) > 1:
            self.model.set_marks(sel)
        else:
            self.model.clear_marks()

    def unmark(self):
        if len(self.selected_indexes()) > 1:
            self.list.unselect_all()
            self.list.select_row(self.row_widgets[self.cursor])

    # ---- the form pane

    def show_form(self):
        rows = self.model.rows
        if not rows:
            return
        n = len(self.model.marks)
        key = ("marks", n) if n > 1 else rows[self.cursor]
        if key is self.form_row or (isinstance(key, tuple) and key == self.form_row):
            return
        self.form_row = key
        self.model.end_edit()
        child = self.pane_box.get_first_child()
        while child is not None:
            nxt = child.get_next_sibling()
            self.pane_box.remove(child)
            child = nxt
        self.form = None
        if n > 1:
            self.pane_box.append(Adw.StatusPage(icon_name="edit-select-all-symbolic", title=f"{n} steps selected",
                                                description="Delete, move, duplicate, copy or cut them, or "
                                                            "run only them (F5). Escape selects one again."))
            self.say(f"{n} steps selected")
            return
        r = rows[self.cursor]
        p = self.model.pane(r)
        head = Gtk.Label(xalign=0, wrap=True, selectable=True)
        head.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        head.set_markup(to_markup(p.heading))
        self.pane_box.append(head)
        if p.fields:
            self.form = Form(p.fields, on_change=self._field_changed, on_focus=lambda f: self.say(f.explain()))
            self.pane_box.append(self.form.widget)
        if p.note and p.note.plain.strip():
            note = Gtk.Label(xalign=0, wrap=True)
            note.set_markup(to_markup(p.note))
            self.pane_box.append(note)
        if not p.editable and r.kind == "step":
            b = Gtk.Button(label="Edit as YAML…", halign=Gtk.Align.START, action_name="win.yaml")
            self.pane_box.append(b)
        if r.issues:
            self.say("⚠ " + "; ".join(r.issues), "warn")
        elif r.kind == "step":
            self.say(f"{r.where} — Enter edits the fields, Ctrl+I inserts after it, F2 edits it as YAML")
        elif r.kind == "meta":
            self.say("The recipe's name and variables; Ctrl+I inserts a step at the top")

    def _focus_form(self):
        if self.form is not None:
            self.form.focus_first()
        else:
            self.say("nothing to edit here")

    def _field_changed(self, f):
        r = self.form_row
        if not hasattr(r, "kind") or self.form is None:
            return
        try:
            self.model.apply_field(r, f.key, self.form.values())
        except ValueError as e:
            self.say(str(e), "error")
            return
        self.update_title()
        if r.kind == "meta":
            self.redraw_labels()
        else:
            self.redraw_labels([self.cursor, 0])
        self.say(("⚠ " + "; ".join(r.issues)) if r.issues else f.explain(), "warn" if r.issues else "")

    # ---- operations

    def _after(self, result, kind=""):
        i, msg = result
        self.refresh_list(i)
        self.say(msg, kind)

    def insert(self):
        InsertDialog(lambda key: (self._after(self.model.insert_new(self.cursor, key)),
                                  GLib.idle_add(lambda: self._focus_form() and False))).present(self)

    def delete(self):
        self._after(self.model.delete(self.cursor))

    def move(self, step):
        marks = self.selected_indexes()
        i, msg = self.model.move(self.cursor, step)
        if msg:
            self.say(msg, "warn")
            return
        moved = [m + (i - self.cursor) for m in marks] if len(marks) > 1 and i is not None else None
        self.refresh_list(i, moved)

    def duplicate(self):
        self._after(self.model.duplicate(self.cursor))

    def copy(self):
        self.say(self.model.copy(self.cursor)[1])

    def cut(self):
        self._after(self.model.cut(self.cursor))

    def paste(self):
        self._after(self.model.paste(self.cursor))

    def undo(self):
        if not self.model.undo():
            self.say("nothing to undo")
            return
        self.refresh_list()

    def redo(self):
        if not self.model.redo():
            self.say("nothing to redo")
            return
        self.refresh_list()

    def edit_yaml(self):
        r, title, text = self.model.yaml_target(self.cursor)

        def apply(new_text):
            try:
                i = self.model.replace_yaml(r, new_text)
            except ValueError as e:
                return str(e)
            self.refresh_list(i)
            return None
        YamlDialog(title, text, apply).present(self)

    def check(self):
        problems = self.model.problems()
        if not problems:
            self.say("✔ no problems found (conditions and files in the image are only checked when it runs: "
                     "try a dry run, F5)", "ok")
            return
        i, where, issue = problems[0]
        self.refresh_list(i)
        self.say(f"{len(problems)} problem(s); first: {where}: {issue}", "warn")

    # ---- files

    def save(self, then=None, path=None):
        if self.model.path is None and path is None:
            self.save_as(then)
            return
        try:
            note = self.model.save(path)
        except OSError as e:
            self.say(f"cannot save: {e}", "error")
            return
        self.update_title()
        self.say(note or f"saved {self.model.path}", "warn" if note else "ok")
        if then:
            then()

    def save_as(self, then=None):
        filters, f = _yaml_filters()
        default = Path(self.model.default_save_path())
        d = Gtk.FileDialog(title="Save recipe as", filters=filters, default_filter=f, initial_name=default.name)
        if default.parent.is_dir():
            d.set_initial_folder(Gio.File.new_for_path(str(default.parent)))

        def done(dlg, res):
            try:
                g = dlg.save_finish(res)
            except GLib.Error:
                return
            if g and g.get_path():
                self.save(then, path=g.get_path())
        d.save(self, None, done)

    def open_other(self):
        open_recipe_file(self, lambda p: self.app.recipe_window(p))

    def _close_request(self, _w):
        if not self.model.dirty:
            return False
        d = Adw.AlertDialog(heading="Save changes?",
                            body=f"{self.model.display_name()} has unsaved changes.")
        d.add_response("cancel", "Cancel")
        d.add_response("discard", "Discard")
        d.add_response("save", "Save")
        d.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_response_appearance("save", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("save")
        d.set_close_response("cancel")

        def answered(_d, r):
            if r == "discard":
                self.model.dirty = False
                self.close()
            elif r == "save":
                self.save(then=self.close)
        d.connect("response", answered)
        d.present(self)
        return True

    # ---- running

    def run(self):
        image = self.image or self.app.default_image()
        RunRecipeDialog(image, len(self.model.marks) > 1, self.model.doc.get("vars"), self._run).present(self)

    def _run(self, image, scope, dry, vars_):
        self.image = image
        selected = scope == "selected"
        tmp = self.model.run_file(self.cursor, selected)
        if tmp is None:
            self.say("no step selected", "warn")
            return
        image = image if image.startswith("/dev/") else os.path.abspath(os.path.expanduser(image))
        root = P.needs_root([(image, not dry)])
        title = f"{'Dry run' if dry else 'Run'}: {self.model.doc.get('name') or 'recipe'}" \
                + (" (selected steps)" if selected else "")

        def done(rc, dialog):
            try:
                os.unlink(tmp)
            except OSError:
                pass
            i = self.model.run_finished(rc, dialog.errors, selected)
            if i is not None:
                self.refresh_list(i)
            self.redraw_labels()
            if rc == 0:
                self.say("dry run finished without errors" if dry else "recipe applied", "ok")
            else:
                self.say(dialog.errors[-1].removeprefix("piimg: ") if dialog.errors
                         else f"failed (exit status {rc})", "error")
        RunDialog(title, self.model.run_argv(image, tmp, dry, vars_), abort="warn", root=root,
                  on_done=done).present(self)

