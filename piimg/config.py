"""piimg's settings: ~/.config/piimg/config.toml (or $XDG_CONFIG_HOME/piimg).

    piimg config                         show every setting and where it comes from
    piimg config images_dir ~/Images     set one
    piimg config --unset images_dir      back to the default

Under sudo (or pkexec, as the GUI uses for cards), the invoking user's file is
used, so `sudo piimg ...` sees the same settings (and image folder) as plain
`piimg`.
"""
import os
import pwd
import tomllib
from pathlib import Path

from .errors import PiImgError

SETTINGS = {
    # key: (default, help)
    "images_dir": ("~/Downloads", "where `piimg images download` puts official images, and where "
                                  "`piimg images local` looks for them"),
    "catalog_url": ("https://downloads.raspberrypi.com/os_list_imagingutility_v4.json",
                    "the OS list Raspberry Pi Imager uses"),
    "catalog_max_age_hours": ("24", "how long a downloaded catalog is reused before fetching it again"),
}


def invoking_user():
    """When running as root for someone through sudo or pkexec: their
    pwd entry (pw_uid, pw_gid, pw_dir, ...). Otherwise None."""
    if os.geteuid() != 0:
        return None
    try:
        if os.environ.get("SUDO_USER"):
            return pwd.getpwnam(os.environ["SUDO_USER"])
        for var in ("SUDO_UID", "PKEXEC_UID"):
            if os.environ.get(var, "").isdigit():
                return pwd.getpwuid(int(os.environ[var]))
    except KeyError:
        pass
    return None


def give_to_user(path):
    """Under sudo or pkexec, make a file piimg created for the user theirs."""
    who = invoking_user()
    if who:
        try:
            os.chown(path, who.pw_uid, who.pw_gid)
        except OSError:
            pass


def user_home():
    """The home of the user piimg works for: under sudo or pkexec, the invoking user's."""
    who = invoking_user()
    return Path(who.pw_dir) if who else Path.home()


def _xdg(var, fallback):
    v = os.environ.get(var)
    if v and os.geteuid() != 0:
        return Path(v)
    return user_home() / fallback


def config_path():
    return _xdg("XDG_CONFIG_HOME", ".config") / "piimg" / "config.toml"


def cache_dir():
    return _xdg("XDG_CACHE_HOME", ".cache") / "piimg"


def load():
    p = config_path()
    if not p.exists():
        return {}
    try:
        return tomllib.loads(p.read_text())
    except tomllib.TOMLDecodeError as e:
        raise PiImgError(f"{p}: {e}")


def get(key):
    if key not in SETTINGS:
        raise PiImgError(f"unknown setting {key!r} (known: {', '.join(SETTINGS)})")
    v = str(load().get(key, SETTINGS[key][0]))
    if key.endswith("_dir"):
        v = str(Path(v.replace("~", str(user_home()), 1) if v.startswith("~") else v))
    return v


def images_dir():
    return Path(get("images_dir"))


def _quote(v):
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def save(values):
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# piimg settings; see `piimg config`"] + [f"{k} = {_quote(v)}" for k, v in sorted(values.items())]
    tmp = p.with_name(p.name + ".part")
    tmp.write_text("\n".join(lines) + "\n")
    tmp.replace(p)
    who = invoking_user()
    if who:  # keep it the user's file
        for path in (p, p.parent):
            os.chown(path, who.pw_uid, who.pw_gid)


def set_value(key, value):
    if key not in SETTINGS:
        raise PiImgError(f"unknown setting {key!r} (known: {', '.join(SETTINGS)})")
    if key.endswith("_hours") and not str(value).replace(".", "", 1).isdigit():
        raise PiImgError(f"{key} must be a number")
    values = load()
    values[key] = value
    save(values)


def unset(key):
    values = load()
    values.pop(key, None)
    save(values)


def table():
    from .output import Table
    values = load()
    t = Table(["setting", "value", "source", "meaning"], title=f"Settings ({config_path()})")
    for k, (default, help_) in SETTINGS.items():
        t.add(k, get(k), "config file" if k in values else "default", help_)
    return t
