"""The piimg command line.

Every subcommand is an argparse subparser carrying a `Meta` (as `parser.piimg`)
and optional hints on its arguments (`action.kind`, ...). The TUI (tui.py)
builds its command list and its parameter forms from this same parser, so a
command added here shows up there too. See `Meta` for what the TUI uses.

IMAGE, wherever it appears, may be an image file, a qcow2 overlay, or the SD
card / USB disk of a Pi (/dev/sdX, /dev/mmcblkN; needs root). See disks.py
for how cards are protected while piimg works on them.
"""
import argparse
import getpass
import os
import sys
from dataclasses import dataclass, field
from typing import Callable, Optional, Union

from . import output, progress
from .guest import Image, PiImgError
from .image import commit, diff, flatten, make_overlay, resize, unpack
from .recipes import REGISTRY

IMAGE_HELP = "image file, qcow2 overlay, or a Pi's SD card such as /dev/sdc (a card needs root)"
GROUPS = ("Inspect", "Repair & configure", "Files", "Recipes", "Image & card", "Official images")


@dataclass
class Meta:
    """How the TUI presents and runs a subcommand.

    group:   heading it is listed under (GROUPS)
    output:  'log' (scrolling text), 'table' (one table, shown as a grid),
             'text' (a document, shown in a pager), or 'terminal' (needs the
             real terminal, e.g. an editor: the TUI suspends itself)
    writes:  True if running it changes the target; or a function of the parsed
             args (e.g. `fsck` only writes with --repair)
    confirm: optional function(args) -> str: a warning to confirm before running
    typed_confirm: optional function(args) -> str the user must type to confirm
    yes_flag: flag the TUI adds once it has confirmed (so the command won't ask)
    abort:   'yes' (Stop button), 'warn' (Stop asks first: stopping may leave
             things half done) or 'no'
    target:  dest of the positional the TUI fills with the open image (None: none)
    tui:     False hides the command from the TUI
    """
    group: str = "Inspect"
    output: str = "log"
    writes: Union[bool, Callable] = False
    confirm: Optional[Callable] = None
    typed_confirm: Optional[Callable] = None
    yes_flag: Optional[str] = None
    abort: str = "yes"
    target: Optional[str] = "image"
    tui: bool = True
    func: Optional[Callable] = None
    extra: dict = field(default_factory=dict)

    def writes_for(self, args):
        return self.writes(args) if callable(self.writes) else bool(self.writes)


def _arg(p, *names, kind=None, stdin_flag=None, tui=True, **kw):
    """add_argument with TUI hints:
    kind: 'image', 'file' (local, must exist), 'newfile' (local, created),
          'dir', 'device', 'path' (inside the image), 'secret' (masked),
          'recipe', 'size', 'unit', 'user', 'lines' (multi-line text)
    stdin_flag: for a secret, the flag that makes the command read it from
          stdin; the TUI passes it that way instead of on the command line
    tui:  False hides the argument from the TUI's forms
    """
    a = p.add_argument(*names, **kw)
    a.kind, a.stdin_flag, a.tui = kind, stdin_flag, tui
    return a


def _image(p, **kw):
    return _arg(p, "image", kind="image", help=kw.pop("help", IMAGE_HELP), **kw)


def _cmd(sub, name, help, func, description=None, **meta):
    p = sub.add_parser(name, help=help, description=description or help[0].upper() + help[1:] + ".")
    p.piimg = Meta(func=func, **meta)
    p.set_defaults(func=func, meta=p.piimg)
    return p


# ------------------------------------------------------------------ helpers

def _read_secret(args, what="password", confirm=True):
    """The secret from --password-stdin, --password, or a prompt."""
    if getattr(args, "password_stdin", False):
        line = sys.stdin.readline()
        if not line:
            raise PiImgError(f"--password-stdin: no {what} on stdin")
        return line.rstrip("\r\n")
    if getattr(args, "password", None):
        return args.password
    if not sys.stdin.isatty():
        return None
    pw = getpass.getpass(f"New {what}: ")
    if confirm and getpass.getpass(f"Repeat {what}: ") != pw:
        raise PiImgError(f"the {what}s do not match")
    return pw


def _secret_options(p, what="password"):
    g = p.add_mutually_exclusive_group()
    _arg(g, "--password", kind="secret", stdin_flag="--password-stdin",
         help=f"the {what} (visible in your shell history; prefer the prompt or --password-stdin)")
    _arg(g, "--password-stdin", action="store_true", tui=False, help=f"read the {what} from the first line of stdin")
    return g


def _open(args, readonly=False, **kw):
    return Image(args.image, readonly=readonly, **kw)


def _direct_card(args):
    from .disks import is_block_device
    path = getattr(args, "image", None)
    return path is not None and is_block_device(path)


# ------------------------------------------------------------------ handlers

def h_unpack(a): return str(unpack(a.src, a.dst))
def h_overlay(a): return str(make_overlay(a.image, a.overlay))
def h_flatten(a): return str(flatten(a.image, a.output, fmt=a.format, compress=a.compress))
def h_resize(a): return f"{a.image}: {resize(a.image, a.size)}"
def h_commit(a): return commit(a.image)
def h_diff(a): return diff(a.image)


def h_burn(a):
    from .burn import burn, print_disks
    if a.device is None:
        print_disks()
        return None
    return burn(a.image, a.device, expand=a.expand, assume_yes=a.yes)


def h_disks(a):
    from .disks import disks_table
    return disks_table()


def h_ls(a):
    with _open(a, readonly=True) as img:
        if a.long:
            from .output import Table
            t = Table(["mode", "owner", "size", "name"], title=a.path)
            for name in img.listdir(a.path):
                p = f"{a.path.rstrip('/')}/{name}"
                st = img.g.lstatns(p)
                kind = "l" if img.g.is_symlink(p) else "d" if img.g.is_dir(p) else "-"
                t.add(f"{kind}{st['st_mode'] & 0o7777:04o}", f"{st['st_uid']}:{st['st_gid']}", st["st_size"],
                      name + (f" -> {img.g.readlink(p)}" if kind == "l" else "/" if kind == "d" else ""))
            return t
        return "\n".join(img.listdir(a.path))


def h_cat(a):
    with _open(a, readonly=True) as img:
        data = img.read(a.path)
    sys.stdout.buffer.write(data)
    sys.stdout.flush()


def h_run(a):
    from .script import Runner, find_recipe
    variables = dict(v.split("=", 1) for v in a.var if "=" in v)
    if len(variables) != len(a.var):
        raise PiImgError("--var needs NAME=VALUE")
    Runner(a.image, find_recipe(a.recipe), variables, dry_run=a.dry_run).run()


def h_recipes(a):
    import yaml
    from .script import bundled_recipes_dir
    t = output.Table(["recipe", "title", "variables"], title="Bundled recipe files")
    for f in sorted(bundled_recipes_dir().glob("*.y*ml")):
        doc = yaml.safe_load(f.read_text()) or {}
        t.add(f.stem, doc.get("name", ""), ", ".join(doc.get("vars") or {}))
    return t


def h_shell(a):
    import subprocess
    from .disks import maybe_claim
    from .guest import _ensure_supermin_kernel, _image_format
    _ensure_supermin_kernel()
    with maybe_claim(a.image, readonly=False):
        rc = subprocess.run(["guestfish", "--format=" + _image_format(a.image), "-a", a.image, "-i"]).returncode
    if rc:
        raise PiImgError(f"guestfish exited with status {rc}")


def h_clean(a):
    from .live import clean
    return "removed leftovers" if clean(a.image) else "nothing to clean"


def h_apply(a):
    with _open(a) as img:
        REGISTRY[a.recipe].apply(img, a)
    return f"applied {a.recipe} to {a.image}"


def h_info(a):
    from .system import info_table
    with _open(a, readonly=True) as img:
        return info_table(img, a.image)


def h_diagnose(a):
    from .system import diagnose
    with _open(a, readonly=True) as img:
        return diagnose(img, scan=a.scan)


def h_users(a):
    from .system import users_table
    with _open(a, readonly=True) as img:
        return users_table(img, keys=a.keys)


def h_passwd(a):
    from .recipes.userdata import hash_password
    from .system import set_password, set_shell
    if a.shell and not (a.password or a.password_stdin or a.password_hash or a.lock or a.unlock):
        with _open(a) as img:
            return set_shell(img, a.user, a.shell)
    if a.lock or a.unlock:
        pwhash = None
    elif a.password_hash:
        pwhash = a.password_hash
    else:
        pw = _read_secret(a)
        if not pw:
            raise PiImgError("no password given (use --password-stdin, --password or a terminal)")
        pwhash = hash_password(pw)
    with _open(a) as img:
        report = []
        if a.shell:
            report.append(set_shell(img, a.user, a.shell))
        report.append(set_password(img, a.user, pwhash=pwhash, lock=a.lock, unlock=a.unlock))
        return "\n".join(report)


def h_ssh_key(a):
    from .system import add_ssh_keys, remove_ssh_keys
    keys = list(a.key)
    for f in a.keyfile:
        keys += [l for l in open(os.path.expanduser(f)).read().splitlines() if l.strip()]
    if not keys:
        raise PiImgError("give a public key file (e.g. ~/.ssh/id_ed25519.pub) or --key")
    with _open(a) as img:
        return (remove_ssh_keys if a.remove else add_ssh_keys)(img, a.user, keys)


def h_settings(a):
    from .system import settings_table
    with _open(a, readonly=True) as img:
        return settings_table(img)


def h_services(a):
    from .system import services_table
    with _open(a, readonly=True) as img:
        return services_table(img, all_units=a.all)


def h_service(a):
    from .system import unit_action
    with _open(a, readonly=a.action == "show") as img:
        return unit_action(img, a.action, a.unit)


def h_autostart(a):
    from .system import autostart_table
    with _open(a, readonly=True) as img:
        return autostart_table(img)


def h_boot_mode(a):
    from .system import boot_mode_table, set_boot_mode
    with _open(a, readonly=a.mode is None) as img:
        if a.mode is None:
            return boot_mode_table(img)
        return set_boot_mode(img, a.mode, a.user)


def h_hostname(a):
    from .system import get_hostname, set_hostname
    with _open(a, readonly=a.name is None) as img:
        return get_hostname(img) if a.name is None else set_hostname(img, a.name)


def h_wifi(a):
    from .system import add_wifi, network_table, remove_wifi, set_wifi_country
    if a.remove:
        if a.ssid is None:
            raise PiImgError("--remove needs the profile's name (see `piimg wifi IMAGE`)")
        with _open(a) as img:
            return remove_wifi(img, a.ssid)
    if a.ssid is None:
        with _open(a, readonly=a.country is None) as img:
            if a.country:
                set_wifi_country(img, a.country)
            return network_table(img)
    pw = None if a.open else _read_secret(a, "wifi password", confirm=False)
    if pw is None and not a.open:
        raise PiImgError("give the wifi password (--password-stdin, --password, a terminal) or --open")
    with _open(a) as img:
        return add_wifi(img, a.ssid, pw, a.country, a.hidden, a.priority)


def h_logs(a):
    from .system import boots_table, journal_lines
    with _open(a, readonly=True) as img:
        if a.list_boots:
            return boots_table(img, a.scan)
        color = sys.stdout.isatty() or os.environ.get("PIIMG_COLOR") == "1"
        return "\n".join(journal_lines(img, a.boot, a.lines, a.unit, a.priority, a.grep, a.scan, color))


def h_fsck(a):
    from .system import fsck
    repair = a.repair or a.yes
    with _open(a, readonly=not repair, mount=False) as img:
        return fsck(img, repair=repair, assume_yes=a.yes)


def h_edit(a):
    from .system import edit_file
    with _open(a) as img:
        return edit_file(img, a.path, a.editor)


def h_get(a):
    from .system import get_files
    with _open(a, readonly=True) as img:
        return get_files(img, a.src, a.dest)


def h_put(a):
    from .system import put_files
    with _open(a) as img:
        return put_files(img, a.src, a.dest, a.owner, a.mode)


def h_config_txt(a):
    from .system import edit_config_txt
    changing = bool(a.add or a.remove or a.set)
    with _open(a, readonly=not changing) as img:
        return edit_config_txt(img, a.add, a.remove, a.set, a.section)


def h_cmdline(a):
    from .system import edit_cmdline
    with _open(a, readonly=not (a.add or a.remove)) as img:
        return edit_cmdline(img, a.add, a.remove)


def h_images_list(a):
    from .catalog import catalog_table
    return catalog_table(show_all=a.all, refresh=a.refresh, archive=a.archive, series=a.series)


def h_images_download(a):
    from .catalog import download
    return "\n".join(download(spec, unpack=a.unpack, refresh=a.refresh) for spec in a.images)


def h_images_local(a):
    from .catalog import local_table
    return local_table()


def h_images_verify(a):
    from .catalog import _local_files, verify
    from .config import images_dir
    names = a.files or [p.name for p in _local_files(images_dir()) if not p.name.endswith(".part")]
    return verify(names)


def h_images_delete(a):
    from .catalog import delete
    return delete(a.files)


def h_config(a):
    from . import config
    if a.unset:
        config.unset(a.unset)
    elif a.key and a.value is not None:
        config.set_value(a.key, a.value)
    elif a.key:
        return config.get(a.key)
    return config.table()


def h_tui(a):
    from .tui import main as tui_main
    return tui_main(a.image, a.overlay)


def h_gui(a):
    from . import gui
    if a.install_desktop or a.uninstall_desktop:
        print(gui.install_desktop(remove=a.uninstall_desktop))
        return 0
    return gui.main(a.image, a.overlay, a.recipe)


def h_recipe_edit(a):
    from .tui import edit_recipe
    return edit_recipe(a.recipe, a.image)


# ------------------------------------------------------------------- parser

def _burn_typed(a):
    from .disks import find_disk
    return find_disk(a.device).name if a.device else None


def build_parser(parser_class=argparse.ArgumentParser):
    p = parser_class(prog="piimg", description="Customize, inspect and repair Raspberry Pi OS images "
                     "and SD cards, offline.",
                     epilog="Run `piimg tui IMAGE` for the interactive interface, or `piimg gui` for "
                            "the desktop one.")
    p.add_argument("--progress", choices=progress.MODES, default=None,
                   help="how to show progress of slow steps, on stderr: auto = live bars on a terminal "
                        "and plain lines otherwise (default; env PIIMG_PROGRESS); json = for programs")
    p.add_argument("--json", action="store_true", help="print tables as JSON lines")
    sub = p.add_subparsers(dest="cmd", required=True, metavar="COMMAND")

    # ---- the TUI itself
    s = _cmd(sub, "tui", "interactive interface (pick a card or image if none is given)", h_tui, tui=False,
             description="Full-screen interface: run every piimg command on one image, overlay or card, "
                         "with forms for their options. With OVERLAY, TARGET is only read and all changes "
                         "go to OVERLAY (a qcow2 file, created if missing), to be committed later or not.")
    _arg(s, "image", nargs="?", kind="image", help=IMAGE_HELP)
    _arg(s, "overlay", nargs="?", kind="newfile", help="qcow2 file to write changes to, leaving IMAGE untouched")
    s = _cmd(sub, "gui", "desktop interface (GTK 4 / libadwaita)", h_gui, tui=False,
             description="The desktop version of `piimg tui`: run every command on one image, overlay or "
                         "card, with forms for their options, and edit recipes in their own windows. It "
                         "runs as you and asks for your password (polkit) when a command needs root for a "
                         "card. Needs: sudo apt install python3-gi gir1.2-adw-1 gir1.2-gtksource-5 "
                         "gir1.2-vte-3.91")
    _arg(s, "image", nargs="?", kind="image", help=IMAGE_HELP)
    _arg(s, "overlay", nargs="?", kind="newfile", help="qcow2 file to write changes to, leaving IMAGE untouched")
    _arg(s, "--recipe", kind="recipe", help="open this recipe file (or bundled recipe's name) in the recipe "
                                            "editor; without IMAGE, only the editor opens")
    _arg(s, "--install-desktop", action="store_true",
         help="add piimg to the desktop's app launcher (~/.local/share/applications) and exit")
    _arg(s, "--uninstall-desktop", action="store_true", help="remove it from the app launcher and exit")

    # ---- Inspect
    s = _cmd(sub, "info", "summary: partitions, OS, hostname, boot mode, users", h_info, output="table")
    _image(s)
    s = _cmd(sub, "diagnose", "look for common reasons a Pi won't boot, show its screen, log in or autostart",
             h_diagnose, output="table",
             description="Check root= and fstab against the partitions, kernel/firmware/overlay files, "
                         "display settings, boot target and autologin, accounts, wifi, disk space, "
                         "filesystem state, and the errors and failed units of the last boot.")
    _image(s)
    _arg(s, "--scan", type=int, default=5000, help="journal entries to read from the end (default 5000)")
    s = _cmd(sub, "users", "list accounts, their password state, ssh keys and groups", h_users, output="table")
    _image(s)
    _arg(s, "--keys", action="store_true", help="also show the authorized ssh keys themselves")
    s = _cmd(sub, "settings", "the raspi-config settings: hostname, boot, interfaces, locale, ...", h_settings,
             output="table",
             description="What raspi-config would show, read offline: hostname, boot mode, wifi country, "
                         "ssh, I2C, SPI, serial port and console, 1-Wire, locale, time zone and keyboard. "
                         "Change them with a recipe's raspi_config step (or the GUI's System editor).")
    _image(s)
    s = _cmd(sub, "autostart", "list everything that starts at boot or login (services, autologin, "
             "kiosk/desktop autostart, cron, rc.local)", h_autostart, output="table")
    _image(s)
    s = _cmd(sub, "services", "list enabled and masked systemd units (--all: every unit)", h_services,
             output="table",
             description="List the systemd units that are enabled or masked. With --all, also the "
                         "disabled ones (they have an [Install] section and could be enabled) and the "
                         "static ones (they start only when another unit needs them).")
    _image(s)
    _arg(s, "--all", action="store_true", help="also list disabled and static units")
    s = _cmd(sub, "logs", "show the systemd journal of a past boot", h_logs, output="log",
             description="Read the journal the Pi wrote to its card. Shows the last boot by default; "
                         "--boot -1 is the one before. Only the last --scan entries are read (about "
                         "2000 per second).")
    _image(s)
    _arg(s, "--boot", type=int, default=0, help="0 = last boot, -1 = the one before, ...; 1 = oldest in range")
    _arg(s, "-n", "--lines", type=int, default=300, help="show at most this many lines, the last ones (0 = all)")
    _arg(s, "-u", "--unit", kind="unit", help="only this unit (e.g. lightdm, kiosk.service)")
    _arg(s, "-p", "--priority", choices=("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"),
         help="only this priority and more severe (err = errors only)")
    _arg(s, "-g", "--grep", help="only messages matching this regular expression (case-insensitive)")
    _arg(s, "--scan", type=int, default=20000, help="journal entries to read from the end (default 20000)")
    _arg(s, "--list-boots", action="store_true", help="list the boots found instead")
    s = _cmd(sub, "fsck", "check the filesystems (read-only), or repair them", h_fsck, output="table",
             writes=lambda a: a.repair or a.yes, abort="warn",
             confirm=lambda a: ("Repairing changes the filesystems in place. Make a backup first if the "
                                "data matters (piimg flatten).") if a.repair or a.yes else None)
    _image(s)
    _arg(s, "--repair", action="store_true", help="fix what can be fixed safely (e2fsck -p, fsck.vfat -a)")
    _arg(s, "-y", "--yes", action="store_true", help="answer yes to every repair question (e2fsck -y); implies --repair")
    s = _cmd(sub, "ls", "list a directory inside the image", h_ls, group="Files", output="log")
    _image(s)
    _arg(s, "path", nargs="?", default="/", kind="path", help="directory inside the image (default /)")
    _arg(s, "-l", "--long", action="store_true", help="show mode, owner and size")
    s = _cmd(sub, "cat", "print a file from inside the image", h_cat, group="Files", output="text")
    _image(s)
    _arg(s, "path", kind="path", help="file inside the image, e.g. /boot/firmware/config.txt")

    # ---- Repair & configure
    g = "Repair & configure"
    s = _cmd(sub, "passwd", "set, reset, lock or unlock a user's password", h_passwd, group=g, writes=True,
             description="Change USER's password in the image's /etc/shadow. Without --password, "
                         "--password-hash or --password-stdin it asks on the terminal. To set up a fresh "
                         "image's first account the way the first-boot wizard does, use "
                         "`piimg apply first-boot` instead.")
    _image(s)
    _arg(s, "user", kind="user", help="account name (see `piimg users`)")
    sg = _secret_options(s)
    _arg(sg, "--password-hash", help="an already hashed password (crypt format, $6$... or $y$...)")
    _arg(sg, "--lock", action="store_true", help="lock the password (key logins still work)")
    _arg(sg, "--unlock", action="store_true", help="unlock a locked password")
    _arg(s, "--shell", help="also set the login shell, e.g. /bin/bash (the image's 'pi' has "
                            "/usr/sbin/nologin until first-boot setup); alone, leaves the password as it is")
    s = _cmd(sub, "ssh-key", "authorize (or remove) ssh public keys for a user", h_ssh_key, group=g, writes=True)
    _image(s)
    _arg(s, "user", kind="user", help="account name")
    _arg(s, "keyfile", nargs="*", kind="file", help="public key files, e.g. ~/.ssh/id_ed25519.pub")
    _arg(s, "--key", action="append", default=[], kind="lines", help="a public key as text (repeatable)")
    _arg(s, "--remove", action="store_true", help="take these keys out instead of adding them")
    s = _cmd(sub, "boot-mode", "show or set console/desktop boot and autologin (raspi-config B1-B4)",
             h_boot_mode, group=g, writes=lambda a: a.mode is not None)
    _image(s)
    _arg(s, "mode", nargs="?", choices=("console", "console-autologin", "desktop", "desktop-autologin"),
         help="what to boot into; leave out to show the current setting")
    _arg(s, "--user", kind="user", help="who to log in automatically (default: the first regular user)")
    s = _cmd(sub, "service", "enable, disable, mask, unmask or show a systemd unit", h_service, group=g,
             writes=lambda a: a.action != "show", output="log")
    _image(s)
    _arg(s, "action", choices=("show", "enable", "disable", "mask", "unmask"), help="what to do")
    _arg(s, "unit", kind="unit", help="unit name; .service is added if there is no suffix")
    s = _cmd(sub, "hostname", "show or set the hostname", h_hostname, group=g, writes=lambda a: a.name is not None)
    _image(s)
    _arg(s, "name", nargs="?", help="new hostname; leave out to show it")
    s = _cmd(sub, "wifi", "add or remove a wifi network, set the wifi country, or list network profiles",
             h_wifi, group=g, writes=lambda a: a.ssid is not None or a.country is not None)
    _image(s)
    _arg(s, "ssid", nargs="?", help="network name; leave out to list the profiles")
    _secret_options(s, "wifi password")
    _arg(s, "--open", action="store_true", help="an open network (no password)")
    _arg(s, "--country", help="two-letter wifi country code, e.g. GB, US, DE (Raspberry Pi OS needs one)")
    _arg(s, "--hidden", action="store_true", help="the network does not broadcast its name")
    _arg(s, "--priority", type=int, default=0, help="higher is tried first (default 0)")
    _arg(s, "--remove", action="store_true", help="delete the profile named SSID instead of adding one")
    s = _cmd(sub, "config-txt", "show or change config.txt (display, overlays, hardware)", h_config_txt,
             group=g, writes=lambda a: bool(a.add or a.remove or a.set), output="text")
    _image(s)
    _arg(s, "--add", action="append", default=[], kind="lines", help="line to add, e.g. dtoverlay=vc4-kms-v3d")
    _arg(s, "--remove", action="append", default=[], kind="lines", help="exact line to remove")
    _arg(s, "--set", action="append", default=[], kind="lines", metavar="KEY=VALUE",
         help="set KEY=VALUE where KEY is set (or commented out), else append it")
    _arg(s, "--section", default="all", help="[section] for --add: all, pi4, pi5, cm4... (default all)")
    s = _cmd(sub, "cmdline", "show or change the kernel command line (cmdline.txt)", h_cmdline, group=g,
             writes=lambda a: bool(a.add or a.remove), output="text")
    _image(s)
    _arg(s, "--add", action="append", default=[], kind="lines", help="parameter to add, e.g. video=HDMI-A-1:1280x720@60")
    _arg(s, "--remove", action="append", default=[], kind="lines",
         help="parameter to remove; 'name=' removes every name=... (e.g. 'console=')")
    s = _cmd(sub, "clean", "remove files a killed live run left in the image", h_clean, group=g, writes=True)
    _image(s)

    # ---- Files
    s = _cmd(sub, "edit", "edit a file inside the image with $EDITOR", h_edit, group="Files", writes=True,
             output="terminal")
    _image(s)
    _arg(s, "path", kind="path", help="file inside the image (created if missing)")
    _arg(s, "--editor", help="editor command (default: $VISUAL, $EDITOR, nano, vi)")
    s = _cmd(sub, "get", "copy a file or directory out of the image", h_get, group="Files")
    _image(s)
    _arg(s, "src", kind="path", help="file or directory inside the image")
    _arg(s, "dest", nargs="?", default=".", kind="dir", help="local destination (default: current directory)")
    s = _cmd(sub, "put", "copy a local file or directory into the image", h_put, group="Files", writes=True)
    _image(s)
    _arg(s, "src", kind="file", help="local file or directory")
    _arg(s, "dest", kind="path", help="destination inside the image (a directory, or the new file's path)")
    _arg(s, "--owner", kind="user", help="user[:group] inside the image (default root)")
    _arg(s, "--mode", help="file mode in octal, e.g. 0755 (default: the local file's)")
    s = _cmd(sub, "shell", "interactive file-level shell inside the image (guestfish)", h_shell, group="Files",
             writes=True, output="terminal")
    _image(s)

    # ---- Recipes
    g = "Recipes"
    s = _cmd(sub, "run", "run a recipe file (path, or the name of a bundled one) on an image", h_run, group=g,
             writes=lambda a: not a.dry_run, abort="warn")
    _image(s)
    _arg(s, "recipe", kind="recipe", help="recipe file, or the name of a bundled recipe (see `piimg recipes`)")
    _arg(s, "--var", action="append", default=[], metavar="NAME=VALUE", kind="vars", help="set a recipe variable")
    _arg(s, "--dry-run", action="store_true", help="show what would change; write nothing")
    _cmd(sub, "recipes", "list bundled recipe files", h_recipes, group=g, output="table", target=None)
    s = _cmd(sub, "recipe-edit", "edit a recipe file in the recipe editor", h_recipe_edit, group=g,
             output="terminal", target=None, tui=False)
    _arg(s, "recipe", nargs="?", kind="recipe", help="recipe file to edit or create, or a bundled recipe's name")
    _arg(s, "--image", kind="image", help="image to run the recipe on (Run / Dry run in the editor)")
    s = _cmd(sub, "apply", "apply a built-in Python recipe to an image", None, group=g, writes=True, tui=False)
    rsub = s.add_subparsers(dest="recipe", required=True, metavar="RECIPE")
    for name, r in REGISTRY.items():
        rp = _cmd(rsub, name, r.help, h_apply, group=g, writes=True)
        _image(rp)
        r.add_arguments(rp)

    # ---- Official images
    g = "Official images"
    s = _cmd(sub, "images", "list, download, verify and delete official Raspberry Pi OS images", None,
             group=g, tui=False, target=None)
    isub = s.add_subparsers(dest="images_cmd", required=True, metavar="ACTION")
    s = _cmd(isub, "list", "list the official Raspberry Pi OS images you can download", h_images_list,
             group=g, output="table", target=None,
             description="The current Raspberry Pi OS images from the list Raspberry Pi Imager uses, "
                         "with which ones are already in your images folder. --archive lists every "
                         "release on downloads.raspberrypi.com instead.")
    _arg(s, "--archive", action="store_true", help="every past release of every series (slower: reads the "
                                                   "download server's index pages)")
    _arg(s, "--series", help="with --archive: only series containing this, e.g. lite_arm64")
    _arg(s, "--all", action="store_true", help="also list the third-party OSes in the Imager list")
    _arg(s, "--refresh", action="store_true", help="fetch the list again even if the cached copy is recent")
    s = _cmd(isub, "download", "download official images to the images folder, verified", h_images_download,
             group=g, target=None,
             description="Download to the images folder (`piimg config images_dir`). The SHA-256 is "
                         "checked against the published one before the file gets its final name; an "
                         "interrupted download resumes. IMAGE is a name from `piimg images list` (a unique "
                         "part is enough: arm64-lite), SERIES@DATE from --archive, or a URL.")
    _arg(s, "images", nargs="+", metavar="IMAGE", kind="catalog", help="image(s) to download, e.g. trixie-arm64-lite")
    _arg(s, "--unpack", action="store_true", help="also unpack the .img.xz to a .img and verify that too")
    _arg(s, "--refresh", action="store_true", help="fetch the image list again first")
    _cmd(isub, "local", "list the images in the images folder and whether they are verified", h_images_local,
         group=g, output="table", target=None)
    s = _cmd(isub, "verify", "check downloaded images against their published SHA-256", h_images_verify,
             group=g, output="table", target=None)
    _arg(s, "files", nargs="*", metavar="FILE", kind="localimage",
         help="files in the images folder (default: all of them)")
    s = _cmd(isub, "delete", "delete images from the images folder", h_images_delete, group=g, target=None,
             writes=False, confirm=lambda a: "Delete " + ", ".join(a.files) + "? This cannot be undone.")
    _arg(s, "files", nargs="+", metavar="FILE", kind="localimage", help="files in the images folder")
    s = _cmd(sub, "config", "show or change piimg's settings (images folder, ...)", h_config, group=g,
             output="table", target=None)
    _arg(s, "key", nargs="?", choices=("images_dir", "catalog_url", "catalog_max_age_hours"),
         help="setting to show or change")
    _arg(s, "value", nargs="?", help="new value")
    _arg(s, "--unset", choices=("images_dir", "catalog_url", "catalog_max_age_hours"),
         help="return a setting to its default")

    # ---- Image & card
    g = "Image & card"
    _cmd(sub, "disks", "list SD cards and USB disks, and whether piimg may write them", h_disks, group=g,
         output="table", target=None)
    s = _cmd(sub, "unpack", "decompress .img.xz/.gz/.zip to a raw .img", h_unpack, group=g, target=None)
    _arg(s, "src", kind="file", help="downloaded image, e.g. 2026-10-06-raspios-trixie-arm64-lite.img.xz")
    _arg(s, "dst", nargs="?", kind="newfile", help="output .img (default: next to SRC)")
    s = _cmd(sub, "overlay", "create a qcow2 copy-on-write overlay on an image or card", h_overlay, group=g)
    _image(s, help="base image or card (only ever read)")
    _arg(s, "overlay", kind="newfile", help="new .qcow2 file that will receive all changes")
    s = _cmd(sub, "flatten", "merge an overlay and its base into a new standalone image (or back up a card)",
             h_flatten, group=g,
             description="Write IMAGE with its whole backing chain merged in to a new file. "
                         "Raw (sparse) by default; qcow2 without a backing file if OUTPUT "
                         "ends in .qcow2. IMAGE may be a card: that makes a backup of it. "
                         "Nothing is changed.")
    _image(s)
    _arg(s, "output", kind="newfile", help="new image file (.img or .qcow2)")
    _arg(s, "--format", choices=("raw", "qcow2"), help="output format (default: from OUTPUT's suffix)")
    _arg(s, "--compress", action="store_true", help="compress the clusters (qcow2 output only)")
    s = _cmd(sub, "resize", "grow an image (or a card's root filesystem) and its root filesystem", h_resize,
             group=g, writes=True, abort="no",
             confirm=lambda a: "Growing the partition and filesystem cannot be undone." if _direct_card(a) else None,
             description="Grow IMAGE's virtual disk, then grow its last partition (the root "
                         "filesystem on Raspberry Pi OS) and the ext4 filesystem in it to fill "
                         "the disk. On a qcow2 overlay only the overlay is written; the base "
                         "image stays as it is. Without SIZE (or 'max') the disk keeps its size: "
                         "use that on a card. Shrinking is not supported.")
    _image(s)
    _arg(s, "size", nargs="?", kind="size",
         help="new total size (8G) or growth (+2G); units K, M, G, T (binary). Default: max (fill the disk)")
    s = _cmd(sub, "diff", "list the files an overlay changes compared with its base", h_diff, group=g,
             output="text")
    _image(s, help="qcow2 overlay")
    s = _cmd(sub, "commit", "write an overlay's changes into its base image or card", h_commit, group=g,
             writes=True, abort="warn",
             confirm=lambda a: "This writes every change in the overlay into its base image (or card). "
                               "The base is changed for good and the overlay is emptied.")
    _image(s, help="qcow2 overlay")
    s = _cmd(sub, "burn", "write an image (overlays included) to an SD card or USB disk; needs root", h_burn,
             group=g, abort="warn", writes=False, typed_confirm=_burn_typed, yes_flag="--yes",
             confirm=lambda a: (f"This ERASES everything on {a.device} and writes the image to it."
                                if a.device else None),
             description="Write IMAGE, a raw image or a qcow2 overlay (merged with its base on "
                         "the fly), to the whole disk DEVICE, erasing it. Without DEVICE, list "
                         "the disks that could be written. Needs root: sudo .venv/bin/piimg burn ...")
    _image(s)
    _arg(s, "device", nargs="?", kind="device", help="e.g. /dev/sdc or /dev/mmcblk0")
    _arg(s, "--expand", action="store_true",
         help="grow the root partition and filesystem to fill the disk after writing "
              "(Raspberry Pi OS also does this on first boot while cmdline.txt has 'resize')")
    _arg(s, "-y", "--yes", action="store_true", tui=False, help="do not ask for confirmation")
    return p


def iter_commands(parser=None):
    """(words, subparser) for every runnable subcommand, nested ones included:
    (('info',), p), (('apply', 'userdata'), p), ..."""
    parser = parser or build_parser()

    def walk(p, prefix):
        for a in p._actions:
            if isinstance(a, argparse._SubParsersAction):
                for name, sp in a.choices.items():
                    if any(isinstance(x, argparse._SubParsersAction) for x in sp._actions):
                        yield from walk(sp, prefix + (name,))
                    else:
                        yield prefix + (name,), sp
    yield from walk(parser, ())


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.cmd in ("tui", "gui", "recipe-edit"):
            return args.func(args) or 0
        with progress.session(args.progress):
            output.emit(args.func(args), as_json=args.json)
    except PiImgError as e:
        print(f"piimg: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:  # libguestfs reports everything as RuntimeError
        if type(e) is not RuntimeError:
            raise
        print(f"piimg: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("piimg: interrupted", file=sys.stderr)
        return 130
    return 0
