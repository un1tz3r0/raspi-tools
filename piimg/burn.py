"""Write an image (raw or a qcow2 overlay, flattened on the fly) to a disk such
as an SD card or USB stick, and optionally grow its root filesystem to fill it.

This is the one piimg command that needs root: only root can write to
/dev/sdX. Safety rules, all checked before anything is written:

  - the target must be a whole disk, not a partition, and not read-only;
  - it must not hold anything the running system uses: refused if any part of
    it is mounted outside /media, /run/media or /mnt, is swap, or is used by
    device-mapper (LUKS, LVM) or RAID; no source image file may live on it;
  - it must be at least as large as the image;
  - the user confirms (or passes --yes); then partitions mounted under those
    removable-media directories, i.e. desktop automounts, are unmounted.

While writing, piimg holds an exclusive BSD lock (flock) on the disk. That is
systemd-udevd's documented protocol (https://systemd.io/BLOCK_DEVICE_LOCKING/):
udev holds back its events for a locked disk, so the desktop cannot probe or
automount half-written partitions, or mount the root filesystem while it is
being resized. When the lock is released, the kernel re-reads the new
partition table and the desktop sees the finished card.
"""
import fcntl
import os
import subprocess
import sys
from pathlib import Path

from . import progress
from .disks import (AUTOMOUNT_DIRS, BLKFLSBUF, BLKRRPART, Disk, find_disk, list_disks,  # noqa: F401
                    print_disks, sudo_hint as _sudo_hint)
from .guest import Image, PiImgError
from .image import backing_chain, grow_last_partition, image_info, qemu_img


def _confirm(disk, image, expand):
    lines = [f"This will ERASE everything on {disk.describe()}",
             f"and write {image} to it."]
    if disk.automounts:
        lines.append("These will be unmounted first: "
                     + ", ".join(f"{dev} ({mp})" for dev, mp in disk.automounts))
    if expand:
        lines.append("Then the last partition and its filesystem are grown to fill the disk.")
    print("\n".join(lines), file=sys.stderr)
    if not sys.stdin.isatty():
        raise PiImgError("not confirmed: stdin is not a terminal; pass --yes to confirm")
    try:
        answer = input(f"Type the device name ({disk.name}) to continue: ")
    except EOFError:
        answer = ""
    if answer.strip() not in (disk.name, disk.path):
        raise PiImgError("not confirmed; nothing was written")


def burn(image, device, expand=False, assume_yes=False):
    """Write `image` to the whole disk `device`. Returns a short report."""
    image = Path(image)
    info = image_info(image)
    vsize = info["virtual-size"]
    disk = find_disk(device)

    reasons = disk.why_not()
    if reasons:
        raise PiImgError(f"refusing to write to {disk.path}: " + "; ".join(reasons))
    chain_devs = {f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}"
                  for st in (p.stat() for p in backing_chain(image))}
    if chain_devs & disk.devnums:
        raise PiImgError(f"{image} (or its base image) is stored on {disk.path}; copy it elsewhere first")
    if disk.size < vsize:
        raise PiImgError(f"{disk.path} is {progress.fmt_size(disk.size)}, smaller than the image "
                         f"({progress.fmt_size(vsize)})")
    if os.geteuid() != 0:  # writing, unmounting and the flush/re-read ioctls all need root
        raise PiImgError(f"writing to {disk.path} needs root. Run:\n  {_sudo_hint()}")

    if not assume_yes:
        _confirm(disk, image, expand)

    fd = os.open(disk.path, os.O_RDONLY | os.O_CLOEXEC)
    locked = False
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except BlockingIOError:
            raise PiImgError(f"{disk.path} is locked by another program (a partitioner, or another burn?)")

        for dev, mp in disk.automounts:
            r = subprocess.run(["umount", mp], capture_output=True, text=True)
            if r.returncode:
                raise PiImgError(f"could not unmount {dev} from {mp}: {r.stderr.strip()}")
        # The kernel's own answer to "is anything still using this disk?": an O_EXCL open of
        # a whole disk fails with EBUSY while any of its partitions is mounted or claimed.
        try:
            os.close(os.open(disk.path, os.O_RDONLY | os.O_EXCL | os.O_CLOEXEC))
        except OSError as e:
            raise PiImgError(f"{disk.path} is still in use ({e.strerror}); nothing was written")

        # -n: the target exists (don't create it); -t none: O_DIRECT, so progress shows data
        # that really reached the card rather than data parked in the page cache.
        qemu_img(["convert", "-n", "-t", "none", "-f", info["format"], "-O", "raw", image, disk.path],
                 f"Writing {image.name} to {disk.path}", total=vsize)
        report = f"wrote {progress.fmt_size(vsize)} to {disk.path}"
        expand_error = None
        if expand:
            try:
                with Image(disk.path, format="raw", mount=False, claim=False) as img:
                    report += "; " + grow_last_partition(img)
            except PiImgError as e:
                expand_error = e
        with progress.task(f"Flushing {disk.path}"):
            os.fsync(fd)
            fcntl.ioctl(fd, BLKFLSBUF)
        if expand_error:
            raise PiImgError(f"{report}, but growing the root filesystem failed: {expand_error}\n"
                             "The card holds the complete image and is usable; Raspberry Pi OS grows "
                             "the root filesystem itself on first boot (cmdline.txt 'resize').")
    finally:
        if locked:
            fcntl.flock(fd, fcntl.LOCK_UN)
            try:
                fcntl.ioctl(fd, BLKRRPART)  # let the host see the new partitions
            except OSError:
                pass  # e.g. something grabbed a partition already; udev re-reads it later
        os.close(fd)
    return report
