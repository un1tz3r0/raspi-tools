"""The open target: an image, an overlay or a card, and what can be opened.

A front end works on one target at a time. With an overlay, the target is
only read: the overlay (qcow2) is created on it if missing and every command
works on the overlay; `commit` later writes the changes into the target.
Without one, commands change the target itself.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from ..disks import is_block_device, list_disks
from ..progress import fmt_size


def short_path(p):
    """A path as short as it can be shown: relative to the current directory or ~."""
    p = str(p)
    if p.startswith("/dev/"):
        return p
    try:
        rel = os.path.relpath(p)
        if not rel.startswith(".."):
            return rel
    except ValueError:
        pass
    home = os.path.expanduser("~")
    return "~" + p[len(home):] if p.startswith(home + "/") else p


def describe_target(path, overlay):
    p = short_path(path)
    overlay = overlay and short_path(overlay)
    if overlay:
        return f"{p} → {overlay}  (target only read; changes go to the overlay)"
    if is_block_device(p):
        return f"{p}  (card: changes are written to it directly)"
    return f"{p}  (changes are written to it)"


def suggest_overlay(path):
    """A file name for a new overlay on `path`."""
    p = str(path)
    return (Path(p).name if not p.startswith("/dev/") else Path(p).name + "-card") + "-changes.qcow2"


def absolute(path):
    return path if str(path).startswith("/dev/") else os.path.abspath(os.path.expanduser(str(path)))


def can_open(path, write):
    """True if this process may open `path` as needed (a card may need root)."""
    if os.geteuid() == 0 or not is_block_device(path):
        return True
    return os.access(path, os.R_OK | (os.W_OK if write else 0))


@dataclass
class Candidate:
    path: str
    kind: str          # disk | image | overlay
    what: str          # "disk: SD card reader", "image"
    size: str
    note: str = ""
    pi: bool = False   # a disk that looks like a Raspberry Pi OS card
    root: bool = False  # opening it needs root

    @property
    def name(self):
        return self.path if self.kind == "disk" else Path(self.path).name


def candidates(folders=None):
    """What a picker offers: removable disks (not the system's), then .img and
    .qcow2 files in the current directory and the images folder. Returns
    (list of Candidate, warning or None)."""
    out, warning = [], None
    try:
        disks = list_disks()
    except Exception as e:  # noqa: BLE001 - lsblk missing etc.: files still work
        disks, warning = [], f"cannot list disks: {e}"
    for d in disks:
        reasons = d.why_not()
        if reasons and not d.looks_like_pi:
            continue  # the system disk etc.: never offered
        note = "; ".join(reasons) if reasons else ("Raspberry Pi OS card" if d.looks_like_pi else "")
        out.append(Candidate(d.path, "disk", f"disk: {d.model or d.transport or '?'}", fmt_size(d.size),
                             note, d.looks_like_pi, not can_open(d.path, True)))
    if folders is None:
        from ..config import images_dir
        folders = [Path.cwd()]
        try:
            if images_dir().resolve() != Path.cwd().resolve():
                folders.append(images_dir())
        except Exception:  # noqa: BLE001 - a broken config must not stop the picker
            pass
    seen = set()
    for i, folder in enumerate(folders):
        folder = Path(folder)
        files = sorted(folder.glob("*")) if folder.is_dir() else []
        for f in files:
            if f.suffix not in (".img", ".qcow2") or not f.exists() or f.resolve() in seen:
                continue
            seen.add(f.resolve())
            kind = "overlay" if f.suffix == ".qcow2" else "image"
            out.append(Candidate(str(f), kind, kind, fmt_size(f.stat().st_size),
                                 "" if i == 0 else "images folder"))
    return out, warning
