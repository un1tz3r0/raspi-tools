"""Preferences: piimg's own settings (`piimg config`), changed one at a time.

They are not in the image, take effect at once and are cheap to set, so this
is a plain preferences dialog (apply per row), not an editor with a Commit.
"""
from gi.repository import Adw, GLib, Gtk

from ..frontend import proc as P


def _esc(t):
    return GLib.markup_escape_text(str(t or ""))


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, win, capture):
        super().__init__(title="Preferences", search_enabled=False, content_width=640, content_height=620)
        self.win, self.capture = win, capture
        self.page = Adw.PreferencesPage(title="piimg", icon_name="preferences-system-symbolic")
        self.add(self.page)
        self.groups = []
        self.rows = {}
        self.status = Adw.PreferencesGroup()
        self.status.add(Adw.ActionRow(title="Reading the settings…"))
        self.page.add(self.status)
        self.load()

    def load(self):
        def done(rc, out, err):
            table = P.first_table(out)
            if rc or table is None:
                self._message(P.last_error(err) or f"cannot read the settings (exit status {rc})")
                return
            self.show(table)
        self.capture(["config"], done)

    def _message(self, text):
        for g in [self.status, *self.groups]:
            self.page.remove(g)
        self.groups = []
        self.status = Adw.PreferencesGroup()
        self.status.add(Adw.ActionRow(title=_esc(text)))
        self.page.add(self.status)

    def show(self, table):
        for g in [self.status, *self.groups]:
            try:
                self.page.remove(g)
            except Exception:  # noqa: BLE001 - not added yet
                pass
        self.status = Adw.PreferencesGroup()
        self.groups = []
        self.rows = {}
        cols = table["columns"]
        footer = table.get("title") or ""
        for row in table["rows"]:
            v = dict(zip(cols, row))
            key = v["setting"]
            g = Adw.PreferencesGroup(title=_esc(key), description=_esc(v.get("meaning")))
            entry = Adw.EntryRow(title="Value" + (" (default)" if v.get("source") == "default" else ""),
                                 text=str(v.get("value") or ""), show_apply_button=True)
            entry.connect("apply", lambda e, k=key: self.set_value(k, e.get_text()))
            reset = Gtk.Button(icon_name="edit-clear-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                               tooltip_text="Back to the default", sensitive=v.get("source") != "default")
            reset.connect("clicked", lambda _b, k=key: self.unset(k))
            entry.add_suffix(reset)
            g.add(entry)
            self.rows[key] = entry
            self.page.add(g)
            self.groups.append(g)
        self.page.set_description(_esc(footer))

    def set_value(self, key, value):
        self._run(["config", key, value], f"{key} = {value}")

    def unset(self, key):
        self._run(["config", "--unset", key], f"{key}: back to the default")

    def _run(self, argv, what):
        def done(rc, out, err):
            if rc:
                self.add_toast(Adw.Toast(title=_esc(P.last_error(err) or f"failed (exit status {rc})")))
                return
            self.add_toast(Adw.Toast(title=_esc(what)))
            table = P.first_table(out)
            if table:
                self.show(table)
        self.capture(argv, done)
