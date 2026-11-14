"""Commands that need a real terminal (shell, edit) run in a VTE terminal dialog."""
import os
import signal

from gi.repository import Adw, GLib, Gtk, Pango, Vte

from ..frontend import proc as P
from .run import short_arg


class TerminalDialog(Adw.Dialog):
    """Run `piimg ARGV` (no JSON output) in a terminal. on_done(rc)."""

    def __init__(self, title, argv, root=False, on_done=None):
        super().__init__(title=title, content_width=980, content_height=640)
        self.on_done, self.rc = on_done, None
        self.term = Vte.Terminal(hexpand=True, vexpand=True, scrollback_lines=10000)
        self.term.set_font(Pango.FontDescription.from_string("Monospace 11"))
        self.term.connect("child-exited", self._exited)
        header = Adw.HeaderBar()
        header.set_title_widget(Adw.WindowTitle(title=title, subtitle=("as root: " if root else "")
                                                + "piimg " + " ".join(map(short_arg, argv))))
        tv = Adw.ToolbarView(content=self.term)
        tv.add_top_bar(header)
        self.status = Gtk.Label(xalign=0, margin_top=6, margin_bottom=6, margin_start=12, margin_end=12,
                                css_classes=["dim-label"], label="running — close the program in the terminal "
                                                                 "to finish (closing this window stops it)")
        tv.add_bottom_bar(self.status)
        self.set_child(tv)
        self.pid = None
        env = [f"{k}={v}" for k, v in P.child_env().items()]
        try:
            cmd = P.piimg_command(argv, root=root, cwd=os.getcwd(), json_mode=False, supervise=False)
        except P.RootUnavailable as e:
            self.term.feed(f"{e}\r\n".encode())
            return
        self.term.spawn_async(Vte.PtyFlags.DEFAULT, os.getcwd(), cmd, env, GLib.SpawnFlags.SEARCH_PATH,
                              None, None, -1, None, self._spawned)
        self.connect("closed", self._closed)

    def _spawned(self, term, pid, error):
        if error is not None:
            term.feed(f"cannot start piimg: {error.message}\r\n".encode())
            self._exited(term, 127 << 8)
            return
        self.pid = pid
        term.grab_focus()

    def _exited(self, _term, status):
        try:
            self.rc = os.waitstatus_to_exitcode(status)
        except ValueError:
            self.rc = status
        self.pid = None
        self.status.set_label(f"finished (exit status {self.rc}); close this window to go back")
        if self.on_done:
            self.on_done(self.rc)

    def _closed(self, *_):
        if self.pid:  # closed while running: hang up, as closing a terminal would
            try:
                os.kill(self.pid, signal.SIGHUP)
            except (ProcessLookupError, PermissionError):
                pass
