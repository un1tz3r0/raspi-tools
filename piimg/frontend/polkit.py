"""Opt-in: let polkit remember your password for a few minutes for piimg's root commands.

    sudo .venv/bin/piimg gui --install-polkit       (and --uninstall-polkit)

Without it, a front end runs a root command as `pkexec env ... python -m
piimg ...`, and polkit asks for the password every time: pkexec's own action
is auth_admin, and it covers any program, so it cannot be relaxed for piimg.
Installing writes two root-owned files:

- LAUNCHER, a small script with this venv's python written into it, which
  runs only `python -m piimg ARGS` (optionally under the supervisor) with a
  working directory and the variables in proc.KEEP_ENV;
- POLICY, a polkit action for LAUNCHER alone, with auth_admin_keep: after
  you type the password, polkit does not ask again for about five minutes.

The trade-off is the same as sudo's timestamp: for those minutes, a program
you run could start piimg as root without asking. piimg's files belong to
you, so that is no weaker than the password you just typed for it, but it is
why this is opt-in.
"""
import os
import sys
from pathlib import Path

from ..errors import PiImgError

ACTION = "io.github.un1tz3r0.piimg.run-as-root"
LAUNCHER = Path("/usr/local/libexec/piimg-root")
POLICY = Path("/usr/share/polkit-1/actions") / f"{ACTION}.policy"
MARK = "# python: "


def launcher_text(python):
    from .proc import KEEP_ENV
    return f'''#!{python} -I
# Installed by `piimg gui --install-polkit`; pkexec runs it (polkit action {ACTION}).
# It runs only piimg, from this python:
#   piimg-root [--cwd DIR] [--supervise] [NAME=VALUE ...] -- PIIMG-ARGS...
{MARK}{python}
import os
import sys

PY = {python!r}
KEEP = {tuple(KEEP_ENV)!r}
args = sys.argv[1:]
supervise = False
while args and args[0] != "--":
    a = args.pop(0)
    if a == "--supervise":
        supervise = True
    elif a == "--cwd" and args:
        os.chdir(args.pop(0))
    elif a.partition("=")[0] in KEEP and "=" in a:
        name, _, value = a.partition("=")
        os.environ[name] = value
    else:
        sys.exit(f"piimg-root: unexpected argument {{a!r}}")
if not args:
    sys.exit("usage: piimg-root [--cwd DIR] [--supervise] [NAME=VALUE ...] -- PIIMG-ARGS...")
cmd = [PY, "-I", "-u", "-m", "piimg", *args[1:]]
if supervise:
    cmd = [PY, "-I", "-u", "-m", "piimg.frontend.supervise", *cmd]
os.execv(PY, cmd)
'''


def policy_text(launcher=LAUNCHER):
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE policyconfig PUBLIC "-//freedesktop//DTD PolicyKit Policy Configuration 1.0//EN"
 "http://www.freedesktop.org/standards/PolicyKit/1/policyconfig.dtd">
<!-- Installed by piimg (piimg gui, install-polkit option); its uninstall-polkit option removes it.
     No double hyphens in here: XML comments cannot hold them, and polkitd ignores a file it cannot parse. -->
<policyconfig>
  <vendor>piimg</vendor>
  <action id="{ACTION}">
    <description>Run a piimg command on a card</description>
    <message>piimg needs your password to open a card</message>
    <defaults>
      <allow_any>auth_admin</allow_any>
      <allow_inactive>auth_admin</allow_inactive>
      <allow_active>auth_admin_keep</allow_active>
    </defaults>
    <annotate key="org.freedesktop.policykit.exec.path">{launcher}</annotate>
  </action>
</policyconfig>
'''


def launcher_python(launcher=LAUNCHER):
    """The python an installed launcher runs, or None."""
    try:
        for line in Path(launcher).read_text().splitlines()[:8]:
            if line.startswith(MARK):
                return line[len(MARK):].strip()
    except OSError:
        pass
    return None


def installed(launcher=LAUNCHER, policy=POLICY, python=None):
    """True if the launcher and its policy are installed for this venv's python.

    Compared by directory: a venv's python is a symlink to the system one, so
    resolving it would match every venv."""
    py = launcher_python(launcher)
    return (py is not None and Path(policy).exists()
            and Path(py).parent == Path(python or sys.executable).parent)


def install(remove=False, launcher=LAUNCHER, policy=POLICY, python=None, check_root=True):
    if check_root and os.geteuid() != 0:
        verb = "--uninstall-polkit" if remove else "--install-polkit"
        raise PiImgError(f"this writes system files, so it needs root: sudo {sys.executable} -m piimg gui {verb}")
    launcher, policy = Path(launcher), Path(policy)
    if remove:
        gone = [str(p) for p in (policy, launcher) if p.exists()]
        for p in (policy, launcher):
            p.unlink(missing_ok=True)
        return f"removed {', '.join(gone)}" if gone else "nothing to remove"
    python = python or sys.executable
    launcher.parent.mkdir(parents=True, exist_ok=True)
    tmp = launcher.with_name(launcher.name + ".new")
    tmp.write_text(launcher_text(python))
    tmp.chmod(0o755)
    tmp.replace(launcher)
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text(policy_text(launcher))
    policy.chmod(0o644)
    return (f"wrote {launcher} (runs piimg with {python}) and {policy}; polkit now remembers your "
            f"password for about five minutes after piimg asks for it")
