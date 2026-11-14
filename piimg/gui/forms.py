"""Forms: frontend.fields.FieldDef -> libadwaita preference rows.

    str     Adw.EntryRow (a menu of suggestions, a file chooser for paths)
    secret  Adw.PasswordEntryRow
    int     Adw.EntryRow for digits (it may be left empty)
    bool    Adw.SwitchRow
    choice  Adw.ComboRow ("(not set)" first unless required)
    lines, map, text, yaml
            a row holding a source view (YAML highlighted)

Help for the focused field goes to on_focus(field); required fields get the
"error" style until they have a value.
"""
from gi.repository import Adw, GLib, Gtk, GtkSource, Pango

from ..frontend.fields import from_text, to_text

NOT_SET = "(not set)"
FILE_KINDS = {"file": "open", "image": "open", "recipe": "open", "newfile": "save", "dir": "folder"}


def source_view(text="", language=None, monospace=True, wrap=False, editable=True):
    """A GtkSource.View with a buffer, following the light/dark style."""
    buf = GtkSource.Buffer()
    if language:
        lang = GtkSource.LanguageManager.get_default().get_language(language)
        if lang:
            buf.set_language(lang)
    _follow_style(buf)
    buf.set_text(text or "")
    view = GtkSource.View(buffer=buf, monospace=monospace, editable=editable,
                          wrap_mode=Gtk.WrapMode.WORD_CHAR if wrap else Gtk.WrapMode.NONE,
                          top_margin=6, bottom_margin=6, left_margin=8, right_margin=8,
                          auto_indent=True, insert_spaces_instead_of_tabs=True, tab_width=2,
                          indent_width=2)
    return view


def _follow_style(buf):
    sm = Adw.StyleManager.get_default()
    schemes = GtkSource.StyleSchemeManager.get_default()

    def apply(*_):
        scheme = schemes.get_scheme("Adwaita-dark" if sm.get_dark() else "Adwaita")
        if scheme:
            buf.set_style_scheme(scheme)
    apply()
    sm.connect("notify::dark", apply)


def buffer_text(buf):
    return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)


class TextRow(Adw.PreferencesRow):
    """A titled multi-line box in a preferences group."""

    def __init__(self, f):
        super().__init__(title=f.label, activatable=False, focusable=False)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=8, margin_bottom=10,
                      margin_start=12, margin_end=12)
        self.title_label = Gtk.Label(xalign=0, css_classes=["caption-heading"])
        box.append(self.title_label)
        self.view = source_view(to_text(f, f.value), "yaml" if f.type == "yaml" else None,
                                monospace=f.type != "text", wrap=f.type == "text")
        tall = f.type in ("text", "yaml") and not f.data.get("short")
        sw = Gtk.ScrolledWindow(child=self.view, min_content_height=220 if tall else 96,
                                hscrollbar_policy=Gtk.PolicyType.AUTOMATIC, css_classes=["card"])
        sw.set_overflow(Gtk.Overflow.HIDDEN)
        box.append(sw)
        hint = {"lines": "one per line", "map": "KEY=VALUE, one per line"}.get(f.type)
        if hint:
            box.append(Gtk.Label(label=hint, xalign=0, css_classes=["dim-label", "caption"]))
        self.set_child(box)
        self.set_title_text(f.label, f.required)

    def set_title_text(self, label, required):
        self.title_label.set_markup(GLib.markup_escape_text(label) + (" <span foreground='#e01b24'>*</span>"
                                                                      if required else ""))

    @property
    def buffer(self):
        return self.view.get_buffer()


class Form:
    """A preferences group of rows, one per field. .widget is the group."""

    def __init__(self, fields, on_change=None, on_focus=None, title=None, description=None):
        self.fields = list(fields)
        self.on_change, self.on_focus = on_change, on_focus
        self.widget = Adw.PreferencesGroup(title=GLib.markup_escape_text(title or ""),
                                           description=description or None)
        self.rows = {}
        self.focusables = {}
        for f in self.fields:
            row, focus = self._make(f)
            row.set_tooltip_text(f.help or None)
            self.rows[f.key] = row
            self.focusables[f.key] = focus
            fc = Gtk.EventControllerFocus()
            fc.connect("enter", lambda _c, f=f: self.on_focus and self.on_focus(f))
            row.add_controller(fc)
            self.widget.add(row)
        self.check_required()

    # ---- building

    def _make(self, f):
        v = f.value
        title = GLib.markup_escape_text(f.label) + (" *" if f.required else "")
        if f.type == "bool":
            row = Adw.SwitchRow(title=title, subtitle=GLib.markup_escape_text(f.help or ""), active=bool(v))
            row.connect("notify::active", self._changed, f)
            return row, row
        if f.type == "choice":
            items = [str(c) for c in f.choices]
            if not f.required:
                items = [NOT_SET] + items
            row = Adw.ComboRow(title=title, model=Gtk.StringList.new(items),
                               subtitle=GLib.markup_escape_text(f.placeholder or ""))
            sv = str(v) if v not in (None, "") else None
            if sv in items:
                row.set_selected(items.index(sv))
            elif f.required and items:
                row.set_selected(0)
            row.connect("notify::selected", self._changed, f)
            return row, row
        if f.multiline:
            row = TextRow(f)
            row.buffer.connect("changed", self._changed, None, f)
            return row, row.view
        cls = Adw.PasswordEntryRow if f.type == "secret" else Adw.EntryRow
        row = cls(title=title)
        if f.type == "int":
            row.set_input_purpose(Gtk.InputPurpose.DIGITS)
        row.set_text("" if v is None else str(v))
        if f.placeholder and not row.get_text():
            row.set_tooltip_text(f.placeholder)
        kind = f.data.get("kind")
        if f.suggestions:
            row.add_suffix(self._suggest_button(f, lambda s, row=row: row.set_text(s)))
        if kind in FILE_KINDS:
            row.add_suffix(self._file_button(f, row, FILE_KINDS[kind]))
        row.connect("changed", self._changed, None, f)
        return row, row

    def _suggest_button(self, f, choose):
        lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["boxed-list"])
        for s in f.suggestions:
            lb.append(Gtk.Label(label=s, xalign=0, margin_top=6, margin_bottom=6, margin_start=8,
                                margin_end=8, ellipsize=Pango.EllipsizeMode.MIDDLE))
        sw = Gtk.ScrolledWindow(child=lb, max_content_height=320, propagate_natural_height=True,
                                propagate_natural_width=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        pop = Gtk.Popover(child=sw)
        btn = Gtk.MenuButton(icon_name="view-list-symbolic", popover=pop, valign=Gtk.Align.CENTER,
                             tooltip_text="Suggestions", css_classes=["flat"])

        def activated(_lb, row):
            choose(row.get_child().get_label())
            pop.popdown()
        lb.set_activate_on_single_click(True)
        lb.connect("row-activated", activated)
        return btn

    def _file_button(self, f, row, mode):
        btn = Gtk.Button(icon_name="folder-open-symbolic" if mode != "save" else "document-save-as-symbolic",
                         valign=Gtk.Align.CENTER, css_classes=["flat"], tooltip_text="Choose…")

        def done(dialog, result):
            try:
                gfile = {"open": dialog.open_finish, "save": dialog.save_finish,
                         "folder": dialog.select_folder_finish}[mode](result)
            except GLib.Error:
                return  # cancelled
            if gfile is not None and gfile.get_path():
                row.set_text(gfile.get_path())

        def clicked(_b):
            dialog = Gtk.FileDialog(title=f.label, modal=True)
            win = row.get_root()
            {"open": dialog.open, "save": dialog.save, "folder": dialog.select_folder}[mode](win, None, done)
        btn.connect("clicked", clicked)
        return btn

    # ---- values

    def value(self, key):
        """The field's current value: str, bool, list (lines) or dict (map); '' if empty."""
        f = next(x for x in self.fields if x.key == key)
        row = self.rows[key]
        if f.type == "bool":
            return row.get_active()
        if f.type == "choice":
            item = row.get_selected_item()
            s = item.get_string() if item is not None else ""
            return "" if s == NOT_SET else s
        if f.multiline:
            return from_text(f, buffer_text(row.buffer))
        return row.get_text()

    def values(self):
        return {f.key: self.value(f.key) for f in self.fields}

    def set_value(self, key, value):
        f = next(x for x in self.fields if x.key == key)
        row = self.rows[key]
        if f.type == "bool":
            row.set_active(bool(value))
        elif f.type == "choice":
            items = [row.get_model().get_string(i) for i in range(row.get_model().get_n_items())]
            if str(value) in items:
                row.set_selected(items.index(str(value)))
        elif f.multiline:
            row.buffer.set_text(to_text(f, value))
        else:
            row.set_text("" if value is None else str(value))

    # ---- focus and checks

    def focus_first(self):
        for f in self.fields:
            w = self.focusables[f.key]
            if w.get_sensitive() and w.grab_focus():
                return True
        return False

    def focus(self, key):
        w = self.focusables.get(key)
        if w is not None:
            w.grab_focus()

    def check_required(self):
        """Mark empty required fields; returns the first one, or None."""
        first = None
        for f in self.fields:
            if not f.required or f.type == "bool":
                continue
            empty = f.is_empty(self.value(f.key))
            row = self.rows[f.key]
            (row.add_css_class if empty else row.remove_css_class)("error")
            if empty and first is None:
                first = f
        return first

    def _changed(self, *args):
        f = args[-1]
        if f.required:
            self.check_required()
        if self.on_change:
            self.on_change(f)
