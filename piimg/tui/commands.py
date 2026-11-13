"""From the CLI's argparse definitions to TUI forms, and back to a command line.

The TUI never calls command code directly: it runs `python -m piimg ...` as a
child process (see run.py), so a command behaves exactly as on the command
line, can be stopped with SIGINT, and cannot take the TUI down with it.
"""
import argparse
from dataclasses import dataclass

from ..cli import GROUPS, Meta, build_parser, iter_commands
from .widgets import FieldDef

SECRET_HINTS = ("password",)


class ArgError(Exception):
    pass


class RaisingParser(argparse.ArgumentParser):
    """An ArgumentParser that raises instead of printing usage and exiting."""

    def error(self, message):
        raise ArgError(message)

    def exit(self, status=0, message=None):
        raise ArgError(message or f"exit {status}")


@dataclass
class Command:
    words: tuple           # ('passwd',) or ('apply', 'userdata')
    parser: argparse.ArgumentParser
    meta: Meta

    @property
    def name(self):
        return " ".join(self.words)

    @property
    def help(self):
        return self.parser.description or ""

    def summary(self):
        # the help= given to add_parser lives on the parent's choices action
        return getattr(self.parser, "_piimg_summary", "") or self.help

    def usage(self):
        return self.parser.format_usage().strip()

    def actions(self):
        return [a for a in self.parser._actions
                if not isinstance(a, (argparse._HelpAction, argparse._SubParsersAction))]

    def field_actions(self):
        """The actions the form shows: not --help, not the target, not tui=False."""
        return [a for a in self.actions()
                if getattr(a, "tui", True) and a.dest != self.meta.target]

    def has_form(self):
        return bool(self.field_actions())


def load_commands():
    cmds = []
    parser = build_parser()
    # remember each subcommand's one-line help (stored on the parent's _ChoicesPseudoAction)
    def summaries(p):
        for a in p._actions:
            if isinstance(a, argparse._SubParsersAction):
                for ca in a._choices_actions:
                    sp = a.choices[ca.dest]
                    sp._piimg_summary = ca.help
                    summaries(sp)
    summaries(parser)
    for words, sp in iter_commands(parser):
        meta = getattr(sp, "piimg", None) or Meta()
        if meta.tui and words[0] not in ("tui", "recipe-edit"):
            cmds.append(Command(words, sp, meta))
    order = {g: i for i, g in enumerate(GROUPS)}
    cmds.sort(key=lambda c: order.get(c.meta.group, 99))
    return cmds


def _label(a):
    if a.option_strings:
        return max(a.option_strings, key=len)
    return a.dest


def fields_for(cmd, values=None):
    """FieldDefs for a command's form. `values` overrides defaults (dest -> value)."""
    out = []
    values = values or {}
    for a in cmd.field_actions():
        kind = getattr(a, "kind", None)
        required = (not a.option_strings and a.nargs not in ("?", "*")) or bool(a.required)
        help_ = (a.help or "").replace("%(default)s", str(a.default))
        default = None if a.default in (None, argparse.SUPPRESS) else a.default
        if isinstance(a, (argparse._StoreTrueAction, argparse._StoreFalseAction)):
            ftype, default = "bool", bool(default)
        elif a.choices:
            ftype = "choice"
        elif kind == "secret" or (any(h in a.dest for h in SECRET_HINTS) and "hash" not in a.dest
                                  and "stdin" not in a.dest):
            ftype = "secret"
        elif isinstance(a, argparse._AppendAction) or a.nargs in ("*", "+") or kind == "lines":
            ftype, default = "lines", list(default or [])
        elif a.type is int:
            ftype = "int"
        else:
            ftype = "str"
        if kind == "vars":
            ftype = "vars"
        choices = tuple(a.choices or ())
        if kind in ("catalog", "localimage"):
            try:
                if kind == "catalog":
                    from ..catalog import cached_catalog_slugs
                    suggestions = tuple(cached_catalog_slugs())
                else:
                    from ..catalog import _local_files
                    from ..config import images_dir
                    suggestions = tuple(p.name for p in _local_files(images_dir()))
            except Exception:  # noqa: BLE001
                suggestions = ()
        else:
            suggestions = ()
        if kind == "device":
            from ..disks import list_disks
            try:
                choices = tuple(d.path for d in list_disks() if not d.why_not())
            except Exception:  # noqa: BLE001 - no lsblk: free text
                choices = ()
            ftype = "choice" if choices else "str"
        placeholder = {"path": "path inside the image, e.g. /etc/hostname", "file": "local file",
                       "newfile": "new local file", "dir": "local directory", "device": "/dev/sdX",
                       "size": "e.g. +2G or 8G (empty: fill the disk)", "unit": "e.g. ssh.service",
                       "user": "account name",
                       "catalog": "e.g. trixie-arm64-lite (one per line)",
                       "localimage": "file name in the images folder (one per line)"}.get(kind, a.metavar if isinstance(a.metavar, str) else "")
        f = FieldDef(key=a.dest, label=_label(a), type=ftype, required=required, help=help_,
                     choices=choices, value=values.get(a.dest, default),
                     placeholder=placeholder or "", suggestions=suggestions)
        f.data = {"action": a, "kind": kind}
        if ftype == "choice" and default is not None:
            f.placeholder = f"(default: {default})"
        out.append(f)
    return out


def build_argv(cmd, values, target=None):
    """Form values (dest -> value) -> (argv after 'piimg', stdin text or None).
    Options first as --opt=value, then '--' and the positionals."""
    opts, pos, stdin = [], [], None
    for a in cmd.actions():
        if a.dest == cmd.meta.target and target is not None and not a.option_strings:
            pos.append(str(target))
            continue
        if not getattr(a, "tui", True) or a.dest not in values:
            continue
        v = values[a.dest]
        if not a.option_strings:
            if isinstance(v, list):
                pos += [str(x) for x in v]
            elif v not in (None, ""):
                pos.append(str(v))
            continue
        opt = _label(a)
        if isinstance(a, argparse._StoreTrueAction):
            if v:
                opts.append(opt)
        elif isinstance(a, argparse._StoreFalseAction):
            if not v:
                opts.append(opt)
        elif isinstance(v, list):
            opts += [f"{opt}={x}" for x in v]
        elif isinstance(v, dict):
            opts += [f"{opt}={k}={x}" for k, x in v.items()]
        elif v not in (None, ""):
            if getattr(a, "stdin_flag", None):
                opts.append(a.stdin_flag)
                stdin = str(v) + "\n"
            elif a.default is not None and str(v) == str(a.default):
                continue  # unchanged default: keep the command line short
            else:
                opts.append(f"{opt}={v}")
    return list(cmd.words) + opts + (["--"] + pos if pos else []), stdin


def parse(argv):
    """Validate a command line the way the CLI would; returns the Namespace or
    raises ArgError with argparse's message."""
    return build_parser(RaisingParser).parse_args(argv)
