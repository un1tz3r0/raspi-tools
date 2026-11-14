"""Offline editing of Raspberry Pi OS images with libguestfs (no root needed).

Everything here works on the image file directly. Nothing inside the image is
executed (the libguestfs appliance is x86 and cannot run arm binaries); see
docs/DESIGN.md for the "offline first" rule.
"""
import atexit
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path

from . import progress
from .errors import PiImgError


def _import_guestfs():
    """Import the libguestfs Python bindings (apt: python3-guestfs), even from a
    venv that can't see them: see sysimport.import_system."""
    from .sysimport import import_system
    try:
        return import_system("guestfs")
    except ImportError:
        pass
    v = f"{sys.version_info.major}.{sys.version_info.minor}"
    raise PiImgError(
        "cannot import guestfs (libguestfs Python bindings).\n"
        "Install them with: sudo apt install python3-guestfs\n"
        f"They must match this Python ({sys.executable}, {v}); a uv-managed or "
        "different-version interpreter cannot load the distro build.")


# ---------------------------------------------------------------- host setup

def _ensure_supermin_kernel():
    """Make libguestfs usable by an unprivileged user on Debian/Ubuntu.

    Ubuntu ships /boot/vmlinuz-* as mode 0600, so supermin cannot build its
    appliance as a normal user. We unpack a copy of the same kernel package
    into ~/.cache/piimg and point supermin at it. No sudo needed.
    """
    if os.environ.get("SUPERMIN_KERNEL"):
        return
    ver = platform.uname().release
    boot_kernel = Path("/boot") / f"vmlinuz-{ver}"
    if not boot_kernel.exists() or os.access(boot_kernel, os.R_OK):
        return  # fine as-is (readable, or not a /boot layout we understand)

    if not (shutil.which("apt-get") and shutil.which("dpkg-deb")):
        raise PiImgError(
            f"{boot_kernel} is not readable, so libguestfs cannot start.\n"
            f"Fix with: sudo chmod +r /boot/vmlinuz-*   (undone by kernel updates)")

    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "piimg" / f"kernel-{ver}"
    kernel = cache / "boot" / f"vmlinuz-{ver}"
    if not kernel.exists():
        cache.mkdir(parents=True, exist_ok=True)
        with progress.task("Fetching a readable copy of the kernel for libguestfs (first run only)"):
            for pkg in (f"linux-image-{ver}", f"linux-image-unsigned-{ver}"):
                r = subprocess.run(["apt-get", "download", pkg], cwd=cache,
                                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
                if r.returncode == 0:
                    break
            else:
                raise PiImgError(
                    f"Could not download a kernel package for {ver} to give libguestfs.\n"
                    f"Fix with: sudo chmod +r /boot/vmlinuz-*")
            for deb in cache.glob("linux-image-*.deb"):
                subprocess.run(["dpkg-deb", "-x", str(deb), str(cache)], check=True)
                deb.unlink()
    os.environ["SUPERMIN_KERNEL"] = str(kernel)
    os.environ["SUPERMIN_KERNEL_VERSION"] = ver
    os.environ["SUPERMIN_MODULES"] = f"/lib/modules/{ver}"


def _borrow_sudo_users_appliance():
    """Under sudo or pkexec, boot the invoking user's already-built libguestfs appliance.

    Otherwise root builds its own (~2 GB in /var/tmp/.guestfs-0, a minute or
    more, and it fails with ENOSPC on a full disk). libguestfs's "fixed
    appliance" mode takes a LIBGUESTFS_PATH directory holding kernel, initrd,
    root and a README.fixed marker; we point one at the user's build. The
    appliance's root disk is only ever used as a read-only backing file.
    """
    from .config import invoking_user
    who = invoking_user()
    if who is None or os.environ.get("LIBGUESTFS_PATH"):
        return
    uid = who.pw_uid
    built = Path(os.environ.get("LIBGUESTFS_CACHEDIR") or "/var/tmp") / f".guestfs-{uid}" / "appliance.d"
    parts = ("kernel", "initrd", "root")
    if not all((built / p).is_file() for p in parts):
        return  # the user never ran piimg; let root build its own
    fixed = Path(tempfile.mkdtemp(prefix="piimg-appliance-"))
    atexit.register(shutil.rmtree, fixed, ignore_errors=True)
    for p in parts:
        (fixed / p).symlink_to(built / p)
    (fixed / "README.fixed").write_text(f"piimg: borrowed from {built} (user {uid})\n")
    os.environ["LIBGUESTFS_PATH"] = str(fixed)


def _image_format(path):
    from .disks import check_access, is_block_device
    if is_block_device(path):
        check_access(path, write=False)
        return "raw"  # a card holds a raw disk image; don't let qemu-img guess
    if not os.path.exists(path):
        raise PiImgError(f"No such image: {path}")
    r = subprocess.run(["qemu-img", "info", "--output=json", str(path)], capture_output=True, text=True)
    if r.returncode:
        raise PiImgError(f"qemu-img cannot read {path}: {r.stderr.strip()}")
    return json.loads(r.stdout)["format"]


# ----------------------------------------------------------------- the image

class Image:
    """An opened, mounted Pi image. Use as a context manager.

    path: an image file, a qcow2 overlay, or a whole disk such as /dev/sdc (an
          SD card taken from a Pi). A disk is claimed while open (disks.claim:
          safety checks, automounts unmounted, exclusive lock) unless claim=False,
          for callers that hold the disk themselves (burn).
    format: the disk format ('raw', 'qcow2'); default: ask qemu-img.
    mount: False opens the disk without inspecting or mounting anything, for
           work on the partitions and filesystems themselves (see grow_last_partition).
    """

    def __init__(self, path, readonly=False, format=None, mount=True, claim=True):
        self.path = Path(path)
        self.readonly = readonly
        self.format = format
        self.mount = mount
        self.claim = claim
        self.g = None
        self.disk = None  # the disks._Claim while a block device is held
        self._claim_cm = None
        self.boot = None  # '/boot/firmware' on bookworm/trixie, '/boot' before

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc):
        self.close(ok=exc[0] is None)

    def open(self):
        if not self.path.exists():
            raise PiImgError(f"No such image: {self.path}")
        from .disks import is_block_device, maybe_claim
        if self.claim and is_block_device(self.path):
            self._claim_cm = maybe_claim(self.path, self.readonly)
            self.disk = self._claim_cm.__enter__()
        try:
            self._launch()
        except BaseException:
            if self.g is not None:
                self.g.close()
                self.g = None
            self._release(*sys.exc_info())
            raise

    def _launch(self):
        _ensure_supermin_kernel()
        _borrow_sudo_users_appliance()
        self.__dict__.pop("_dpkg_status", None)
        guestfs = _import_guestfs()
        g = guestfs.GuestFS(python_return_dict=True)
        g.add_drive_opts(str(self.path), format=self.format or _image_format(self.path),
                         readonly=self.readonly)
        # Slow: the first launch builds libguestfs's appliance, later ones just boot it.
        with progress.task("Starting libguestfs appliance"):
            try:
                g.launch()
            except RuntimeError as e:
                g.close()
                raise PiImgError(f"libguestfs failed to start: {e}\n"
                                 f"Run with LIBGUESTFS_DEBUG=1 LIBGUESTFS_TRACE=1 for details.")
        self.g = g
        if self.mount:
            self._mount()

    def _release(self, *exc):
        cm, self._claim_cm, self.disk = self._claim_cm, None, None
        if cm is not None:
            cm.__exit__(*(exc or (None, None, None)))

    def _mount(self):
        g = self.g
        roots = g.inspect_os()
        if len(roots) != 1:
            raise PiImgError(f"Expected one OS in the image, found {len(roots)}")
        mps = g.inspect_get_mountpoints(roots[0])
        for mp in sorted(mps, key=len):  # '/' first
            (g.mount_ro if self.readonly else g.mount)(mps[mp], mp)
        if "/boot/firmware" in mps:
            self.boot = "/boot/firmware"
        elif "/boot" in mps:
            self.boot = "/boot"
        else:
            raise PiImgError(f"Could not find the boot partition (mount points: {mps})")

    def close(self, ok=True):
        if self.g is None:
            self._release()
            return
        try:
            with progress.task("Closing image" if self.readonly else "Writing changes to image"):
                if not self.readonly:
                    self.g.sync()
                self.g.umount_all()
                self.g.shutdown()
        finally:
            self.g.close()
            self.g = None
            self._release()

    # ------------------------------------------------------------ file access

    def exists(self, path):
        return self.g.exists(path)

    def lexists(self, path):
        return self.g.is_symlink(path) or self.g.exists(path)

    def read(self, path):
        return self.g.read_file(path)

    def read_text(self, path):
        return self.read(path).decode("utf-8")

    def listdir(self, path):
        return self.g.ls(path)

    def write(self, path, data, mode=0o644, uid=0, gid=0):
        """Write a file, creating parent dirs. str is encoded as UTF-8."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        parent = os.path.dirname(path)
        if parent and not self.g.is_dir(parent):
            self.g.mkdir_p(parent)
        self.g.write(path, data)
        # vfat (boot partition) has no modes/owners; these are harmless no-ops there
        self.g.chmod(mode, path)
        self.g.chown(uid, gid, path)

    def replace_text(self, path, text):
        """Rewrite an existing file, keeping its mode and owner."""
        st = self.g.statns(path)
        self.write(path, text, mode=st["st_mode"] & 0o7777, uid=st["st_uid"], gid=st["st_gid"])

    def symlink(self, target, link):
        parent = os.path.dirname(link)
        if parent and not self.g.is_dir(parent):
            self.g.mkdir_p(parent)
        self.g.ln_sf(target, link)

    def append_line_once(self, path, line):
        text = self.read_text(path) if self.exists(path) else ""
        if line in text.splitlines():
            return False
        if text and not text.endswith("\n"):
            text += "\n"
        self.write(path, text + line + "\n", mode=0o644)
        return True

    # ------------------------------------------------------- Pi conveniences

    @property
    def config_txt(self):
        return f"{self.boot}/config.txt"

    @property
    def cmdline_txt(self):
        return f"{self.boot}/cmdline.txt"

    def config_txt_add(self, line, section="all"):
        """Ensure `line` is present in the given [section] of config.txt.

        Added at the end of that section (the last one with that name); the
        section is created at the end of the file if missing. Returns True if
        the file changed.
        """
        lines = self.read_text(self.config_txt).splitlines()
        header = f"[{section}]"
        hdr_idx = [i for i, l in enumerate(lines) if re.fullmatch(r"\s*\[.*\]\s*", l)]
        starts = [i for i in hdr_idx if lines[i].strip() == header]

        if not starts:
            if section == "all" and not hdr_idx:
                start, end = -1, len(lines)  # no sections at all: top level is [all]
            else:
                lines += ["", header, line]
                self.write(self.config_txt, "\n".join(lines) + "\n")
                return True
        else:
            start = starts[-1]
            end = next((i for i in hdr_idx if i > start), len(lines))

        if line in (l.strip() for l in lines[start + 1:end]):
            return False
        # insert after the last non-blank line of the block
        ins = end
        while ins > start + 1 and not lines[ins - 1].strip():
            ins -= 1
        lines.insert(ins, line)
        self.write(self.config_txt, "\n".join(lines) + "\n")
        return True

    def cmdline_add(self, param):
        """Add a kernel parameter to cmdline.txt (single line) if absent."""
        words = self.read_text(self.cmdline_txt).split()
        if param in words:
            return False
        self.write(self.cmdline_txt, " ".join(words + [param]) + "\n")
        return True

    def cmdline_remove(self, param):
        """Remove a kernel parameter. A trailing '=' removes any value
        (e.g. 'console=' removes every console=... word)."""
        words = self.read_text(self.cmdline_txt).split()
        keep = [w for w in words if not (w == param or (param.endswith("=") and w.startswith(param)))]
        if keep == words:
            return False
        self.write(self.cmdline_txt, " ".join(keep) + "\n")
        return True

    def config_var(self, key, value):
        """raspi-config's set_config_var: every line `[#]key=...` becomes
        `key=value` (commented ones too); appended if there is none."""
        lines = self.read_text(self.config_txt).splitlines()
        pat = re.compile(r"^#?[ \t]*" + re.escape(key) + "=")
        new, hit = [], False
        for l in lines:
            if pat.match(l):
                new.append(f"{key}={value}"); hit = True
            else:
                new.append(l)
        if not hit:
            new.append(f"{key}={value}")
        if new != lines:
            self.write(self.config_txt, "\n".join(new) + "\n")
            return True
        return False

    def sub(self, path, pattern, repl, count=0, flags=re.M):
        """Regex-substitute in a text file. Returns the number of substitutions."""
        text = self.read_text(path)
        new, n = re.subn(pattern, repl, text, count=count, flags=flags)
        if n and new != text:
            self.replace_text(path, new)
        return n

    def lookup_owner(self, owner):
        """'user[:group]' with names or numbers -> (uid, gid), resolved from the image."""
        user, _, group = str(owner).partition(":")

        def resolve(token, dbfile):
            if token.isdigit():
                return int(token)
            for line in self.read_text(dbfile).splitlines():
                f = line.split(":")
                if f[0] == token:
                    return int(f[2])
            raise PiImgError(f"No such {'user' if 'passwd' in dbfile else 'group'} in the image: {token!r}")

        uid = resolve(user, "/etc/passwd")
        gid = resolve(group, "/etc/group") if group else (uid if not user.isdigit() and user != "root" else 0)
        if not group and user != "root" and not user.isdigit():
            # default to the user's primary group, like chown user
            for line in self.read_text("/etc/passwd").splitlines():
                f = line.split(":")
                if f[0] == user:
                    gid = int(f[3])
        return uid, gid

    def disable_unit(self, unit):
        """Remove the .wants/.requires symlinks `systemctl enable` makes."""
        if "." not in unit:
            unit += ".service"
        removed = []
        for d in self.listdir("/etc/systemd/system"):
            if d.endswith((".wants", ".requires")):
                link = f"/etc/systemd/system/{d}/{unit}"
                if self.lexists(link):
                    self.g.rm(link)
                    removed.append(link)
        return removed

    def mask_unit(self, unit):
        if "." not in unit:
            unit += ".service"
        p = f"/etc/systemd/system/{unit}"
        if self.exists(p) and not self.g.is_symlink(p):
            # systemctl refuses this too: the mask link would replace the unit file
            raise PiImgError(f"Cannot mask {unit}: its unit file is {p} (a local unit); disable it instead")
        self.symlink("/dev/null", p)

    def unmask_unit(self, unit):
        """Remove a /dev/null mask link; False if the unit was not masked."""
        if "." not in unit:
            unit += ".service"
        p = f"/etc/systemd/system/{unit}"
        if self.g.is_symlink(p) and self.g.readlink(p) == "/dev/null":
            self.g.rm(p)
            return True
        return False

    def package_installed(self, name):
        if not hasattr(self, "_dpkg_status"):
            self._dpkg_status = self.read_text("/var/lib/dpkg/status")
        for blk in self._dpkg_status.split("\n\n"):
            if re.search(rf"^Package: {re.escape(name)}$", blk, re.M):
                return bool(re.search(r"^Status: .* installed$", blk, re.M))
        return False

    UNIT_DIRS = ("/etc/systemd/system", "/usr/local/lib/systemd/system", "/usr/lib/systemd/system",
                 "/lib/systemd/system")

    def find_unit(self, unit):
        """Path of the unit file for `unit` (for an instance like getty@tty1.service,
        its template getty@.service), or None."""
        names = [unit]
        m = re.fullmatch(r"([^@]+@)[^.]+(\.\w+)", unit)
        if m:
            names.append(m.group(1) + m.group(2))
        for name in names:
            for d in self.UNIT_DIRS:
                p = f"{d}/{name}"
                if self.exists(p) and not (self.g.is_symlink(p) and self.g.readlink(p) == "/dev/null"):
                    return p
        return None

    def enable_unit(self, unit):
        """Equivalent of `systemctl enable UNIT`, done by creating the symlinks
        named in the unit's [Install] WantedBy=/RequiredBy=."""
        if "." not in unit:
            unit += ".service"
        src = self.find_unit(unit)
        if src is None:
            raise PiImgError(f"Unit {unit} not found in the image")
        text = self.read_text(src)
        install = re.search(r"^\[Install\]\s*$(.*?)(?=^\[|\Z)", text, re.S | re.M)
        targets = []
        if install:
            for key, val in re.findall(r"^(WantedBy|RequiredBy)\s*=\s*(.*)$", install.group(1), re.M):
                targets += [(key, t) for t in val.split()]
        if not targets:
            raise PiImgError(f"Unit {unit} has no WantedBy=/RequiredBy= to enable")
        for key, target in targets:
            suffix = "wants" if key == "WantedBy" else "requires"
            self.symlink(src, f"/etc/systemd/system/{target}.{suffix}/{unit}")
        return [t for _, t in targets]

    def nm_connection(self, conn_id, sections, mode=0o600):
        """Write a NetworkManager keyfile profile. `sections` is an ordered dict
        {section: {key: value}} (connection.id/uuid are filled in)."""
        conn = {"id": conn_id, "uuid": str(uuid.uuid5(uuid.NAMESPACE_DNS, "piimg:" + conn_id))}
        conn.update(sections.get("connection", {}))
        out = ["[connection]"] + [f"{k}={v}" for k, v in conn.items()]
        for name, kv in sections.items():
            if name == "connection":
                continue
            out += ["", f"[{name}]"] + [f"{k}={v}" for k, v in kv.items()]
        # NM requires keyfiles to be root:root 0600 or it ignores them
        self.write(f"/etc/NetworkManager/system-connections/{conn_id}.nmconnection",
                   "\n".join(out) + "\n", mode=mode)
