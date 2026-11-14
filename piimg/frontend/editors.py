"""Editors: read the target's state, change it, then commit the changes in one go.

Some commands come in pairs: one lists things (services, users, wifi
profiles), others change one of them at a time (service enable ...). An
editor puts such a pair behind one page:

    query       a read-only command whose output (a table, or text for a
                file editor) is the initial state
    items       one per row; editable columns can be changed, items can be
                added, removed or reordered where the editor allows it, and
                each item remembers its original values, so the editor
                knows what changed (dirty) and can revert it
    commit      the changes become the steps of a recipe, run once with
                `piimg run TARGET RECIPE`: one start of the image instead of
                one per change, the run dialog's progress, and --dry-run
                for a preview. (An editor of something that is not in the
                image, like the images folder, runs its own command.)

Layouts: "list" (items as rows) and "text" (one file in a text editor). A
"review" editor (overlay changes) only shows; its Commit runs a command.

An Editor looks enough like a frontend.commands.Command (name, summary(),
help, meta.group, meta.target) to sit in a front end's command list, where
it stands in for the commands in `replaces`. Toolkit-neutral: piimg.gui
(and later the TUI) draw it.
"""
import difflib
import os
import re
import tempfile
from dataclasses import dataclass, field
from types import SimpleNamespace

from . import proc as P
from .fields import FieldDef

RECIPE = "@RECIPE@"     # stands for the recipe file in a commit's command line


@dataclass
class Column:
    key: str
    label: str
    editable: bool = False
    kind: str = "text"      # text, choice, switch (values "on"/"off"), secret, lines, int
    inline: bool = False    # on the row itself (else in the row's expanded part)
    help: str = ""
    placeholder: str = ""


@dataclass
class Item:
    key: str
    orig: dict
    values: dict = field(default_factory=dict)
    new: bool = False
    removed: bool = False

    def __post_init__(self):
        if not self.values:
            self.values = dict(self.orig)

    def changed(self, col=None):
        if col is not None:
            return self.values.get(col) != self.orig.get(col)
        return self.new or self.removed or self.values != self.orig

    def get(self, col):
        return self.values.get(col, "")


class Editor:
    """Base class. A subclass sets the class attributes and writes steps()."""

    name = ""            # its name in the command list
    title = ""           # page heading
    summary_text = ""    # one line, for the list
    help = ""            # a paragraph, for the page
    group = "Repair & configure"
    replaces = ()        # commands this editor stands for in a front end's list
    query = ()           # the read-only command, without the target: ("services", "--all")
    columns = []         # [Column]; the first is the key
    filters = [("all", "All")]   # [(id, label)]; see matches()
    noun = ("item", "items")
    row_actions = []     # [(label, icon, function(target, item) -> argv)]: run as plain commands
    layout = "list"      # list | text | review
    language = None      # text layout: GtkSourceView language id
    needs_target = True
    needs_overlay = False
    can_add = False
    can_remove = False
    ordered = False      # the order of the items means something (and can be changed)
    can_preview = True   # a dry run is possible
    add_label = "Add"
    remove_label = "Remove"
    internal = ()        # columns that are bookkeeping (shown elsewhere or not at all), not details

    def __init__(self):
        self.items = []
        self.by_key = {}
        self.orig_order = []
        self.loaded = False
        self.footer = ""
        self._serial = 0
        self.meta = SimpleNamespace(group=self.group, target="image" if self.needs_target else None,
                                    writes=self.needs_target, output="editor", abort="warn")

    # ---- looking like a Command

    is_editor = True

    def summary(self):
        return self.summary_text

    def has_form(self):
        return False

    # ---- loading

    def query_argv(self, target):
        return [*self.query, *(["--", target] if self.needs_target else [])]

    def load_output(self, out):
        """The query's stdout lines -> the items."""
        table = P.first_table(out)
        if table is None:
            raise ValueError("the command printed no table")
        self.load(table)

    def load(self, table):
        """Fill the items from a table (a dict: columns, rows, footer), dropping any changes."""
        cols = table.get("columns") or []
        items = []
        for row in table.get("rows") or []:
            values = {c: ("" if v is None else str(v)) for c, v in zip(cols, row)}
            values = self.from_row(values)
            if values is not None:
                items.append(Item(values[self.columns[0].key], values))
        self.footer = table.get("footer") or ""
        self.set_items(items)

    def set_items(self, items):
        self.items = self.sort(items)
        self.by_key = {i.key: i for i in self.items}
        self.orig_order = [i.key for i in self.items]
        self.loaded = True

    def sort(self, items):
        return items

    def from_row(self, values):
        """A table row's values -> an item's values (None to leave the row out)."""
        return values

    # ---- how an item looks

    def label(self, item):
        return item.key

    def subtitle(self, item):
        return "  ·  ".join(item.get(c.key) for c in self.columns[1:]
                            if not c.editable and item.get(c.key))

    def column(self, key):
        return next(c for c in self.columns if c.key == key)

    def kind(self, item, col):
        return self.column(col).kind

    def editable(self, item, col):
        return self.column(col).editable

    def choices(self, item, col):
        """The values a choice column can take for this item (for a text column: suggestions)."""
        return []

    def removable(self, item):
        return self.can_remove

    # ---- changing

    def set(self, key, col, value):
        item = self.by_key[key]
        if not self.editable(item, col):
            raise KeyError(f"{col} cannot be changed")
        if self.kind(item, col) == "choice" and value not in self.choices(item, col):
            raise ValueError(f"{key} cannot be {value}")
        item.values[col] = value
        return item.changed()

    def add_fields(self):
        """The fields of the Add form."""
        return []

    def new_values(self, values):
        """The Add form's values -> a new item's values (raise ValueError to refuse)."""
        raise NotImplementedError

    def add(self, values, after=None):
        """A new item from the Add form; after the item `after` (else at the end)."""
        vals = self.new_values(values)
        key = vals[self.columns[0].key]
        if key in self.by_key or not key:
            self._serial += 1
            key = f"{key}#new{self._serial}"
        item = Item(key, {c.key: "" for c in self.columns}, dict(vals), new=True)
        at = self.items.index(self.by_key[after]) + 1 if after in self.by_key else len(self.items)
        self.items.insert(at, item)
        self.by_key[key] = item
        return item

    def remove(self, key):
        """Mark an item for removal (again: keep it). A new item just goes."""
        item = self.by_key[key]
        if item.new:
            self.items.remove(item)
            del self.by_key[key]
            return None
        if not self.removable(item):
            raise ValueError(f"{key} cannot be removed")
        item.removed = not item.removed
        return item

    def move(self, key, delta):
        """Move an item up (-1) or down (+1); False at an end."""
        i = self.items.index(self.by_key[key])
        j = i + delta
        if not (self.ordered and 0 <= j < len(self.items)):
            return False
        self.items.insert(j, self.items.pop(i))
        return True

    def revert(self, key=None):
        if key is not None:
            item = self.by_key[key]
            if item.new:
                self.remove(key)
                return
            item.values, item.removed = dict(item.orig), False
            return
        self.items = [self.by_key[k] for k in self.orig_order]
        self.by_key = {i.key: i for i in self.items}
        for item in self.items:
            item.values, item.removed = dict(item.orig), False

    @property
    def order_changed(self):
        if not self.ordered:
            return False
        return [i.key for i in self.items if not i.new and not i.removed] != \
            [k for k in self.orig_order if not self.by_key[k].removed]

    @property
    def dirty(self):
        return self.order_changed or any(i.changed() for i in self.items)

    def changed_items(self):
        return [i for i in self.items if i.changed()]

    def show_value(self, item, col, value):
        if self.kind(item, col) == "secret":
            return "(new)" if value else "(unchanged)"
        if self.kind(item, col) == "lines":
            n = len([v for v in str(value).splitlines() if v.strip()])
            return f"{n} line{'s' if n != 1 else ''}"
        return value or "(none)"

    def describe_changes(self):
        """One line per change, for a confirmation."""
        out = []
        for i in self.items:
            if i.new:
                out.append(f"add {self.label(i)}")
            elif i.removed:
                out.append(f"{self.remove_label.lower()} {self.label(i)}")
            else:
                for c in self.columns:
                    if self.editable(i, c.key) and i.changed(c.key):
                        out.append(f"{self.label(i)}: {c.label.lower()} "
                                   f"{self.show_value(i, c.key, i.orig.get(c.key))} → "
                                   f"{self.show_value(i, c.key, i.get(c.key))}")
        if self.order_changed:
            out.append("new order: " + ", ".join(self.label(i) for i in self.items if not i.removed))
        return out

    def problems(self):
        """Why the changes cannot be committed as they are ([] if they can)."""
        return []

    def count_text(self):
        n = len([i for i in self.items if not i.new])
        changed = len(self.changed_items())
        text = f"{n} {self.noun[n != 1]}"
        return text + (f", {changed} changed" if changed else "")

    # ---- filtering

    def matches(self, item, needle="", filter_id="all"):
        if filter_id == "changed" and not item.changed():
            return False
        needle = needle.lower().strip()
        return not needle or needle in self.label(item).lower() or \
            any(needle in str(v).lower() for k, v in item.values.items() if self.kind(item, k) != "secret")

    # ---- committing

    def steps(self):
        """The recipe steps that make the changes (a list of {action: argument})."""
        raise NotImplementedError

    def recipe(self):
        return {"name": f"{self.title} (piimg editor)", "steps": self.steps()}

    def recipe_text(self):
        from .recipe import dump
        return dump(self.recipe())

    def commit(self, target, dry=False):
        """(argv, recipe text or None): how to make the changes. RECIPE in argv
        stands for the file the recipe is written to."""
        return ["run", *(["--dry-run"] if dry else []), "--", target, RECIPE], self.recipe_text()

    @staticmethod
    def write_recipe(text, name="editor"):
        """Write a recipe to a private temporary file (the caller removes it). It
        may hold a wifi password: only the user (and root) can read it."""
        fd, path = tempfile.mkstemp(prefix=f"piimg-{name}-", suffix=".yaml")
        with os.fdopen(fd, "w") as f:
            f.write(text)
        return path

    @staticmethod
    def commit_argv(target, recipe_path, dry=False):
        return ["run", *(["--dry-run"] if dry else []), "--", target, recipe_path]


def _onoff(v):
    return "on" if str(v).lower() in ("on", "true", "yes", "1") else "off"


# ------------------------------------------------------------------ services

class ServicesEditor(Editor):
    name = "services"
    title = "Services"
    summary_text = "enable, disable, mask or unmask systemd units"
    help = ("Every systemd service, socket, timer and path unit in the image, and whether it starts "
            "at boot. Change the state of the ones you want, then Commit: the changes are made in one "
            "run, the way systemctl would (enabled = linked into the targets its [Install] section "
            "names; masked = it cannot start at all). Static units have no [Install] section: they "
            "start when another unit needs them, and can only be masked.")
    replaces = ("services", "service")
    query = ("services", "--all")
    columns = [Column("unit", "Unit"), Column("state", "State", editable=True, kind="choice", inline=True,
                                                 help="what happens to the unit at boot"),
               Column("wanted by", "Wanted by"), Column("origin", "Origin"),
               Column("description", "Description"), Column("unit file", "Unit file")]
    filters = [("startable", "Enabled & disabled"), ("all", "All"), ("enabled", "Enabled"),
               ("disabled", "Disabled"), ("static", "Static"), ("masked", "Masked"), ("changed", "Changed")]
    noun = ("unit", "units")
    row_actions = [("Show the unit file", "document-properties-symbolic",
                    lambda target, item: ["service", "--", target, "show", item.key])]

    STATES = ("enabled", "disabled", "static", "masked")

    def from_row(self, values):
        if values["state"].startswith("BROKEN"):
            values["state"] = "broken"
        return values

    def subtitle(self, item):
        return "  ·  ".join(item.get(k) for k in ("wanted by", "origin", "description") if item.get(k))

    def choices(self, item, col):
        if col != "state":
            return []
        orig = item.orig["state"]
        # systemctl refuses to mask a unit whose own file is in /etc/systemd/system
        maskable = not item.orig.get("unit file", "").startswith("/etc/systemd/system/")
        mask = ["masked"] if maskable else []
        if orig in ("enabled", "disabled"):
            return ["enabled", "disabled", *mask]
        if orig == "static":
            return ["static", *mask]
        if orig == "masked":
            return ["masked", "unmasked", "enabled"]
        if orig == "broken":   # links to a unit file that is gone
            return ["broken", "disabled", *mask]
        return [orig]

    def matches(self, item, needle="", filter_id="startable"):
        if not super().matches(item, needle, filter_id):
            return False
        state = item.orig["state"]
        if filter_id == "startable":
            return state != "static" or item.changed()
        if filter_id in self.STATES:
            return state == filter_id or item.get("state") == filter_id
        return True

    def steps(self):
        ops = {"unmask": [], "disable": [], "mask": [], "enable": []}
        for i in self.changed_items():
            old, new = i.orig["state"], i.get("state")
            if old == "masked":
                ops["unmask"].append(i.key)
            if new == "enabled":
                ops["enable"].append(i.key)
            elif new == "disabled" or (new == "masked" and old in ("enabled", "broken")):
                ops["disable"].append(i.key)
            if new == "masked":
                ops["mask"].append(i.key)
        arg = {k: v for k, v in ops.items() if v}
        return [{"systemd": arg}] if arg else []


# --------------------------------------------------------------------- users

SHELLS = ["/bin/bash", "/bin/sh", "/usr/bin/zsh", "/usr/bin/fish", "/usr/sbin/nologin"]


class UsersEditor(Editor):
    name = "users"
    title = "Users"
    summary_text = "passwords, shells, ssh keys; rename or delete accounts"
    help = ("The accounts people log in as (root and uid 1000 and up). For each one you can set a new "
            "password, lock or unlock it, change its login shell or name, edit its authorized ssh keys "
            "(one per line), or delete it with its home folder. A new password is hashed here and only "
            "the hash goes into the image. To create the first account of a fresh image, use "
            "`apply first-boot`.")
    replaces = ("users", "passwd", "ssh-key")
    group = "Repair & configure"
    query = ("users", "--keys")
    columns = [Column("user", "User"),
               Column("name", "Name", editable=True, help="rename the account (home, groups and sudoers "
                                                          "follow)"),
               Column("new password", "New password", editable=True, kind="secret",
                      help="leave empty to keep the password"),
               Column("locked", "Password locked", editable=True, kind="switch",
                      help="a locked password cannot be used to log in (ssh keys still work)"),
               Column("shell", "Login shell", editable=True,
                      help="/usr/sbin/nologin means the account cannot log in"),
               Column("keys", "Authorized ssh keys", editable=True, kind="lines",
                      help="one public key per line, e.g. the contents of ~/.ssh/id_ed25519.pub"),
               Column("uid", "uid"), Column("password", "Password"), Column("groups", "Groups"),
               Column("note", "Note")]
    noun = ("account", "accounts")
    can_remove = True
    remove_label = "Delete"

    def from_row(self, values):
        values["name"] = values["user"]
        values["new password"] = ""
        values["locked"] = "on" if values.get("password") == "locked" else "off"
        values["keys"] = values.get("keys", "")
        return values

    def subtitle(self, item):
        keys = len([k for k in item.get("keys").splitlines() if k.strip()])
        parts = [f"uid {item.get('uid')}", f"password {item.get('password')}", item.get("shell"),
                 f"{keys} ssh key{'s' if keys != 1 else ''}", item.get("note")]
        return "  ·  ".join(p for p in parts if p)

    def editable(self, item, col):
        if col == "name" and item.orig.get("uid") == "0":
            return False
        return super().editable(item, col)

    def removable(self, item):
        return item.orig.get("uid") != "0"

    def choices(self, item, col):
        return SHELLS if col == "shell" else []

    def problems(self):
        from ..system import USERNAME_RE
        out = []
        names = [i.get("name") for i in self.items if not i.removed]
        for i in self.changed_items():
            if i.removed:
                continue
            if i.changed("name") and not USERNAME_RE.fullmatch(i.get("name") or ""):
                out.append(f"{i.key}: {i.get('name')!r} is not a valid user name (lower case letters, "
                           "digits and -, starting with a letter)")
            if names.count(i.get("name")) > 1:
                out.append(f"{i.key}: there is already an account called {i.get('name')}")
            if i.changed("shell") and not i.get("shell").startswith("/"):
                out.append(f"{i.key}: the shell must be a full path, e.g. /bin/bash")
            if i.get("new password") and i.get("locked") == "on" and not i.changed("locked"):
                pass   # a new password on a locked account stays locked: allowed
        return out

    def describe_changes(self):
        out = []
        for i in self.changed_items():
            if i.removed:
                out.append(f"delete {i.key} (and its home folder)")
                continue
            if i.changed("name"):
                out.append(f"rename {i.key} → {i.get('name')}")
            if i.get("new password"):
                out.append(f"{i.key}: new password")
            if i.changed("locked"):
                out.append(f"{i.key}: {'lock' if i.get('locked') == 'on' else 'unlock'} the password")
            if i.changed("shell"):
                out.append(f"{i.key}: shell {i.orig.get('shell')} → {i.get('shell')}")
            add, remove = self._key_diff(i)
            if add or remove:
                out.append(f"{i.key}: ssh keys " + ", ".join(p for p in (f"+{len(add)}" if add else "",
                                                                          f"-{len(remove)}" if remove else "")
                                                             if p))
        return out

    @staticmethod
    def _key_diff(item):
        old = [k.strip() for k in item.orig.get("keys", "").splitlines() if k.strip()]
        new = [k.strip() for k in item.get("keys").splitlines() if k.strip()]
        return [k for k in new if k not in old], [k for k in old if k not in new]

    def steps(self):
        from ..recipes.userdata import hash_password
        out = []
        for i in self.changed_items():
            arg = {"name": i.key}
            if i.removed:
                arg["delete"] = True
                out.append({"user": arg})
                continue
            if i.get("new password"):
                arg["password_hash"] = hash_password(i.get("new password"))
            if i.changed("locked"):
                arg["lock" if i.get("locked") == "on" else "unlock"] = True
            if i.changed("shell"):
                arg["shell"] = i.get("shell")
            add, remove = self._key_diff(i)
            if remove:
                arg["remove_keys"] = remove
            if add:
                arg["add_keys"] = add
            if i.changed("name"):
                arg["rename"] = i.get("name")
            if len(arg) > 1:
                out.append({"user": arg})
        return out


# ------------------------------------------------------------ system settings

class SystemEditor(Editor):
    name = "system"
    title = "System settings"
    summary_text = "hostname, boot mode, ssh, interfaces, locale, time zone, keyboard"
    help = ("The settings raspi-config changes, read from the image. Commit makes the changes the way "
            "raspi-config's own code does, offline. The boot mode's autologin logs in the first regular "
            "user; a new locale is compiled on first boot.")
    replaces = ("hostname", "boot-mode", "settings")
    query = ("settings",)
    columns = [Column("setting", "Setting"), Column("value", "Value", editable=True, inline=True),
               Column("label", "Label"), Column("kind", "Kind"), Column("help", "Help")]
    noun = ("setting", "settings")
    internal = ("label", "kind", "help")

    def describe_changes(self):
        return [f"{self.label(i)}: {self.show_value(i, 'value', i.orig.get('value'))} → "
                f"{self.show_value(i, 'value', i.get('value'))}" for i in self.changed_items()]

    def label(self, item):
        return item.get("label") or item.key

    def subtitle(self, item):
        return item.get("help")

    def kind(self, item, col):
        if col == "value":
            return {"switch": "switch", "choice": "choice"}.get(item.orig.get("kind"), "text")
        return super().kind(item, col)

    def choices(self, item, col):
        if col == "value" and item.key == "boot mode":
            from ..system import BOOT_MODES
            return list(BOOT_MODES)
        return []

    def problems(self):
        out = []
        for i in self.changed_items():
            v = i.get("value").strip()
            if i.key == "hostname" and not re.fullmatch(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?", v):
                out.append(f"{v!r} is not a valid hostname (letters, digits and '-', up to 63)")
            elif i.key == "wifi country" and not re.fullmatch(r"[A-Za-z]{2}", v):
                out.append(f"the wifi country is a two-letter code, not {v!r}")
            elif i.key in ("locale", "timezone", "keyboard") and (not v or " " in v):
                out.append(f"{self.label(i)}: {v!r} is not valid")
        return out

    def steps(self):
        from ..system import settings_calls
        changes = {i.key: i.get("value").strip() for i in self.changed_items()}
        if "wifi country" in changes:
            changes["wifi country"] = changes["wifi country"].upper()
        return [{"raspi_config": settings_calls(changes)}] if changes else []


# ---------------------------------------------------------------------- wifi

class WifiEditor(Editor):
    name = "wifi"
    title = "Wi-Fi networks"
    summary_text = "add, remove and order the wifi networks the Pi connects to"
    help = ("The NetworkManager profiles in the image. The Pi tries the networks from the top down "
            "(the order sets their priority); networks with Connect automatically off are only used "
            "when asked for. Passwords are stored in the image, readable only by root. Set the wifi "
            "country under System settings: Raspberry Pi OS keeps wifi off until one is set.")
    replaces = ("wifi",)
    query = ("wifi",)
    columns = [Column("connection", "Profile"),
               Column("autoconnect", "Connect automatically", editable=True, kind="switch", inline=True),
               Column("ssid / interface", "SSID"), Column("type", "Type"), Column("security", "Security"),
               Column("hidden", "Hidden"), Column("priority", "Priority"), Column("file", "File"),
               Column("password", "Password")]
    noun = ("profile", "profiles")
    can_add = True
    can_remove = True
    ordered = True
    add_label = "Add Network"

    def from_row(self, values):
        values["autoconnect"] = _onoff(values.get("autoconnect", "true"))
        values["password"] = ""
        return values

    def sort(self, items):
        def prio(i):
            try:
                return -int(i.orig.get("priority") or 0)
            except ValueError:
                return 0
        return sorted(items, key=lambda i: (prio(i), i.key.lower()))

    def subtitle(self, item):
        sec = item.get("security")
        parts = [item.get("type"), f"ssid {item.get('ssid / interface')}" if item.get("ssid / interface") else "",
                 "open" if sec == "none" else "password" if sec else "", "hidden" if item.get("hidden") == "true"
                 else ""]
        return "  ·  ".join(p for p in parts if p)

    def add_fields(self):
        return [FieldDef("ssid", "Network name (SSID)", required=True),
                FieldDef("password", "Password", "secret", help="8 to 63 characters; leave empty for an "
                                                                "open network"),
                FieldDef("hidden", "Hidden network", "bool", help="the network does not broadcast its name")]

    def new_values(self, values):
        ssid = (values.get("ssid") or "").strip()
        pw = values.get("password") or ""
        if not ssid:
            raise ValueError("the network needs a name")
        if pw and not 8 <= len(pw) <= 63:
            raise ValueError("a WPA password is 8 to 63 characters")
        return {"connection": re.sub(r"[/\\]", "_", ssid), "type": "wifi", "ssid / interface": ssid,
                "autoconnect": "on", "security": "wpa-psk" if pw else "none",
                "hidden": "true" if values.get("hidden") else "false", "priority": "0", "file": "",
                "password": pw}

    def describe_changes(self):
        out = super().describe_changes()
        return [l.replace("add ", "add network ", 1) if l.startswith("add ") else l for l in out]

    def _priorities(self):
        """{key: priority} for the current order (top = highest), when it changed."""
        kept = [i for i in self.items if not i.removed]
        if not (self.order_changed or any(i.new for i in kept)):
            return {}
        n = len(kept)
        return {i.key: (n - k) * 10 for k, i in enumerate(kept)}

    def steps(self):
        prio = self._priorities()
        arg = {"remove": [], "add": [], "set": {}}
        for i in self.items:
            if i.removed:
                arg["remove"].append(i.key)
            elif i.new:
                net = {"ssid": i.get("ssid / interface"), "priority": prio.get(i.key, 0)}
                if i.get("password"):
                    net["password"] = i.get("password")
                if i.get("hidden") == "true":
                    net["hidden"] = True
                arg["add"].append(net)
            else:
                opts = {}
                if i.key in prio and str(prio[i.key]) != (i.orig.get("priority") or "0"):
                    opts["priority"] = prio[i.key]
                if i.changed("autoconnect"):
                    opts["autoconnect"] = i.get("autoconnect") == "on"
                if opts:
                    arg["set"][i.key] = opts
        # new networks that should not connect by themselves
        for i in self.items:
            if i.new and not i.removed and i.get("autoconnect") == "off":
                arg["set"][i.get("connection")] = {"autoconnect": False}
        arg = {k: v for k, v in arg.items() if v}
        return [{"wifi": arg}] if arg else []


# ------------------------------------------------------------ text file editors

class TextEditor(Editor):
    """One file as text: the query prints it, commit replaces it."""
    layout = "text"

    def __init__(self):
        super().__init__()
        self.orig_text = ""
        self.text = ""

    def load_output(self, out):
        self.orig_text = self.text = "\n".join(out).rstrip("\n") + "\n"
        self.loaded = True

    @property
    def dirty(self):
        return self.loaded and self.text.rstrip("\n") != self.orig_text.rstrip("\n")

    def revert(self, key=None):
        self.text = self.orig_text

    def changed_lines(self):
        old, new = self.orig_text.splitlines(), self.text.splitlines()
        return list(difflib.unified_diff(old, new, lineterm="", n=0))[2:]

    def describe_changes(self):
        return [l for l in self.changed_lines() if not l.startswith("@@")]

    def count_text(self):
        n = len(self.text.splitlines())
        changed = len(self.describe_changes())
        return f"{n} lines" + (f", {changed} changed" if changed else "")


class ConfigTxtEditor(TextEditor):
    name = "config-txt"
    title = "config.txt"
    summary_text = "edit the firmware's config.txt (display, overlays, hardware)"
    help = ("The boot partition's config.txt, read by the firmware before Linux starts: display modes, "
            "device tree overlays and parameters, hardware interfaces. [section] lines apply what follows "
            "to some models only ([pi4], [pi5], [all]). Commit replaces the file with this text.")
    replaces = ("config-txt",)
    query = ("config-txt",)
    language = "ini"
    group = "Repair & configure"

    def problems(self):
        if not self.text.strip():
            return ["config.txt would be empty"]
        return []

    def steps(self):
        return [{"config_txt": {"content": self.text}}]


class CmdlineEditor(Editor):
    name = "cmdline"
    title = "Kernel command line"
    summary_text = "edit cmdline.txt: kernel parameters, in order"
    help = ("The kernel's parameters from the boot partition's cmdline.txt, in order. Change one in "
            "place, add, remove or move them; Commit writes the line back. The Pi will not boot "
            "without root=; console= appears once per console (the last one is where boot messages "
            "go).")
    replaces = ("cmdline",)
    query = ("cmdline",)
    columns = [Column("id", "Parameter"), Column("parameter", "Parameter", editable=True, inline=True,
                                                  placeholder="name=value")]
    noun = ("parameter", "parameters")
    can_add = True
    can_remove = True
    ordered = True
    add_label = "Add Parameter"

    KNOWN = {"root": "the root filesystem", "rootfstype": "its filesystem type", "console": "a console",
             "fsck.repair": "repair the root filesystem at boot", "rootwait": "wait for the root device",
             "quiet": "fewer boot messages", "splash": "the boot splash screen", "resize": "grow the root "
             "filesystem on first boot", "init": "the first program", "video": "a display mode",
             "modules-load": "kernel modules to load", "cfg80211.ieee80211_regdom": "the wifi country",
             "systemd.run": "a command on first boot", "plymouth.ignore-serial-consoles":
             "no splash on serial consoles", "dwc_otg.lpm_enable": "USB link power management",
             "ds": "the device tree"}

    def load_output(self, out):
        words = " ".join(out).split()
        items = [Item(f"p{n}", {"id": f"p{n}", "parameter": w}) for n, w in enumerate(words)]
        self.footer = ""
        self.set_items(items)

    def label(self, item):
        return item.get("parameter").split("=", 1)[0] or "(empty)"

    def subtitle(self, item):
        return self.KNOWN.get(self.label(item), "")

    def add_fields(self):
        return [FieldDef("parameter", "Parameter", required=True, placeholder="e.g. video=HDMI-A-1:1280x720@60")]

    def new_values(self, values):
        p = (values.get("parameter") or "").strip()
        if not p or any(ch.isspace() for ch in p):
            raise ValueError("a parameter is one word, without spaces")
        return {"id": f"new{self._serial}", "parameter": p}

    def describe_changes(self):
        out = []
        for i in self.items:
            if i.new:
                out.append(f"add {i.get('parameter')}")
            elif i.removed:
                out.append(f"remove {i.orig.get('parameter')}")
            elif i.changed("parameter"):
                out.append(f"{i.orig.get('parameter')} → {i.get('parameter')}")
        if self.order_changed:
            out.append("new order")
        if out:
            out.append("new line: " + self.line())
        return out

    def line(self):
        return " ".join(i.get("parameter").strip() for i in self.items if not i.removed and i.get("parameter").strip())

    def problems(self):
        out = []
        words = [i.get("parameter").strip() for i in self.items if not i.removed]
        if any(any(ch.isspace() for ch in w) for w in words):
            out.append("a parameter cannot contain spaces (add another parameter instead)")
        if not any(w.startswith("root=") for w in words):
            out.append("there is no root= parameter: the Pi would not find its root filesystem")
        return out

    def steps(self):
        return [{"cmdline": {"set": self.line()}}]


# ------------------------------------------------------------- images folder

class ImagesEditor(Editor):
    name = "images"
    title = "Images folder"
    summary_text = "the images you downloaded: verify or delete them"
    help = ("The images in your images folder (`piimg config images_dir`) and whether their SHA-256 "
            "matches the published one. Mark the ones to delete and Commit; Verify checks one again. "
            "Download more with `images download` (see `images list`).")
    group = "Official images"
    replaces = ("images local", "images delete")
    query = ("images", "local")
    columns = [Column("file", "File"), Column("size", "Size"), Column("release", "Release"),
               Column("image", "Image"), Column("verified", "Verified"), Column("note", "Note")]
    noun = ("image", "images")
    needs_target = False
    can_remove = True
    can_preview = False
    remove_label = "Delete"
    row_actions = [("Verify", "security-high-symbolic",
                    lambda target, item: ["images", "verify", "--", item.key])]

    def describe_changes(self):
        return [f"delete {i.key}" for i in self.items if i.removed]

    def commit(self, target, dry=False):
        return ["images", "delete", "--", *[i.key for i in self.items if i.removed]], None


# ----------------------------------------------------------- overlay changes

DIFF_RE = re.compile(r"^([-+=]) (\S) (\d+)\s+(\d+) (.*?)(?: -> (.*))?$")


class OverlayEditor(Editor):
    """Review only: the files the overlay changes, and Commit to write them to its base."""
    name = "changes"
    title = "Overlay changes"
    summary_text = "the files the overlay changes; commit them to the base image or card"
    help = ("What the open overlay changes compared with its base (virt-diff; slow on a large image). "
            "Nothing here is editable: Commit writes all of the overlay's changes into the base image or "
            "card (`piimg commit`), and the overlay is empty afterwards.")
    group = "Image & card"
    replaces = ("diff", "commit")
    query = ("diff",)
    layout = "review"
    needs_overlay = True
    can_preview = False
    columns = [Column("path", "Path"), Column("change", "Change"), Column("type", "Type"),
               Column("detail", "Detail")]
    filters = [("all", "All"), ("added", "Added"), ("removed", "Removed"), ("changed", "Changed")]
    noun = ("file", "files")
    TYPES = {"-": "file", "d": "directory", "l": "link", "c": "device", "b": "device", "p": "fifo", "s": "socket"}

    def load_output(self, out):
        seen = {}
        for line in out:
            m = DIFF_RE.match(line)
            if m:
                sign, typ, _mode, size, path, link = m.groups()
                prev = seen.get(path)
                change = {"+": "added", "-": "removed", "=": "changed"}[sign]
                if prev is not None and prev["change"] != change:
                    change = "changed"
                seen[path] = {"path": path, "change": change, "type": self.TYPES.get(typ, typ),
                              "detail": f"-> {link}" if link else f"{size} bytes"}
            elif line.startswith("# changed:") and seen:
                last = list(seen.values())[-1]
                last["detail"] += "  (" + line[2:].strip() + ")"
        self.footer = "" if seen else "The overlay changes nothing."
        self.set_items([Item(p, v) for p, v in seen.items()])

    def subtitle(self, item):
        return f"{item.get('change')}  ·  {item.get('type')}  ·  {item.get('detail')}"

    def matches(self, item, needle="", filter_id="all"):
        if filter_id in ("added", "removed", "changed") and item.get("change") != filter_id:
            return False
        return super().matches(item, needle, "all")

    @property
    def dirty(self):
        return False

    def commit(self, target, dry=False):
        return ["commit", "--", target], None


EDITORS = (ServicesEditor, UsersEditor, SystemEditor, WifiEditor, ConfigTxtEditor, CmdlineEditor,
           ImagesEditor, OverlayEditor)


def make_editors():
    return [cls() for cls in EDITORS]


def list_entries(cmds, editors, hide=()):
    """The command list with each editor in place of the commands it replaces
    (where the first of them in the editor's own group was), without `hide`."""
    replaced = {name for e in editors for name in e.replaces} | set(hide)
    group_of = {c.name: c.meta.group for c in cmds}
    anchors = {}
    for e in editors:
        names = [c.name for c in cmds if c.name in e.replaces]
        same = [n for n in names if group_of[n] == e.group]
        if same or names:
            anchors.setdefault((same or names)[0], []).append(e)
    out = []
    for c in cmds:
        if c.name not in replaced:
            out.append(c)
        out += anchors.get(c.name, [])
    for e in editors:
        if e not in out:   # nothing it replaces is listed: after the last entry of its group
            idx = [k for k, c in enumerate(out) if c.meta.group == e.group]
            out.insert(idx[-1] + 1 if idx else len(out), e)
    return out
