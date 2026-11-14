"""The run dialog: runs one piimg command as a child process and shows it.

See frontend.proc for the protocol. One progress bar per running step; the
output as a log, or for 'table' commands the first table as a grid with the
selected row's details under it. Stop sends SIGINT, then SIGTERM, then
SIGKILL. A command that needs root runs through pkexec under the
supervisor, which takes the stop requests on its stdin.
"""
import os
import subprocess
import time

from gi.repository import Adw, Gio, GLib, GObject, Gtk, Pango
from rich.text import Text

from ..frontend import proc as P
from ..progress import fmt_duration
from ..frontend.target import short_path
from .markup import TagCache, to_markup


def short_arg(a):
    return short_path(a) if a.startswith("/") else a


class Child:
    """A piimg child process whose stdout and stderr lines arrive in the main loop.

    on_out(line), on_err(line), on_exit(rc). root: through pkexec (supervised)."""

    def __init__(self, argv, on_out, on_err, on_exit, stdin=None, root=False, cwd=None, json_mode=True,
                 env=None):
        self.on_out, self.on_err, self.on_exit = on_out, on_err, on_exit
        self.supervised = P.supervised(root)
        self.rc = None
        self._open_streams = 2
        self._exit_status = None
        env = P.child_env(env)
        cmd = P.piimg_command(argv, root=root, env=env, cwd=cwd or os.getcwd(), json_mode=json_mode)
        self.cmdline = cmd
        self.proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdin=subprocess.PIPE if (stdin or self.supervised)
                                     else subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     start_new_session=True, pass_fds=())
        if self.supervised:
            self._write(P.stdin_orders(stdin))  # stdin stays open: it carries the stop requests
        elif stdin:
            self._write(stdin.encode())
            self.proc.stdin.close()
        for stream, handler in ((self.proc.stdout, on_out), (self.proc.stderr, on_err)):
            gin = Gio.UnixInputStream.new(os.dup(stream.fileno()), True)
            stream.close()
            data = Gio.DataInputStream.new(gin)
            data.set_newline_type(Gio.DataStreamNewlineType.ANY)
            self._read(data, handler)
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, self.proc.pid, self._exited)

    @property
    def pid(self):
        return self.proc.pid

    def _write(self, data):
        try:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()
        except (BrokenPipeError, ValueError, OSError):
            pass

    def _read(self, data, handler):
        def got(stream, result):
            try:
                line, _len = stream.read_line_finish_utf8(result)
            except GLib.Error:
                line = None
            except UnicodeDecodeError:
                line = ""
            if line is None:
                self._stream_done()
                return
            handler(line.rstrip("\r"))
            stream.read_line_async(GLib.PRIORITY_DEFAULT, None, got)
        data.read_line_async(GLib.PRIORITY_DEFAULT, None, got)

    def _exited(self, pid, status):
        try:
            rc = os.waitstatus_to_exitcode(status)
        except ValueError:
            rc = status
        self.proc.returncode = rc  # GLib reaped it, not Popen
        self._exit_status = rc
        # a grandchild may still hold the pipes open: don't wait for them forever
        GLib.timeout_add(1500, self._finish)
        self._maybe_finish()

    def _stream_done(self):
        self._open_streams -= 1
        self._maybe_finish()

    def _maybe_finish(self):
        if self._exit_status is not None and self._open_streams <= 0:
            self._finish()

    def _finish(self):
        if self.rc is None and self._exit_status is not None:
            self.rc = self._exit_status
            if self.proc.stdin and not self.proc.stdin.closed:
                try:
                    self.proc.stdin.close()
                except OSError:
                    pass
            self.on_exit(self.rc)
        return False

    def signal(self, sig):
        """Send sig to the command (its whole process group). False if it is gone."""
        if self.rc is not None:
            return False
        if self.supervised:
            self._write(P.stop_order(sig))
            return True
        try:
            os.killpg(self.proc.pid, sig)
            return True
        except ProcessLookupError:
            return False


class TaskRow(Gtk.Box):
    """One running step: description, bar, elapsed time, details."""

    def __init__(self, ev):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        head = Gtk.Box(spacing=12)
        self.desc = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END,
                              css_classes=["heading"])
        self.desc.set_label(ev.get("description", ""))
        self.time = Gtk.Label(css_classes=["dim-label", "numeric"])
        head.append(self.desc)
        head.append(self.time)
        self.bar = Gtk.ProgressBar(show_text=False)
        self.detail = Gtk.Label(xalign=0, css_classes=["dim-label", "caption"], ellipsize=Pango.EllipsizeMode.END)
        self.append(head)
        self.append(self.bar)
        self.append(self.detail)
        self._pulse = None
        self.apply(ev)

    def apply(self, ev):
        total = ev.get("total")
        if total:
            if self._pulse:
                GLib.source_remove(self._pulse)
                self._pulse = None
            self.bar.set_fraction(min(1.0, (ev.get("completed") or 0) / total))
        elif self._pulse is None:
            self._pulse = GLib.timeout_add(150, self._do_pulse)
        self.time.set_label(fmt_duration(ev.get("elapsed", 0)))
        self.detail.set_markup(to_markup(P.task_detail(ev)))

    def _do_pulse(self):
        self.bar.pulse()
        return True

    def stop(self):
        if self._pulse:
            GLib.source_remove(self._pulse)
            self._pulse = None


class TableRowItem(GObject.Object):
    def __init__(self, cells):
        super().__init__()
        self.cells = cells


def table_view(t, on_select=None):
    """A Gtk.ColumnView for a frontend table ({title, columns, rows, footer})."""
    store = Gio.ListStore(item_type=TableRowItem)
    for r in t["rows"]:
        store.append(TableRowItem(list(r)))
    sel = Gtk.SingleSelection(model=store, autoselect=False, can_unselect=True)
    view = Gtk.ColumnView(model=sel, show_row_separators=True, reorderable=True,
                          css_classes=["data-table"])
    cols = t["columns"]
    for i, name in enumerate(cols):
        fac = Gtk.SignalListItemFactory()

        def setup(_f, item):
            item.set_child(Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END, max_width_chars=60,
                                     margin_start=4, margin_end=4))

        def bind(_f, item, i=i, name=name):
            cells = item.get_item().cells
            v = cells[i] if i < len(cells) else ""
            lab = item.get_child()
            style = P.status_style(name, v)
            lab.set_markup(to_markup(Text(v, style)) if style else GLib.markup_escape_text(v))
            lab.set_tooltip_text(v if len(v) > 40 else None)
        fac.connect("setup", setup)
        fac.connect("bind", bind)
        col = Gtk.ColumnViewColumn(title=name, factory=fac, resizable=True)
        col.set_expand(i == len(cols) - 1)
        view.append_column(col)
    if on_select:
        sel.connect("notify::selected", lambda s, _p: on_select(s.get_selected()))
    return view


def table_text(t):
    """A table as aligned rich Text, for tables shown inside a log."""
    rows = [t["columns"]] + [list(r) for r in t["rows"]]
    widths = [max(len(str(r[i])) if i < len(r) else 0 for r in rows) for i in range(len(t["columns"]))]
    out = Text()
    if t.get("title"):
        out.append(t["title"] + "\n", "bold")
    for n, r in enumerate(rows):
        for i, w in enumerate(widths):
            v = str(r[i]) if i < len(r) else ""
            out.append(v.ljust(w) + ("  " if i < len(widths) - 1 else ""),
                       "bold" if n == 0 else P.status_style(t["columns"][i], v))
        out.append("\n")
    if t.get("footer"):
        out.append(t["footer"] + "\n", "dim")
    return out


class RunDialog(Adw.Dialog):
    """Run `piimg ARGV` and show what it does. on_done(rc, dialog) when it ends;
    dialog.errors holds its `piimg: ...` lines."""

    def __init__(self, title, argv, stdin=None, layout="log", abort="yes", root=False, cwd=None, on_done=None):
        super().__init__(title=title, content_width=900, content_height=640)
        self.argv, self.stdin, self.layout, self.abort, self.root = argv, stdin, layout, abort, root
        self.cwd, self.on_done = cwd, on_done
        self.child = None
        self.rc = None
        self.stops = 0
        self.tasks = {}
        self.errors = []
        self.got_table = False
        self.table = None
        self.started = time.monotonic()

        header = Adw.HeaderBar(show_end_title_buttons=False, show_start_title_buttons=False)
        header.set_title_widget(Adw.WindowTitle(title=title, subtitle=("as root: " if root else "")
                                                + "piimg " + " ".join(map(short_arg, argv))))
        self.stop_btn = Gtk.Button(label="Stop", css_classes=["destructive-action"])
        self.stop_btn.connect("clicked", lambda _b: self.stop())
        self.close_btn = Gtk.Button(label="Close", sensitive=False)
        self.close_btn.connect("clicked", lambda _b: self.close())
        header.pack_start(self.close_btn)
        if abort != "no":
            header.pack_end(self.stop_btn)
        self.spinner = Adw.Spinner()
        header.pack_end(self.spinner)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12, margin_bottom=12,
                       margin_start=12, margin_end=12)
        self.tasks_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        body.append(self.tasks_box)
        self.table_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, vexpand=layout == "table",
                                 visible=False)
        body.append(self.table_box)
        self.log_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                                     wrap_mode=Gtk.WrapMode.NONE if layout == "text" else Gtk.WrapMode.WORD_CHAR,
                                     top_margin=8, bottom_margin=8, left_margin=10, right_margin=10)
        self.log_tags = TagCache(self.log_view.get_buffer())
        self.log_end = self.log_view.get_buffer().create_mark(None, self.log_view.get_buffer().get_end_iter(), False)
        self.log_scroll = Gtk.ScrolledWindow(child=self.log_view, vexpand=True, css_classes=["card"],
                                             min_content_height=120)
        self.log_scroll.set_overflow(Gtk.Overflow.HIDDEN)
        self.log_scroll.set_visible(layout != "table")  # shown once something is logged
        body.append(self.log_scroll)

        self.status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
        bottom = Gtk.Box(margin_top=6, margin_bottom=6, margin_start=12, margin_end=12)
        bottom.append(self.status)

        tv = Adw.ToolbarView(content=body)
        tv.add_top_bar(header)
        tv.add_bottom_bar(bottom)
        self.toast = Adw.ToastOverlay(child=tv)
        self.set_child(self.toast)
        self.set_can_close(False)
        self.connect("close-attempt", self._close_attempt)
        self.tick = GLib.timeout_add_seconds(1, self._tick)
        GLib.idle_add(self._start)

    # ---- running

    def _start(self):
        env = {"PIIMG_COLOR": "1"}
        try:
            self.child = Child(self.argv, self._out, self._err, self._finished, stdin=self.stdin,
                               root=self.root, cwd=self.cwd, env=env)
        except (OSError, P.RootUnavailable) as e:
            self.log(Text(f"cannot start piimg: {e}", "bold red"))
            self._finished(127)
            return False
        self._tick()
        return False

    def _tick(self):
        if self.rc is None:
            note = {"no": " — this step cannot be stopped safely"}.get(self.abort, "")
            self.status.set_label(f"running for {fmt_duration(time.monotonic() - self.started)}{note}")
            return True
        return False

    def log(self, text):
        self.log_scroll.set_visible(True)
        buf = self.log_view.get_buffer()
        self.log_tags.append(text)
        self.log_tags.append("\n")
        adj = self.log_scroll.get_vadjustment()
        if adj.get_value() + adj.get_page_size() >= adj.get_upper() - 40 or self.rc is None:
            self.log_view.scroll_mark_onscreen(self.log_end)
        buf.move_mark(self.log_end, buf.get_end_iter())

    def _out(self, line):
        t = P.parse_table(line)
        if t is None:
            self.log(Text.from_ansi(line) if "\x1b" in line else Text(line))
        elif self.layout == "table" and not self.got_table:
            self.got_table = True
            self._show_table(t)
        else:
            self.log(table_text(t))

    def _show_table(self, t):
        self.table = t
        self.table_box.set_visible(True)
        self.log_scroll.set_vexpand(False)
        self.log_scroll.set_min_content_height(90)
        self.log_scroll.set_max_content_height(140)
        if t.get("title"):
            self.table_box.append(Gtk.Label(label=t["title"], xalign=0, css_classes=["heading"]))
        view = table_view(t, self._row_selected)
        sw = Gtk.ScrolledWindow(child=view, vexpand=True, css_classes=["card"], min_content_height=200)
        sw.set_overflow(Gtk.Overflow.HIDDEN)
        self.table_box.append(sw)
        self.row_detail = Gtk.Label(xalign=0, wrap=True, selectable=True, css_classes=["dim-label"])
        self.row_detail.set_label(t.get("footer") or "")
        self.table_box.append(self.row_detail)

    def _row_selected(self, i):
        t = self.table
        if not (0 <= i < len(t["rows"])):
            self.row_detail.set_label(t.get("footer") or "")
            return
        out = Text()
        for c, v in zip(t["columns"], t["rows"][i]):
            if v:
                out.append(f"{c}: ", "bold")
                out.append(str(v) + "\n")
        self.row_detail.set_markup(to_markup(out).rstrip("\n"))

    def _err(self, line):
        if P.is_event(line):
            ev = P.parse_event(line)
            if ev is not None:
                self._event(ev)
            return
        if "\x1b" in line:  # a tool that coloured its output anyway: keep the colours, drop the codes
            text = Text.from_ansi(line)
            line = text.plain
        else:
            text = Text(line, P.message_style(line))
        if line.startswith("piimg: "):
            self.errors.append(line)
        self.log(text)

    def _event(self, ev):
        tid = ev.get("id")
        if ev["event"] == "begin":
            row = TaskRow(ev)
            self.tasks[tid] = row
            self.tasks_box.append(row)
        elif ev["event"] == "update" and tid in self.tasks:
            self.tasks[tid].apply(ev)
        elif ev["event"] == "end":
            row = self.tasks.pop(tid, None)
            if row is not None:
                row.stop()
                self.tasks_box.remove(row)
            line = P.end_line(ev)
            if line is not None:
                self.log(line)

    def _finished(self, rc):
        self.rc = rc
        for row in list(self.tasks.values()):
            row.stop()
            self.tasks_box.remove(row)
        self.tasks.clear()
        text, kind = P.finished(rc, time.monotonic() - self.started)
        self.status.set_markup(f"<b><span foreground='{ {'ok': '#2ec27e', 'stopped': '#d4a000'}.get(kind, '#e01b24') }'>"
                               f"{GLib.markup_escape_text(text)}</span></b>")
        self.spinner.set_visible(False)
        self.stop_btn.set_visible(False)
        self.close_btn.set_sensitive(True)
        self.close_btn.add_css_class("suggested-action")
        self.close_btn.grab_focus()
        self.set_can_close(True)
        if self.on_done:
            self.on_done(rc, self)

    # ---- stopping

    def stop(self):
        if self.rc is not None or self.child is None:
            return
        if self.abort == "no":
            self.toast.add_toast(Adw.Toast(title="This step cannot be stopped safely; wait for it to finish."))
            return
        if self.abort == "warn" and self.stops == 0:
            d = Adw.AlertDialog(heading="Stop this command?",
                                body="Stopping now may leave the image or card half changed.")
            d.add_response("cancel", "Keep running")
            d.add_response("stop", "Stop")
            d.set_response_appearance("stop", Adw.ResponseAppearance.DESTRUCTIVE)
            d.set_default_response("cancel")
            d.connect("response", lambda _d, r: r == "stop" and self._signal())
            d.present(self)
            return
        self._signal()

    def _signal(self):
        sig = P.STOP_SIGNALS[min(self.stops, 2)]
        if not self.child.signal(sig):
            return
        self.stops += 1
        self.stop_btn.set_label(P.STOP_LABELS[min(self.stops, 2)])
        self.log(Text(f"-- sent {sig.name}", "yellow"))

    def _close_attempt(self, _d):
        if self.rc is None:
            self.toast.add_toast(Adw.Toast(title="Still running: stop it first."))
