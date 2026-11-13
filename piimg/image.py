"""Whole-image helpers: unpack downloads, make copy-on-write overlays, flatten
an overlay into a standalone image, and grow an image's root filesystem."""
import gzip
import json
import lzma
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path

from . import progress
from .guest import Image, PiImgError

MiB = 1 << 20
# MBR stores a partition's start and length as 32-bit sector counts, so nothing
# can end past sector 2^32 - 1 (2 TiB with 512-byte sectors). The Pi's own
# first-boot resize (initramfs resize_early) applies the same cap.
MBR_LAST_SECTOR = 2**32 - 1
GPT_BACKUP_SECTORS = 33  # backup partition entries (32) + backup header (1) at the end of the disk


def image_info(path):
    """qemu-img's view of an image: dict with at least 'format' and 'virtual-size'."""
    path = Path(path)
    if not path.exists():
        raise PiImgError(f"No such image: {path}")
    r = subprocess.run(["qemu-img", "info", "--output=json", str(path)], capture_output=True, text=True)
    if r.returncode:
        raise PiImgError(f"qemu-img cannot read {path}: {r.stderr.strip()}")
    return json.loads(r.stdout)


def backing_chain(path):
    """The image and every backing file under it, as absolute Paths."""
    r = subprocess.run(["qemu-img", "info", "--backing-chain", "--output=json", str(path)],
                       capture_output=True, text=True)
    if r.returncode:
        raise PiImgError(f"qemu-img cannot read {path}: {r.stderr.strip()}")
    return [Path(i["filename"]).resolve() for i in json.loads(r.stdout)]


_PROGRESS_RE = re.compile(rb"\((\d+(?:\.\d+)?)/100%\)")


def qemu_img(args, description, total):
    """Run `qemu-img <subcommand> -p ...` and show its progress as a task.

    `args[0]` is the qemu-img subcommand; -p is inserted after it. qemu-img
    prints `(12.34/100%)` on stdout, roughly every 1%, which is turned into
    bytes of `total`.
    """
    cmd = ["qemu-img", args[0], "-p", *map(str, args[1:])]
    with tempfile.TemporaryFile() as err, \
            progress.task(description, total=total, as_bytes=True, stall=(30, 120)) as t:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=err)
        try:
            tail = b""
            while chunk := os.read(proc.stdout.fileno(), 4096):
                tail = (tail + chunk)[-256:]
                found = _PROGRESS_RE.findall(tail)
                if found:
                    t.update(int(float(found[-1]) / 100 * total))
            rc = proc.wait()
        finally:
            if proc.poll() is None:  # interrupted: don't leave qemu-img writing behind our back
                proc.terminate()
                proc.wait()
        if rc:
            err.seek(0)
            msg = err.read().decode(errors="replace").strip()
            raise PiImgError(f"qemu-img {args[0]} failed: {msg or f'exit status {rc}'}")
        t.update(total)


def unpack(src, dst=None):
    """Decompress .img.xz / .img.gz / .zip (containing one .img) to a raw .img."""
    src = Path(src)
    if dst is None:
        dst = src.with_suffix("") if src.suffix in (".xz", ".gz") else src.with_suffix(".img")
    dst = Path(dst)
    if dst.exists():
        raise PiImgError(f"{dst} already exists")
    tmp = dst.with_name(dst.name + ".part")
    try:
        with open(src, "rb") as raw:
            # The uncompressed size of an .xz/.gz is not known up front, so progress is
            # measured on the compressed side: how much of the download has been consumed.
            if src.suffix == ".xz":
                fin, total, position = lzma.LZMAFile(raw), os.fstat(raw.fileno()).st_size, raw.tell
            elif src.suffix == ".gz":
                fin, total, position = gzip.GzipFile(fileobj=raw), os.fstat(raw.fileno()).st_size, raw.tell
            elif src.suffix == ".zip":
                z = zipfile.ZipFile(raw)
                imgs = [n for n in z.namelist() if n.endswith(".img")]
                if len(imgs) != 1:
                    raise PiImgError(f"Expected one .img in {src}, found {imgs}")
                fin, total = z.open(imgs[0]), z.getinfo(imgs[0]).file_size
                position = lambda: written  # a zip member is read by seeking, so count output instead
            else:
                raise PiImgError(f"Don't know how to unpack {src.suffix!r} files")
            written = 0
            with progress.task(f"Unpacking {src.name}", total=total, as_bytes=True,
                               stall=(10, 60)) as t, fin, open(tmp, "wb") as fout:
                while chunk := fin.read(1 << 20):
                    fout.write(chunk)
                    written += len(chunk)
                    t.update(position(), note=f"{progress.fmt_size(written)} written")
                t.result = f"{progress.fmt_size(written)} written"
        tmp.rename(dst)
    finally:
        tmp.unlink(missing_ok=True)
    return dst


def make_overlay(base, overlay):
    """Create a qcow2 overlay on `base` (an image file, an overlay, or a card such as
    /dev/sdc), so edits never touch the base. The base is only ever read."""
    from .guest import _image_format
    base, overlay = Path(base).resolve(), Path(overlay)
    if overlay.exists():
        raise PiImgError(f"{overlay} already exists")
    if not base.exists():
        raise PiImgError(f"No such image: {base}")
    r = subprocess.run(["qemu-img", "create", "-q", "-f", "qcow2", "-b", str(base),
                        "-F", _image_format(base), str(overlay)], capture_output=True, text=True)
    if r.returncode:
        raise PiImgError(f"qemu-img create failed: {r.stderr.strip()}")
    return overlay


def ensure_overlay(base, overlay):
    """Use `overlay` as the working copy of `base`: create it if missing, otherwise
    check that it really is an overlay of `base`. Returns the overlay's Path."""
    overlay = Path(overlay)
    if not overlay.exists():
        return make_overlay(base, overlay)
    info = image_info(overlay)
    backing = info.get("full-backing-filename") or info.get("backing-filename")
    if info["format"] != "qcow2" or not backing:
        raise PiImgError(f"{overlay} exists and is not a qcow2 overlay")
    if Path(backing if Path(backing).is_absolute() else overlay.parent / backing).resolve() != Path(base).resolve():
        raise PiImgError(f"{overlay} is an overlay of {backing}, not of {base}")
    return overlay


def commit(overlay):
    """Write an overlay's changes into its base image (which may be a card), then
    empty the overlay. Returns a one-line report."""
    from .disks import maybe_claim
    overlay = Path(overlay)
    info = image_info(overlay)
    backing = info.get("full-backing-filename") or info.get("backing-filename")
    if not backing:
        raise PiImgError(f"{overlay} is not an overlay (it has no base image)")
    base = Path(backing if Path(backing).is_absolute() else overlay.parent / backing)
    with maybe_claim(base, readonly=False):
        qemu_img(["commit", "-f", "qcow2", overlay], f"Writing {overlay.name}'s changes into {base}",
                 total=info["virtual-size"])
    return f"committed {overlay} into {base}; the overlay is now empty"


def diff(overlay):
    """List the files an overlay changes relative to its base (virt-diff)."""
    import shutil
    from .guest import _ensure_supermin_kernel, _image_format
    from .disks import check_access, is_block_device
    info = image_info(overlay)
    backing = info.get("full-backing-filename") or info.get("backing-filename")
    if not backing:
        raise PiImgError(f"{overlay} is not an overlay (it has no base image)")
    if not shutil.which("virt-diff"):
        raise PiImgError("virt-diff is not installed (sudo apt install guestfs-tools)")
    if is_block_device(backing):
        check_access(backing, write=False)
    _ensure_supermin_kernel()
    cmd = ["virt-diff", "--format=" + _image_format(backing), "-a", str(backing),
           "--format=qcow2", "-A", str(overlay)]
    with progress.task(f"Comparing {Path(overlay).name} with its base"):
        r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode not in (0,):
        raise PiImgError(f"virt-diff failed: {r.stderr.strip()}")
    return r.stdout.rstrip() or "no differences"


def flatten(src, dst, fmt=None, compress=False):
    """Write `src` (usually an overlay) with all of its backing chain merged in to
    a new standalone image `dst`. The sources are not changed.

    fmt: 'raw' (default; sparse, ready for dd or Raspberry Pi Imager) or 'qcow2'
         (default when dst ends in .qcow2; no backing file).
    compress: zlib-compress the qcow2 clusters (smaller, slower to write).
    """
    src, dst = Path(src), Path(dst)
    if dst.exists():
        raise PiImgError(f"{dst} already exists")
    fmt = fmt or ("qcow2" if dst.suffix == ".qcow2" else "raw")
    if compress and fmt != "qcow2":
        raise PiImgError("--compress only works with qcow2 output")
    info = image_info(src)
    tmp = dst.with_name(dst.name + ".part")
    try:
        qemu_img(["convert", "-f", info["format"], "-O", fmt, *(["-c"] if compress else []), src, tmp],
                 f"Flattening {src.name} into {dst.name}", total=info["virtual-size"])
        tmp.rename(dst)
    finally:
        tmp.unlink(missing_ok=True)
    return dst


_SIZE_RE = re.compile(r"(\+)?(\d+(?:\.\d+)?)\s*([KMGT])(?:i?B)?", re.I)


def parse_size(text, current):
    """'8G' (absolute) or '+2G' (grow by), binary units K/M/G/T like qemu-img.
    Returns bytes, rounded up to a whole MiB."""
    m = _SIZE_RE.fullmatch(text.strip())
    if not m:
        raise PiImgError(f"Bad size {text!r}: use e.g. 8G (total) or +2G (grow by); units K, M, G, T")
    n = float(m.group(2)) * 1024 ** ("KMGT".index(m.group(3).upper()) + 1)
    n = int(n) + (current if m.group(1) else 0)
    return -(-n // MiB) * MiB


def resize(image, size=None):
    """Grow an image to `size` (see parse_size), then grow its last partition and
    the ext4 filesystem in it to fill the new space. Shrinking is not supported.

    size None or "max": leave the disk's size alone and only grow the last
    partition to fill it (for a card: use the whole card).

    On a qcow2 overlay only the overlay is written: its virtual disk may be
    larger than the backing file, and the extra space reads as zeros. The base
    image is not changed. A raw image is grown in place.
    Returns grow_last_partition()'s report.
    """
    from .disks import is_block_device
    image = Path(image)
    info = image_info(image)
    current = info["virtual-size"]
    if size in (None, "", "max"):
        with Image(image, format=info["format"], mount=False) as img:
            if img.disk is not None:
                img.disk.reread = True
            return grow_last_partition(img)
    if is_block_device(image):
        raise PiImgError(f"{image} is a disk; its size is fixed. Use 'max' to grow the root "
                         "filesystem to fill it")
    new = parse_size(size, current)
    if new < current:
        raise PiImgError(f"{image} is {progress.fmt_size(current)}; shrinking to "
                         f"{progress.fmt_size(new)} is not supported")
    if new > current:
        r = subprocess.run(["qemu-img", "resize", "-q", "-f", info["format"], str(image), str(new)],
                           capture_output=True, text=True)
        if r.returncode:
            raise PiImgError(f"qemu-img resize failed: {r.stderr.strip()}")
    with Image(image, format=info["format"], mount=False) as img:
        return grow_last_partition(img)


def grow_last_partition(img):
    """Grow the last partition on the disk to the end of the disk, then the
    ext2/3/4 filesystem in it to fill the partition. `img` is an Image opened
    with mount=False. On Raspberry Pi OS the last partition is the root fs.

    The partition keeps its start sector and number, and the MBR disk ID is
    kept, so root=PARTUUID=... in cmdline.txt and /etc/fstab stay valid.
    Returns a one-line report.
    """
    g = img.g
    dev = g.list_devices()[0]
    ptype = g.part_get_parttype(dev)
    parts = g.part_list(dev)
    if not parts:
        raise PiImgError("The image has no partitions")
    last = max(parts, key=lambda p: p["part_start"])
    num = last["part_num"]
    if ptype == "msdos" and num > 4:
        raise PiImgError(f"The last partition ({num}) is a logical partition inside an extended one; "
                         "growing that is not supported")
    part = next(p for p in g.list_partitions()
                if g.part_to_dev(p) == dev and g.part_to_partnum(p) == num)
    fstype = g.vfs_type(part)
    if fstype not in ("ext2", "ext3", "ext4"):
        raise PiImgError(f"The last partition ({part}) holds {fstype or 'no filesystem'}; "
                         "only ext2/3/4 can be grown")

    def fs_bytes():
        sb = g.tune2fs_l(part)
        return int(sb["Block count"]) * int(sb["Block size"])

    with progress.task(f"Checking the filesystem on partition {num}"):
        g.e2fsck_f(part)  # resize2fs insists on a freshly checked filesystem
    old_fs = fs_bytes()

    if ptype == "gpt":
        g.part_expand_gpt(dev)  # move the backup GPT to the (new) end of the disk
    ss = g.blockdev_getss(dev)
    disk_sectors = g.blockdev_getsize64(dev) // ss
    target_end = disk_sectors - 1
    if ptype == "gpt":
        target_end -= GPT_BACKUP_SECTORS
    elif ptype == "msdos":
        target_end = min(target_end, MBR_LAST_SECTOR)
    cur_end = last["part_end"] // ss
    if target_end > cur_end:
        with progress.task(f"Growing partition {num}"):
            g.part_resize(dev, num, target_end)

    part_bytes = g.blockdev_getsize64(part)
    if part_bytes - old_fs >= MiB:
        with progress.task(f"Growing the {fstype} filesystem on partition {num}"):
            g.resize2fs(part)
    new_fs = fs_bytes()
    if new_fs == old_fs:
        return f"partition {num} already fills the disk ({progress.fmt_size(new_fs)} filesystem)"
    return (f"partition {num}: {fstype} filesystem grown from {progress.fmt_size(old_fs)} "
            f"to {progress.fmt_size(new_fs)}")
