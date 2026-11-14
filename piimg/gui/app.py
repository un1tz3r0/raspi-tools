"""The application and its main window: commands on the left; on the right the
selected command's help, options and the open target's summary, or the page
of an editor (frontend.editors: read, change, commit)."""
import os
import signal

from gi.repository import Adw, Gio, GLib, Gtk, Pango

from ..disks import is_block_device
from ..frontend import commands as C
from ..frontend import editors as E
from ..frontend import proc as P
from ..frontend import target as T
from . import APP_ID
from .dialogs import CommandForm, confirm, say
from .editor import EditorPage
from .markup import to_markup
from .opentarget import OpenDialog, remember
from .run import Child, RunDialog
from .terminal import TerminalDialog

CSS = """
.command-help { font-size: 0.95em; }
.target-banner { padding: 6px 12px; }
.data-table { font-size: 0.95em; }
.step-list row { padding: 2px 6px; }
.step-list row.marked { background-color: alpha(@accent_bg_color, 0.18); }
.step-list row.error-step { box-shadow: inset 3px 0 @error_color; }
.help-pane { padding: 12px; }
row.changed { box-shadow: inset 3px 0 @accent_color; }
"""


def capture(argv, on_done, root=False, timeout=180):
    """Run a piimg command quietly; on_done(rc, stdout lines, stderr lines without events).
    Returns the Child (child.signal() stops it)."""
    out, err = [], []
    state = {"done": False}

    def finished(rc):
        if not state["done"]:
            state["done"] = True
            on_done(rc, out, err)

    child = Child(argv, out.append, lambda l: None if P.is_event(l) else err.append(l), finished, root=root)

    def too_slow():
        if child.rc is None:
            child.signal(signal.SIGTERM)
        return False
    GLib.timeout_add_seconds(timeout, too_slow)
    return child


def _rows(listbox):
    child = listbox.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.ListBoxRow):
            yield child
        child = child.get_next_sibling()


HIDDEN = ("config",)     # in the main menu instead: Preferences


def is_editor(c):
    return getattr(c, "is_editor", False)


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app, target=None, overlay=None):
        super().__init__(application=app, title="piimg", default_width=1180, default_height=780)
        self.set_size_request(360, 400)
        self.app = app
        self.target, self.overlay = None, None
        self.all_cmds = C.load_commands()
        self.editors = E.make_editors()
        # the list: editors stand in for the commands they replace (services + service)
        self.cmds = E.list_entries(self.all_cmds, self.editors, hide=HIDDEN)
        self.pages = {}           # editor name -> EditorPage
        self.last_values = {}
        self.current = None       # the selected Command
        self.cmd_form = None      # its CommandForm (None if it has no options)
        self.info_child = None
        self.info_rows = []
        self._build()
        self._actions()
        self.connect("close-request", self._close_request)
        self.fill_commands()
        if target:
            GLib.idle_add(lambda: self.open_target(target, overlay) and False)
        else:
            self.show_target()
            GLib.idle_add(lambda: self.action_open() and False)

    # ---- layout

    def _build(self):
        self.toasts = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView(min_sidebar_width=260, max_sidebar_width=380,
                                             sidebar_width_fraction=0.3)
        self.toasts.set_child(self.split)
        self.set_content(self.toasts)

        # sidebar: the commands
        sh = Adw.HeaderBar()
        sh.set_title_widget(Adw.WindowTitle(title="piimg", subtitle="Raspberry Pi OS images and cards"))
        menu = Gio.Menu()
        sec = Gio.Menu()
        sec.append("Open…", "win.open")
        sec.append("Recipe Editor…", "win.recipe-editor")
        sec.append("Refresh Target Info", "win.refresh-info")
        menu.append_section(None, sec)
        sec = Gio.Menu()
        sec.append("Preferences", "app.preferences")
        sec.append("Keyboard Shortcuts", "app.shortcuts")
        sec.append("About piimg", "app.about")
        sec.append("Quit", "app.quit")
        menu.append_section(None, sec)
        sh.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True,
                                   tooltip_text="Main Menu"))
        self.search = Gtk.SearchEntry(placeholder_text="Filter commands", margin_start=8, margin_end=8,
                                      margin_top=4, margin_bottom=4)
        self.search.connect("search-changed", lambda e: self.fill_commands())
        self.search.connect("activate", lambda e: self._activate_first())
        self.search.connect("stop-search", lambda e: (e.set_text(""), self.list.grab_focus()))
        # a click selects a command and shows its options on the right; Enter or a
        # double click moves there (it does not run it: that is the Run button)
        self.list = Gtk.ListBox(css_classes=["navigation-sidebar"], selection_mode=Gtk.SelectionMode.SINGLE,
                                activate_on_single_click=False)
        self.list.connect("row-selected", self._row_selected)
        self.list.connect("row-activated", lambda _l, row: self.go_to_form())
        sidebar = Adw.ToolbarView(content=Gtk.ScrolledWindow(child=self.list, vexpand=True,
                                                             hscrollbar_policy=Gtk.PolicyType.NEVER))
        sidebar.add_top_bar(sh)
        sidebar.add_top_bar(self.search)
        self.split.set_sidebar(Adw.NavigationPage(child=sidebar, title="Commands", tag="commands"))

        # content: the command and the target
        ch = Adw.HeaderBar()
        self.target_title = Adw.WindowTitle(title="No target", subtitle="")
        ch.set_title_widget(self.target_title)
        open_btn = Gtk.Button(icon_name="document-open-symbolic", tooltip_text="Open a card or image (Ctrl+O)",
                              action_name="win.open")
        ch.pack_start(open_btn)
        edit_btn = Gtk.Button(icon_name="document-edit-symbolic", tooltip_text="Recipe editor (Ctrl+E)",
                              action_name="win.recipe-editor")
        ch.pack_end(edit_btn)
        self.banner = Adw.Banner(revealed=False)
        self.banner.connect("button-clicked", lambda _b: self.action_open())

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18, margin_top=18, margin_bottom=24,
                      margin_start=18, margin_end=18)
        self.cmd_title = Gtk.Label(xalign=0, css_classes=["title-2"], wrap=True, hexpand=True)
        self.run_btn = Gtk.Button(label="Run", css_classes=["suggested-action", "pill"], valign=Gtk.Align.CENTER,
                                  tooltip_text="Run the command (Ctrl+Return)")
        self.run_btn.connect("clicked", lambda _b: self.run_current())
        head = Gtk.Box(spacing=12)
        head.append(self.cmd_title)
        head.append(self.run_btn)
        box.append(head)
        # the description and usage; the options are in the form below it
        self.cmd_help = Gtk.Label(xalign=0, wrap=True, selectable=True, css_classes=["command-help"])
        self.cmd_help.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        box.append(self.cmd_help)
        self.form_slot = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.form_slot)
        # help for the focused option, and why the form cannot run yet
        self.field_help = Gtk.Label(xalign=0, wrap=True, visible=False, css_classes=["dim-label"])
        self.field_help.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        box.append(self.field_help)

        self.info_group = Adw.PreferencesGroup(title="Target")
        self.info_spinner = Adw.Spinner(visible=False)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Read the target again (Ctrl+R)",
                             action_name="win.refresh-info", css_classes=["flat"])
        suffix = Gtk.Box(spacing=6)
        suffix.append(self.info_spinner)
        suffix.append(refresh)
        self.info_group.set_header_suffix(suffix)
        box.append(self.info_group)
        clamp = Adw.Clamp(child=box, maximum_size=900, tightening_threshold=600)
        # a command's page, or an editor's (added when first shown)
        self.content_stack = Gtk.Stack(vexpand=True)
        self.content_stack.add_named(Gtk.ScrolledWindow(child=clamp, vexpand=True,
                                                        hscrollbar_policy=Gtk.PolicyType.NEVER), "command")
        content = Adw.ToolbarView(content=self.content_stack)
        content.add_top_bar(ch)
        content.add_top_bar(self.banner)
        self.content_page = Adw.NavigationPage(child=content, title="Command", tag="command")
        self.split.set_content(self.content_page)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

    def _actions(self):
        for name, cb in (("open", lambda *_: self.action_open()),
                         ("recipe-editor", lambda *_: self.app.recipe_picker(self)),
                         ("refresh-info", lambda *_: self.load_info(force=True)),
                         ("search", lambda *_: self.search.grab_focus()),
                         ("run-command", lambda *_: self.run_current()),
                         ("close", lambda *_: self.close())):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)

    # ---- the command list

    def fill_commands(self):
        keep = self.current
        self.list.remove_all()
        group = None
        first = None
        needle = self.search.get_text()
        pairs = C.visible(self.cmds, needle, bool(self.overlay))
        if self._dirty_page(keep) and all(c is not keep for _g, c in pairs):
            pairs.insert(0, (keep.meta.group, keep))   # an editor with changes stays listed
        for g, c in pairs:
            row = Gtk.ListBoxRow()
            row.cmd = c
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, margin_top=4, margin_bottom=4)
            name = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
            name.set_markup(self._entry_markup(c))
            row.name_label = name
            summ = Gtk.Label(label=c.summary(), xalign=0, ellipsize=Pango.EllipsizeMode.END,
                             css_classes=["dim-label", "caption"])
            box.append(name)
            box.append(summ)
            row.set_child(box)
            row.set_tooltip_text(c.summary())
            if g != group:
                group = g
                row.group_title = g
            self.list.append(row)
            if first is None or (keep is not None and c is keep):
                first = row
        self.list.set_header_func(self._header)
        if first is not None:
            self.list.select_row(first)

    @staticmethod
    def _header(row, before):
        title = getattr(row, "group_title", None)
        if title:
            lb = Gtk.Label(label=title, xalign=0, margin_top=12 if before else 6, margin_start=12,
                           margin_bottom=4, css_classes=["heading", "dim-label"])
            row.set_header(lb)
        else:
            row.set_header(None)

    def _activate_first(self):
        row = self.list.get_selected_row() or self.list.get_row_at_index(0)
        if row is not None:
            self.list.select_row(row)
            self.go_to_form()

    def go_to_form(self):
        """Show the selected command's page and focus its first option (or Run)."""
        self.split.set_show_content(True)
        if is_editor(self.current):
            page = self.pages[self.current.name]
            (page.view if self.current.layout == "text" else page.search).grab_focus()
        elif not (self.cmd_form and self.cmd_form.focus_first()):
            self.run_btn.grab_focus()

    @staticmethod
    def _entry_markup(c):
        m = f"<b>{GLib.markup_escape_text(c.name)}</b>"
        if is_editor(c):
            m += "  <span alpha='60%'>editor</span>"
            if c.dirty:
                m += "  <b>● changed</b>"
        return m

    def _row_selected(self, _l, row):
        if row is None or getattr(self, "_selecting_back", False):
            return
        c = row.cmd
        page = self._dirty_page(self.current)
        if c is not self.current and page is not None:
            # leaving an editor with changes: stay on it, and ask
            back = next((r for r in _rows(self.list) if r.cmd is self.current), None)
            if back is not None:
                self._selecting_back = True
                self.list.select_row(back)
                self._selecting_back = False
            self.ask_discard(page, lambda: self.list.select_row(row) if row.get_parent() else None)
            return
        if is_editor(c):
            self._show_editor(c)
            return
        self.content_stack.set_visible_child_name("command")
        if c is not self.current:
            self.remember_form()
            self.current = c
            self.cmd_title.set_label(f"piimg {c.name}")
            self.cmd_help.set_markup(to_markup(C.command_help(c, title=False, options=False)).rstrip("\n"))
            self.say("")
            if self.cmd_form is not None:
                self.form_slot.remove(self.cmd_form.widget)
            self.cmd_form = CommandForm(c, self.last_values.get(c.name), self.say) if c.has_form() else None
            if self.cmd_form is not None:
                self.form_slot.append(self.cmd_form.widget)
            self.content_page.set_title(c.name)
        ready = not c.meta.target or self.work_path is not None
        self.run_btn.set_sensitive(ready)
        self.run_btn.set_tooltip_text("Run the command (Ctrl+Return)" if ready else "Open a card or image first")

    def say(self, text, kind=""):
        say(self.field_help, text, kind)
        self.field_help.set_visible(bool(text))

    def _show_editor(self, ed):
        if self.cmd_form is not None:
            self.remember_form()
            self.form_slot.remove(self.cmd_form.widget)
            self.cmd_form = None
        self.current = ed
        page = self.page_for(ed)
        self.content_stack.set_visible_child(page)
        self.content_page.set_title(ed.title)
        if page.stale and not ed.dirty:
            self.editor_query(page)

    def remember_form(self):
        """Keep what was typed in the current form (not secrets) for when it comes back."""
        if self.cmd_form is not None:
            self.last_values[self.current.name] = self.cmd_form.kept_values()

    # ---- the target

    @property
    def work_path(self):
        return self.overlay or self.target

    @property
    def direct_card(self):
        return self.target is not None and not self.overlay and is_block_device(self.target)

    def action_open(self):
        OpenDialog(self.open_target, current=self.target).present(self)

    def open_target(self, path, overlay):
        page = self._dirty_page()
        if page is not None:
            self.ask_discard(page, lambda: self.open_target(path, overlay))
            return
        from ..errors import PiImgError
        from ..image import ensure_overlay
        path = T.absolute(path)
        if not os.path.exists(path):
            self.toast(f"No such file or device: {path}")
            return
        if overlay:
            overlay = T.absolute(overlay)
            if not os.path.exists(overlay) and not T.can_open(path, False):
                # creating it reads the card: do that as root, then open it
                def made(rc, dialog):
                    if rc == 0:
                        dialog.close()
                        self.open_target(path, overlay)
                RunDialog("Create the overlay", ["overlay", "--", path, overlay], root=True,
                          on_done=made).present(self)
                return
            try:
                overlay = str(ensure_overlay(path, overlay))
            except PiImgError as e:
                self.toast(str(e))
                return
        self.target, self.overlay = path, overlay
        remember(path)
        if overlay:
            remember(overlay)
        self.show_target()
        self.fill_commands()
        self.load_info()

    def show_target(self):
        if self.target is None:
            self.target_title.set_title("No target")
            self.target_title.set_subtitle("open a card or an image")
            self.set_title("piimg")
            self.banner.set_title("No card or image is open: commands that work on one are disabled")
            self.banner.set_button_label("Open…")
            self.banner.set_revealed(True)
            self.show_info([], None)
            self.editors_stale()
            return
        name = os.path.basename(self.target) if not self.target.startswith("/dev/") else self.target
        self.set_title(f"{name} — piimg")
        self.target_title.set_title(name + (f" → {os.path.basename(self.overlay)}" if self.overlay else ""))
        self.target_title.set_subtitle(T.describe_target(self.target, self.overlay).split("  (", 1)[-1]
                                       .rstrip(")"))
        if self.direct_card:
            self.banner.set_title(f"Changes are written to the card {self.target} directly")
            self.banner.set_button_label("Use an Overlay…")
            self.banner.add_css_class("error")
            self.banner.set_revealed(True)
        else:
            self.banner.set_revealed(False)
        self.editors_stale()
        if self.current:
            self._row_selected(None, self.list.get_selected_row())

    def show_info(self, rows, error=None, note=None):
        for r in self.info_rows:
            self.info_group.remove(r)
        self.info_rows = []

        def add(row):
            self.info_group.add(row)
            self.info_rows.append(row)
        if self.target is None:
            row = Adw.ActionRow(title="Nothing open")
            btn = Gtk.Button(label="Open…", valign=Gtk.Align.CENTER, action_name="win.open")
            row.add_suffix(btn)
            add(row)
            return
        if note:
            row = Adw.ActionRow(title=GLib.markup_escape_text(note))
            if note.startswith("Reading this card"):
                btn = Gtk.Button(label="Read", valign=Gtk.Align.CENTER, css_classes=["suggested-action"])
                btn.connect("clicked", lambda _b: self.load_info(force=True, root_ok=True))
                row.add_suffix(btn)
            add(row)
        if error:
            row = Adw.ActionRow(title="Cannot read the target", subtitle=GLib.markup_escape_text(error),
                                subtitle_selectable=True)
            row.add_css_class("error")
            add(row)
        for item, value in rows:
            row = Adw.ActionRow(title=GLib.markup_escape_text(str(item)),
                                subtitle=GLib.markup_escape_text(str(value)), subtitle_selectable=True)
            row.add_css_class("property")
            add(row)

    def _info_needs_root(self):
        if self.target is None or not is_block_device(self.target):
            return False
        return not T.can_open(self.target, False)

    def load_info(self, force=False, root_ok=False):
        if self.target is None:
            return
        self.cancel_info()
        root = self._info_needs_root()
        if root and not root_ok:
            self.show_info([], None, note="Reading this card needs your password")
            return
        self.info_spinner.set_visible(True)
        self.show_info([], None, note="Reading…")

        def done(rc, out, err):
            if self.info_child is not child:
                return
            self.info_child = None
            self.info_spinner.set_visible(False)
            table = P.first_table(out)
            if rc or table is None:
                msg = (err[-1] if err else f"exit status {rc}").removeprefix("piimg: ")
                self.show_info([], msg)
            else:
                self.show_info(table["rows"])
        try:
            child = capture(["info", self.work_path], done, root=root)
        except (OSError, P.RootUnavailable) as e:
            self.info_spinner.set_visible(False)
            self.show_info([], str(e))
            return
        self.info_child = child

    def cancel_info(self):
        """Stop reading the summary: it holds the image open, and a command that
        writes would find it locked."""
        if self.info_child is not None:
            self.info_child.signal(signal.SIGINT)
            self.info_child = None
            self.info_spinner.set_visible(False)

    # ---- running a command

    def toast(self, text, timeout=5):
        self.toasts.add_toast(Adw.Toast(title=GLib.markup_escape_text(text), timeout=timeout))

    def start_command(self, cmd):
        """Select cmd (if it is not already) and run it with the options in its form."""
        if cmd is not self.current:
            row = next((r for r in _rows(self.list) if r.cmd is cmd), None)
            if row is None:
                return
            self.list.select_row(row)
        self.run_current()

    def run_current(self):
        cmd = self.current
        if cmd is None:
            return
        if is_editor(cmd):
            self.editor_commit(self.pages[cmd.name])
            return
        target = self.work_path if cmd.meta.target else None
        if cmd.meta.target and target is None:
            self.toast("Open a card or image first")
            self.action_open()
            return
        if self.cmd_form is not None:
            got = self.cmd_form.collect(target)
            if got is None:
                return
            argv, stdin, args, values = got
            self.last_values[cmd.name] = values
        else:
            argv, stdin = C.build_argv(cmd, {}, target)
            try:
                args = C.parse(argv)
            except C.ArgError as e:
                self.say(str(e), "error")
                return
        self.say("")
        self._confirm_and_run(cmd, argv, stdin, args)

    def _confirm_and_run(self, cmd, argv, stdin, args):
        writes = cmd.meta.writes_for(args)
        try:
            conf = C.confirmation(cmd, argv, args, self.target if self.direct_card else None)
        except C.ArgError as e:
            self.toast(str(e))
            return
        if conf.needed:
            confirm(self, f"piimg {cmd.name}", conf.messages, conf.typed,
                    on_ok=lambda: self._run(cmd, conf.argv, stdin, args, writes))
        else:
            self._run(cmd, conf.argv, stdin, args, writes)

    def needs_root(self, cmd, args, writes):
        pairs = []
        if cmd.meta.target and self.target:
            pairs.append((self.target, False) if self.overlay else (self.target, writes))
        for a in cmd.actions():
            if getattr(a, "kind", None) == "device" and getattr(args, a.dest, None):
                pairs.append((getattr(args, a.dest), True))
        return P.needs_root(pairs)

    def _run(self, cmd, argv, stdin, args, writes, after=None, title=None):
        """Run a command in the run dialog (or a terminal); after(rc) when it ends."""
        self.cancel_info()
        root = self.needs_root(cmd, args, writes)
        title = title or f"piimg {cmd.name}"

        def done(rc, *_):
            if rc == 0 and after is None:
                self.toast(f"{cmd.name}: done")
            if writes or cmd.meta.output == "terminal":
                self.editors_stale()
                self.load_info(root_ok=root)
            elif self.info_child is None and not self.info_rows_loaded():
                self.load_info()
            if after is not None:
                after(rc)
        if cmd.meta.output == "terminal":
            TerminalDialog(title, argv, root=root, on_done=done).present(self)
            return
        RunDialog(title, argv, stdin, cmd.meta.output, cmd.meta.abort, root=root, on_done=done).present(self)

    def info_rows_loaded(self):
        return any(isinstance(r, Adw.ActionRow) and "property" in r.get_css_classes() for r in self.info_rows)

    def find_cmd(self, name):
        return next(c for c in self.all_cmds if c.name == name)

    # ---- editors

    def page_for(self, ed):
        page = self.pages.get(ed.name)
        if page is None:
            page = self.pages[ed.name] = EditorPage(self, ed)
            self.content_stack.add_named(page, f"editor:{ed.name}")
        return page

    def _dirty_page(self, ed=None):
        """The page of `ed` (or of any editor) if it has changes, else None."""
        for e in ([ed] if ed is not None else self.editors):
            if is_editor(e) and e.dirty and e.name in self.pages:
                return self.pages[e.name]
        return None

    def editors_stale(self):
        """The target changed (or a command wrote it): read again when shown."""
        for page in self.pages.values():
            if not page.editor.dirty:
                page.stale = True
        if is_editor(self.current) and self.current.name in self.pages:
            page = self.pages[self.current.name]
            if page.stale and self.content_stack.get_visible_child() is page:
                self.editor_query(page)

    def editor_changed(self, page):
        for r in _rows(self.list):
            if r.cmd is page.editor:
                r.name_label.set_markup(self._entry_markup(page.editor))

    def editor_query(self, page):
        ed = page.editor
        old = page.child
        if old is not None and old.rc is None:
            page.child = None
            old.signal(signal.SIGTERM)
        target = self.work_path if ed.needs_target else None
        if ed.needs_target and target is None:
            page.show_status("No card or image is open", f"Open one to see its {ed.noun[1]}.", "Open…",
                             icon="drive-removable-media-symbolic", action=self.action_open)
            return
        if ed.needs_overlay and not self.overlay:
            page.show_status("No overlay is open", "Open the card or image with an overlay to see what it "
                             "changes.", "Open…", icon="drive-removable-media-symbolic", action=self.action_open)
            return
        root = ed.needs_target and self._info_needs_root()
        if root and not getattr(page, "root_ok", False):
            def go():
                page.root_ok = True
                self.editor_query(page)
            page.show_status("Reading this card needs your password", f"{self.target} can only be read "
                             "as root; polkit will ask for your password.", "Read",
                             icon="dialog-password-symbolic", action=go)
            return
        page.show_loading(f"Reading the {ed.noun[1]}…")

        def failed(msg):
            page.show_status("Cannot read the target" if ed.needs_target else f"Cannot read the {ed.noun[1]}",
                             msg, "Try Again", icon="dialog-error-symbolic", action=lambda: self.editor_query(page))

        def done(rc, out, err):
            if page.child is not child:
                return
            page.child = None
            if rc:
                failed(P.last_error(err) or (err[-1] if err else f"exit status {rc}"))
                return
            try:
                ed.load_output(out)
            except ValueError as e:
                failed(str(e))
                return
            page.show_items()
        try:
            child = capture(ed.query_argv(target), done, root=root, timeout=600)
        except (OSError, P.RootUnavailable) as e:
            failed(str(e))
            return
        page.child = child

    def _command_for(self, argv):
        """The Command an argv runs (the longest match of its words)."""
        best = None
        for c in self.all_cmds:
            if list(c.words) == argv[:len(c.words)] and (best is None or len(c.words) > len(best.words)):
                best = c
        return best

    def editor_commit(self, page, dry=False, then=None):
        """Make the editor's changes (dry: a preview); then() after a commit that worked."""
        ed = page.editor
        target = self.work_path
        if ed.needs_target and target is None:
            return
        if ed.layout != "review" and not ed.dirty:
            return
        problems = ed.problems()
        if problems and not dry:
            self.toast(problems[0])
            return
        try:
            argv, text = ed.commit(target, dry)
        except Exception as e:  # noqa: BLE001 - hashing a password, ...: say why
            self.toast(f"Cannot commit: {e}")
            return
        cmd = self._command_for(argv)
        placeholder = E.RECIPE
        try:
            args = C.parse([a if a != placeholder else os.path.join(GLib.get_tmp_dir(), "x.yaml") for a in argv])
            conf = C.confirmation(cmd, argv, args, self.target if self.direct_card and not dry else None)
        except C.ArgError as e:
            self.toast(str(e))
            return

        def after(rc, path=None):
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
            if dry:
                return
            if rc == 0:
                self.toast(f"{ed.title}: committed")
                page.stale = True
                if ed.layout != "review":
                    ed.revert()
                self.editor_query(page)
                if then is not None:
                    then()
            # on failure the changes stay, to be fixed or tried again

        def start():
            path = ed.write_recipe(text, ed.name) if text is not None else None
            run_argv = [path if a == placeholder else a for a in conf.argv]
            title = f"{'Preview' if dry else 'Commit'}: {ed.title}"
            self._run(cmd, run_argv, None, args, writes=not dry and ed.needs_target,
                      after=lambda rc: after(rc, path), title=title)
        if dry:
            start()
            return
        if ed.layout == "review":
            # the command's own confirmation (commit asks before writing a card)
            messages = conf.messages or [f"Write the overlay's {len(ed.items)} changed "
                                         f"file{'s' if len(ed.items) != 1 else ''} into "
                                         f"{self.target}? The overlay is empty afterwards."]
            confirm(self, f"Commit the overlay to {os.path.basename(self.target or '')}?", messages,
                    conf.typed, ok_label="Commit", danger=True, on_ok=start)
            return
        changes = ed.describe_changes()
        shown = changes[:15] + ([f"… and {len(changes) - 15} more"] if len(changes) > 15 else [])
        if ed.needs_target:
            where = os.path.basename(self.overlay) if self.overlay else self.target
        else:
            where = ed.title.lower()
        messages = [f"{len(changes)} change{'s' if len(changes) != 1 else ''} to {where}:\n"
                    + "\n".join(f"• {c}" for c in shown), *conf.messages]
        confirm(self, f"Commit changes to {ed.title}?", messages, conf.typed, ok_label="Commit",
                danger=self.direct_card or any(i.removed for i in getattr(ed, "items", [])), on_ok=start)

    def editor_row_action(self, page, argv, label):
        try:
            args = C.parse(argv)
        except C.ArgError as e:
            self.toast(str(e))
            return
        cmd = self.find_cmd(argv[0])
        self._run(cmd, argv, None, args, writes=False, title=label)

    def ask_discard(self, page, then):
        """The editor on `page` has changes: commit them, discard them, or stay."""
        ed = page.editor
        changes = ed.describe_changes()
        shown = changes[:10] + ([f"… and {len(changes) - 10} more"] if len(changes) > 10 else [])
        d = Adw.AlertDialog(heading=f"Commit the changes to {ed.title}?",
                            body="They are not in the target yet:\n" + "\n".join(f"• {c}" for c in shown))
        d.add_response("cancel", "Keep Editing")
        d.add_response("discard", "Discard")
        d.add_response("commit", "Commit…")
        d.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
        d.set_response_appearance("commit", Adw.ResponseAppearance.SUGGESTED)
        d.set_default_response("commit")
        d.set_close_response("cancel")

        def response(_d, r):
            if r == "discard":
                page.revert()
                then()
            elif r == "commit":
                self.editor_commit(page, then=then)
        d.connect("response", response)
        d.present(self)
        self.discard_dialog = d
        return d

    def _close_request(self, _w):
        page = self._dirty_page()
        if page is None:
            return False
        self.ask_discard(page, self.close)
        return True


class PiImgApplication(Adw.Application):
    def __init__(self, target=None, overlay=None, recipe=None):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self.args = (target, overlay, recipe)
        self.main_window = None
        GLib.set_application_name("piimg")

    def do_startup(self):
        Adw.Application.do_startup(self)
        css = Gtk.CssProvider()
        css.load_from_string(CSS)
        from gi.repository import Gdk
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        for name, cb in (("quit", lambda *_: self.quit_all()), ("about", lambda *_: self.about()),
                         ("shortcuts", lambda *_: self.shortcuts()), ("preferences", lambda *_: self.preferences())):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)
        for action, accels in ACCELS.items():
            self.set_accels_for_action(action, accels)

    def do_activate(self):
        target, overlay, recipe = self.args
        if recipe and not target:
            self.recipe_window(recipe)
            return
        self.main_window = MainWindow(self, target, overlay)
        self.main_window.present()
        if recipe:
            self.recipe_window(recipe)

    def default_image(self):
        return self.main_window.work_path if self.main_window else None

    def recipe_window(self, recipe=None, template=None):
        from pathlib import Path

        from ..frontend.recipe import find_template
        from .recipe import RecipeWindow
        if recipe and template is None and not Path(recipe).exists():
            template = find_template(recipe)
            if template:
                recipe = None
        try:
            win = RecipeWindow(self, recipe, template)
        except (OSError, ValueError) as e:
            if self.main_window:
                self.main_window.toast(f"cannot open the recipe: {e}")
            else:
                print(f"piimg: cannot open the recipe: {e}")
            return None
        win.present()
        return win

    def recipe_picker(self, parent):
        from .recipe import RecipePickDialog
        RecipePickDialog(lambda path, template: self.recipe_window(path, template)).present(parent)

    def quit_all(self):
        for w in list(self.get_windows()):
            w.close()

    def preferences(self):
        from .prefs import PreferencesDialog
        win = self.get_active_window()
        d = PreferencesDialog(win, capture)
        d.present(win)
        return d

    def about(self):
        d = Adw.AboutDialog(application_name="piimg", application_icon="media-flash",
                            developer_name="raspi-tools", version=_version(),
                            comments="Customize, inspect and repair Raspberry Pi OS images and SD cards, offline.",
                            website="https://github.com/un1tz3r0/raspi-tools", license_type=Gtk.License.APACHE_2_0)
        d.present(self.get_active_window())

    def shortcuts(self):
        d = Adw.ShortcutsDialog()
        for title, items in SHORTCUTS:
            sec = Adw.ShortcutsSection(title=title)
            for label, accel in items:
                sec.add(Adw.ShortcutsItem(title=label, accelerator=accel))
            d.add(sec)
        d.present(self.get_active_window())


def _version():
    try:
        from importlib.metadata import version
        return version("raspi-tools")
    except Exception:  # noqa: BLE001
        return ""


ACCELS = {
    "app.quit": ["<Control>q"],
    "app.shortcuts": ["<Control>question"],
    "app.preferences": ["<Control>comma"],
    "win.open": ["<Control>o"],
    "win.recipe-editor": ["<Control>e"],
    "win.refresh-info": ["<Control>r"],
    "win.search": ["<Control>f"],
    "win.run-command": ["<Control>Return"],
    "win.close": ["<Control>w"],
    # the recipe editor (window-wide; the list-only keys are in recipe.py)
    "win.save": ["<Control>s"],
    "win.save-as": ["<Control><Shift>s"],
    "win.insert": ["<Control>i", "Insert"],
    "win.run": ["F5"],
    "win.check": ["F7"],
    "win.yaml": ["F2"],
}

SHORTCUTS = [
    ("Main window", [("Open a card or image", "<Control>o"), ("Filter commands", "<Control>f"),
                     ("Run the selected command, or commit an editor", "<Control>Return"), ("Read the target again", "<Control>r"),
                     ("Recipe editor", "<Control>e"), ("Keyboard shortcuts", "<Control>question"), ("Preferences", "<Control>comma"),
                     ("Quit", "<Control>q")]),
    ("Forms", [("Run", "<Control>Return")]),
    ("Recipe editor", [("Insert a step", "<Control>i"), ("Delete the selected steps", "Delete"),
                       ("Move up / down", "<Alt>Up <Alt>Down"), ("Duplicate", "<Control>d"),
                       ("Copy / cut / paste steps", "<Control>c <Control>x <Control>v"),
                       ("Undo / redo", "<Control>z <Control><Shift>z"),
                       ("Select a range / more steps", "<Shift>Up <Control>space"),
                       ("Edit as YAML", "F2"), ("Check", "F7"), ("Save", "<Control>s"),
                       ("Save as", "<Control><Shift>s"), ("Run / dry run", "F5"), ("Close", "<Control>w")]),
]
