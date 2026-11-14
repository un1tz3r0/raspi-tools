"""Running one piimg command as a child process, and reading what it says.

    python -m piimg --progress=json --json COMMAND ...

stderr carries progress events (progress.EVENT_PREFIX + JSON) and messages;
stdout carries the output: text lines (ANSI colours kept when PIIMG_COLOR=1)
and tables ({"table": ...} lines).

Stopping sends SIGINT to the child's process group (the CLI cleans up as on
Ctrl-C: qemu-img is stopped, libguestfs closes the image); stopping again
sends SIGTERM, then SIGKILL.

Root: a front end runs as the user. A command that must open a card the user
cannot open runs through pkexec (polkit asks for the password); see
needs_root() and piimg_command(root=True). The user cannot signal a root
process, so such a child runs under supervise.py and takes its stdin and stop
requests as JSON lines on its stdin (see stdin_orders and stop_order).
"""
import asyncio
import json
import os
import shutil
import signal
import sys

from rich.text import Text

from ..output import STATUS_STYLES
from ..progress import EVENT_PREFIX, fmt_duration

STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGKILL)
STOP_LABELS = ("Stop", "Terminate", "Kill")
# what the child needs from our environment when pkexec has cleared it
KEEP_ENV = ("PIIMG_COLOR", "PIIMG_PROGRESS", "LIBGUESTFS_DEBUG", "LIBGUESTFS_TRACE", "LIBGUESTFS_CACHEDIR",
            "LANG", "LC_ALL", "TERM", "EDITOR", "VISUAL")


class RootUnavailable(Exception):
    pass


def child_env(extra=None):
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    env.update(extra or {})
    return env


def piimg_command(argv, root=False, env=None, cwd=None, json_mode=True, supervise=True):
    """The command line that runs `piimg ARGV` as a child.

    root: run it through pkexec. pkexec clears the environment and starts in
    /, so the variables the child needs and the working directory are passed
    through `env`; it sets PKEXEC_UID, which piimg uses to find the user's
    settings and libguestfs appliance (config.invoking_user). supervise=False
    for a command in a terminal: the terminal stops it (Ctrl-C), not us."""
    base = [sys.executable, "-m", "piimg", *(["--progress=json", "--json"] if json_mode else []), *argv]
    if not supervised(root):
        return base
    pkexec, envbin = shutil.which("pkexec"), shutil.which("env") or "/usr/bin/env"
    if not pkexec:
        raise RootUnavailable("this needs root, and pkexec (polkit) is not installed: "
                              "run piimg from a terminal with sudo instead")
    env = env or os.environ
    keep = [f"{k}={env[k]}" for k in KEEP_ENV if k in env]
    return [pkexec, envbin, *(["-C", str(cwd)] if cwd else []), "PYTHONUNBUFFERED=1", *keep,
            *([sys.executable, "-m", "piimg.frontend.supervise"] if supervise else []), *base]


def supervised(root):
    """True if a child started with root=root runs under supervise.py (through pkexec)."""
    return bool(root) and os.geteuid() != 0


def stdin_orders(text):
    """What to write to a supervised child's stdin to pass it `text` (or nothing) and close it."""
    from .supervise import encode
    return (encode(data=text) if text else b"") + encode(close=True)


def stop_order(sig):
    from .supervise import encode
    return encode(signal=int(sig))


def needs_root(paths_and_modes):
    """True if any (path, write) pair is a disk this process cannot open."""
    from .target import can_open
    return any(p and not can_open(p, w) for p, w in paths_and_modes)


async def run_capture(argv, timeout=None, root=False):
    """Run a piimg command quietly; returns (rc, stdout lines, stderr lines without events)."""
    sup = supervised(root)
    proc = await asyncio.create_subprocess_exec(
        *piimg_command(argv, root=root), stdin=asyncio.subprocess.PIPE if sup else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=child_env())
    if sup:  # keep its stdin open: end of input tells the supervisor to stop the command
        proc.stdin.write(stdin_orders(None))
        await proc.stdin.drain()
    try:
        out, err = await asyncio.wait_for(asyncio.gather(proc.stdout.read(), proc.stderr.read()), timeout)
        await proc.wait()
    except BaseException:  # cancelled or timed out: don't leave it holding the image open
        if proc.returncode is None:
            if sup:
                proc.stdin.close()  # the supervisor stops the root child
            else:
                proc.kill()
            await proc.wait()
        raise
    finally:
        if sup and not proc.stdin.is_closing():
            proc.stdin.close()
    errs = [l for l in err.decode(errors="replace").splitlines() if not l.startswith(EVENT_PREFIX)]
    return proc.returncode, out.decode(errors="replace").splitlines(), errs


def parse_table(line):
    """A stdout line -> the table it carries ({title, columns, rows, footer}) or None."""
    if not line.startswith('{"table"'):
        return None
    try:
        return json.loads(line)["table"]
    except (ValueError, KeyError):
        return None


def first_table(lines):
    return next((t for t in map(parse_table, lines) if t is not None), None)


def parse_event(line):
    """A stderr line -> its progress event (dict with "event": begin/update/end) or None."""
    if not line.startswith(EVENT_PREFIX):
        return None
    try:
        ev = json.loads(line[len(EVENT_PREFIX):])
    except ValueError:
        return None
    return ev if isinstance(ev, dict) and "event" in ev else None


def is_event(line):
    return line.startswith(EVENT_PREFIX)


def message_style(line):
    """How to show a stderr message: errors bold red, the rest yellow."""
    return "bold red" if line.startswith("piimg: ") and "note:" not in line else "yellow"


def status_style(column, value):
    return STATUS_STYLES.get(value.lower(), "") if column == "status" else ""


def task_detail(ev):
    """An event's status parts (a list of [text, rich style]) as one rich Text."""
    detail = Text()
    for i, (text, style) in enumerate(ev.get("parts") or []):
        if i:
            detail.append(" · ", "dim")
        detail.append(text, style)
    return detail


def end_line(ev):
    """What to log when a task ends: a ✔/✘ line for slow or failed ones, else None."""
    if ev.get("elapsed", 0) < 1.5 and ev.get("ok"):
        return None
    ok = ev.get("ok")
    line = Text("✔ " if ok else "✘ ", "green" if ok else "red")
    line.append(ev.get("description", ""))
    if ev.get("result"):
        line.append(f" · {ev['result']}", "dim")
    line.append(f" · {fmt_duration(ev.get('elapsed', 0))}"
                + ("" if ok else (" interrupted" if ev.get("interrupted") else " failed")),
                "dim" if ok else "red")
    return line


def finished(rc, took):
    """(message, kind) for a finished run: kind is ok, stopped or failed."""
    took = fmt_duration(took)
    if rc == 0:
        return f"finished in {took}", "ok"
    if rc in (130, -signal.SIGINT):
        return f"stopped after {took}", "stopped"
    if rc == 126:  # pkexec: not authorized / dialog dismissed
        return f"not authorized (exit status {rc})", "failed"
    return f"failed after {took} (exit status {rc})", "failed"


def last_error(lines):
    """The last `piimg: ...` error among stderr lines, without the prefix."""
    errs = [l for l in lines if l.startswith("piimg: ")]
    return errs[-1].removeprefix("piimg: ") if errs else None
