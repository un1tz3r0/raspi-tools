"""Last-resort "live" steps: boot the image in QEMU, run a script, shut down.

Booting the real system would run cloud-init's first-boot pass and use up
the user-data we wrote, so we do NOT boot systemd. The kernel is started with
init=/usr/lib/piimg/live-init, a tiny shell script that mounts the basics,
optionally brings up QEMU user-mode networking, runs the injected script, and
reports completion in two independent ways:

  1. a console line  `PIIMG-DONE rc=N`   (seen live on the serial port)
  2. a marker file   /var/lib/piimg/done  (read back from the filesystem after
     the VM has stopped)

Everything injected is removed again by the host afterwards, even on failure.
Only aarch64 (kernel8.img, QEMU raspi3b) images are supported so far.
"""
import asyncio
import os
import re
import shlex
import shutil
import sys
import tempfile
import time
from pathlib import Path

from . import progress
from .guest import Image, PiImgError

LIVE_DIR = "/usr/lib/piimg"
STATE_DIR = "/var/lib/piimg"
DONE_RE = re.compile(rb"PIIMG-DONE rc=(-?\d+)")
# Console markers that tell the status line what the guest is doing now.
PHASES = ((re.compile(r"PIIMG-RUN\b"), "running script"),
          (re.compile(r"PIIMG-DONE\b"), "script finished, guest shutting down"))

LIVE_INIT = r"""#!/bin/sh
# Injected by piimg; removed again after the run.
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin HOME=/root TERM=${TERM:-vt100}
mount -t proc proc /proc; mount -t sysfs sys /sys
mount -t devtmpfs dev /dev 2>/dev/null
mkdir -p /dev/pts; mount -t devpts devpts /dev/pts
mount -t tmpfs tmpfs /run; mount -t tmpfs tmpfs /tmp
mount -o remount,rw /
mount /boot/firmware 2>/dev/null || mount /boot 2>/dev/null
echo 1 > /proc/sys/kernel/sysrq
hostname piimg-build

. /usr/lib/piimg/live.env
# the emulated Pi has no RTC and boots in 1970, which makes apt reject every signature
[ -n "$PIIMG_NOW" ] && date -s "@$PIIMG_NOW" >/dev/null

if [ "$PIIMG_NETWORK" = 1 ]; then
    modprobe -a usbnet cdc_ether rndis_host smsc95xx 2>/dev/null
    n=0; IF=
    while [ $n -lt 20 ] && [ -z "$IF" ]; do
        IF=$(ls /sys/class/net | grep -v '^lo$' | head -n1); [ -z "$IF" ] && sleep 1; n=$((n+1))
    done
    if [ -n "$IF" ]; then
        ip link set "$IF" up
        ip addr add 10.0.2.15/24 dev "$IF"
        ip route add default via 10.0.2.2
        echo "nameserver 10.0.2.3" > /tmp/resolv.conf
        mount --bind /tmp/resolv.conf /etc/resolv.conf
        echo "PIIMG-NET up via $IF"
    else
        echo "PIIMG-NET down (no network interface appeared)"
    fi
fi

echo "PIIMG-RUN"
/usr/lib/piimg/live-script
rc=$?

mkdir -p /var/lib/piimg
echo "$rc" > /var/lib/piimg/done
sync
echo "PIIMG-DONE rc=$rc"
mount -o remount,ro / 2>/dev/null
sync
echo b > /proc/sysrq-trigger
sleep 60
"""


def _extract_boot_files(img, tmp):
    boot = img.boot
    if not img.exists(f"{boot}/kernel8.img"):
        raise PiImgError("Live steps currently support 64-bit images only (no kernel8.img found).")
    dtb = "bcm2710-rpi-3-b-plus.dtb"
    kernel_local, dtb_local = Path(tmp) / "kernel8.img", Path(tmp) / dtb
    img.g.download(f"{boot}/kernel8.img", str(kernel_local))
    img.g.download(f"{boot}/{dtb}", str(dtb_local))
    return kernel_local, dtb_local


def _inject(img, script, env, network):
    _cleanup(img)  # leftovers from an earlier run that was killed
    img.g.mkdir_p(LIVE_DIR)
    img.write(f"{LIVE_DIR}/live-init", LIVE_INIT, mode=0o755)
    img.write(f"{LIVE_DIR}/live-script", script if script.startswith("#!") else "#!/bin/bash\n" + script,
              mode=0o755)
    lines = [f"export PIIMG_NETWORK={1 if network else 0}", f"export PIIMG_NOW={int(time.time())}"]
    lines += [f"export {k}={shlex.quote(str(v))}" for k, v in (env or {}).items()]
    img.write(f"{LIVE_DIR}/live.env", "\n".join(lines) + "\n")
    if img.g.is_dir(STATE_DIR):
        img.g.rm_rf(STATE_DIR)


def _cleanup(img):
    for d in (LIVE_DIR, STATE_DIR):
        if img.g.is_dir(d):
            img.g.rm_rf(d)


def qemu_command(image_path, image_format, kernel, dtb, network=True, memory="1G"):
    cmd = ["qemu-system-aarch64", "-M", "raspi3b", "-m", memory, "-display", "none",
           "-no-reboot", "-monitor", "none",
           "-kernel", str(kernel), "-dtb", str(dtb),
           "-drive", f"file={image_path},format={image_format},if=sd",
           "-append", "console=ttyAMA1,115200 root=/dev/mmcblk0p2 "
                      "rootfstype=ext4 rootwait rw panic=-1 loglevel=3 "
                      f"init={LIVE_DIR}/live-init"]
    if network:
        cmd += ["-netdev", "user,id=n0", "-device", "usb-net,netdev=n0"]
    # raspi3b: the first -serial is the PL011 (Linux names it ttyAMA1 here). The mini UART
    # cannot register under QEMU (no clock model), so it must not be the console.
    cmd += ["-serial", "stdio", "-serial", "null"]
    return cmd


class _Lines:
    """Reassemble console bytes into the lines a terminal would have shown."""
    MAX = 1 << 16  # a guest that never prints a newline must not grow this without bound

    def __init__(self):
        self.buf = b""

    def feed(self, chunk):
        *lines, self.buf = (self.buf + chunk).split(b"\n")
        if len(self.buf) > self.MAX:
            lines.append(self.buf)
            self.buf = b""
        return [self._text(l) for l in lines]

    def flush(self):
        lines, self.buf = ([self._text(self.buf)] if self.buf else []), b""
        return lines

    @staticmethod
    def _text(raw):
        # "\r\n" line ends, and progress meters that redraw with a bare "\r": keep what was left on screen
        return raw.decode("utf-8", "replace").rstrip("\r").rsplit("\r", 1)[-1]


async def _watch(proc, log, timeout, task=None, on_line=None):
    """Stream the console to `log` (raw bytes) and `on_line` (text lines); return the rc from
    the PIIMG-DONE line (or None). `task`, if given, learns the guest is alive and which phase it is in."""
    seen_rc = None
    buf = b""
    lines = _Lines()
    if task:
        task.set_deadline(timeout)

    def handle(line):
        if on_line:
            on_line(line)
        if task:
            for pattern, note in PHASES:
                if pattern.match(line):
                    task.update(note=note)

    async def pump():
        nonlocal seen_rc, buf
        while True:
            chunk = await proc.stdout.read(4096)
            if not chunk:
                for line in lines.flush():
                    handle(line)
                return
            log(chunk)
            if task:
                task.pulse()
            for line in lines.feed(chunk):
                handle(line)
            buf = (buf + chunk)[-4096:]
            m = DONE_RE.search(buf)
            if m and seen_rc is None:
                seen_rc = int(m.group(1))

    try:
        await asyncio.wait_for(pump(), timeout)
    except asyncio.TimeoutError:
        raise PiImgError(f"Live step timed out after {timeout}s")
    return seen_rc


async def run_live(image_path, image_format, script, *, env=None, network=True, timeout=1800,
                   quiet=False):
    """Run `script` inside the booted image. Returns the script's exit code.

    The caller must have closed its libguestfs handle: only one writer may
    have the image open at a time.
    """
    if not shutil.which("qemu-system-aarch64"):
        raise PiImgError("qemu-system-aarch64 is not installed (sudo apt install qemu-system-arm)")
    # With a live status area on a terminal, console lines are printed through it so they
    # scroll above the bar. In every other case (redirected, CI, plain mode) the guest's
    # bytes are written to stdout unchanged, as they always were.
    line_mode = progress.is_fancy() and sys.stdout.isatty()
    out = sys.stdout.buffer

    def log(b):
        if not quiet and not line_mode:
            out.write(b.replace(b"\r\n", b"\n")); out.flush()

    def on_line(line):
        if line_mode and not quiet:
            progress.output(line)

    from .disks import maybe_claim
    # A card stays claimed (locked, automounts kept away) from injection to cleanup.
    with maybe_claim(image_path), tempfile.TemporaryDirectory(prefix="piimg-live-") as tmp:
        with Image(image_path) as img:
            kernel, dtb = _extract_boot_files(img, tmp)
            _inject(img, script, env, network)
        rc_console = None
        try:
            # How long this takes is up to the script, so there is no ETA: the status line shows
            # the phase, the time since the guest last printed anything, and when we give up.
            with progress.task("Running script in the QEMU guest", stall=(60, 300),
                               stall_label="no output") as t:
                t.update(note="booting guest")
                proc = await asyncio.create_subprocess_exec(
                    *qemu_command(image_path, image_format, kernel, dtb, network),
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE)
                try:
                    rc_console = await _watch(proc, log, timeout, t, on_line)
                    t.deadline = None
                    t.update(note="waiting for QEMU to exit")
                    try:
                        await asyncio.wait_for(proc.wait(), 30)  # guest reboots itself to stop
                    except asyncio.TimeoutError:
                        proc.terminate(); await proc.wait()
                finally:
                    if proc.returncode is None:
                        proc.kill(); await proc.wait()
                err = (await proc.stderr.read()).decode(errors="replace").strip()
                if rc_console is not None:
                    t.result = f"script exited {rc_console}"
            err = "\n".join(l for l in err.splitlines()
                            if "usbnet: failed control transaction" not in l)  # benign QEMU/dwc_otg noise
            if err:
                print(f"[qemu] {err}", file=sys.stderr)
        finally:
            # Read the marker file, then always remove everything we injected
            with Image(image_path) as img:
                marker = None
                if img.exists(f"{STATE_DIR}/done"):
                    marker = int(img.read_text(f"{STATE_DIR}/done").strip())
                _cleanup(img)

    if marker is None:
        raise PiImgError("The live script never reported completion (no marker file). "
                         "The guest may have crashed; re-run with output enabled to see the console.")
    if rc_console is not None and rc_console != marker:
        raise PiImgError(f"Completion signals disagree: console rc={rc_console}, marker file rc={marker}")
    return marker



def clean(image_path):
    """Remove anything a live run left behind (e.g. after the host process was
    killed mid-run). Returns True if there was something to remove."""
    with Image(image_path) as img:
        found = any(img.g.is_dir(d) for d in (LIVE_DIR, STATE_DIR))
        _cleanup(img)
    return found
