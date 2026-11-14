"""The open dialog: a card, an image file or an overlay, and optionally a new overlay."""
import os
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

from ..frontend import target as T

RECENT_APP = "piimg"
IMAGE_SUFFIXES = (".img", ".qcow2", ".raw")


def recent_images(limit=8):
    """Images and overlays piimg opened before (Gtk.RecentManager), newest first."""
    out = []
    try:
        items = Gtk.RecentManager.get_default().get_items()
    except Exception:  # noqa: BLE001
        return out
    items.sort(key=lambda i: i.get_modified().to_unix() if i.get_modified() else 0, reverse=True)
    for it in items:
        if not it.has_application(RECENT_APP):
            continue
        path = Gio.File.new_for_uri(it.get_uri()).get_path()
        if path and path.endswith(IMAGE_SUFFIXES) and os.path.exists(path):
            out.append(path)
        if len(out) >= limit:
            break
    return out


def remember(path):
    if str(path).startswith("/dev/"):
        return
    try:
        uri = Gio.File.new_for_path(str(path)).get_uri()
        data = Gtk.RecentData()
        data.app_name, data.app_exec = RECENT_APP, "piimg gui %f"
        data.mime_type = "application/x-qemu-disk" if str(path).endswith(".qcow2") else "application/x-raw-disk-image"
        Gtk.RecentManager.get_default().add_full(uri, data)
    except Exception:  # noqa: BLE001 - recent files are a nicety
        pass


class OpenDialog(Adw.Dialog):
    """Pick a target. on_open(path, overlay or None)."""

    def __init__(self, on_open, current=None):
        super().__init__(title="Open an SD card or image", content_width=640, content_height=720)
        self.on_open = on_open
        self.chosen = current

        header = Adw.HeaderBar()
        self.open_btn = Gtk.Button(label="Open", css_classes=["suggested-action"], sensitive=False)
        self.open_btn.connect("clicked", lambda _b: self._open())
        header.pack_end(self.open_btn)
        refresh = Gtk.Button(icon_name="view-refresh-symbolic", tooltip_text="Look for cards and images again")
        refresh.connect("clicked", lambda _b: self.refresh())
        header.pack_start(refresh)

        self.page = Adw.PreferencesPage()
        self.found = Adw.PreferencesGroup(title="Cards and images",
                                          description="Cards attached to this computer, and images in the "
                                                      "current folder and the images folder")
        browse = Gtk.Button(label="Other File…", valign=Gtk.Align.CENTER, css_classes=["flat"])
        browse.connect("clicked", self._browse)
        self.found.set_header_suffix(browse)
        self.recent = Adw.PreferencesGroup(title="Recent")
        self.page.add(self.found)
        self.page.add(self.recent)

        ov = Adw.PreferencesGroup(title="Overlay",
                                  description="Keep the target read-only: every change goes to an overlay file "
                                              "(qcow2), to be committed into the target later, or thrown away.")
        self.use_overlay = Adw.SwitchRow(title="Write changes to an overlay")
        self.overlay_row = Adw.EntryRow(title="Overlay file", sensitive=False)
        pick = Gtk.Button(icon_name="document-save-as-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                          tooltip_text="Choose where to put it")
        pick.connect("clicked", self._pick_overlay)
        self.overlay_row.add_suffix(pick)
        self.use_overlay.connect("notify::active", lambda r, _p: self.overlay_row.set_sensitive(r.get_active()))
        ov.add(self.use_overlay)
        ov.add(self.overlay_row)
        self.page.add(ov)

        self.selected_label = Gtk.Label(xalign=0, wrap=True, margin_top=6, margin_bottom=6, margin_start=12,
                                        margin_end=12, css_classes=["dim-label"])
        tv = Adw.ToolbarView(content=self.page)
        tv.add_top_bar(header)
        tv.add_bottom_bar(self.selected_label)
        self.toast = Adw.ToastOverlay(child=tv)
        self.set_child(self.toast)
        self.found_rows = []
        self.recent_rows = []
        self.refresh()
        if current:
            self._choose(current)

    def refresh(self):
        for r in self.found_rows:
            self.found.remove(r)
        for r in self.recent_rows:
            self.recent.remove(r)
        self.found_rows, self.recent_rows = [], []
        items, warning = T.candidates()
        if warning:
            self.toast.add_toast(Adw.Toast(title=warning))
        check = None
        for c in items:
            icon = {"disk": "drive-removable-media-symbolic", "overlay": "document-edit-symbolic"}.get(
                c.kind, "media-optical-symbolic")
            sub = " · ".join(x for x in (c.what, c.size, c.note) if x)
            row = Adw.ActionRow(title=GLib.markup_escape_text(c.name), subtitle=GLib.markup_escape_text(sub),
                                activatable=True)
            row.add_prefix(Gtk.Image(icon_name=icon))
            if c.root:
                row.add_suffix(Gtk.Image(icon_name="system-lock-screen-symbolic",
                                         tooltip_text="Opening it asks for your password"))
            radio = Gtk.CheckButton(valign=Gtk.Align.CENTER, group=check)
            check = check or radio
            row.add_suffix(radio)
            row.set_activatable_widget(radio)
            radio.connect("toggled", lambda b, p=c.path: b.get_active() and self._choose(p))
            row.path, row.radio = c.path, radio
            self.found.add(row)
            self.found_rows.append(row)
        if not items:
            row = Adw.ActionRow(title="No card or image found here",
                                subtitle="Choose a file with “Other File…”")
            self.found.add(row)
            self.found_rows.append(row)
        known = {c.path for c in items}
        for p in recent_images():
            if p in known:
                continue
            row = Adw.ActionRow(title=GLib.markup_escape_text(Path(p).name),
                                subtitle=GLib.markup_escape_text(T.short_path(Path(p).parent)), activatable=True)
            row.add_prefix(Gtk.Image(icon_name="document-open-recent-symbolic"))
            radio = Gtk.CheckButton(valign=Gtk.Align.CENTER, group=check)
            check = check or radio
            row.add_suffix(radio)
            row.set_activatable_widget(radio)
            radio.connect("toggled", lambda b, p=p: b.get_active() and self._choose(p))
            row.path, row.radio = p, radio
            self.recent.add(row)
            self.recent_rows.append(row)
        self.recent.set_visible(bool(self.recent_rows))

    def _choose(self, path):
        self.chosen = path
        for r in self.found_rows + self.recent_rows:
            if getattr(r, "path", None) == path and not r.radio.get_active():
                r.radio.set_active(True)
        text = self.overlay_row.get_text()
        if not text or text.endswith("-changes.qcow2"):
            folder = Path.cwd() if path.startswith("/dev/") else Path(path).parent
            self.overlay_row.set_text(str(folder / T.suggest_overlay(path)))
        self.selected_label.set_label(f"Selected: {path}")
        self.open_btn.set_sensitive(True)

    def _browse(self, _b):
        f = Gtk.FileFilter(name="Disk images")
        for pat in ("*.img", "*.qcow2", "*.raw", "*.IMG"):
            f.add_pattern(pat)
        every = Gtk.FileFilter(name="All files")
        every.add_pattern("*")
        filters = Gio.ListStore(item_type=Gtk.FileFilter)
        filters.append(f)
        filters.append(every)
        dialog = Gtk.FileDialog(title="Open an image", filters=filters, default_filter=f)

        def done(d, res):
            try:
                gfile = d.open_finish(res)
            except GLib.Error:
                return
            if gfile and gfile.get_path():
                for r in self.found_rows + self.recent_rows:
                    if getattr(r, "radio", None):
                        r.radio.set_active(False)
                self._choose(gfile.get_path())
        dialog.open(self.get_root(), None, done)

    def _pick_overlay(self, _b):
        cur = self.overlay_row.get_text()
        dialog = Gtk.FileDialog(title="Overlay file", initial_name=Path(cur).name if cur else "changes.qcow2")
        if cur and Path(cur).parent.is_dir():
            dialog.set_initial_folder(Gio.File.new_for_path(str(Path(cur).parent)))

        def done(d, res):
            try:
                gfile = d.save_finish(res)
            except GLib.Error:
                return
            if gfile and gfile.get_path():
                self.overlay_row.set_text(gfile.get_path())
        dialog.save(self.get_root(), None, done)

    def _open(self):
        if not self.chosen:
            return
        overlay = self.overlay_row.get_text().strip() if self.use_overlay.get_active() else None
        if self.use_overlay.get_active() and not overlay:
            self.toast.add_toast(Adw.Toast(title="Name the overlay file first"))
            return
        self.close()
        self.on_open(self.chosen, overlay)
