"""Recipe files: a YAML description of changes to make to an image.

See docs/RECIPE-FORMAT.md for the full reference. Shape:

    name: Example
    vars:
      rotation: {default: 90, choices: [0, 90, 180, 270]}
    steps:
      - mkdir: /opt/thing
      - write: {path: /opt/thing/hello, content: "hi\\n", mode: 0o755}
      - when: {grep: {path: "{{boot}}/config.txt", pattern: "^dtparam=spi=on"}}
        then: [{echo: "spi already on"}]
        else: [{raspi_config: ["do_spi 0"]}]
      - live: {script: "apt-get install -y foo", network: true}
"""
import asyncio
import os
import re
import stat
import sys
from pathlib import Path

import yaml

from .guest import Image, PiImgError, _image_format
from . import live as live_mod

VAR_RE = re.compile(r"\{\{\s*([A-Za-z_]\w*)\s*(?:\|([^}]*))?\}\}")


class RecipeError(PiImgError):
    pass


RASPI_CONFIG_OFFLINE = ("do_i2c", "do_spi", "do_ssh", "do_serial_hw", "do_serial_cons", "do_onewire",
                        "do_hostname", "do_boot_behaviour", "do_wifi_country", "do_change_locale",
                        "do_change_timezone", "do_configure_keyboard")


def bundled_recipes_dir():
    return Path(__file__).parent / "recipes"


def find_recipe(name_or_path):
    p = Path(name_or_path)
    if p.is_file():
        return p
    for ext in (".yaml", ".yml"):
        q = bundled_recipes_dir() / (str(name_or_path) + ext)
        if q.is_file():
            return q
    raise RecipeError(f"No recipe file or bundled recipe named {name_or_path!r}")


def parse_mode(m):
    """File mode. Ints are taken as given (YAML reads 0755 as octal); strings
    are octal ('0755', '755', '0o755'). Quote modes in YAML to be safe."""
    if isinstance(m, int):
        return m
    return int(str(m).replace("0o", "", 1), 8)


class Runner:
    def __init__(self, image_path, recipe_path, variables=None, dry_run=False, log=print):
        self.image_path = Path(image_path)
        self.recipe_path = Path(recipe_path)
        self.dir = self.recipe_path.parent
        self.dry = dry_run
        self.log = log
        self.img = None
        doc = yaml.safe_load(self.recipe_path.read_text())
        if not isinstance(doc, dict) or "steps" not in doc:
            raise RecipeError(f"{recipe_path}: a recipe is a mapping with a 'steps' list")
        self.doc = doc
        self.vars = self._resolve_vars(doc.get("vars") or {}, variables or {})

    # ------------------------------------------------------------ variables

    def _resolve_vars(self, spec, given):
        out = {}
        unknown = set(given) - set(spec)
        if unknown:
            raise RecipeError(f"Unknown variable(s): {', '.join(sorted(unknown))} "
                              f"(recipe declares: {', '.join(spec) or 'none'})")
        for name, d in spec.items():
            d = d or {}
            if name in given:
                val = given[name]
            elif "default" in d:
                val = d["default"]
            elif d.get("required"):
                raise RecipeError(f"Variable {name!r} is required: pass --var {name}=...")
            else:
                continue
            if "choices" in d and str(val) not in [str(c) for c in d["choices"]]:
                raise RecipeError(f"Variable {name!r} must be one of {d['choices']}, not {val!r}")
            out[name] = val
        return out

    def sub(self, s):
        if not isinstance(s, str):
            return s

        def rep(m):
            name, fallback = m.group(1), m.group(2)
            if name == "boot":
                return self.img.boot
            if name in self.vars:
                return str(self.vars[name])
            if fallback is not None:
                return fallback
            raise RecipeError(f"Variable {{{{{name}}}}} is not set (declare it under 'vars:' and pass --var)")
        return VAR_RE.sub(rep, s)

    def _walk(self, obj):
        if isinstance(obj, str):
            return self.sub(obj)
        if isinstance(obj, list):
            return [self._walk(o) for o in obj]
        if isinstance(obj, dict):
            return {k: self._walk(v) for k, v in obj.items()}
        return obj

    # ----------------------------------------------------------------- run

    def run(self):
        fmt = _image_format(self.image_path)
        self._fmt = fmt
        self.img = Image(self.image_path, readonly=self.dry)
        self.img.open()
        try:
            self.log(f"== {self.doc.get('name', self.recipe_path.name)}"
                     f"{'  (dry run)' if self.dry else ''}")
            self.run_steps(self.doc["steps"], "steps")
        finally:
            if self.img is not None:
                self.img.close()
                self.img = None

    def run_steps(self, steps, where):
        if not isinstance(steps, list):
            raise RecipeError(f"{where}: expected a list of steps")
        for i, step in enumerate(steps, 1):
            self.run_step(step, f"{where}[{i}]")

    def run_step(self, step, where):
        if not isinstance(step, dict):
            raise RecipeError(f"{where}: a step is a mapping, got {step!r}")
        step = dict(step)
        label = step.pop("name", None)
        ignore = step.pop("ignore_errors", False)
        when = step.pop("when", None)
        branches = "then" in step or "else" in step
        then, els = step.pop("then", None), step.pop("else", None)

        if branches:
            if when is None:
                raise RecipeError(f"{where}: then/else need a 'when'")
            if step:
                raise RecipeError(f"{where}: unexpected keys next to then/else: {', '.join(step)}")
            taken = self.cond(when, where)
            self.log(f"  {where} {(label + ': ') if label else ''}when -> {'then' if taken else 'else'}")
            self.run_steps((then if taken else els) or [], f"{where}.{'then' if taken else 'else'}")
            return
        if when is not None and not self.cond(when, where):
            self.log(f"  {where} skipped (when false){': ' + label if label else ''}")
            return
        if len(step) != 1:
            raise RecipeError(f"{where}: a step has exactly one action, found: {', '.join(step) or 'none'}")
        (op, arg), = step.items()
        fn = getattr(self, f"op_{op}", None)
        if fn is None:
            known = sorted(n[3:] for n in dir(self) if n.startswith("op_"))
            raise RecipeError(f"{where}: unknown action {op!r}. Known: {', '.join(known)}")
        try:
            if op == "live":
                pass  # script text is substituted only with template: true
            elif op == "write" and isinstance(arg, dict) and not arg.get("template"):
                arg = {k: (v if k == "content" else self._walk(v)) for k, v in arg.items()}
            else:
                arg = self._walk(arg)
            fn(arg, where)
        except RecipeError:
            if not ignore:
                raise
        except PiImgError as e:
            if not ignore:
                raise RecipeError(f"{where} ({op}): {e}") from None
        except (KeyError, TypeError, ValueError) as e:
            raise RecipeError(f"{where} ({op}): bad arguments: {e!r}") from None

    # ----------------------------------------------------------- conditions

    def cond(self, c, where="when"):
        c = self._walk(c) if not isinstance(c, bool) else c
        if isinstance(c, bool):
            return c
        if not isinstance(c, dict) or len(c) != 1:
            raise RecipeError(f"{where}: a condition is a mapping with exactly one key, got {c!r}")
        (k, v), = c.items()
        img = self.img
        if k == "exists":
            return img.lexists(v)
        if k == "grep":
            p = v["path"]
            return img.exists(p) and re.search(v["pattern"], img.read_text(p), re.M) is not None
        if k == "not":
            return not self.cond(v, where)
        if k == "all":
            return all(self.cond(x, where) for x in v)
        if k == "any":
            return any(self.cond(x, where) for x in v)
        if k == "var":
            name = v["name"]
            if "set" in v:
                return (name in self.vars) == bool(v["set"])
            val = self.vars.get(name)
            if "equals" in v:
                return str(val) == str(v["equals"])
            if "in" in v:
                return str(val) in [str(x) for x in v["in"]]
            raise RecipeError(f"{where}: var condition needs set/equals/in")
        if k == "package":
            return img.package_installed(v)
        if k == "os_release":
            text = img.read_text("/etc/os-release")
            return all(re.search(rf'^{re.escape(a)}="?{re.escape(str(b))}"?$', text, re.M) for a, b in v.items())
        raise RecipeError(f"{where}: unknown condition {k!r} "
                          f"(exists, grep, not, all, any, var, package, os_release)")

    # -------------------------------------------------------------- actions

    def _mut(self, msg):
        self.log(f"  - {msg}")
        return not self.dry

    def _own(self, d):
        if "owner" in d:
            return self.img.lookup_owner(d["owner"])
        return 0, 0

    def op_echo(self, arg, where):
        self.log(f"  echo: {arg}")

    def op_fail(self, arg, where):
        raise RecipeError(f"{where}: {arg}")

    def op_mkdir(self, arg, where):
        d = {"path": arg} if isinstance(arg, str) else arg
        if self._mut(f"mkdir {d['path']}"):
            self.img.g.mkdir_p(d["path"])
            if "mode" in d:
                self.img.g.chmod(parse_mode(d["mode"]), d["path"])
            if "owner" in d:
                self.img.g.chown(*self._own(d), d["path"])

    def op_write(self, arg, where):
        path = arg["path"]
        mode = parse_mode(arg.get("mode", 0o644))
        if_exists = arg.get("if_exists", "overwrite")
        if "content_file" in arg:
            data = (self.dir / arg["content_file"]).read_bytes()
        else:
            data = arg["content"].encode() if isinstance(arg["content"], str) else bytes(arg["content"])
        if self.img.lexists(path):
            if if_exists == "skip":
                self.log(f"  - write {path}: exists, skipped"); return
            if if_exists == "error":
                raise RecipeError(f"{where}: {path} already exists")
        if self._mut(f"write {path} ({len(data)} bytes, {oct(mode)})"):
            uid, gid = self._own(arg)
            self.img.write(path, data, mode=mode, uid=uid, gid=gid)

    def op_copy(self, arg, where):
        src = self.dir / arg["src"]
        dest = arg["dest"]
        if src.is_file():
            if self._mut(f"copy {src.name} -> {dest}"):
                uid, gid = self._own(arg)
                mode = parse_mode(arg["mode"]) if "mode" in arg else stat.S_IMODE(src.stat().st_mode)
                self.img.write(dest, src.read_bytes(), mode=mode, uid=uid, gid=gid)
            return
        if not src.is_dir():
            raise RecipeError(f"{where}: {src} does not exist")
        for root, dirs, files in os.walk(src):
            rel = Path(root).relative_to(src)
            for name in sorted(files):
                f = Path(root) / name
                target = str(Path(dest) / rel / name)
                if f.is_symlink():
                    if self._mut(f"symlink {target} -> {os.readlink(f)}"):
                        self.img.symlink(os.readlink(f), target)
                elif self._mut(f"copy {f.relative_to(src)} -> {target}"):
                    uid, gid = self._own(arg)
                    self.img.write(target, f.read_bytes(), mode=stat.S_IMODE(f.stat().st_mode), uid=uid, gid=gid)

    def op_symlink(self, arg, where):
        if self._mut(f"symlink {arg['path']} -> {arg['target']}"):
            self.img.symlink(arg["target"], arg["path"])

    def op_remove(self, arg, where):
        path = arg if isinstance(arg, str) else arg["path"]
        if self._mut(f"remove {path}"):
            self.img.g.rm_rf(path)

    def op_chmod(self, arg, where):
        if self._mut(f"chmod {arg['mode']} {arg['path']}"):
            self.img.g.chmod(parse_mode(arg["mode"]), arg["path"])

    def op_chown(self, arg, where):
        if self._mut(f"chown {arg['owner']} {arg['path']}"):
            self.img.g.chown(*self.img.lookup_owner(arg["owner"]), arg["path"])

    def op_line(self, arg, where):
        """Ansible-style lineinfile. `match` regex: the (first) matching line is
        replaced by `line`; with state: absent, matching lines are removed."""
        path, state = arg["path"], arg.get("state", "present")
        line, match = arg.get("line"), arg.get("match")
        text = self.img.read_text(path) if self.img.exists(path) else ""
        lines = text.splitlines()
        if state == "absent":
            pat = re.compile(match) if match else None
            new = [l for l in lines if not ((pat and pat.search(l)) or (not pat and l == line))]
        else:
            new = list(lines)
            if match:
                pat = re.compile(match)
                idx = [i for i, l in enumerate(new) if pat.search(l)]
                if idx:
                    new[idx[0]] = line
                elif line not in new:
                    new.append(line)
            elif line not in new:
                new.append(line)
        if new == lines:
            self.log(f"  - line {path}: no change"); return
        if self._mut(f"line {path} ({state})"):
            self.img.write(path, "\n".join(new) + "\n")

    def op_sub(self, arg, where):
        if self._mut(f"sub {arg['path']} s/{arg['pattern']}/{arg['replace']}/"):
            n = self.img.sub(arg["path"], arg["pattern"], arg["replace"], count=arg.get("count", 0))
            if n == 0 and arg.get("must_match"):
                raise RecipeError(f"{where}: pattern {arg['pattern']!r} did not match in {arg['path']}")

    def op_config_txt(self, arg, where):
        section = arg.get("section", "all")
        for l in ([arg["add"]] if isinstance(arg.get("add"), str) else arg.get("add", [])):
            if self._mut(f"config.txt [{section}] += {l}"):
                self.img.config_txt_add(l, section)
        for l in ([arg["remove"]] if isinstance(arg.get("remove"), str) else arg.get("remove", [])):
            if self._mut(f"config.txt -= {l}"):
                lines = self.img.read_text(self.img.config_txt).splitlines()
                self.img.write(self.img.config_txt, "\n".join(x for x in lines if x.strip() != l) + "\n")
        for k, v in (arg.get("set") or {}).items():
            if self._mut(f"config.txt {k}={v}"):
                self.img.config_var(k, v)

    def op_cmdline(self, arg, where):
        for p in ([arg["add"]] if isinstance(arg.get("add"), str) else arg.get("add", [])):
            if self._mut(f"cmdline += {p}"):
                self.img.cmdline_add(p)
        for p in ([arg["remove"]] if isinstance(arg.get("remove"), str) else arg.get("remove", [])):
            if self._mut(f"cmdline -= {p}"):
                self.img.cmdline_remove(p)

    def op_modules(self, arg, where):
        """Load kernel modules at boot (modules-load.d)."""
        mods = [arg] if isinstance(arg, str) else arg["add"] if isinstance(arg, dict) else arg
        for m in mods:
            if self._mut(f"load module at boot: {m}"):
                self.img.append_line_once("/etc/modules-load.d/piimg.conf", m)

    def op_systemd(self, arg, where):
        for key, fn in (("enable", self.img.enable_unit), ("disable", self.img.disable_unit),
                        ("mask", self.img.mask_unit)):
            units = arg.get(key, [])
            for u in ([units] if isinstance(units, str) else units):
                if self._mut(f"systemctl {key} {u}"):
                    fn(u)

    def op_recipe(self, arg, where):
        """Run another recipe file (bundled name or path relative to this one)."""
        name = arg if isinstance(arg, str) else arg["name"]
        ref = self.dir / name
        sub = Runner(self.image_path, ref if ref.is_file() else find_recipe(name),
                     (arg.get("vars") if isinstance(arg, dict) else None) or {}, self.dry, self.log)
        sub.img = self.img
        sub._fmt = self._fmt
        sub.run_steps(sub.doc["steps"], f"{name}.steps")

    def op_python_recipe(self, arg, where):
        """Run a built-in Python recipe (see `piimg apply --help`), e.g. gadget-net."""
        import argparse
        from .recipes import REGISTRY
        r = REGISTRY[arg["name"]]
        p = argparse.ArgumentParser()
        r.add_arguments(p)
        ns = p.parse_args([f"--{k.replace('_', '-')}" + ("" if v is True else f"={v}")
                           for k, v in (arg.get("args") or {}).items() if v is not False])
        if self._mut(f"python recipe {arg['name']}"):
            r.apply(self.img, ns)

    def op_raspi_config(self, arg, where):
        """Offline equivalents of `raspi-config nonint do_X VALUE` (0 = enable)."""
        for item in ([arg] if isinstance(arg, str) else arg):
            words = str(item).split()
            if words and words[0] == "nonint":
                words = words[1:]
            fn, val = words[0], (words[1] if len(words) > 1 else None)
            if self._mut(f"raspi-config {fn} {val}"):
                self._raspi_config(fn, val, where)

    def _raspi_config(self, fn, val, where):
        img = self.img
        on = val == "0"
        flag = "on" if on else "off"
        if fn == "do_i2c":
            img.config_var("dtparam=i2c_arm", flag)
            self._unblacklist("i2c[-_]bcm2708")
            if on:
                if img.exists("/etc/modules"):
                    img.sub("/etc/modules", r"^#[ \t]*(i2c[-_]dev)", r"\1")
                img.append_line_once("/etc/modules", "i2c-dev")
        elif fn == "do_spi":
            img.config_var("dtparam=spi", flag)
            self._unblacklist("spi[-_]bcm2708")
        elif fn == "do_ssh":
            if on:
                img.enable_unit("ssh.service")
            else:
                img.disable_unit("ssh.service")
        elif fn == "do_serial_hw":
            img.config_var("enable_uart", "1" if on else "0")
        elif fn == "do_serial_cons":
            if on:
                words = img.read_text(img.cmdline_txt).split()
                if "console=ttyAMA0" in " ".join(words):
                    img.sub(img.cmdline_txt, r"console=ttyAMA0", "console=serial0")
                elif not any(w.startswith(("console=serial0", "console=ttyAMA0")) for w in words):
                    img.sub(img.cmdline_txt, r"root=", "console=serial0,115200 root=", count=1)
            else:
                img.sub(img.cmdline_txt, r"console=ttyAMA0,[0-9]+ ?", "")
                img.sub(img.cmdline_txt, r"console=serial0,[0-9]+ ?", "")
        elif fn == "do_onewire":
            if on:
                img.sub(img.config_txt, r"^#dtoverlay=w1-gpio", "dtoverlay=w1-gpio")
                if not re.search(r"^dtoverlay=w1-gpio", img.read_text(img.config_txt), re.M):
                    img.config_txt_add("dtoverlay=w1-gpio")
            else:
                img.sub(img.config_txt, r"^dtoverlay=w1-gpio", "#dtoverlay=w1-gpio")
        elif fn == "do_hostname":
            from .system import set_hostname
            set_hostname(img, val)
        elif fn == "do_boot_behaviour":
            from .system import set_boot_mode
            modes = {"B1": "console", "B2": "console-autologin", "B3": "desktop", "B4": "desktop-autologin"}
            if (val or "")[:2] not in modes:
                raise RecipeError(f"{where}: do_boot_behaviour takes B1, B2, B3 or B4")
            set_boot_mode(img, modes[val[:2]], self.vars.get("autologin_user"))
        elif fn == "do_wifi_country":
            from .system import set_wifi_country
            set_wifi_country(img, val)
        elif fn in ("do_change_locale", "do_change_locale_rc_gui"):
            from .system import schedule_locale_gen, set_locale
            if set_locale(img, val):
                schedule_locale_gen(img)
        elif fn in ("do_change_timezone", "do_change_timezone_rc_gui"):
            from .system import set_timezone
            set_timezone(img, val)
        elif fn == "do_configure_keyboard":
            from .system import set_keyboard
            set_keyboard(img, val)
        else:
            raise RecipeError(
                f"{where}: raspi-config {fn} has no offline translation yet "
                f"(supported: {' '.join(RASPI_CONFIG_OFFLINE)}). "
                f"Run it in a live step instead: live: {{script: 'raspi-config nonint {fn} ...'}}")

    def _unblacklist(self, mod_regex):
        bl = "/etc/modprobe.d/raspi-blacklist.conf"
        if self.img.exists(bl):
            self.img.sub(bl, rf"^(blacklist[ \t]*{mod_regex})", r"#\1")

    def op_live(self, arg, where):
        """Boot the image and run a script as root. Last resort: see DESIGN.md."""
        d = {"script": arg} if isinstance(arg, str) else dict(arg)
        script = (self.dir / d["script_file"]).read_text() if "script_file" in d else d["script"]
        if d.get("template"):
            script = self.sub(script)
        env = {k: self.sub(str(v)) for k, v in (d.get("env") or {}).items()}
        if self._mut(f"live: boot and run script ({len(script.splitlines())} lines)"):
            self.img.close()
            try:
                rc = asyncio.run(live_mod.run_live(
                    self.image_path, self._fmt, script, env=env,
                    network=d.get("network", True), timeout=d.get("timeout", 1800)))
            finally:
                self.img.open()
                self.img.__dict__.pop("_dpkg_status", None)
            if rc != 0 and not d.get("allow_failure"):
                raise RecipeError(f"{where}: live script exited with status {rc}")
            self.log(f"  live script finished, rc={rc}")
