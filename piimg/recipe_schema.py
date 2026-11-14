"""What each recipe step looks like, for editors (the TUI's recipe editor).

script.Runner is what runs recipes; this module only describes their shape:
for every action, its fields with a type, whether they are required, and help
text. Keep it in step with script.Runner.op_* and docs/RECIPE-FORMAT.md
(tests/test_recipe_schema.py checks that every op_ has an entry here).

Field types:
  str      one line of text           text   several lines (file content, scripts)
  int      a number                   bool   true/false
  mode     a file mode ("0755")       choice one of `choices`
  list     several values, one per line
  map      KEY=VALUE pairs, one per line
  yaml     free YAML (conditions)
"""
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Field:
    name: str
    type: str = "str"
    required: bool = False
    help: str = ""
    choices: tuple = ()
    default: object = None


@dataclass
class Action:
    name: str
    summary: str
    help: str
    fields: list = field(default_factory=list)
    scalar: Optional[str] = None   # the field a bare value fills: `remove: /path`
    listform: Optional[str] = None  # the field a bare list fills: `modules: [a, b]`
    category: str = "Files"
    example: str = ""


PATH = "path inside the image; {{boot}} is the boot partition's mount point"
OWNER = "user:group, resolved from the image's passwd/group (numbers work too, e.g. 1000:1000)"
MODE = 'file mode in octal, quoted: "0755"'

ACTIONS = {a.name: a for a in [
    Action("echo", "print a message", "Prints a message in the run log. Useful to label sections and to "
           "see which branch of a when: was taken.",
           [Field("message", "str", True, "the text to print; {{vars}} are replaced")],
           scalar="message", category="Control", example='echo: "configuring {{display}}"'),
    Action("fail", "stop the recipe with an error", "Aborts the recipe with this message. Use it with "
           "when: to refuse images the recipe does not support.",
           [Field("message", "str", True, "the error message")], scalar="message", category="Control",
           example='fail: "this kernel has no pitft22 overlay"'),
    Action("mkdir", "create a directory", "Creates a directory and its parents (mkdir -p).",
           [Field("path", "str", True, PATH), Field("mode", "mode", help=MODE), Field("owner", help=OWNER)],
           scalar="path", example="mkdir: {path: /opt/thing, mode: \"0755\"}"),
    Action("write", "write a file", "Writes a file, creating its directory. content is taken literally "
           "(no {{var}} replacement) unless template is true, so shell scripts are safe. content_file is "
           "read from next to the recipe file instead.",
           [Field("path", "str", True, PATH), Field("content", "text", help="the file's content"),
            Field("content_file", help="local file (relative to the recipe) to use as the content"),
            Field("mode", "mode", help=MODE + "; default 0644"), Field("owner", help=OWNER),
            Field("if_exists", "choice", choices=("overwrite", "skip", "error"),
                  help="what to do if the file exists (default overwrite)"),
            Field("template", "bool", help="replace {{vars}} in content")],
           example="write: {path: /etc/motd, content: \"hello\\n\"}"),
    Action("copy", "copy local files into the image", "Copies a file, or a directory's whole contents "
           "(modes and symlinks kept), from next to the recipe file into the image.",
           [Field("src", "str", True, "local file or directory, relative to the recipe file"),
            Field("dest", "str", True, PATH), Field("mode", "mode", help=MODE + " (files only)"),
            Field("owner", help=OWNER)], example="copy: {src: files/opt/demo, dest: /opt/demo}"),
    Action("symlink", "create a symbolic link", "Creates (or replaces) a symbolic link.",
           [Field("path", "str", True, "the link to create"), Field("target", "str", True, "what it points to")],
           example="symlink: {path: /opt/current, target: /opt/v2}"),
    Action("remove", "delete a file or directory", "Deletes a path recursively (rm -rf).",
           [Field("path", "str", True, PATH)], scalar="path", example="remove: /etc/xdg/autostart/piwiz.desktop"),
    Action("chmod", "change a file's mode", "Sets the mode of a path.",
           [Field("path", "str", True, PATH), Field("mode", "mode", True, MODE)],
           example='chmod: {path: /opt/run.sh, mode: "0755"}'),
    Action("chown", "change a file's owner", "Sets the owner of a path. Users that only cloud-init creates "
           "at first boot do not exist yet: use numbers.",
           [Field("path", "str", True, PATH), Field("owner", "str", True, OWNER)],
           example='chown: {path: /home/pi/app, owner: "1000:1000"}'),
    Action("line", "make sure a line is (not) in a file", "Like Ansible's lineinfile. With match, the first "
           "line matching the regex is replaced by line (else line is appended). With state: absent, "
           "matching lines (or lines equal to line) are removed.",
           [Field("path", "str", True, PATH), Field("line", help="the line"),
            Field("match", help="regular expression selecting the line to replace / remove"),
            Field("state", "choice", choices=("present", "absent"), help="default present")],
           category="Edit files", example="line: {path: /etc/hosts, line: \"10.0.0.2 host\", match: '^10\\.0\\.0\\.2'}"),
    Action("sub", "regex search and replace in a file", "Regular expression substitution (Python syntax, "
           "multiline mode: ^ and $ match at each line).",
           [Field("path", "str", True, PATH), Field("pattern", "str", True, "regular expression"),
            Field("replace", "str", True, r"replacement; \1 refers to a group"),
            Field("count", "int", help="replace at most this many (default all)"),
            Field("must_match", "bool", help="fail if the pattern is not found")],
           category="Edit files", example="sub: {path: /etc/default/keyboard, pattern: 'XKBLAYOUT=.*', replace: 'XKBLAYOUT=\"us\"'}"),
    Action("config_txt", "edit config.txt", "Adds lines to a [section] of config.txt (no duplicates), "
           "removes exact lines, and sets key=value the way raspi-config does (rewriting commented or "
           "active key= lines).",
           [Field("add", "list", help="lines to add, e.g. dtoverlay=vc4-kms-v3d"),
            Field("remove", "list", help="exact lines to remove"),
            Field("set", "map", help="key=value settings, e.g. hdmi_force_hotplug=1"),
            Field("section", help="section for add: all (default), pi4, pi5, cm4, ..."),
            Field("content", "text", help="the whole file: replaces config.txt first (then add/remove/set)")],
           category="Boot & hardware", example="config_txt: {add: [dtoverlay=dwc2], section: all}"),
    Action("cmdline", "edit the kernel command line", "Adds or removes parameters in cmdline.txt. A "
           "trailing = in remove removes every value of that parameter (console= removes all consoles). "
           "set replaces the whole line first.",
           [Field("add", "list", help="parameters to add"), Field("remove", "list", help="parameters to remove"),
            Field("set", "str", help="the whole command line (replaces it before add/remove)")],
           category="Boot & hardware", example="cmdline: {add: [modules-load=dwc2]}"),
    Action("modules", "load kernel modules at boot", "Adds module names to "
           "/etc/modules-load.d/piimg.conf.",
           [Field("add", "list", True, "module names, e.g. i2c-dev")], scalar="add", listform="add",
           category="Boot & hardware", example="modules: [i2c-dev]"),
    Action("systemd", "enable, disable, mask or unmask units", "Creates or removes the symlinks systemctl "
           "would. No running systemd needed. Applied in the order unmask, disable, mask, enable.",
           [Field("enable", "list", help="units to enable"), Field("disable", "list", help="units to disable"),
            Field("mask", "list", help="units to mask"), Field("unmask", "list", help="units to unmask")],
           category="System", example="systemd: {enable: [ssh.service]}"),
    Action("user", "change an account", "Changes an existing account: a new password (as a crypt "
           "hash: `openssl passwd -6`), lock or unlock it, its login shell, ssh keys, its name; or "
           "deletes it. Create accounts with the first_boot or userdata recipes.",
           [Field("name", "str", True, "the account"),
            Field("password_hash", "str", help="new password, hashed ($6$... or $y$...)"),
            Field("lock", "bool", help="lock the password (key logins still work)"),
            Field("unlock", "bool", help="unlock a locked password"),
            Field("shell", "str", help="login shell, e.g. /bin/bash"),
            Field("add_keys", "list", help="ssh public keys to authorize"),
            Field("remove_keys", "list", help="ssh public keys to take out (whole lines)"),
            Field("rename", "str", help="new name (home, groups and sudoers follow)"),
            Field("delete", "bool", help="delete the account"),
            Field("remove_home", "bool", help="with delete: also its home folder (default true)")],
           scalar="name", category="System", example="user: {name: pi, shell: /bin/bash, add_keys: [ssh-ed25519 AAAA...]}"),
    Action("wifi", "wifi profiles", "NetworkManager profiles: add networks (written like `piimg wifi`), "
           "remove profiles by name, change a profile's priority (higher is tried first) or autoconnect, "
           "and set the wifi country.",
           [Field("add", "yaml", help="list of {ssid, password, hidden, priority}"),
            Field("remove", "list", help="profile names to delete"),
            Field("set", "yaml", help="{profile name: {priority: N, autoconnect: true/false}}"),
            Field("country", "str", help="two-letter wifi country, e.g. GB")],
           category="System", example="wifi: {add: [{ssid: home, password: secret123}], country: GB}"),
    Action("raspi_config", "raspi-config settings", "raspi-config nonint calls translated to offline "
           "edits. 0 = enable, 1 = disable, as in raspi-config. Supported: do_i2c do_spi do_ssh "
           "do_serial_hw do_serial_cons do_onewire do_hostname NAME do_boot_behaviour B1..B4 "
           "do_wifi_country XX do_change_locale LOCALE do_change_timezone ZONE do_configure_keyboard LAYOUT.",
           [Field("calls", "list", True, 'one call per line, e.g. "do_i2c 0" or "do_hostname mypi"')],
           scalar="calls", listform="calls", category="System", example='raspi_config: ["do_spi 0"]'),
    Action("recipe", "run another recipe", "Runs another recipe file (path relative to this one, or a "
           "bundled recipe's name) on the same image.",
           [Field("name", "str", True, "recipe file or bundled name"),
            Field("vars", "map", help="its variables, NAME=VALUE")],
           scalar="name", category="Control", example="recipe: {name: pitft, vars: {display: 28c}}"),
    Action("python_recipe", "run a built-in Python recipe", "Runs a built-in Python recipe (see "
           "`piimg apply --help`) with its options.",
           [Field("name", "choice", True, "recipe", choices=("gadget-net", "userdata")),
            Field("args", "map", help="options without the leading --, e.g. iface=usb0; true for flags")],
           category="Control", example="python_recipe: {name: gadget-net, args: {iface: usb0}}"),
    Action("live", "boot the image and run a script (slow; last resort)", "Boots the image in QEMU with "
           "a tiny init (not systemd) and runs the script as root. Only for things that must execute arm "
           "code: apt-get install, compiling, vendor installers. Scripts must be non-interactive.",
           [Field("script", "text", help="the shell script"),
            Field("script_file", help="local script file (relative to the recipe) instead"),
            Field("network", "bool", help="give the guest internet access (default true)", default=True),
            Field("timeout", "int", help="seconds before giving up (default 1800)"),
            Field("env", "map", help="environment variables, NAME=VALUE"),
            Field("template", "bool", help="replace {{vars}} in the script"),
            Field("allow_failure", "bool", help="a non-zero exit does not abort the recipe")],
           scalar="script", category="Live (emulation)",
           example="live: {script: \"apt-get install -y i2c-tools\"}"),
]}

# Not actions, but things the editor can insert.
IF_BLOCK_HELP = ("A conditional block: when: holds a condition; the steps under then: run if it is "
                 "true, those under else: if it is false. Conditions: exists: PATH, grep: {path, pattern}, "
                 "package: NAME, os_release: {KEY: value}, var: {name, equals | in | set}, and "
                 "not: / all: [..] / any: [..] to combine them.")
CONDITION_EXAMPLE = "grep: {path: \"{{boot}}/config.txt\", pattern: '^dtparam=spi=on'}"
COMMON_HELP = {
    "name": "a label for this step, shown in the run log",
    "when": "run this step only if the condition is true (YAML; e.g. exists: /etc/foo)",
    "ignore_errors": "carry on if this step fails",
}
VAR_HELP = ("Variables make a recipe a template: {{name}} in any string is replaced by the value given "
            "with --var name=VALUE (or the default). Per variable: default, required, choices, help.")
CATEGORIES = ("Control", "Files", "Edit files", "Boot & hardware", "System", "Live (emulation)")


def step_action(step):
    """The action name of a step mapping, 'if' for a then/else block, or None."""
    if not isinstance(step, dict):
        return None
    if "then" in step or "else" in step:
        return "if"
    keys = [k for k in step if k not in ("name", "when", "ignore_errors")]
    return keys[0] if len(keys) == 1 else None


def arg_to_values(action, arg):
    """A step's argument (scalar, list or mapping) -> {field: value} for a form."""
    a = ACTIONS[action]
    if isinstance(arg, dict):
        return dict(arg)
    if isinstance(arg, list) and a.listform:
        return {a.listform: list(arg)}
    if a.scalar:
        return {a.scalar: arg}
    return {}


def values_to_arg(action, values):
    """{field: value} from a form -> the step's argument, in the shortest form the
    recipe format allows (a bare scalar or list when only that field is set)."""
    a = ACTIONS[action]
    set_ = {k: v for k, v in values.items() if v not in (None, "", [], {})}
    if a.listform and list(set_) == [a.listform]:
        return list(set_[a.listform])
    if a.scalar and list(set_) == [a.scalar] and not isinstance(set_[a.scalar], (list, dict)):
        return set_[a.scalar]
    return set_


def new_step(action):
    """A blank step for the insert dialog."""
    if action == "if":
        return {"when": {"exists": "/etc/os-release"}, "then": [{"echo": "true"}], "else": []}
    a = ACTIONS[action]
    if a.scalar and a.fields[0].name == a.scalar and a.fields[0].type == "str":
        return {action: ""}
    if a.listform:
        return {action: []}
    return {action: {f.name: "" for f in a.fields if f.required}}
