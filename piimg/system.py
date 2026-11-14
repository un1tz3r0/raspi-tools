"""Inspecting and repairing an installed Raspberry Pi OS, offline.

Everything here works on an open guest.Image: an image file, an overlay, or
the SD card of a Pi that no longer boots, logs in, shows its screen or starts
its kiosk. Nothing in the image is executed (see DESIGN.md, "offline first");
where raspi-config or systemctl would run on the Pi, the same file changes are
made directly, following raspi-config's own code where there is one.
"""
import datetime
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import progress
from .errors import PiImgError
from .output import Table

NOLOGIN_SHELLS = ("/usr/sbin/nologin", "/sbin/nologin", "/bin/false", "/usr/bin/false", "")
WIZARD_USER = "rpi-first-boot-wizard"  # desktop images' first-boot setup account
GETTY_AUTOLOGIN = "/etc/systemd/system/getty@tty1.service.d/autologin.conf"
LIGHTDM_CONF = "/etc/lightdm/lightdm.conf"
PRIORITIES = ("emerg", "alert", "crit", "err", "warning", "notice", "info", "debug")


# ------------------------------------------------------------------ accounts

def passwd_entries(img):
    out = []
    for line in img.read_text("/etc/passwd").splitlines():
        f = line.split(":")
        if len(f) >= 7:
            out.append({"name": f[0], "uid": int(f[2]), "gid": int(f[3]), "gecos": f[4],
                        "home": f[5], "shell": f[6]})
    return out


def find_user(img, name):
    for u in passwd_entries(img):
        if u["name"] == name:
            return u
    return None


def shadow_entries(img):
    if not img.exists("/etc/shadow"):
        return {}
    return {f[0]: f for f in (l.split(":") for l in img.read_text("/etc/shadow").splitlines()) if len(f) >= 2}


def group_members(img):
    """group name -> set of supplementary members."""
    out = {}
    for line in img.read_text("/etc/group").splitlines():
        f = line.split(":")
        if len(f) >= 4:
            out[f[0]] = set(filter(None, f[3].split(",")))
    return out


def password_state(field):
    if field is None:
        return "no shadow entry"
    if field == "":
        return "EMPTY (no password)"
    if field.startswith("!"):
        return "locked"
    if field in ("*", "x"):
        return "none"
    m = re.match(r"\$(\w+)\$", field)
    algo = {"1": "md5", "5": "sha256", "6": "sha512", "y": "yescrypt", "gy": "gost-yescrypt",
            "2b": "bcrypt"}.get(m.group(1), m.group(1)) if m else "des"
    return f"set ({algo})"


def authorized_keys(img, user):
    p = f"{user['home']}/.ssh/authorized_keys"
    if not img.exists(p):
        return []
    return [l for l in img.read_text(p).splitlines() if l.strip() and not l.lstrip().startswith("#")]


def login_users(img):
    """Accounts a person could log in as: root and uid 1000..59999, with a real shell."""
    return [u for u in passwd_entries(img)
            if (u["uid"] == 0 or 1000 <= u["uid"] < 60000) and u["shell"] not in NOLOGIN_SHELLS]


def users_table(img, keys=False):
    """Accounts people log in as. keys: also a column with the authorized
    keys themselves, one per line (for editors)."""
    shadow, groups = shadow_entries(img), group_members(img)
    t = Table(["user", "uid", "shell", "password", "ssh keys", "groups", "note"] + (["keys"] if keys else []),
              title="Accounts")
    for u in passwd_entries(img):
        if not (u["uid"] == 0 or 1000 <= u["uid"] < 60000):
            continue
        sh = shadow.get(u["name"])
        state = password_state(sh[1] if sh else None)
        mine = sorted(g for g, members in groups.items() if u["name"] in members)
        notes = []
        if u["shell"] in NOLOGIN_SHELLS:
            notes.append("cannot log in (no shell)")
        if u["name"] == WIZARD_USER:
            notes.append("first-boot wizard account")
        if "sudo" in mine:
            notes.append("admin (sudo)")
        have = authorized_keys(img, u)
        t.add(u["name"], u["uid"], u["shell"], state, len(have) or "",
              ", ".join(g for g in mine if g in ("sudo", "adm", "video", "gpio", "dialout", "netdev",
                                                 "render", "input")),
              "; ".join(notes), *(["\n".join(have)] if keys else []))
    return t


def set_password(img, user, pwhash=None, lock=False, unlock=False):
    """Set, lock or unlock `user`'s password in /etc/shadow. Returns a report."""
    u = find_user(img, user)
    if u is None:
        hint = ""
        if img.exists(f"{img.boot}/user-data") and not img.exists("/var/lib/cloud/instance"):
            hint = ("\nThis image has not booted yet: create the first account with "
                    "`piimg apply userdata IMAGE --user NAME --password ...`")
        names = ", ".join(x["name"] for x in login_users(img)) or "none"
        raise PiImgError(f"No user {user!r} in the image (accounts that can log in: {names}){hint}")
    lines = img.read_text("/etc/shadow").splitlines()
    for i, line in enumerate(lines):
        f = line.split(":")
        if f[0] != user:
            continue
        while len(f) < 9:
            f.append("")
        if pwhash:
            f[1] = pwhash
            f[2] = str(int(time.time() // 86400))  # last change, days since the epoch
        if lock and not f[1].startswith("!"):
            f[1] = "!" + f[1]
        if unlock:
            f[1] = f[1].lstrip("!") or "*"
        lines[i] = ":".join(f)
        break
    else:
        raise PiImgError(f"{user} has no entry in /etc/shadow")
    img.replace_text("/etc/shadow", "\n".join(lines) + "\n")
    what = "password set" if pwhash else "locked" if lock else "unlocked"
    report = f"{user}: {what}"
    if u["shell"] in NOLOGIN_SHELLS:
        report += (f"\nnote: {user}'s shell is {u['shell'] or '(none)'}, so it still cannot log in"
                   + (" (the first-boot rename has not run yet?)" if user == "pi" else ""))
    return report


def add_ssh_keys(img, user, keys):
    u = find_user(img, user)
    if u is None:
        raise PiImgError(f"No user {user!r} in the image")
    ssh_dir, path = f"{u['home']}/.ssh", f"{u['home']}/.ssh/authorized_keys"
    if not img.g.is_dir(ssh_dir):
        img.g.mkdir_p(ssh_dir)
    img.g.chmod(0o700, ssh_dir)
    img.g.chown(u["uid"], u["gid"], ssh_dir)
    have = authorized_keys(img, u)
    text = img.read_text(path) if img.exists(path) else ""
    added = [k for k in (k.strip() for k in keys) if k and k not in have]
    if added:
        if text and not text.endswith("\n"):
            text += "\n"
        img.write(path, text + "\n".join(added) + "\n", mode=0o600, uid=u["uid"], gid=u["gid"])
    report = f"{user}: {len(added)} key(s) added, {len(have) + len(added)} authorized"
    if not ssh_enabled(img):
        report += "\nnote: the ssh server is not enabled; `piimg service IMAGE enable ssh`"
    return report


def remove_ssh_keys(img, user, keys):
    """Take keys out of `user`'s authorized_keys (whole lines; comments and
    other keys stay)."""
    u = find_user(img, user)
    if u is None:
        raise PiImgError(f"No user {user!r} in the image")
    path = f"{u['home']}/.ssh/authorized_keys"
    if not img.exists(path):
        return f"{user}: no authorized_keys"
    drop = {k.strip() for k in keys if k.strip()}
    lines = img.read_text(path).splitlines()
    keep = [l for l in lines if l.strip() not in drop]
    removed = len(lines) - len(keep)
    if removed:
        img.write(path, "\n".join(keep) + ("\n" if keep else ""), mode=0o600, uid=u["uid"], gid=u["gid"])
    return f"{user}: {removed} key(s) removed, {len(authorized_keys(img, u))} authorized"


def set_shell(img, user, shell):
    """chsh: set `user`'s login shell. The shell must exist in the image."""
    u = find_user(img, user)
    if u is None:
        raise PiImgError(f"No user {user!r} in the image")
    if not img.exists(shell):
        raise PiImgError(f"{shell} does not exist in the image")
    shells = img.read_text("/etc/shells").split() if img.exists("/etc/shells") else []
    _edit_db(img, "/etc/passwd", lambda f: f[0] == user, lambda f: f.__setitem__(6, shell))
    note = "" if shell in shells else f"\nnote: {shell} is not listed in /etc/shells"
    return f"{user}: shell {u['shell'] or '(none)'} -> {shell}{note}"


def _edit_db(img, path, match, change):
    """Edit the colon-separated lines of passwd/shadow/group/gshadow in place.
    match(fields) selects lines; change(fields) edits the list or returns False to drop the line."""
    if not img.exists(path):
        return 0
    out, n = [], 0
    for line in img.read_text(path).splitlines():
        f = line.split(":")
        if line and match(f):
            n += 1
            if change(f) is False:
                continue
            line = ":".join(f)
        out.append(line)
    if n:
        img.replace_text(path, "\n".join(out) + "\n")
    return n


def _rename_member(img, path, old, new, members_field):
    def change(f):
        members = f[members_field].split(",") if f[members_field] else []
        f[members_field] = ",".join(new if m == old else m for m in members)
    _edit_db(img, path, lambda f: len(f) > members_field and old in f[members_field].split(","), change)


def _drop_member(img, path, name, members_field):
    def change(f):
        f[members_field] = ",".join(m for m in f[members_field].split(",") if m and m != name)
    _edit_db(img, path, lambda f: len(f) > members_field and name in f[members_field].split(","), change)


USERNAME_RE = re.compile(r"[a-z][a-z0-9-]{0,31}")


def rename_user(img, old, new):
    """usermod -l NEW -m -d /home/NEW OLD; groupmod -n NEW OLD (its own group),
    as userconf-pi's rename_user() does, plus subuid/subgid and sudoers.d."""
    if not USERNAME_RE.fullmatch(new) or new == "root":
        raise PiImgError(f"{new!r} is not a valid user name: lower-case letters, digits and '-', "
                         "starting with a letter, at most 32 characters")
    u = find_user(img, old)
    if u is None:
        raise PiImgError(f"No user {old!r} in the image")
    if old == new:
        return f"{old}: name unchanged"
    if find_user(img, new) is not None:
        raise PiImgError(f"there is already a user named {new!r}")
    groups = {f[0]: f for f in (l.split(":") for l in img.read_text("/etc/group").splitlines()) if len(f) >= 3}
    if new in groups and groups[new][2] != str(u["gid"]):
        raise PiImgError(f"there is already a group named {new!r}")
    old_home, new_home = u["home"], f"/home/{new}"
    for path in ("/etc/passwd", "/etc/shadow"):
        def change(f, path=path):
            f[0] = new
            if path == "/etc/passwd":
                f[5] = new_home
        _edit_db(img, path, lambda f: f[0] == old, change)
    own_group = old in groups and groups[old][2] == str(u["gid"])
    for path in ("/etc/group", "/etc/gshadow"):
        if own_group:
            _edit_db(img, path, lambda f: f[0] == old, lambda f: f.__setitem__(0, new))
        _rename_member(img, path, old, new, 3)
    for path in ("/etc/subuid", "/etc/subgid"):
        _edit_db(img, path, lambda f: f[0] == old, lambda f: f.__setitem__(0, new))
    if img.g.is_dir(old_home) and not img.exists(new_home):
        img.g.mv(old_home, new_home)
    for path in (f"/var/spool/cron/crontabs/{old}", f"/var/mail/{old}"):
        if img.exists(path):
            img.g.mv(path, path.rsplit("/", 1)[0] + "/" + new)
    sudoers = "/etc/sudoers.d/010_pi-nopasswd"
    if img.exists(sudoers):
        img.sub(sudoers, rf"^{re.escape(old)} ", f"{new} ")
    for conf, pat in ((LIGHTDM_CONF, r"^autologin-user=" + re.escape(old) + r"\s*$"),
                      (GETTY_AUTOLOGIN, r"--autologin " + re.escape(old) + r"\b")):
        if img.exists(conf):
            img.sub(conf, pat, (f"autologin-user={new}" if conf == LIGHTDM_CONF else f"--autologin {new}"))
    return f"{old} renamed to {new} (home {new_home})"


def delete_user(img, name, remove_home=True):
    """userdel [-r]: remove an account, its own group, its group memberships and home."""
    u = find_user(img, name)
    if u is None:
        return f"{name}: no such user"
    for path in ("/etc/passwd", "/etc/shadow", "/etc/subuid", "/etc/subgid"):
        _edit_db(img, path, lambda f: f[0] == name, lambda f: False)
    for path in ("/etc/group", "/etc/gshadow"):
        _drop_member(img, path, name, 3)
        # its own (user-private) group, if nobody else uses it
        _edit_db(img, path, lambda f: f[0] == name and len(f) > 3 and not f[3], lambda f: False)
    if remove_home and u["home"].startswith("/home/") and img.g.is_dir(u["home"]):
        img.g.rm_rf(u["home"])
    return f"{name}: deleted"


# -------------------------------------------------------------- localisation

def set_locale(img, locale):
    """raspi-config do_change_locale_rc_gui: enable it in /etc/locale.gen and make
    it the default (/etc/default/locale). Compiling the locale (locale-gen) runs
    arm code, so it is left to a one-shot unit on first boot when needed.
    Returns True if locale-gen must run."""
    gen = img.read_text("/etc/locale.gen") if img.exists("/etc/locale.gen") else ""
    supported = img.read_text("/usr/share/i18n/SUPPORTED") if img.exists("/usr/share/i18n/SUPPORTED") else ""
    loc = locale if re.search(rf"^{re.escape(locale)} ", supported, re.M) else locale + ".UTF-8"
    if supported and not re.search(rf"^{re.escape(loc)} ", supported, re.M):
        raise PiImgError(f"{locale!r} is not a supported locale (see /usr/share/i18n/SUPPORTED), "
                         "e.g. en_US.UTF-8 or de_DE.UTF-8")
    active_before = {l.split()[0] for l in gen.splitlines() if l.strip() and not l.lstrip().startswith("#")}
    lines = []
    found = False
    for l in gen.splitlines():
        bare = l.lstrip("# ").strip()
        if bare.split(" ")[0] == loc:
            lines.append(bare)
            found = True
        elif l.strip() and not l.lstrip().startswith("#"):
            lines.append("# " + l)  # raspi-config keeps only the chosen locale
        else:
            lines.append(l)
    if not found:
        line = next((l for l in supported.splitlines() if l.startswith(loc + " ")), f"{loc} UTF-8")
        lines.append(line)
    img.write("/etc/locale.gen", "\n".join(lines) + "\n")
    img.write("/etc/default/locale", f"LANG={loc}\nLC_ALL={loc}\nLANGUAGE={loc}\n")
    return loc not in active_before


FIRSTBOOT_UNIT = "piimg-locale-gen.service"


def schedule_locale_gen(img):
    """A one-shot unit that compiles the locales on first boot, before anyone logs in."""
    img.write(f"/etc/systemd/system/{FIRSTBOOT_UNIT}", """\
# Written by piimg: compile the locales chosen offline (locale-gen runs arm code).
[Unit]
Description=Generate locales chosen by piimg
DefaultDependencies=no
After=local-fs.target
Before=systemd-user-sessions.service display-manager.service getty@tty1.service
ConditionPathExists=/var/lib/piimg-locale-gen

[Service]
Type=oneshot
ExecStart=/usr/sbin/locale-gen
ExecStartPost=/bin/rm -f /var/lib/piimg-locale-gen

[Install]
WantedBy=multi-user.target
""")
    img.write("/var/lib/piimg-locale-gen", "")
    img.enable_unit(FIRSTBOOT_UNIT)


def set_timezone(img, tz):
    """raspi-config do_change_timezone_rc_gui (dpkg-reconfigure tzdata): /etc/timezone
    and the /etc/localtime link."""
    zone = f"/usr/share/zoneinfo/{tz}"
    if ".." in tz or not img.g.is_file(zone):
        raise PiImgError(f"{tz!r} is not a time zone in the image (e.g. Europe/Berlin, America/New_York)")
    img.write("/etc/timezone", tz + "\n")
    if img.lexists("/etc/localtime"):
        img.g.rm("/etc/localtime")
    img.g.ln_s(zone, "/etc/localtime")


def set_keyboard(img, layout, model="pc105", variant="", options="", user=None):
    """raspi-config do_change_keyboard_rc_gui: /etc/default/keyboard (console),
    the user's and the greeter's labwc environment (desktop)."""
    kb = "/etc/default/keyboard"
    text = img.read_text(kb) if img.exists(kb) else ""
    for key, val in (("XKBMODEL", model), ("XKBLAYOUT", layout), ("XKBVARIANT", variant), ("XKBOPTIONS", options)):
        if re.search(rf"^{key}=", text, re.M):
            text = re.sub(rf"^{key}=.*$", f'{key}="{val}"', text, flags=re.M)
        else:
            text += f'{key}="{val}"\n'
    img.write(kb, text)
    xkb = {"XKB_DEFAULT_MODEL": model, "XKB_DEFAULT_LAYOUT": layout, "XKB_DEFAULT_VARIANT": variant,
           "XKB_DEFAULT_OPTIONS": options}
    targets = []
    if user:
        u = find_user(img, user)
        if u:
            targets.append((f"{u['home']}/.config/labwc/environment", u))
    greeter = "/etc/xdg/labwc-greeter/environment"
    targets.append((greeter if img.exists(greeter) else "/usr/share/labwc/environment", None))
    for path, u in targets:
        if u is None and not img.exists(path):
            continue
        env = img.read_text(path) if img.exists(path) else ""
        for k, v in xkb.items():
            if re.search(rf"^{k}", env, re.M):
                env = re.sub(rf"^{k}.*$", f"{k}={v}", env, flags=re.M)
            else:
                env += ("" if not env or env.endswith("\n") else "\n") + f"{k}={v}\n"
        if u is not None:
            for d in (f"{u['home']}/.config", f"{u['home']}/.config/labwc"):
                if not img.g.is_dir(d):
                    img.g.mkdir(d)
                    img.g.chown(u["uid"], u["gid"], d)
            img.write(path, env, uid=u["uid"], gid=u["gid"])
        else:
            img.replace_text(path, env)


def set_browser(img, browser, user=None):
    """raspi-config do_browser: the x-www-browser alternative, and the user's
    default web browser (what xdg-settings writes)."""
    exe = f"/usr/bin/{browser}"
    if not img.exists(exe):
        raise PiImgError(f"{browser} is not installed in the image")
    alt = "/var/lib/dpkg/alternatives/x-www-browser"
    if img.exists(alt):
        lines = img.read_text(alt).split("\n")
        lines[0] = "manual"
        img.replace_text(alt, "\n".join(lines))
    img.symlink(exe, "/etc/alternatives/x-www-browser")
    if user:
        u = find_user(img, user)
        if u:
            cfg = f"{u['home']}/.config"
            if not img.g.is_dir(cfg):
                img.g.mkdir_p(cfg)
                img.g.chown(u["uid"], u["gid"], cfg)
            path = f"{cfg}/mimeapps.list"
            text = img.read_text(path) if img.exists(path) else ""
            entries = {t: f"{browser}.desktop" for t in ("x-scheme-handler/http", "x-scheme-handler/https",
                                                          "text/html", "x-scheme-handler/about",
                                                          "x-scheme-handler/unknown")}
            body = [l for l in text.splitlines() if l.split("=")[0] not in entries]
            if "[Default Applications]" not in body:
                body.append("[Default Applications]")
            i = body.index("[Default Applications]") + 1
            body[i:i] = [f"{k}={v}" for k, v in entries.items()]
            img.write(path, "\n".join(body) + "\n", uid=u["uid"], gid=u["gid"])


# --------------------------------------------------------------------- units

def _wants_dirs(img, base="/etc/systemd/system"):
    if not img.g.is_dir(base):
        return []
    return [d for d in img.listdir(base) if d.endswith((".wants", ".requires"))]


def enabled_links(img):
    """[(target, unit, link target)] for every .wants/.requires symlink."""
    out = []
    for d in _wants_dirs(img):
        for unit in img.listdir(f"/etc/systemd/system/{d}"):
            p = f"/etc/systemd/system/{d}/{unit}"
            tgt = img.g.readlink(p) if img.g.is_symlink(p) else p
            out.append((d.rsplit(".", 1)[0], unit, tgt))
    return out


def masked_units(img):
    base = "/etc/systemd/system"
    return [n for n in img.listdir(base)
            if img.g.is_symlink(f"{base}/{n}") and img.g.readlink(f"{base}/{n}") == "/dev/null"]


def ssh_enabled(img):
    return any(u in ("ssh.service", "sshd.service", "ssh.socket") for _, u, _ in enabled_links(img)) \
        or img.exists(f"{img.boot}/ssh") or img.exists(f"{img.boot}/ssh.txt")


def default_target(img):
    p = "/etc/systemd/system/default.target"
    if img.g.is_symlink(p):
        return os.path.basename(img.g.readlink(p))
    return "graphical.target (no default.target link; systemd's default)"


UNIT_SUFFIXES = (".service", ".socket", ".timer", ".path")


def _unit_info(text):
    """(Description, install targets) of a unit file's text."""
    m = re.search(r"^Description\s*=\s*(.*)$", text, re.M)
    install = re.search(r"^\[Install\]\s*$(.*?)(?=^\[|\Z)", text, re.S | re.M)
    targets = []
    if install:
        for _key, val in re.findall(r"^(WantedBy|RequiredBy|Also)\s*=\s*(.*)$", install.group(1), re.M):
            targets += val.split()
    return (m.group(1).strip() if m else ""), targets


def unit_files(img):
    """{unit name: path} of the unit files systemctl would list (services,
    sockets, timers, paths), the first found in UNIT_DIRS order; not
    templates (name@.service), aliases (symlinks) or masks."""
    out = {}
    for d in img.UNIT_DIRS:
        if not img.g.is_dir(d):
            continue
        for n in img.listdir(d):
            if n in out or not n.endswith(UNIT_SUFFIXES) or "@." in n:
                continue
            p = f"{d}/{n}"
            if img.g.is_symlink(p) or not img.g.is_file(p):
                continue
            out[n] = p
    return out


def services_table(img, all_units=False):
    """Enabled and masked units; with all_units also the disabled ones (those
    with an [Install] section) and the static ones (without: they start only
    when something else needs them, and can only be masked)."""
    by_unit = {}
    for target, unit, tgt in enabled_links(img):
        by_unit.setdefault(unit, []).append(target)
    masked = set(masked_units(img))
    files = unit_files(img) if all_units else {}
    title = "Systemd units" if all_units else "Enabled and masked units"
    t = Table(["unit", "state", "wanted by", "unit file", "origin", "description"], title=title)

    def origin(f):
        return "local" if f and f.startswith(("/etc/", "/usr/local/")) else "package"
    rows = []
    for unit in by_unit:
        if unit in masked:
            continue
        f = img.find_unit(unit)
        desc = _unit_info(img.read_text(f))[0] if f else ""
        rows.append((unit, "enabled" if f else "BROKEN (no unit file)", ", ".join(by_unit[unit]), f or "",
                     origin(f), desc))
    for unit in masked:
        real = files.get(unit)   # the unit file under the mask, for its description
        rows.append((unit, "masked", "", "/dev/null", "local", _unit_info(img.read_text(real))[0] if real else ""))
    for unit, f in files.items():
        if unit in by_unit or unit in masked:
            continue
        desc, targets = _unit_info(img.read_text(f))
        rows.append((unit, "disabled" if targets else "static", "", f, origin(f), desc))
    for r in sorted(rows, key=lambda r: r[0]):
        t.add(*r)
    t.footer = f"default target: {default_target(img)}"
    return t


def unit_show(img, unit):
    if "." not in unit:
        unit += ".service"
    out = []
    masked = f"/etc/systemd/system/{unit}"
    if img.g.is_symlink(masked) and img.g.readlink(masked) == "/dev/null":
        out.append(f"# {unit} is MASKED (/etc/systemd/system/{unit} -> /dev/null)")
    f = img.find_unit(unit)
    links = [t for t, u, _ in enabled_links(img) if u == unit]
    out.append(f"# unit file: {f or 'not found'}")
    out.append(f"# enabled in: {', '.join(links) if links else 'nothing (not enabled)'}")
    if f:
        out += ["", img.read_text(f).rstrip()]
    dropin = f"/etc/systemd/system/{unit}.d"
    if img.g.is_dir(dropin):
        for n in sorted(img.listdir(dropin)):
            out += ["", f"# drop-in {dropin}/{n}", img.read_text(f"{dropin}/{n}").rstrip()]
    return "\n".join(out)


def unit_action(img, action, unit):
    if "." not in unit:
        unit += ".service"
    if action == "enable":
        return f"{unit}: enabled in {', '.join(img.enable_unit(unit))}"
    if action == "disable":
        removed = img.disable_unit(unit)
        return f"{unit}: removed {len(removed)} link(s)" + ("" if removed else " (it was not enabled)")
    if action == "mask":
        img.mask_unit(unit)
        return f"{unit}: masked (it cannot start, even when something else wants it)"
    if action == "unmask":
        return f"{unit}: unmasked" if img.unmask_unit(unit) else f"{unit}: was not masked"
    if action == "show":
        return unit_show(img, unit)
    raise PiImgError(f"unknown action {action!r}")


# ----------------------------------------------------------- boot behaviour

def _first_regular_user(img):
    for u in login_users(img):
        if u["uid"] >= 1000 and u["name"] != WIZARD_USER:
            return u["name"]
    return None


def boot_mode_state(img):
    getty_user = None
    if img.exists(GETTY_AUTOLOGIN):
        m = re.search(r"--autologin\s+(\S+)", img.read_text(GETTY_AUTOLOGIN))
        getty_user = m.group(1) if m else None
    lightdm_user = lightdm_session = None
    if img.exists(LIGHTDM_CONF):
        text = img.read_text(LIGHTDM_CONF)
        m = re.search(r"^autologin-user=(.*)$", text, re.M)
        lightdm_user = m.group(1).strip() if m else None
        m = re.search(r"^autologin-session=(.*)$", text, re.M)
        lightdm_session = m.group(1).strip() if m else None
    target = default_target(img)
    desktop = target.startswith("graphical")
    if desktop:
        mode = "desktop-autologin" if lightdm_user else "desktop"
    else:
        mode = "console-autologin" if getty_user else "console"
    return {"mode": mode, "default target": target, "console autologin": getty_user or "off",
            "desktop autologin": lightdm_user or "off", "desktop session": lightdm_session or "(default)",
            "display manager": "lightdm" if img.exists(LIGHTDM_CONF) else "none installed"}


BOOT_MODES = ("console", "console-autologin", "desktop", "desktop-autologin")


def set_boot_mode(img, mode, user=None):
    """raspi-config's do_boot_behaviour B1..B4, offline. Returns a report."""
    if mode not in BOOT_MODES:
        raise PiImgError(f"boot mode must be one of {', '.join(BOOT_MODES)}")
    desktop = mode.startswith("desktop")
    autologin = mode.endswith("autologin")
    target = "graphical.target" if desktop else "multi-user.target"
    if desktop and not (img.exists("/etc/init.d/lightdm") or img.find_unit("lightdm.service")):
        raise PiImgError("This image has no desktop (lightdm is not installed); a Lite image can only "
                         "boot to the console")
    src = img.find_unit(target)
    if src is None:
        raise PiImgError(f"{target} not found in the image")
    img.symlink(src, "/etc/systemd/system/default.target")
    if autologin:
        user = user or _first_regular_user(img)
        if not user:
            raise PiImgError("No account to log in automatically; pass --user")
        if find_user(img, user) is None:
            raise PiImgError(f"No user {user!r} in the image")
        if desktop:
            img.sub(LIGHTDM_CONF, r"^(#|)autologin-user=.*", f"autologin-user={user}")
            if not re.search(r"^autologin-user=", img.read_text(LIGHTDM_CONF), re.M):
                img.sub(LIGHTDM_CONF, r"^\[Seat:\*\]\s*$", f"[Seat:*]\nautologin-user={user}", count=1)
        img.write(GETTY_AUTOLOGIN, "[Service]\nExecStart=\n"
                  f"ExecStart=-/sbin/agetty --autologin {user} --noclear %I $TERM\n")
    else:
        if desktop and img.exists(LIGHTDM_CONF):
            img.sub(LIGHTDM_CONF, r"^autologin-user=.*", "#autologin-user=")
        if img.exists(GETTY_AUTOLOGIN):
            img.g.rm(GETTY_AUTOLOGIN)
            d = os.path.dirname(GETTY_AUTOLOGIN)
            if not img.listdir(d):
                img.g.rmdir(d)
    return f"boot mode: {mode}" + (f" as {user}" if autologin else "") + f" (default target {target})"


def boot_mode_table(img):
    t = Table(["setting", "value"], title="Boot behaviour")
    for k, v in boot_mode_state(img).items():
        t.add(k, v)
    return t


# --------------------------------------------------------------- hostname

def set_hostname(img, name):
    if not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?", name):
        raise PiImgError(f"{name!r} is not a valid hostname (letters, digits and '-', up to 63)")
    img.write("/etc/hostname", name + "\n")
    if img.exists("/etc/hosts"):
        if not img.sub("/etc/hosts", r"^[ \t]*127\.0\.1\.1[ \t].*", f"127.0.1.1\t{name}"):
            img.append_line_once("/etc/hosts", f"127.0.1.1\t{name}")
    report = f"hostname: {name}"
    ud = f"{img.boot}/user-data"
    if img.exists(ud) and re.search(r"^\s*\"?hostname\"?\s*:", img.read_text(ud), re.M) \
            and not img.exists("/var/lib/cloud/instance"):
        report += f"\nnote: {ud} also sets a hostname, and cloud-init applies it on first boot"
    return report


def get_hostname(img):
    return img.read_text("/etc/hostname").strip() if img.exists("/etc/hostname") else "(none)"


# ------------------------------------------------------------- system settings

def _config_lines(img):
    """config.txt's active lines (not comments), stripped."""
    if not img.exists(img.config_txt):
        return []
    return [l.strip() for l in img.read_text(img.config_txt).splitlines()
            if l.strip() and not l.lstrip().startswith("#")]


def _dtparam_on(lines, name):
    """raspi-config's get_i2c/get_spi: dtparam=...,NAME[=on],... in config.txt."""
    pat = re.compile(rf"^(device_tree_param|dtparam)=([^,]*,)*{name}(=(on|true|yes|1))?(,.*)?$")
    return any(pat.match(l) for l in lines)


def _env_value(img, path, key):
    if not img.exists(path):
        return ""
    m = re.search(rf"^{key}=\"?([^\"\n]*)\"?", img.read_text(path), re.M)
    return m.group(1).strip() if m else ""


SETTINGS = [
    # key, label, kind (text, choice, switch), raspi-config call, help
    ("hostname", "Hostname", "text", "do_hostname", "the name the Pi uses on the network"),
    ("boot mode", "Boot into", "choice", "do_boot_behaviour", "console or desktop, with or without "
     "logging in automatically (the first regular user)"),
    ("wifi country", "Wi-Fi country", "text", "do_wifi_country", "two-letter code, e.g. GB, US, DE; "
     "wifi stays off until one is set"),
    ("ssh", "SSH server", "switch", "do_ssh", "start the ssh server at boot"),
    ("i2c", "I2C", "switch", "do_i2c", "the ARM I2C bus (dtparam=i2c_arm) and the i2c-dev module"),
    ("spi", "SPI", "switch", "do_spi", "the SPI bus (dtparam=spi)"),
    ("serial port", "Serial port", "switch", "do_serial_hw", "the UART (enable_uart=1)"),
    ("serial console", "Serial console", "switch", "do_serial_cons", "a login console on the serial "
     "port (console=serial0 on the kernel command line)"),
    ("1-wire", "1-Wire", "switch", "do_onewire", "the 1-Wire bus on GPIO 4 (dtoverlay=w1-gpio)"),
    ("locale", "Locale", "text", "do_change_locale", "e.g. en_GB.UTF-8; compiled on first boot if new"),
    ("timezone", "Time zone", "text", "do_change_timezone", "e.g. Europe/London, America/New_York"),
    ("keyboard", "Keyboard layout", "text", "do_configure_keyboard", "an XKB layout, e.g. gb, us, de"),
]


def settings_state(img):
    """What raspi-config would show, read offline: {key: value}; switches are on/off."""
    lines = _config_lines(img)
    words = img.read_text(img.cmdline_txt).split() if img.exists(img.cmdline_txt) else []
    onoff = {True: "on", False: "off"}
    reg = next((w.split("=", 1)[1] for w in words if w.startswith("cfg80211.ieee80211_regdom=")), "")
    tz = img.read_text("/etc/timezone").strip() if img.exists("/etc/timezone") else ""
    if not tz and img.g.is_symlink("/etc/localtime"):
        tz = img.g.readlink("/etc/localtime").split("zoneinfo/", 1)[-1]
    return {
        "hostname": get_hostname(img),
        "boot mode": boot_mode_state(img)["mode"],
        "wifi country": reg,
        "ssh": onoff[ssh_enabled(img)],
        "i2c": onoff[_dtparam_on(lines, "i2c(_arm)?")],
        "spi": onoff[_dtparam_on(lines, "spi")],
        "serial port": onoff["enable_uart=1" in lines],
        "serial console": onoff[any(w.startswith(("console=serial0", "console=ttyAMA0", "console=ttyS0"))
                                    for w in words)],
        "1-wire": onoff[any(l.startswith("dtoverlay=w1-gpio") for l in lines)],
        "locale": _env_value(img, "/etc/default/locale", "LANG"),
        "timezone": tz,
        "keyboard": _env_value(img, "/etc/default/keyboard", "XKBLAYOUT"),
    }


def settings_table(img):
    t = Table(["setting", "value", "label", "kind", "help"], title="System settings")
    state = settings_state(img)
    for key, label, kind, _call, help_ in SETTINGS:
        t.add(key, state[key], label, kind, help_)
    return t


def settings_calls(changes):
    """{key: new value} -> raspi-config nonint calls (raspi_config recipe step)."""
    calls = {k: c for k, _l, _k, c, _h in SETTINGS}
    modes = {"console": "B1", "console-autologin": "B2", "desktop": "B3", "desktop-autologin": "B4"}
    out = []
    for key, value in changes.items():
        if key == "boot mode":
            value = modes[value]
        elif value in ("on", "off"):
            value = "0" if value == "on" else "1"   # raspi-config: 0 = enable
        out.append(f"{calls[key]} {value}")
    return out


# ------------------------------------------------------------------ autostart

_KIOSK_RE = re.compile(r"startx|xinit|chromium|firefox|cage|labwc|wayfire|kiosk|kodi|python|node|\.sh\b", re.I)


def _home_dirs(img):
    return [u for u in passwd_entries(img) if (u["uid"] == 0 or u["uid"] >= 1000) and img.g.is_dir(u["home"])]


def _text_lines(img, path):
    return [l for l in img.read_text(path).splitlines() if l.strip() and not l.lstrip().startswith("#")]


def autostart_table(img):
    """Everything that starts by itself at boot or login, from all the places a
    kiosk or autorun setup tends to hide."""
    t = Table(["kind", "entry", "source"], title="Things that start automatically")
    st = boot_mode_state(img)
    t.add("boot", f"default target {st['default target']}", "/etc/systemd/system/default.target")
    if st["console autologin"] != "off":
        t.add("autologin", f"console (tty1) as {st['console autologin']}", GETTY_AUTOLOGIN)
    if st["desktop autologin"] != "off":
        t.add("autologin", f"desktop as {st['desktop autologin']}, session {st['desktop session']}", LIGHTDM_CONF)
    # system units: local ones first, they are what someone added
    links = enabled_links(img)
    for target, unit, tgt in sorted(links, key=lambda x: (not x[2].startswith("/etc/"), x[1])):
        f = img.find_unit(unit)
        local = bool(f and f.startswith(("/etc/", "/usr/local/")))
        if local or target not in ("multi-user.target", "sysinit.target", "sockets.target", "timers.target",
                                   "bluetooth.target", "network-online.target", "cloud-init.target",
                                   "cloud-config.target", "remote-fs.target", "printer.target",
                                   "nfs-client.target") or _KIOSK_RE.search(unit):
            t.add("service" + (" (local)" if local else ""), f"{unit} (wanted by {target})", f or tgt)
    n_pkg = sum(1 for _, u, _ in links)
    for unit in masked_units(img):
        t.add("masked", unit, f"/etc/systemd/system/{unit}")
    if img.exists("/etc/rc.local"):
        body = [l for l in _text_lines(img, "/etc/rc.local") if l.strip() != "exit 0"]
        for l in body:
            t.add("rc.local", l.strip(), "/etc/rc.local")
    cron_files = ["/etc/crontab"]
    for d in ("/etc/cron.d", "/var/spool/cron/crontabs"):
        if img.g.is_dir(d):
            cron_files += [f"{d}/{n}" for n in img.listdir(d)]
    for f in cron_files:
        if img.exists(f) and img.g.is_file(f):
            for l in _text_lines(img, f):
                if l.lstrip().startswith("@reboot"):
                    t.add("cron @reboot", l.strip(), f)
    homes = _home_dirs(img)
    places = [("labwc", "/etc/xdg/labwc/autostart"), ("lxsession", "/etc/xdg/lxsession/LXDE-pi/autostart")]
    for u in homes:
        h = u["home"]
        places += [("labwc", f"{h}/.config/labwc/autostart"),
                   ("lxsession", f"{h}/.config/lxsession/LXDE-pi/autostart"),
                   ("xinitrc", f"{h}/.xinitrc"), ("xsession", f"{h}/.xsession")]
    for kind, f in places:
        if img.exists(f) and img.g.is_file(f):
            for l in _text_lines(img, f):
                t.add(kind, l.strip(), f)
    for u in homes:
        f = f"{u['home']}/.config/wayfire.ini"
        if img.exists(f):
            sect = None
            for l in img.read_text(f).splitlines():
                m = re.match(r"\s*\[(.*)\]", l)
                if m:
                    sect = m.group(1)
                elif sect == "autostart" and "=" in l and not l.lstrip().startswith("#"):
                    t.add("wayfire", l.strip(), f)
        for prof in (".bash_profile", ".profile", ".bashrc", ".bash_login"):
            f = f"{u['home']}/{prof}"
            if img.exists(f):
                for l in _text_lines(img, f):
                    if re.search(r"startx|xinit|chromium|cage |labwc|wayfire|kiosk", l):
                        t.add("shell login", l.strip(), f)
    xdg = ["/etc/xdg/autostart"] + [f"{u['home']}/.config/autostart" for u in homes]
    for d in xdg:
        if not img.g.is_dir(d):
            continue
        for n in sorted(img.listdir(d)):
            if not n.endswith(".desktop"):
                continue
            text = img.read_text(f"{d}/{n}")
            m = re.search(r"^Exec=(.*)$", text, re.M)
            hidden = re.search(r"^Hidden\s*=\s*true", text, re.M | re.I)
            if d.startswith("/etc/") and not _KIOSK_RE.search(text) and not hidden:
                continue  # the desktop's own helpers; listed only if they look interesting
            t.add("desktop autostart" + (" (hidden)" if hidden else ""), m.group(1) if m else n, f"{d}/{n}")
    t.footer = (f"{n_pkg} unit links in total (see `piimg services`); standard desktop autostart "
                "entries in /etc/xdg/autostart are not listed")
    return t


# ---------------------------------------------------------------- wifi / net

def network_table(img):
    d = "/etc/NetworkManager/system-connections"
    t = Table(["connection", "type", "ssid / interface", "autoconnect", "file", "priority", "hidden",
               "security"], title="NetworkManager profiles")
    if img.g.is_dir(d):
        for n in sorted(img.listdir(d)):
            text = img.read_text(f"{d}/{n}")

            def val(key):
                m = re.search(rf"^{key}=(.*)$", text, re.M)
                return m.group(1).strip() if m else ""
            t.add(val("id") or n, val("type"), val("ssid") or val("interface-name"),
                  val("autoconnect") or "true", f"{d}/{n}", val("autoconnect-priority") or "0",
                  val("hidden") or "false", val("key-mgmt") or ("none" if val("type") == "wifi" else ""))
    words = img.read_text(img.cmdline_txt).split()
    reg = next((w.split("=", 1)[1] for w in words if w.startswith("cfg80211.ieee80211_regdom=")), None)
    t.footer = f"wifi country: {reg or 'not set (wifi may stay disabled)'}"
    return t


def add_wifi(img, ssid, password=None, country=None, hidden=False, priority=0):
    if password is not None and not 8 <= len(password) <= 63:
        raise PiImgError("a WPA password is 8 to 63 characters")
    sections = {"connection": {"type": "wifi", "autoconnect": "true", "autoconnect-priority": priority},
                "wifi": {"mode": "infrastructure", "ssid": ssid}}
    if hidden:
        sections["wifi"]["hidden"] = "true"
    if password:
        sections["wifi-security"] = {"key-mgmt": "wpa-psk", "psk": password}
    sections["ipv4"] = {"method": "auto"}
    sections["ipv6"] = {"addr-gen-mode": "default", "method": "auto"}
    sections["proxy"] = {}
    if not img.exists("/etc/NetworkManager"):
        raise PiImgError("This image has no NetworkManager (pre-bookworm?); not supported")
    img.nm_connection(re.sub(r"[/\\]", "_", ssid), sections)
    report = [f"wifi profile for {ssid!r} written" + (" (open network)" if not password else "")]
    if country:
        set_wifi_country(img, country)
        report.append(f"wifi country: {country.upper()}")
    elif not any(w.startswith("cfg80211.ieee80211_regdom=") for w in img.read_text(img.cmdline_txt).split()):
        report.append("note: no wifi country set; pass --country XX (Raspberry Pi OS keeps wifi "
                      "off until one is set)")
    return "\n".join(report)


NM_DIR = "/etc/NetworkManager/system-connections"


def _nm_profile(img, name):
    """The keyfile of the profile whose id (or file name) is `name`."""
    if img.g.is_dir(NM_DIR):
        for n in img.listdir(NM_DIR):
            p = f"{NM_DIR}/{n}"
            m = re.search(r"^id=(.*)$", img.read_text(p), re.M)
            if (m and m.group(1).strip() == name) or n in (name, f"{name}.nmconnection"):
                return p
    raise PiImgError(f"No NetworkManager profile {name!r} in the image")


def remove_wifi(img, name):
    p = _nm_profile(img, name)
    img.g.rm(p)
    return f"profile {name!r} removed ({p})"


def set_profile_options(img, name, priority=None, autoconnect=None):
    """Change a profile's autoconnect-priority and autoconnect ([connection])."""
    p = _nm_profile(img, name)
    text = img.read_text(p)
    for key, value in (("autoconnect-priority", priority), ("autoconnect", autoconnect)):
        if value is None:
            continue
        value = str(value).lower() if isinstance(value, bool) else str(value)
        if re.search(rf"^{key}=", text, re.M):
            text = re.sub(rf"^{key}=.*$", f"{key}={value}", text, count=1, flags=re.M)
        else:
            text = re.sub(r"^\[connection\][ \t]*$", f"[connection]\n{key}={value}", text, count=1, flags=re.M)
    img.write(p, text, mode=0o600)
    return f"profile {name!r}: " + ", ".join(f"{k} {v}" for k, v in (("priority", priority),
                                                                      ("autoconnect", autoconnect))
                                              if v is not None)


def set_wifi_country(img, country):
    """raspi-config do_wifi_country, offline: regulatory domain on the kernel
    command line, and wifi unblocked in systemd-rfkill's saved state."""
    country = country.upper()
    if not re.fullmatch(r"[A-Z]{2}", country):
        raise PiImgError(f"{country!r} is not a two-letter country code")
    tab = "/usr/share/zoneinfo/iso3166.tab"
    if img.exists(tab) and not re.search(rf"^{country}\t", img.read_text(tab), re.M):
        raise PiImgError(f"{country} is not an ISO 3166-1 country code")
    img.cmdline_remove("cfg80211.ieee80211_regdom=")
    img.cmdline_add(f"cfg80211.ieee80211_regdom={country}")
    d = "/var/lib/systemd/rfkill"
    if img.g.is_dir(d):
        for n in img.listdir(d):
            if n.endswith(":wlan"):
                img.write(f"{d}/{n}", "0\n")
    state = "/var/lib/NetworkManager/NetworkManager.state"
    if img.exists(state):
        img.sub(state, r"^WirelessEnabled=.*$", "WirelessEnabled=true")


# --------------------------------------------------------------------- logs

def _journal_dir(img):
    d = "/var/log/journal"
    if img.g.is_dir(d) and any(img.listdir(f"{d}/{m}") for m in img.listdir(d) if img.g.is_dir(f"{d}/{m}")):
        return d
    return None


def read_journal(img, scan=20000):
    """The last `scan` journal entries, oldest first, as dicts with t (epoch
    seconds), boot, prio, ident, pid, unit and msg. Seeks to the end of the
    journal first: reading costs about 0.5 ms per entry."""
    d = _journal_dir(img)
    if d is None:
        return None
    g = img.g
    g.journal_open(d)
    try:
        while g.journal_skip(1 << 20) == 1 << 20:  # to the end
            pass
        back = g.journal_skip(-scan)
        out = []
        with progress.task("Reading the systemd journal", total=max(1, back)) as t:
            while g.journal_next():
                e = {x["attrname"]: x["attrval"] for x in g.journal_get()}

                def s(k):
                    v = e.get(k)
                    return v.decode("utf-8", "replace") if isinstance(v, bytes) else (v or "")
                out.append({"t": g.journal_get_realtime_usec() / 1e6, "boot": s("_BOOT_ID"),
                            "prio": int(s("PRIORITY") or 6), "ident": s("SYSLOG_IDENTIFIER") or s("_COMM"),
                            "pid": s("_PID"), "unit": s("_SYSTEMD_UNIT") or s("UNIT"), "msg": s("MESSAGE"),
                            "kernel": s("_TRANSPORT") == "kernel"})
                if len(out) % 250 == 0:
                    t.update(len(out))
            t.update(len(out))
        return out
    finally:
        g.journal_close()


def _boots(entries):
    order = []
    for e in entries:
        if not order or order[-1] != e["boot"]:
            if e["boot"] in order:  # interleaved files: keep first appearance order
                continue
            order.append(e["boot"])
    return order


def _fmt_time(t):
    return datetime.datetime.fromtimestamp(t).strftime("%b %d %H:%M:%S")


def parse_priority(p):
    if p is None:
        return None
    p = str(p).lower()
    if p.isdigit():
        return int(p)
    for i, name in enumerate(PRIORITIES):
        if name.startswith(p[:4]):
            return i
    raise PiImgError(f"priority must be 0-7 or one of {', '.join(PRIORITIES)}")


def journal_lines(img, boot=0, lines=200, unit=None, priority=None, grep=None, scan=20000, color=False):
    entries = read_journal(img, scan)
    if entries is None:
        logs = [f"/var/log/{n}" for n in (img.listdir("/var/log") if img.g.is_dir("/var/log") else [])
                if img.g.is_file(f"/var/log/{n}")]
        raise PiImgError("This image has no persistent systemd journal (never booted, or journald "
                         "keeps logs in RAM only). Text logs: " + (", ".join(logs) or "none")
                         + "\nRead one with `piimg cat IMAGE PATH`.")
    boots = _boots(entries)
    if not boots:
        return ["(the journal is empty)"]
    idx = len(boots) - 1 + boot if boot <= 0 else boot - 1
    if not 0 <= idx < len(boots):
        raise PiImgError(f"boot {boot} is not among the {len(boots)} boot(s) in the last {scan} journal "
                         "entries; use a larger --scan or see --list-boots")
    want = boots[idx]
    maxprio = parse_priority(priority)
    if unit and "." not in unit:
        unit += ".service"
    pat = re.compile(grep, re.I) if grep else None
    out = []
    for e in entries:
        if e["boot"] != want:
            continue
        if maxprio is not None and e["prio"] > maxprio:
            continue
        if unit and e["unit"] != unit:
            continue
        if pat and not pat.search(e["msg"]):
            continue
        who = "kernel" if e["kernel"] else e["ident"] + (f"[{e['pid']}]" if e["pid"] else "")
        line = f"{_fmt_time(e['t'])} {who}: {e['msg']}"
        if color and e["prio"] <= 3:
            line = f"\x1b[1;31m{line}\x1b[0m"
        elif color and e["prio"] == 4:
            line = f"\x1b[33m{line}\x1b[0m"
        out.append(line)
    first = entries[0]["t"] if idx == 0 else None
    head = (f"-- boot {idx - len(boots) + 1 if boot <= 0 else boot} of {len(boots)} in the last "
            f"{len(entries)} entries ({want[:8]})" + (" — may start before the scanned window" if first else ""))
    return [head] + (out[-lines:] if lines else out)


def boots_table(img, scan=20000):
    entries = read_journal(img, scan)
    if entries is None:
        raise PiImgError("This image has no persistent systemd journal")
    t = Table(["boot", "id", "first entry", "last entry", "entries", "errors"], title="Boots in the journal")
    boots = _boots(entries)
    for i, b in enumerate(boots):
        es = [e for e in entries if e["boot"] == b]
        t.add(i - len(boots) + 1, b[:8], _fmt_time(es[0]["t"]), _fmt_time(es[-1]["t"]), len(es),
              sum(1 for e in es if e["prio"] <= 3))
    t.footer = f"from the last {len(entries)} entries (--scan)"
    return t


# --------------------------------------------------------------------- fsck

def fsck(img, repair=False, assume_yes=False):
    """Check (or repair) every filesystem. `img` is opened with mount=False,
    read-only unless repairing (then the check runs on a throwaway snapshot)."""
    g = img.g
    t = Table(["partition", "fs", "label", "status", "detail"], title="Filesystem check")
    for part in g.list_partitions():
        fstype = g.vfs_type(part)
        label = g.vfs_label(part) if fstype else ""
        damaged = False
        if not fstype and _linux_partition(g, part):
            # blkid does not recognise it: on ext4 a superblock with a bad checksum looks like this
            fstype, damaged = "ext4", True
        if fstype in ("ext2", "ext3", "ext4"):
            try:
                state = g.tune2fs_l(part).get("Filesystem state", "?")
            except RuntimeError:
                state = "unreadable superblock"
            if damaged:
                state = "not recognised as a filesystem (damaged superblock?)"
            if repair:
                with progress.task(f"Repairing {part} ({fstype})"):
                    try:
                        g.e2fsck(part, correct=not assume_yes, forceall=assume_yes)
                        t.add(part, fstype, label, "ok", f"checked and repaired if needed (was {state})")
                    except RuntimeError as e:
                        msg = next((l.split(": ", 1)[-1] for l in str(e).splitlines() if l.strip()), str(e))
                        msg = msg.removeprefix("e2fsck: ")
                        hint = BACKUP_SB_HINT if "superblock" in str(e).lower() else \
                            ("" if assume_yes else "; try --yes")
                        t.add(part, fstype, label, "FAIL", f"errors remain: {msg}{hint}")
            else:
                # force a full check on the throwaway snapshot: max mount count reached
                try:
                    g.tune2fs(part, maxmountcount=1, mountcount=1)
                except RuntimeError:
                    pass  # damaged superblock: fsck checks it anyway
                with progress.task(f"Checking {part} ({fstype})"):
                    rc = g.fsck(fstype, part)
                t.add(part, fstype, label, *_fsck_verdict(rc, state))
        elif fstype in ("vfat", "msdos", "fat"):
            with progress.task(f"{'Repairing' if repair else 'Checking'} {part} ({fstype})"):
                rc = g.fsck("vfat", part)
            status, detail = _fsck_verdict(rc, "")
            if repair and rc & 1:
                status, detail = "ok", "errors were found and repaired"
            t.add(part, fstype, label, status, detail)
        else:
            t.add(part, fstype or "?", label, "info", "not checked")
    if not repair:
        t.footer = "checked on a read-only snapshot: nothing was changed. Repair with --repair."
    return t


BACKUP_SB_HINT = ("; if the superblock is damaged, repair from a backup copy on the host: "
                  "sudo e2fsck -b 32768 -y PARTITION (for a card, e.g. /dev/sdc2)")


def _linux_partition(g, part):
    try:
        if g.part_get_parttype(g.part_to_dev(part)) == "msdos":
            return g.part_get_mbr_id(g.part_to_dev(part), g.part_to_partnum(part)) == 0x83
        return g.part_get_gpt_type(g.part_to_dev(part), g.part_to_partnum(part)).upper() in (
            "0FC63DAF-8483-4772-8E79-3D69D8477DE4", "B921B045-1DF0-41C3-AF44-4C6F280D3FAE")
    except RuntimeError:
        return False


def _fsck_verdict(rc, state):
    was = f" (superblock says: {state})" if state else ""
    if rc == 0:
        return "ok", "clean" + was
    if rc & 8:
        return "FAIL", f"could not be checked (fsck status {rc}){was}{BACKUP_SB_HINT}"
    if rc & 4:
        return "FAIL", f"errors that need manual repair (fsck status {rc}); try --repair --yes" + was
    if rc & 1:
        return "warn", f"errors found that --repair can fix (fsck status {rc})" + was
    return "warn", f"fsck status {rc}" + was


# --------------------------------------------------------------- diagnose

def cloud_init_userdata(img):
    """The boot partition's cloud-init user-data as a dict ({} if absent or empty;
    None if it is not valid YAML)."""
    import yaml
    ud = f"{img.boot}/user-data"
    if not img.exists(ud):
        return {}
    try:
        doc = yaml.safe_load(img.read_text(ud))
    except yaml.YAMLError:
        return None
    return doc if isinstance(doc, dict) else {}


def _partition_info(img):
    """[(device, blkid dict)] for each partition."""
    return [(p, img.g.blkid(p)) for p in img.g.list_partitions()]


def _resolve_spec(spec, parts):
    """Does fstab/cmdline spec (PARTUUID=, UUID=, LABEL=, /dev/...) name one of the partitions?"""
    key, _, val = spec.partition("=")
    for dev, b in parts:
        if key == "PARTUUID" and b.get("PART_ENTRY_UUID", "").lower() == val.lower():
            return dev
        if key == "UUID" and b.get("UUID", "").lower() == val.lower():
            return dev
        if key == "LABEL" and b.get("LABEL") == val:
            return dev
    if spec.startswith("/dev/"):
        return spec
    return None


def diagnose(img, scan=5000):
    """Look for the usual reasons a Pi does not boot, show its screen, let you
    log in or start its kiosk. Returns a Table of checks."""
    t = Table(["status", "check", "detail"], title=f"Diagnosis of {img.path}")
    g = img.g
    boot = img.boot

    def add(status, check, detail=""):
        t.add(status, check, detail)

    # --- system
    if img.exists("/etc/os-release"):
        m = re.search(r'^PRETTY_NAME="?([^"\n]*)', img.read_text("/etc/os-release"), re.M)
        add("info", "OS", m.group(1) if m else "?")
    add("info", "hostname", get_hostname(img))

    # --- partitions, root= and fstab
    parts = _partition_info(img)
    cmdline_raw = img.read_text(img.cmdline_txt) if img.exists(img.cmdline_txt) else ""
    if not cmdline_raw:
        add("FAIL", "cmdline.txt", f"{img.cmdline_txt} is missing or empty: the kernel gets no root=")
    elif len([l for l in cmdline_raw.splitlines() if l.strip()]) > 1:
        add("FAIL", "cmdline.txt", "has more than one line; only the first is used. Join them into one line")
    words = cmdline_raw.split()
    root = next((w[5:] for w in words if w.startswith("root=")), None)
    if root:
        dev = _resolve_spec(root, parts)
        add("ok" if dev else "FAIL", "root= in cmdline.txt",
            f"{root} -> {dev}" if dev else f"{root} matches no partition on this disk: the kernel will "
            "wait forever for its root filesystem")
    elif cmdline_raw:
        add("FAIL", "root= in cmdline.txt", "missing")
    if any(w.startswith("init=") for w in words):
        w = next(w for w in words if w.startswith("init="))
        add("FAIL" if "piimg" in w else "warn", "init= in cmdline.txt",
            f"{w}: the normal init (systemd) does not run" + ("; left by a killed piimg live run" if "piimg" in w else ""))
    if "resize" in words:
        add("info", "first boot", "cmdline.txt has 'resize': the root partition grows on next boot")
    if img.exists("/etc/fstab"):
        for line in _text_lines(img, "/etc/fstab"):
            f = line.split()
            if len(f) < 3 or f[2] in ("proc", "tmpfs", "sysfs", "devpts", "swap") or f[0] in ("proc", "tmpfs"):
                continue
            opts = f[3] if len(f) > 3 else ""
            dev = _resolve_spec(f[0], parts)
            if dev:
                add("ok", f"fstab {f[1]}", f"{f[0]} -> {dev}")
            elif f[0].startswith(("PARTUUID=", "UUID=", "LABEL=")):
                add("warn" if "nofail" in opts else "FAIL", f"fstab {f[1]}",
                    f"{f[0]} matches no partition on this disk" + ("" if "nofail" in opts else
                    ": boot stops in emergency mode unless the disk is attached (or add 'nofail')"))
    for dev, b in parts:
        if b.get("TYPE", "").startswith("ext"):
            sb = g.tune2fs_l(dev)
            state = sb.get("Filesystem state", "?")
            if state != "clean":
                add("warn", f"filesystem {dev}", f"state '{state}' (not shut down cleanly?): run `piimg fsck`")

    # --- disk space
    for mp in ("/", boot):
        try:
            sv = g.statvfs(mp)
        except RuntimeError:
            continue
        total = sv["blocks"] * sv["frsize"]
        free = sv["bavail"] * sv["frsize"]
        used = 1 - free / total if total else 0
        status = "FAIL" if used > 0.98 or free < 50 << 20 else "warn" if used > 0.9 else "ok"
        add(status, f"free space on {mp}", f"{progress.fmt_size(free)} free of {progress.fmt_size(total)} "
            f"({used:.0%} used)" + (": a full root filesystem stops the desktop and many services"
                                     if status == "FAIL" and mp == "/" else ""))

    # --- boot files
    cfg = img.read_text(img.config_txt) if img.exists(img.config_txt) else None
    if cfg is None:
        add("FAIL", "config.txt", f"{img.config_txt} is missing")
        cfg = ""
    cfg_lines = [l.strip() for l in cfg.splitlines() if l.strip() and not l.lstrip().startswith("#")]

    def cfg_val(key):
        vals = [l.split("=", 1)[1] for l in cfg_lines if l.startswith(key + "=")]
        return vals[-1] if vals else None
    kernels = [k for k in ("kernel8.img", "kernel_2712.img", "kernel7l.img", "kernel7.img", "kernel.img")
               if img.exists(f"{boot}/{k}")]
    add("ok" if kernels else "FAIL", "kernels", ", ".join(kernels) or "no kernel*.img on the boot partition")
    if cfg_val("kernel") and not img.exists(f"{boot}/{cfg_val('kernel')}"):
        add("FAIL", "kernel= in config.txt", f"{cfg_val('kernel')} does not exist")
    if cfg_val("arm_64bit") == "1" and not any(k in kernels for k in ("kernel8.img", "kernel_2712.img")):
        add("FAIL", "arm_64bit=1", "no 64-bit kernel (kernel8.img) present")
    if cfg_val("auto_initramfs") == "1":
        missing = [f"initramfs{k[6:-4]}" for k in kernels if k.startswith("kernel") and k != "kernel.img"
                   and not img.exists(f"{boot}/initramfs{k[6:-4]}")]
        if missing:
            add("warn", "initramfs", f"auto_initramfs=1 but missing: {', '.join(missing)}")
    firmware = [f for f in ("start.elf", "start4.elf", "fixup.dat", "fixup4.dat", "bootcode.bin")
                if not img.exists(f"{boot}/{f}")]
    if firmware:
        add("warn", "firmware files", "missing (needed by Pi 0-4, not Pi 5): " + ", ".join(firmware))
    if cfg_val("initramfs"):
        name = cfg_val("initramfs").split()[0]
        if not img.exists(f"{boot}/{name}"):
            add("FAIL", "initramfs= in config.txt", f"{name} does not exist")
    overlays = [l.split("=", 1)[1].split(",")[0].strip() for l in cfg_lines if l.startswith("dtoverlay=")]
    missing = [o for o in overlays if o and not img.exists(f"{boot}/overlays/{o}.dtbo")]
    if missing:
        add("FAIL", "dtoverlay= in config.txt", "no such overlay: " + ", ".join(missing))
    elif overlays:
        add("ok", "device tree overlays", ", ".join(overlays))

    # --- display
    kms = [o for o in overlays if o.startswith(("vc4-kms-v3d", "vc4-fkms-v3d"))]
    legacy = [k for k in ("hdmi_group", "hdmi_mode", "hdmi_cvt", "hdmi_timings", "framebuffer_width",
                          "framebuffer_height", "sdtv_mode") if cfg_val(k) is not None]
    video = [w for w in words if w.startswith("video=")]
    disp = []
    disp.append(f"driver: {kms[0] if kms else 'firmware framebuffer (no KMS overlay)'}")
    if video:
        disp.append(" ".join(video))
    if cfg_val("hdmi_force_hotplug"):
        disp.append(f"hdmi_force_hotplug={cfg_val('hdmi_force_hotplug')}")
    add("info", "display", "; ".join(disp))
    if kms and legacy and kms[0].startswith("vc4-kms"):
        add("warn", "display mode settings", f"{', '.join(legacy)} are ignored by the KMS driver "
            "(vc4-kms-v3d); set a mode with video=HDMI-A-1:1280x720@60 in cmdline.txt instead")
    if not kms and img.exists("/etc/lightdm/lightdm.conf"):
        add("warn", "display driver", "no vc4-kms-v3d overlay: the Wayland desktop (labwc) needs KMS")
    tft = [o for o in overlays if re.match(r"(pitft|waveshare|tft|ili9|st7)", o)]
    if tft:
        fbcon = [w for w in words if w.startswith("fbcon=")]
        add("info", "SPI display", f"{', '.join(tft)}; console on it: {' '.join(fbcon) or 'no (fbcon=map not set)'}")
    if not any(w.startswith("console=tty") for w in words):
        add("info", "screen console", "no console=tty1: boot messages and the text login do not show on the screen")

    # --- boot target, autologin, first boot
    st = boot_mode_state(img)
    add("info", "boot mode", f"{st['mode']} (default target {st['default target']})")
    users = {u["name"] for u in passwd_entries(img)}
    for kind in ("console autologin", "desktop autologin"):
        u = st[kind]
        if u != "off" and u not in users:
            add("FAIL", kind, f"logs in as {u!r}, which does not exist")
    if st["mode"].startswith("desktop") and st["display manager"] == "none installed":
        add("FAIL", "desktop", "default target is graphical but no display manager is installed")
    first_boot = []
    if img.exists(f"{boot}/user-data") and not img.exists("/var/lib/cloud/instance") \
            and img.exists("/usr/bin/cloud-init"):
        doc = cloud_init_userdata(img)
        if doc is None:
            add("warn", "cloud-init user-data", f"{boot}/user-data is not valid YAML: cloud-init will ignore it")
        elif doc.get("users"):
            first_boot.append("cloud-init will set up the user(s) in user-data (renaming 'pi')")
        elif doc:
            first_boot.append("cloud-init will apply user-data")
    if img.exists(f"{boot}/userconf.txt") or img.exists(f"{boot}/userconf"):
        first_boot.append("userconf.txt will set up the user")
    cloud_users = any("user(s) in user-data" in x for x in first_boot)
    if cloud_users:
        pass  # cloud-init's userconf run cancels the wizard and userconfig
    elif img.exists("/etc/xdg/autostart/piwiz.desktop"):
        first_boot.append("the desktop first-boot wizard will ask for a user name and password")
    elif not cloud_users and img.exists("/etc/systemd/system/multi-user.target.wants/userconfig.service"):
        first_boot.append("a console prompt (userconfig.service, tty8) will ask for a user name and password")
    if first_boot:
        add("info", "first boot pending", "; ".join(first_boot)
            + ("; `piimg apply first-boot` does this setup offline" if not cloud_users and (
                WIZARD_USER in users or "userconfig" in " ".join(first_boot)) else ""))

    # --- logins
    shadow = shadow_entries(img)
    can_login = []
    for u in login_users(img):
        sh = shadow.get(u["name"])
        pw = password_state(sh[1] if sh else None)
        keys = authorized_keys(img, u)
        if pw.startswith("set") or keys:
            can_login.append(f"{u['name']} ({'password' if pw.startswith('set') else ''}"
                             f"{', ' if pw.startswith('set') and keys else ''}{'ssh key' if keys else ''})")
        if pw.startswith("EMPTY"):
            add("warn", f"user {u['name']}", "has an empty password")
    if can_login:
        add("ok", "accounts that can log in", ", ".join(can_login))
    elif not first_boot:
        add("FAIL", "accounts that can log in", "none: set one with `piimg passwd IMAGE USER`")
    add("ok" if ssh_enabled(img) else "info", "ssh server", "enabled" if ssh_enabled(img) else "not enabled")

    # --- network
    net = network_table(img)
    wifi = [r for r in net.rows if r[1] in ("wifi", "802-11-wireless")]
    reg = next((w for w in words if w.startswith("cfg80211.ieee80211_regdom=")), None)
    if wifi:
        add("ok" if reg else "warn", "wifi", f"{len(wifi)} profile(s): {', '.join(r[2] for r in wifi)}"
            + ("" if reg else "; no wifi country set, so wifi may stay off (`piimg wifi --country`)"))
    rf = "/var/lib/systemd/rfkill"
    if g.is_dir(rf) and any(n.endswith(":wlan") and img.read_text(f"{rf}/{n}").strip() == "1"
                            for n in img.listdir(rf)):
        add("warn", "wifi blocked", "rfkill state saves wifi as blocked; setting a wifi country unblocks it")

    # --- leftovers
    if g.is_dir("/usr/lib/piimg") or g.is_dir("/var/lib/piimg"):
        add("warn", "piimg leftovers", "a live run was interrupted: run `piimg clean IMAGE`")

    # --- last boot's journal
    try:
        entries = read_journal(img, scan)
    except RuntimeError as e:
        entries = None
        add("info", "journal", f"could not be read: {e}")
    if entries:
        boots = _boots(entries)
        last = [e for e in entries if e["boot"] == boots[-1]]
        errs = [e for e in last if e["prio"] <= 3]
        add("info", "last boot", f"{_fmt_time(last[0]['t'])} .. {_fmt_time(last[-1]['t'])}, {len(last)} "
            f"log entries, {len(errs)} errors ({len(boots)} boot(s) in the last {len(entries)} entries)")
        failed = []
        for e in last:
            m = re.match(r"(\S+?): Failed with result|Failed to start (\S+)", e["msg"])
            if m:
                name = (m.group(1) or m.group(2)).rstrip(".")
                if name not in failed:
                    failed.append(name)
        if failed:
            add("warn", "failed units (last boot)", ", ".join(failed[:8]) + (" ..." if len(failed) > 8 else ""))
        for e in errs[-5:]:
            add("note", "error (last boot)", f"{_fmt_time(e['t'])} {e['ident']}: {e['msg'][:200]}")
    elif entries is not None or _journal_dir(img) is None:
        add("info", "journal", "no persistent journal (never booted?)")
    t.footer = "More: `piimg logs`, `piimg autostart`, `piimg users`, `piimg services`, `piimg fsck`"
    return t


def info_table(img, image_path):
    from .image import image_info
    t = Table(["item", "value"], title="Target")
    info = image_info(image_path)
    t.add("path", str(image_path) + (f" (overlay of {info.get('backing-filename')})"
                                     if info.get("backing-filename") else ""))
    t.add("format", f"{info['format']}, {progress.fmt_size(info['virtual-size'])}")
    for dev, b in _partition_info(img):
        line = f"{b.get('TYPE', '?')} {b.get('LABEL', '')} PARTUUID={b.get('PART_ENTRY_UUID', '?')}"
        t.add(f"partition {dev[-1]}", line)
    if img.exists("/etc/os-release"):
        m = re.search(r'^PRETTY_NAME="?([^"\n]*)', img.read_text("/etc/os-release"), re.M)
        t.add("os", m.group(1) if m else "?")
    t.add("hostname", get_hostname(img))
    st = boot_mode_state(img)
    t.add("boot mode", st["mode"])
    t.add("users", ", ".join(u["name"] for u in login_users(img) if u["uid"]) or "none")
    t.add("ssh", "enabled" if ssh_enabled(img) else "off")
    try:
        sv = img.g.statvfs("/")
        t.add("root free", progress.fmt_size(sv["bavail"] * sv["frsize"]))
    except RuntimeError:
        pass
    return t


# ----------------------------------------------------------------- files

def edit_file(img, path, editor=None):
    """Open a file from the image in a local editor; write it back if changed."""
    editor = editor or os.environ.get("VISUAL") or os.environ.get("EDITOR") or \
        next((e for e in ("sensible-editor", "nano", "vim", "vi") if shutil.which(e)), None)
    if not editor:
        raise PiImgError("No editor found: set $EDITOR")
    exists = img.exists(path)
    if exists and not img.g.is_file(path):
        raise PiImgError(f"{path} is not a regular file")
    with tempfile.TemporaryDirectory(prefix="piimg-edit-") as tmp:
        local = Path(tmp) / os.path.basename(path)
        if exists:
            img.g.download(path, str(local))
        else:
            local.write_bytes(b"")
        before = local.read_bytes()
        r = subprocess.run(shlex.split(editor) + [str(local)])
        if r.returncode:
            raise PiImgError(f"{editor} exited with status {r.returncode}; {path} not changed")
        after = local.read_bytes()
        if after == before and exists:
            return f"{path}: not changed"
        if exists:
            _write_like(img, path, after)
        else:
            img.write(path, after)
    return f"{path}: saved ({len(after)} bytes)"


def _write_like(img, path, data):
    st = img.g.statns(path)
    img.write(path, data, mode=st["st_mode"] & 0o7777, uid=st["st_uid"], gid=st["st_gid"])


def get_files(img, src, dest):
    dest = Path(dest)
    if img.g.is_dir(src):
        dest.mkdir(parents=True, exist_ok=True)
        with progress.task(f"Copying {src} out of the image"):
            img.g.copy_out(src, str(dest))
        return f"{src} -> {dest / os.path.basename(src.rstrip('/'))}"
    if not img.exists(src):
        raise PiImgError(f"No such file in the image: {src}")
    if dest.is_dir():
        dest = dest / os.path.basename(src)
    img.g.download(src, str(dest))
    return f"{src} -> {dest}"


def put_files(img, src, dest, owner=None, mode=None):
    src = Path(src)
    if not src.exists():
        raise PiImgError(f"No such local file: {src}")
    uid, gid = img.lookup_owner(owner) if owner else (0, 0)
    if src.is_dir():
        if not img.g.is_dir(dest):
            img.g.mkdir_p(dest)
        with progress.task(f"Copying {src} into the image"):
            img.g.copy_in(str(src), dest)
        top = f"{dest.rstrip('/')}/{src.name}"
        for p in [top] + [f"{top}/{x}" for x in img.g.find(top)]:
            img.g.lchown(uid, gid, p)
        return f"{src} -> {top} (owner {uid}:{gid})"
    if dest.endswith("/") and not img.g.is_dir(dest):
        img.g.mkdir_p(dest)
    target = f"{dest.rstrip('/')}/{src.name}" if img.g.is_dir(dest) else dest
    m = int(str(mode), 8) if mode else (src.stat().st_mode & 0o7777)
    img.write(target, src.read_bytes(), mode=m, uid=uid, gid=gid)
    return f"{src} -> {target} (mode {m:o}, owner {uid}:{gid})"


def edit_config_txt(img, add=(), remove=(), set_=(), section="all"):
    for line in add:
        img.config_txt_add(line, section)
    if remove:
        lines = img.read_text(img.config_txt).splitlines()
        img.write(img.config_txt, "\n".join(l for l in lines if l.strip() not in remove) + "\n")
    for kv in set_:
        if "=" not in kv:
            raise PiImgError(f"--set needs KEY=VALUE, not {kv!r}")
        k, v = kv.split("=", 1)
        img.config_var(k, v)
    return img.read_text(img.config_txt).rstrip()


def edit_cmdline(img, add=(), remove=()):
    for p in remove:
        img.cmdline_remove(p)
    for p in add:
        img.cmdline_add(p)
    return img.read_text(img.cmdline_txt).rstrip()
