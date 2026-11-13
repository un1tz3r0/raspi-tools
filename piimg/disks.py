"""Whole disks (SD cards, USB sticks): finding them, deciding whether they may
be touched, and holding one safely while piimg works on it.

Two users:
  - `burn` overwrites a whole disk (see burn.py);
  - every other command can open a card pulled from a Pi in place of an image
    file (`piimg passwd /dev/sdc pi`). Image.open() then calls claim(), which
    refuses disks the running system uses, unmounts desktop automounts of the
    card before writing, and holds an exclusive flock on the disk while it is
    open for writing, so udev and the desktop leave it alone
    (https://systemd.io/BLOCK_DEVICE_LOCKING/).

Reading or writing /dev/sdX needs root (or membership of the `disk` group).
"""
import fcntl
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from . import progress
from .errors import PiImgError

BLKRRPART = 0x125F  # re-read partition table
BLKFLSBUF = 0x1261  # flush buffers and drop the page cache for the device
AUTOMOUNT_DIRS = ("/media/", "/run/media/", "/mnt/")
_LSBLK_COLS = "NAME,PATH,MAJ:MIN,TYPE,SIZE,RM,RO,TRAN,VENDOR,MODEL,MOUNTPOINTS,LABEL,FSTYPE"


@dataclass
class Disk:
    name: str
    path: str
    size: int
    removable: bool
    readonly: bool
    transport: str
    model: str
    devnums: set = field(default_factory=set)    # "maj:min" of the disk and every partition
    mounts: list = field(default_factory=list)   # (device path, mount point)
    swaps: list = field(default_factory=list)    # device paths in use as swap
    holders: list = field(default_factory=list)  # "sda2 -> crypt" (dm-crypt, LVM, RAID...)
    labels: list = field(default_factory=list)   # filesystem labels of the partitions

    def describe(self):
        bits = [progress.fmt_size(self.size), self.transport or "?", self.model or "unknown model"]
        if self.removable:
            bits.append("removable")
        return f"{self.path} ({', '.join(bits)})"

    @property
    def looks_like_pi(self):
        """Raspberry Pi OS cards have a FAT 'bootfs' (or older 'boot') and an ext4 'rootfs'."""
        return bool({"bootfs", "boot"} & set(self.labels)) and "rootfs" in self.labels

    def why_not(self):
        """Reasons this disk must never be written; empty if it may be."""
        reasons = []
        if self.size == 0:
            reasons.append("it has no medium (empty card reader?)")
        if self.readonly:
            reasons.append("it is read-only (SD card lock switch?)")
        reasons += [f"{dev} is swap" for dev in self.swaps]
        reasons += [f"{h} is in use by the device mapper / RAID" for h in self.holders]
        reasons += [f"{dev} is mounted on {mp}" for dev, mp in self.mounts
                    if not mp.startswith(AUTOMOUNT_DIRS)]
        return reasons

    @property
    def automounts(self):
        """Mounts that look like desktop automounts, so we may unmount them."""
        return [(dev, mp) for dev, mp in self.mounts if mp.startswith(AUTOMOUNT_DIRS)]


def _bool(v):
    return v in (True, "1", 1)  # lsblk older than 2.33 prints "1"/"0"


def _disk_from_lsblk(d):
    disk = Disk(name=d["name"], path=d["path"], size=int(d["size"] or 0), removable=_bool(d["rm"]),
                readonly=_bool(d["ro"]), transport=d.get("tran") or "",
                model=" ".join(filter(None, [(d.get("vendor") or "").strip(), (d.get("model") or "").strip()])))

    def walk(node):
        disk.devnums.add(node["maj:min"])
        if node.get("label"):
            disk.labels.append(node["label"])
        for mp in node.get("mountpoints") or []:
            if mp == "[SWAP]":
                disk.swaps.append(node["path"])
            elif mp:
                disk.mounts.append((node["path"], mp))
        for child in node.get("children") or []:
            if child["type"] != "part":
                disk.holders.append(f"{node['name']} -> {child['name']} ({child['type']})")
            walk(child)

    walk(d)
    return disk


def _lsblk(*devices):
    r = subprocess.run(["lsblk", "-J", "-b", "-o", _LSBLK_COLS, *devices], capture_output=True, text=True)
    if r.returncode:
        raise PiImgError(f"lsblk failed: {r.stderr.strip()}")
    return json.loads(r.stdout)["blockdevices"]


def list_disks():
    """Every whole disk, as Disk objects (loop/zram/optical devices left out)."""
    return [_disk_from_lsblk(d) for d in _lsblk()
            if d["type"] == "disk" and not d["name"].startswith(("zram", "ram"))]


def is_block_device(path):
    try:
        return stat.S_ISBLK(os.stat(path).st_mode)
    except OSError:
        return False


def find_disk(device):
    """Resolve a user-given device (/dev/sdc, sdc, /dev/disk/by-id/...) to a Disk."""
    path = Path(device if "/" in str(device) else f"/dev/{device}").resolve()
    if not path.exists():
        raise PiImgError(f"No such device: {device}")
    sysfs = Path("/sys/class/block") / path.name
    if not path.is_block_device() or not sysfs.exists():
        raise PiImgError(f"{device} is not a block device")
    if (sysfs / "partition").exists():
        whole = sysfs.resolve().parent.name
        raise PiImgError(f"{path} is a partition; give the whole disk, /dev/{whole}")
    found = _lsblk(str(path))
    if not found or found[0]["type"] != "disk":
        raise PiImgError(f"{path} is not a disk (type {found[0]['type'] if found else '?'})")
    return _disk_from_lsblk(found[0])


def disks_table():
    """The disks piimg could open or write, as an output.Table (`piimg disks`)."""
    from .output import Table
    rows = []
    for d in list_disks():
        reasons = d.why_not()
        mounted = ", ".join(mp for _, mp in d.automounts)
        status = "in use" if reasons else "Pi OS card" if d.looks_like_pi else "available"
        note = "; ".join(reasons) if reasons else (f"mounted: {mounted}" if mounted else "")
        rows.append([d.path, progress.fmt_size(d.size), d.transport or "?", d.model or "unknown",
                     ", ".join(d.labels), status, note])
    return Table(["device", "size", "bus", "model", "labels", "status", "note"], rows,
                 title="Disks (\"in use\" disks are never written)")


def print_disks():
    """The listing `piimg burn IMAGE` shows when no device is given."""
    usable, hidden = [], []
    for d in list_disks():
        (hidden if d.why_not() else usable).append(d)
    if not usable:
        print("No disk that could be written to was found. Insert an SD card or USB stick.")
    else:
        print("Disks that piimg burn could write to:")
        for d in usable:
            mounted = ", ".join(mp for _, mp in d.automounts)
            print(f"  {d.describe()}" + (f"  [mounted: {mounted}]" if mounted else ""))
    for d in hidden:
        print(f"  (not offered: {d.path}: {'; '.join(d.why_not())})")


def sudo_hint():
    """The command line the user should run instead, under sudo."""
    return shlex.join(["sudo", sys.executable, "-m", "piimg", *sys.argv[1:]])


def check_access(path, write):
    """Raise a helpful error if this process cannot open the device as needed."""
    if os.access(path, os.R_OK | (os.W_OK if write else 0)):
        return
    raise PiImgError(f"{'writing' if write else 'reading'} {path} needs root (or the 'disk' group). Run:\n"
                     f"  {sudo_hint()}")


def unmount(dev, mountpoint):
    if os.geteuid() != 0 and shutil.which("udisksctl"):
        cmd = ["udisksctl", "unmount", "--no-user-interaction", "-b", dev]  # the desktop's own mounts
    else:
        cmd = ["umount", mountpoint]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        raise PiImgError(f"could not unmount {dev} from {mountpoint}: {(r.stderr or r.stdout).strip()}\n"
                         "Close any window or terminal using it and try again.")


# ------------------------------------------------------------- claiming a disk

class _Claim:
    def __init__(self, disk, fd):
        self.disk, self.fd, self.count, self.reread = disk, fd, 1, False


_claims = {}  # resolved device path -> _Claim; re-entrant within this process
_claims_lock = threading.Lock()


@contextmanager
def claim(device, readonly=False):
    """Hold the disk `device` while piimg works on it in place.

    Read-only: only checks that it can be read, and warns if it is mounted
    (what is read may then be stale). For writing, all of burn's safety rules
    apply except the size check: the disk must not be used by the running
    system (no mounts outside /media, /run/media, /mnt; no swap, LVM, LUKS,
    RAID). Desktop automounts of it are unmounted first. While held, an
    exclusive flock keeps udev and automounters away. On release: fsync and
    drop the page cache, and re-read the partition table if `reread` was set
    on the yielded object (after changing partitions).

    Re-entrant: nested claims of the same disk in one process share one lock
    (flock locks of separate open() calls would conflict with each other).
    """
    path = str(Path(device).resolve())
    with _claims_lock:
        held = _claims.get(path)
        if held is not None:
            held.count += 1
    if held is not None:
        try:
            yield held
        finally:
            with _claims_lock:
                held.count -= 1
        return

    disk = find_disk(path)
    if readonly:
        check_access(disk.path, write=False)
        if disk.mounts:
            print(f"piimg: note: {disk.path} is mounted ({', '.join(mp for _, mp in disk.mounts)}); "
                  "what is read may not match what is on the card", file=sys.stderr)
        yield _Claim(disk, None)
        return

    reasons = disk.why_not()
    if reasons:
        raise PiImgError(f"refusing to modify {disk.path}: " + "; ".join(reasons))
    check_access(disk.path, write=True)
    fd = os.open(disk.path, os.O_RDONLY | os.O_CLOEXEC)
    c = _Claim(disk, fd)
    locked = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError:
            raise PiImgError(f"{disk.path} is locked by another program (a partitioner, or another piimg?)")
        for dev, mp in disk.automounts:
            print(f"piimg: unmounting {dev} from {mp}", file=sys.stderr)
            unmount(dev, mp)
        # The kernel's own answer to "is anything still using this disk?": an O_EXCL open
        # of a whole disk fails with EBUSY while any of its partitions is mounted or claimed.
        try:
            os.close(os.open(disk.path, os.O_RDONLY | os.O_EXCL | os.O_CLOEXEC))
        except OSError as e:
            raise PiImgError(f"{disk.path} is still in use ({e.strerror}); nothing was changed")
        with _claims_lock:
            _claims[path] = c
        try:
            yield c
        finally:
            with _claims_lock:
                _claims.pop(path, None)
            with progress.task(f"Flushing {disk.path}"):
                os.fsync(fd)
                try:
                    fcntl.ioctl(fd, BLKFLSBUF)
                except OSError:
                    pass  # needs CAP_SYS_ADMIN; fsync already did what matters
    finally:
        if locked:
            fcntl.flock(fd, fcntl.LOCK_UN)
            if c.reread:
                try:
                    fcntl.ioctl(fd, BLKRRPART)  # let the host see the changed partitions
                except OSError:
                    pass  # e.g. something grabbed a partition already; udev re-reads it later
        os.close(fd)


@contextmanager
def maybe_claim(path, readonly=False):
    """claim() if `path` is a block device, else nothing."""
    if is_block_device(path):
        with claim(path, readonly) as c:
            yield c
    else:
        yield None
