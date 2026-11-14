"""piimg's desktop interface: GTK 4, libadwaita and PyGObject.

    piimg gui [TARGET [OVERLAY]] [--recipe FILE]
    piimg gui --install-desktop        add piimg to the desktop's app launcher
    sudo piimg gui --install-polkit    let polkit remember the password a while (frontend.polkit)

It does what `piimg tui` does, with the same models (piimg.frontend): pick a
card or image, run any command with a form for its options, watch it in a run
dialog, and edit recipes in their own windows.

The GUI runs as the user. A command that must open a card the user cannot
open runs through pkexec, and polkit asks for the password (see
frontend.proc). The distro's PyGObject is used, like libguestfs: apt install
python3-gi gir1.2-adw-1 gir1.2-gtksource-5 gir1.2-vte-3.91.
"""
import os
import sys
from pathlib import Path

from ..errors import PiImgError

APP_ID = "io.github.un1tz3r0.piimg"
VERSIONS = {"Gtk": "4.0", "Adw": "1", "GtkSource": "5", "Vte": "3.91"}
APT = "python3-gi gir1.2-adw-1 gir1.2-gtksource-5 gir1.2-vte-3.91"


def require():
    """Load PyGObject and the GTK libraries, or raise PiImgError saying what to install."""
    from ..sysimport import import_system
    try:
        gi = import_system("gi")
    except ImportError:
        raise PiImgError(f"cannot import gi (PyGObject). Install it with: sudo apt install {APT}\n"
                         f"It must match this Python ({sys.executable}).")
    missing = []
    for name, version in VERSIONS.items():
        try:
            gi.require_version(name, version)
        except ValueError:
            missing.append(f"{name} {version}")
    if missing:
        raise PiImgError(f"missing GObject libraries: {', '.join(missing)}. Install them with: "
                         f"sudo apt install {APT}")
    return gi


def main(target=None, overlay=None, recipe=None):
    require()
    if not (os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY")
            or os.environ.get("BROADWAY_DISPLAY")):
        raise PiImgError("no display: piimg gui needs a desktop session (use `piimg tui` in a terminal)")
    if os.geteuid() == 0 and (os.environ.get("SUDO_USER") or os.environ.get("PKEXEC_UID")):
        print("piimg: note: run piimg gui as yourself, not with sudo: it asks for the password "
              "itself when a command needs root", file=sys.stderr)
    from .app import PiImgApplication
    app = PiImgApplication(target, overlay, recipe)
    return app.run([sys.argv[0]])


# ------------------------------------------------------------ desktop entry

def desktop_path():
    from ..config import user_home
    data = os.environ.get("XDG_DATA_HOME") if os.geteuid() != 0 else None
    return Path(data or user_home() / ".local" / "share") / "applications" / f"{APP_ID}.desktop"


def desktop_entry():
    exe = sys.executable
    return f"""[Desktop Entry]
Type=Application
Name=piimg
GenericName=Raspberry Pi image tool
Comment=Customize, inspect and repair Raspberry Pi OS images and SD cards
Exec={exe} -m piimg gui %f
TryExec={exe}
Icon=media-flash
Terminal=false
Categories=System;GTK;
Keywords=raspberry;pi;image;sd;card;recipe;
MimeType=application/x-raw-disk-image;application/x-qemu-disk;
StartupNotify=true
StartupWMClass={APP_ID}
"""


def install_desktop(remove=False):
    p = desktop_path()
    if remove:
        if p.exists():
            p.unlink()
            return f"removed {p}"
        return f"nothing to remove ({p} does not exist)"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(desktop_entry())
    from ..config import give_to_user
    give_to_user(p)
    return f"wrote {p}; piimg is now in the app launcher (it runs {sys.executable})"
