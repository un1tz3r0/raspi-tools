"""The page of an editor (frontend.editors): the target's current state, read
by a query when the page is shown, changed in place, and committed in one run.

Layouts: "list" (a row per item: inline controls on the row, the other
editable fields in its expanded part; add, remove and move where the editor
allows), "text" (one file in a source view) and "review" (rows to look at,
and a Commit that runs a command).

The window owns the running (queries go through its capture(), commits
through its run dialog and confirmation); this page draws the items, tracks
what changed, and calls back:

    win.editor_query(page)          read the state (again)
    win.editor_commit(page, dry)    run the changes (dry: a preview)
    win.editor_row_action(page, argv, label)
    win.editor_changed(page)        the dirty state may have changed
    win.ask_discard(page, then)     there are changes: commit, discard or stay
"""
from gi.repository import Adw, GLib, Gtk, Pango

from .dialogs import say
from .forms import Form, buffer_text, source_view


def _esc(text):
    return GLib.markup_escape_text(str(text or ""))


class EditorPage(Gtk.Box):
    def __init__(self, win, editor):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win, self.editor = win, editor
        self.rows = {}           # key -> row
        self.stale = True        # read again when next shown
        self.child = None        # the running query
        self._loading_text = False
        self._build()
        self._update()

    # ---- layout

    def _build(self):
        ed = self.editor
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=18, margin_start=18,
                       margin_end=18, margin_bottom=12)
        top = Gtk.Box(spacing=12)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, hexpand=True)
        titles.append(Gtk.Label(label=ed.title, xalign=0, css_classes=["title-2"]))
        hl = Gtk.Label(label=ed.help, xalign=0, wrap=True, css_classes=["dim-label"])
        hl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        titles.append(hl)
        top.append(titles)
        buttons = Gtk.Box(spacing=6, valign=Gtk.Align.START)
        self.add_btn = Gtk.Button(label=ed.add_label + "…", visible=ed.can_add)
        self.add_btn.connect("clicked", lambda _b: self.add_dialog())
        self.revert_btn = Gtk.Button(label="Revert", tooltip_text="Undo every change",
                                     visible=ed.layout != "review")
        self.revert_btn.connect("clicked", lambda _b: self.revert())
        self.preview_btn = Gtk.Button(label="Preview", visible=ed.can_preview,
                                      tooltip_text="Run the changes as a dry run: show what would be done, "
                                                   "change nothing")
        self.preview_btn.connect("clicked", lambda _b: self.win.editor_commit(self, dry=True))
        self.commit_btn = Gtk.Button(label="Commit…" if ed.layout == "review" else "Commit",
                                     css_classes=["suggested-action"],
                                     tooltip_text="Make the changes (Ctrl+Return)")
        self.commit_btn.connect("clicked", lambda _b: self.win.editor_commit(self))
        for b in (self.add_btn, self.revert_btn, self.preview_btn, self.commit_btn):
            buttons.append(b)
        top.append(buttons)
        head.append(top)

        bar = Gtk.Box(spacing=8)
        self.search = Gtk.SearchEntry(placeholder_text=f"Filter {ed.noun[1]}", hexpand=True,
                                      visible=ed.layout != "text")
        self.search.connect("search-changed", lambda _e: self._refilter())
        self.filter = Gtk.DropDown(model=Gtk.StringList.new([label for _, label in ed.filters]),
                                   tooltip_text="Which ones to show", visible=len(ed.filters) > 1)
        self.filter.connect("notify::selected", lambda *_: self._refilter())
        self.count = Gtk.Label(css_classes=["dim-label", "numeric"], hexpand=ed.layout == "text", xalign=0)
        self.reload_btn = Gtk.Button(icon_name="view-refresh-symbolic", css_classes=["flat"],
                                     tooltip_text="Read again")
        self.reload_btn.connect("clicked", lambda _b: self.reload())
        for w in (self.search, self.filter, self.count, self.reload_btn):
            bar.append(w)
        head.append(bar)
        self.problems = Gtk.Label(xalign=0, wrap=True, visible=False)
        self.problems.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        head.append(self.problems)
        self.append(head)
        self.append(Gtk.Separator())

        self.stack = Gtk.Stack(vexpand=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER)
        box.append(Adw.Spinner(width_request=32, height_request=32))
        self.loading_label = Gtk.Label(label="Reading…", css_classes=["dim-label"])
        box.append(self.loading_label)
        self.stack.add_named(box, "loading")
        self.status = Adw.StatusPage(icon_name="dialog-information-symbolic")
        self.status_btn = Gtk.Button(css_classes=["pill", "suggested-action"], halign=Gtk.Align.CENTER)
        self.status_btn.connect("clicked", lambda _b: self._status_action and self._status_action())
        self._status_action = None
        self.status.set_child(self.status_btn)
        self.stack.add_named(self.status, "status")
        if ed.layout == "text":
            self.view = source_view("", ed.language)
            self.view.get_buffer().connect("changed", self._text_changed)
            sw = Gtk.ScrolledWindow(child=self.view, css_classes=["card"], margin_top=12, margin_bottom=12,
                                    margin_start=18, margin_end=18)
            sw.set_overflow(Gtk.Overflow.HIDDEN)
            self.stack.add_named(sw, "list")
        else:
            self.list = Gtk.ListBox(css_classes=["boxed-list"], selection_mode=Gtk.SelectionMode.NONE,
                                    valign=Gtk.Align.START)
            self.list.set_filter_func(self._visible)
            self.list.set_placeholder(Gtk.Label(label="Nothing to show", margin_top=24, margin_bottom=24,
                                                css_classes=["dim-label"]))
            clamp = Adw.Clamp(child=self.list, maximum_size=1100, margin_top=12, margin_bottom=24,
                              margin_start=12, margin_end=12)
            self.scroller = Gtk.ScrolledWindow(child=clamp, hscrollbar_policy=Gtk.PolicyType.NEVER)
            self.stack.add_named(self.scroller, "list")
        self.append(self.stack)
        self.footer = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"], margin_start=18,
                                margin_bottom=6, margin_top=6, visible=False)
        self.append(self.footer)

    # ---- states

    def show_loading(self, text="Reading…"):
        self.loading_label.set_label(text)
        self.stack.set_visible_child_name("loading")
        self._update()

    def show_status(self, title, text="", button=None, icon="dialog-information-symbolic", action=None):
        self.status.set_title(_esc(title))
        self.status.set_description(_esc(text) if text else None)
        self.status.set_icon_name(icon)
        self.status_btn.set_visible(bool(button))
        if button:
            self.status_btn.set_label(button)
        self._status_action = action
        self.stack.set_visible_child_name("status")
        self._update()

    def show_items(self):
        ed = self.editor
        if ed.layout == "text":
            self._loading_text = True
            self.view.get_buffer().set_text(ed.text)
            self._loading_text = False
        else:
            self.list.remove_all()
            self.rows = {}
            for item in ed.items:
                row = self._make_row(item)
                self.rows[item.key] = row
                self.list.append(row)
        self.footer.set_label(ed.footer)
        self.footer.set_visible(bool(ed.footer))
        self.stack.set_visible_child_name("list")
        self.stale = False
        self._refilter()

    def listing(self):
        return self.stack.get_visible_child_name() == "list"

    # ---- rows

    def _make_row(self, item):
        ed = self.editor
        inline = [c for c in ed.columns if c.editable and c.inline]
        expand = [c for c in ed.columns if c.editable and not c.inline and ed.editable(item, c.key)]
        if expand and not item.removed:
            row = Adw.ExpanderRow(title=_esc(ed.label(item)), subtitle=_esc(ed.subtitle(item)))
            for c in expand:
                row.add_row(self._field_row(item, c))
        else:
            row = Adw.ActionRow(title=_esc(ed.label(item)), subtitle=_esc(ed.subtitle(item)),
                                subtitle_lines=2)
        row.item = item
        if item.get("unit file") or item.get("file"):
            row.set_tooltip_text(item.get("unit file") or item.get("file"))
        for c in inline:
            if ed.editable(item, c.key) and not item.removed:
                row.add_suffix(self._inline(item, c))
        if ed.ordered and ed.layout == "list":
            for icon, delta, tip in (("go-up-symbolic", -1, "Move up"), ("go-down-symbolic", 1, "Move down")):
                b = Gtk.Button(icon_name=icon, css_classes=["flat"], valign=Gtk.Align.CENTER, tooltip_text=tip)
                b.connect("clicked", lambda _b, d=delta: self.move(item.key, d))
                row.add_suffix(b)
        undo = Gtk.Button(icon_name="edit-undo-symbolic", css_classes=["flat"], valign=Gtk.Align.CENTER,
                          tooltip_text="Undo the changes to this one")
        undo.connect("clicked", lambda _b: self.revert(item.key))
        row.add_suffix(undo)
        row.undo = undo
        row.trash = None
        if ed.layout == "list" and (item.new or ed.removable(item)):
            trash = Gtk.Button(icon_name="user-trash-symbolic", css_classes=["flat"], valign=Gtk.Align.CENTER,
                               tooltip_text=f"{ed.remove_label} this one (on Commit)")
            trash.connect("clicked", lambda _b: self.remove(item.key))
            row.add_suffix(trash)
            row.trash = trash
        for label, icon, fn in ed.row_actions:
            b = Gtk.Button(icon_name=icon, css_classes=["flat"], valign=Gtk.Align.CENTER, tooltip_text=label)
            b.connect("clicked", lambda _b, fn=fn, label=label: self.win.editor_row_action(
                self, fn(self.win.work_path, item), f"{label}: {item.key}"))
            row.add_suffix(b)
        self._mark(row)
        return row

    def _inline(self, item, c):
        ed = self.editor
        kind = ed.kind(item, c.key)
        cur = item.get(c.key)
        if kind == "choice":
            choices = ed.choices(item, c.key)
            w = Gtk.DropDown(model=Gtk.StringList.new(choices), valign=Gtk.Align.CENTER,
                             tooltip_text=c.help or None)
            if cur in choices:
                w.set_selected(choices.index(cur))
            w.set_sensitive(len(choices) > 1)
            w.connect("notify::selected", lambda dd, _p: dd.get_selected() < len(choices)
                      and self._set(item, c.key, choices[dd.get_selected()]))
        elif kind == "switch":
            w = Gtk.Switch(active=cur == "on", valign=Gtk.Align.CENTER, tooltip_text=c.help or None)
            w.connect("notify::active", lambda sw, _p: self._set(item, c.key, "on" if sw.get_active() else "off"))
        else:
            w = Gtk.Entry(text=cur, valign=Gtk.Align.CENTER, width_chars=24, max_width_chars=48,
                          placeholder_text=c.placeholder or None, tooltip_text=c.help or None)
            w.connect("changed", lambda e: self._set(item, c.key, e.get_text()))
        w.column = c.key
        return w

    def _field_row(self, item, c):
        ed = self.editor
        kind = ed.kind(item, c.key)
        cur = item.get(c.key)
        if kind == "switch":
            r = Adw.SwitchRow(title=_esc(c.label), subtitle=_esc(c.help), active=cur == "on")
            r.connect("notify::active", lambda sw, _p: self._set(item, c.key, "on" if sw.get_active() else "off"))
        elif kind == "secret":
            r = Adw.PasswordEntryRow(title=_esc(c.label + (f" ({c.help})" if c.help else "")), text=cur)
            r.connect("changed", lambda e: self._set(item, c.key, e.get_text()))
        elif kind == "choice":
            choices = ed.choices(item, c.key)
            r = Adw.ComboRow(title=_esc(c.label), model=Gtk.StringList.new(choices))
            if cur in choices:
                r.set_selected(choices.index(cur))
            r.connect("notify::selected", lambda cr, _p: cr.get_selected() < len(choices)
                      and self._set(item, c.key, choices[cr.get_selected()]))
        elif kind == "lines":
            r = Adw.PreferencesRow(activatable=False, focusable=False, title=_esc(c.label))
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, margin_top=8, margin_bottom=10,
                          margin_start=12, margin_end=12)
            box.append(Gtk.Label(label=c.label, xalign=0, css_classes=["caption-heading"]))
            view = source_view(cur, None, wrap=True)
            view.get_buffer().connect("changed", lambda b: self._set(item, c.key, buffer_text(b)))
            sw = Gtk.ScrolledWindow(child=view, min_content_height=96, css_classes=["card"],
                                    hscrollbar_policy=Gtk.PolicyType.NEVER)
            sw.set_overflow(Gtk.Overflow.HIDDEN)
            box.append(sw)
            if c.help:
                box.append(Gtk.Label(label=c.help, xalign=0, wrap=True, css_classes=["dim-label", "caption"]))
            r.set_child(box)
        else:
            r = Adw.EntryRow(title=_esc(c.label), text=cur)
            r.connect("changed", lambda e: self._set(item, c.key, e.get_text()))
            suggestions = ed.choices(item, c.key)
            if suggestions:
                pop = Gtk.Popover()
                lb = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE, css_classes=["menu"])
                for s in suggestions:
                    lb.append(Gtk.Label(label=s, xalign=0, margin_start=6, margin_end=6, margin_top=4,
                                        margin_bottom=4))
                lb.connect("row-activated", lambda _l, rw, e=r, p=pop: (e.set_text(rw.get_child().get_label()),
                                                                         p.popdown()))
                pop.set_child(lb)
                r.add_suffix(Gtk.MenuButton(icon_name="view-more-symbolic", popover=pop, valign=Gtk.Align.CENTER,
                                            css_classes=["flat"], tooltip_text="Suggestions"))
            if c.help:
                r.set_tooltip_text(c.help)
        r.column = c.key
        return r

    def _set(self, item, col, value):
        if item.get(col) == value:
            return
        try:
            self.editor.set(item.key, col, value)
        except (KeyError, ValueError) as e:
            self.win.toast(str(e))
            return
        row = self.rows.get(item.key)
        if row is not None:
            self._mark(row)
        self._update()

    def _mark(self, row):
        ed, item = self.editor, row.item
        changed = item.changed()
        row.undo.set_visible(changed)
        if row.trash is not None:
            row.trash.set_visible(not item.removed)
        for cls, on in (("changed", changed and not item.removed), ("removed", item.removed)):
            (row.add_css_class if on else row.remove_css_class)(cls)
        title = _esc(ed.label(item))
        if item.removed:
            title = f"<s>{title}</s>  <span size='small' alpha='70%'>({_esc(ed.remove_label.lower())} on commit)</span>"
        elif item.new:
            title += "  <span size='small' alpha='70%'>(new)</span>"
        elif changed:
            was = [c.label.lower() for c in ed.columns if ed.editable(item, c.key) and item.changed(c.key)]
            title += f"  <span size='small' alpha='70%'>({_esc(', '.join(was))} changed)</span>"
        row.set_title(title)

    def _replace_row(self, key):
        """Draw one item again (after a revert), or take its row away (a new item reverted)."""
        old = self.rows.pop(key, None)
        item = self.editor.by_key.get(key)
        if old is None:
            return
        idx = old.get_index()
        self.list.remove(old)
        if item is not None:
            row = self._make_row(item)
            self.rows[key] = row
            self.list.insert(row, idx)

    # ---- filtering

    def filter_id(self):
        i = self.filter.get_selected()
        filters = self.editor.filters
        return filters[i][0] if 0 <= i < len(filters) else filters[0][0]

    def _visible(self, row):
        return self.editor.matches(row.item, self.search.get_text(), self.filter_id())

    def _refilter(self):
        if self.editor.layout != "text":
            self.list.invalidate_filter()
        self._update()

    # ---- changes

    def _text_changed(self, buf):
        if self._loading_text:
            return
        self.editor.text = buffer_text(buf)
        self._update()

    def revert(self, key=None):
        ed = self.editor
        ed.revert(key)
        if ed.layout == "text":
            self.show_items()
        elif key is None:
            self.show_items()
        else:
            self._replace_row(key)
        self._update()

    def remove(self, key):
        self.editor.remove(key)
        self._replace_row(key)   # drawn again without controls (or gone, for a new item)
        self._update()

    def move(self, key, delta):
        if not self.editor.move(key, delta):
            return
        row = self.rows[key]
        idx = row.get_index()
        self.list.remove(row)
        self.list.insert(row, idx + delta)
        self._update()

    def add_dialog(self):
        ed = self.editor
        d = Adw.Dialog(title=ed.add_label, content_width=520)
        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda _b: d.close())
        ok = Gtk.Button(label="Add", css_classes=["suggested-action"])
        header.pack_start(cancel)
        header.pack_end(ok)
        hint = Gtk.Label(xalign=0, wrap=True, margin_start=12, margin_end=12, margin_top=8, margin_bottom=8,
                         css_classes=["dim-label"])
        form = Form(ed.add_fields(), on_focus=lambda f: say(hint, f.explain()))
        page = Adw.PreferencesPage()
        page.add(form.widget)
        tv = Adw.ToolbarView(content=page)
        tv.add_top_bar(header)
        tv.add_bottom_bar(hint)
        d.set_child(tv)
        d.set_default_widget(ok)

        def add(_b=None):
            missing = form.check_required()
            if missing:
                say(hint, f"{missing.label} is required", "error")
                return
            try:
                item = ed.add(form.values())
            except ValueError as e:
                say(hint, str(e), "error")
                return
            d.close()
            row = self._make_row(item)
            self.rows[item.key] = row
            self.list.insert(row, ed.items.index(item))
            self._update()
        ok.connect("clicked", add)
        d.present(self.win)
        GLib.idle_add(lambda: form.focus_first() and False)
        self.add_form = (d, form, add)
        return d

    def reload(self):
        if self.editor.dirty:
            self.win.ask_discard(self, lambda: self.win.editor_query(self))
            return
        self.win.editor_query(self)

    def _update(self):
        ed = self.editor
        dirty = ed.dirty
        listing = self.listing()
        problems = ed.problems() if dirty else []
        say(self.problems, "\n".join(problems), "error")
        self.problems.set_visible(bool(problems))
        if ed.layout == "review":
            self.commit_btn.set_sensitive(listing and bool(ed.items))
        else:
            self.commit_btn.set_sensitive(dirty and listing and not problems)
        self.preview_btn.set_sensitive(dirty and listing and not problems)
        self.revert_btn.set_sensitive(dirty and listing)
        self.add_btn.set_sensitive(listing)
        if ed.loaded and listing:
            if ed.layout == "text":
                self.count.set_label(ed.count_text())
            else:
                shown = sum(1 for i in ed.items if ed.matches(i, self.search.get_text(), self.filter_id()))
                total = ed.count_text()
                self.count.set_label(total if shown == len(ed.items) else f"{shown} shown of {total}")
        else:
            self.count.set_label("")
        self.win.editor_changed(self)
