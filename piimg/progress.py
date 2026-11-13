"""Progress reporting for slow steps (unpacking, booting libguestfs, QEMU runs).

Library code wraps a slow step like this and reports news when it has some:

    with progress.task("Unpacking foo.img.xz", total=size, as_bytes=True) as t:
        ...
        t.update(done_so_far)

How that looks is decided once, by the CLI, via session():

  terminal       a live status area under the normal output: spinner or bar,
                 elapsed time, ETA, and how long the step has shown no sign of
                 life. Steps that finish quickly never show up. Needs `rich`.
  anything else  (pipe, file, CI, TERM=dumb, no rich installed) plain lines on
                 stderr: one when a step has run a few seconds, a heartbeat
                 every 30 s, and a closing line. No escape codes.
  json           (--progress=json, for the TUI) one JSON event per line on
                 stderr, each starting with EVENT_PREFIX: begin, update (at
                 most every 0.25 s while something changes, and once a second
                 for the idle clock) and end.
  no session     silent, so importing piimg as a library prints nothing.

Status goes to stderr. stdout is for data (`piimg cat`, the path `unpack`
prints) and is never written to by this module, except by output() when stdout
and stderr are the same terminal.
"""
import collections
import itertools
import json
import os
import sys
import threading
import time
from contextlib import contextmanager

try:
    from rich.console import Console
    from rich.live import Live
    from rich.progress_bar import ProgressBar
    from rich.spinner import Spinner
    from rich.table import Table
    from rich.text import Text
except ImportError:  # python3-rich not installed: plain mode only
    Console = None

RATE_WINDOW = 10.0  # seconds of history behind the speed and ETA estimates


def fmt_duration(seconds):
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def fmt_size(n):
    n = float(n)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if abs(n) < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} TiB"


# ------------------------------------------------------------------- the task

class Task:
    """One slow step. Make it with task(); use it as a context manager."""

    def __init__(self, reporter, description, *, total=None, as_bytes=False,
                 stall=None, stall_label="no progress"):
        self.reporter = reporter
        self.description = description
        self.total = total              # None: unknown, show a spinner instead of a bar
        self.as_bytes = as_bytes        # format completed/total as sizes and show a speed
        self.stall = stall              # (warn, critical) seconds without activity, or None
        self.stall_label = stall_label  # "no progress for 0:12", "no output for 0:12"
        self.completed = 0
        self.note = ""                  # what it is doing right now ("booting guest")
        self.result = ""                # shown on the closing line ("6.4 GiB written")
        self.deadline = None            # monotonic time at which the step gives up, if it does
        self.started = self.finished = None
        self.ok = None
        self.interrupted = False
        self.spinner = None             # owned by the rich reporter
        self._last = None               # last sign of life
        self._samples = collections.deque()
        self._lock = threading.Lock()

    def __enter__(self):
        self.started = self._last = time.monotonic()
        self._samples.append((self.started, self.completed))
        self.reporter.begin(self)
        return self

    def __exit__(self, exc_type, exc, tb):
        self.finished = time.monotonic()
        self.ok = exc_type is None
        self.interrupted = exc_type is not None and not issubclass(exc_type, Exception)
        self.reporter.end(self)
        return False

    # ---- reporting side

    def update(self, completed=None, *, total=None, note=None):
        """Report news. Moving `completed` counts as a sign of life; `note` does not."""
        now = time.monotonic()
        with self._lock:
            if total is not None:
                self.total = total
            if note is not None:
                self.note = note
            if completed is not None and completed != self.completed:
                self.completed = completed
                self._last = now
                self._samples.append((now, completed))

    def pulse(self):
        """A sign of life that is not progress (e.g. the guest printed something)."""
        self._last = time.monotonic()

    def set_deadline(self, seconds):
        self.deadline = time.monotonic() + seconds

    # ---- reading side

    def elapsed(self, now=None):
        return (self.finished or now or time.monotonic()) - self.started

    def idle(self, now=None):
        return (now or time.monotonic()) - self._last

    def fraction(self):
        return min(1.0, self.completed / self.total) if self.total else None

    def speed(self, now=None):
        """Units per second over the last RATE_WINDOW seconds, or None if too early to say."""
        now = now or time.monotonic()
        with self._lock:
            s = self._samples
            while len(s) > 1 and s[1][0] <= now - RATE_WINDOW:
                s.popleft()
            t0, c0 = s[0]
            if now - t0 < 0.5:
                return None
            return max(0.0, (self.completed - c0) / (now - t0))

    def eta(self, now=None):
        speed = self.speed(now)
        if not self.total or not speed:
            return None
        return max(0.0, (self.total - self.completed) / speed)

    def status_parts(self, now=None):
        """What to say about a running step besides its name and elapsed time, as
        [(text, rich style)]. Shared by every reporter."""
        now = now or time.monotonic()
        parts = []
        if self.total:
            parts.append((f"{self.fraction() * 100:.0f}%", "bold"))
            if self.as_bytes:
                parts.append((f"{fmt_size(self.completed)} / {fmt_size(self.total)}", ""))
                speed = self.speed(now)
                if speed is not None:
                    parts.append((f"{fmt_size(speed)}/s", ""))
            eta = self.eta(now)
            parts.append((f"ETA {fmt_duration(eta)}" if eta is not None else "ETA --:--", ""))
        if self.note:
            parts.append((self.note, "cyan"))
        if self.stall:
            warn, critical = self.stall
            idle = self.idle(now)
            if idle >= min(5.0, warn):
                style = "bold red" if idle >= critical else "yellow" if idle >= warn else "dim"
                # first, so that on a narrow terminal it is the last thing to wrap out of sight
                parts.insert(0, (f"{self.stall_label} for {fmt_duration(idle)}", style))
        if self.deadline is not None:
            parts.append((f"gives up in {fmt_duration(self.deadline - now)}", "dim"))
        return parts


# ------------------------------------------------------------------ reporters

class _Reporter:
    """Silent: what library code gets when nobody opened a session."""
    fancy = False

    def begin(self, task):
        pass

    def end(self, task):
        pass

    def output(self, text):
        print(text, flush=True)

    def close(self):
        pass


class _Ticking(_Reporter):
    """A reporter with a background thread that wakes up every `interval` seconds."""
    interval = 1.0

    def __init__(self):
        self._tasks = []
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="piimg-progress", daemon=True)
        self._thread.start()

    def _run(self):
        while not self._stop.wait(self.interval):
            with self._lock:
                self.tick(time.monotonic())

    def begin(self, task):
        with self._lock:
            self._tasks.append(task)

    def end(self, task):
        with self._lock:
            try:
                self.finish(task)
            finally:
                if task in self._tasks:
                    self._tasks.remove(task)

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        with self._lock:
            self.shutdown()

    def tick(self, now):
        pass

    def finish(self, task):
        pass

    def shutdown(self):
        pass


class _Plain(_Ticking):
    """Plain lines on stderr: for pipes, files and CI logs."""
    announce_after = 3.0  # say something once a step has run this long
    heartbeat = 30.0      # ...and again this often

    def __init__(self):
        self._next = {}
        self._announced = set()
        super().__init__()

    def _say(self, message):
        print(f"piimg: {message}", file=sys.stderr, flush=True)

    def tick(self, now):
        for t in self._tasks:
            if now < self._next.get(t, t.started + self.announce_after):
                continue
            self._next[t] = now + self.heartbeat
            if t in self._announced:
                texts = [text for text, _ in t.status_parts(now)] + [f"{fmt_duration(t.elapsed(now))} elapsed"]
                self._say(f"{t.description}: " + ", ".join(texts))
            else:
                self._announced.add(t)
                self._say(f"{t.description} ...")

    def finish(self, task):
        self._next.pop(task, None)
        if task in self._announced:
            self._announced.discard(task)
            took = fmt_duration(task.elapsed())
            if task.ok:
                self._say(f"{task.description}: done in {took}" + (f" ({task.result})" if task.result else ""))
            else:
                self._say(f"{task.description}: {'interrupted' if task.interrupted else 'failed'} after {took}")


class _Fancy(_Ticking):
    """rich: a live status area under the normal output, on a terminal."""
    fancy = True
    interval = 0.1
    show_after = 0.4  # a step must run this long before it gets a row
    keep_after = 1.5  # ...and this long before it leaves a line behind when it ends

    def __init__(self):
        self.console = Console(stderr=True, highlight=False)
        self._live = None
        super().__init__()

    def output(self, text):
        self.console.print(Text.from_ansi(text), soft_wrap=True)

    def tick(self, now):
        if any(now - t.started >= self.show_after for t in self._tasks):
            if self._live is None:
                # Anything print()ed meanwhile must land above the status area, not inside it.
                # That only works when stdout is this same terminal; if it is redirected to a
                # file, leave it alone so the file still gets its data.
                self._live = Live(_View(self), console=self.console, transient=True,
                                  auto_refresh=False, redirect_stdout=sys.stdout.isatty(),
                                  redirect_stderr=True)
                self._live.start()
            self._live.refresh()
        else:
            self._stop_live()

    def finish(self, task):
        now = time.monotonic()
        if not any(t is not task and now - t.started >= self.show_after for t in self._tasks):
            self._stop_live()  # while `task` is still listed, so the last frame matches the screen
        if task in self._tasks:
            self._tasks.remove(task)
        if not task.ok or task.elapsed() >= self.keep_after:
            self.console.print(self._summary(task))

    def shutdown(self):
        self._stop_live()

    def _stop_live(self):
        if self._live is not None:
            live, self._live = self._live, None
            live.stop()

    def _summary(self, task):
        ascii_only = self.console.options.ascii_only
        sep = " | " if ascii_only else " · "  # rich raises on glyphs the terminal's encoding lacks
        took = fmt_duration(task.elapsed())
        line = Text()
        if task.ok:
            line.append("[ok] " if ascii_only else "✔ ", "green")
            line.append(task.description)
            if task.result:
                line.append(f"{sep}{task.result}", "dim")
            line.append(f"{sep}{took}", "dim")
        else:
            line.append("[FAIL] " if ascii_only else "✘ ", "red")
            line.append(task.description)
            line.append(f"{sep}{'interrupted' if task.interrupted else 'failed'} after {took}", "red")
        return line

    def render_task(self, task, now, options):
        bar_width = max(10, min(32, options.max_width // 4))
        if task.spinner is None:
            task.spinner = Spinner("line" if options.ascii_only else "dots", style="cyan")
        parts = task.status_parts(now)
        # Explicit right padding only: how rich versions collapse symmetric padding differs.
        header = Table.grid(padding=(0, 1, 0, 0))
        header.add_column(width=1)
        header.add_column(no_wrap=True, overflow="ellipsis")
        cells = [task.spinner.render(now), Text(task.description, style="bold")]
        if task.total:
            header.add_column(width=bar_width)
            cells.append(ProgressBar(total=task.total, completed=task.completed, width=bar_width))
        header.add_column(no_wrap=True)
        cells.append(Text(fmt_duration(task.elapsed(now)), style="dim"))
        header.add_row(*cells)
        yield header
        if parts:
            detail = Text()
            for i, (text, style) in enumerate(parts):
                if i:
                    detail.append(" | " if options.ascii_only else " · ", "dim")
                detail.append(text, style)
            # Not Padding(): that pads every line out to the terminal's full width, and a last
            # line that ends in the final column makes the tty's "^C" echo wrap and throw off
            # rich's idea of where the cursor is (a stale line is left behind on Ctrl-C).
            indented = Table.grid()
            indented.add_column(width=2)
            indented.add_column()
            indented.add_row("", detail)
            yield indented


class _View:
    """What the live area draws: one row (or two) per task that has run long enough."""

    def __init__(self, reporter):
        self.reporter = reporter

    def __rich_console__(self, console, options):
        now = time.monotonic()
        for task in list(self.reporter._tasks):
            if now - task.started >= self.reporter.show_after:
                yield from self.reporter.render_task(task, now, options)


class _Json(_Ticking):
    """Machine-readable events for a front end (the TUI) reading our stderr."""
    interval = 0.25
    heartbeat = 1.0

    def __init__(self):
        self._ids = {}
        self._sent = {}
        self._counter = itertools.count(1)
        super().__init__()

    @staticmethod
    def _emit(event):
        sys.stderr.write(EVENT_PREFIX + json.dumps(event) + "\n")
        sys.stderr.flush()

    def _state(self, t, now):
        return {"id": self._ids[t], "description": t.description, "total": t.total,
                "completed": t.completed, "as_bytes": t.as_bytes, "note": t.note,
                "elapsed": round(t.elapsed(now), 1), "parts": t.status_parts(now)}

    def begin(self, task):
        with self._lock:
            self._ids[task] = next(self._counter)
            self._tasks.append(task)
            self._emit({"event": "begin", **self._state(task, time.monotonic())})

    def tick(self, now):
        for t in self._tasks:
            key = (t.completed, t.total, t.note)
            last_key, last_time = self._sent.get(t, (None, 0))
            if key != last_key or now - last_time >= self.heartbeat:
                self._sent[t] = (key, now)
                self._emit({"event": "update", **self._state(t, now)})

    def finish(self, task):
        self._sent.pop(task, None)
        self._emit({"event": "end", "id": self._ids.pop(task, 0), "description": task.description,
                    "ok": task.ok, "interrupted": task.interrupted, "result": task.result,
                    "elapsed": round(task.elapsed(), 1)})

    def output(self, text):
        print(text, flush=True)


# -------------------------------------------------------------------- module API

EVENT_PREFIX = "@piimg-progress "  # starts every --progress=json line on stderr
_reporter = _Reporter()
MODES = ("auto", "fancy", "plain", "json", "off")


def _make_reporter(mode):
    mode = (mode or os.environ.get("PIIMG_PROGRESS") or "auto").lower()
    if mode not in MODES:
        raise ValueError(f"progress mode must be one of {', '.join(MODES)}, not {mode!r}")
    if mode == "off":
        return _Reporter()
    if mode == "json":
        return _Json()
    if mode == "auto":
        # libguestfs debug output goes straight to fd 2 and would scribble over the live area
        debugging = os.environ.get("LIBGUESTFS_DEBUG") or os.environ.get("LIBGUESTFS_TRACE")
        if Console is None or debugging:
            return _Plain()
        console = Console(stderr=True)
        return _Fancy() if console.is_terminal and not console.is_dumb_terminal else _Plain()
    if mode == "fancy":
        if Console is None:
            print("piimg: the 'rich' package is not installed; using plain progress "
                  "(sudo apt install python3-rich)", file=sys.stderr)
            return _Plain()
        return _Fancy()
    return _Plain()


@contextmanager
def session(mode=None):
    """Choose how task()s are reported for the duration of the block.

    mode: auto (default; fancy on a terminal, plain otherwise), fancy, plain or
    off. The PIIMG_PROGRESS environment variable sets the default.
    """
    global _reporter
    previous, _reporter = _reporter, _make_reporter(mode)
    current = _reporter
    try:
        yield current
    finally:
        current.close()
        _reporter = previous


def task(description, **kwargs):
    """A slow step: `with task("Doing it") as t:`. See Task for the keyword arguments."""
    return Task(_reporter, description, **kwargs)


def is_fancy():
    """True while a live status area is in use, i.e. ordinary output should go through output()."""
    return _reporter.fancy


def output(text):
    """Print one line of ordinary output where it won't tear the status area."""
    _reporter.output(text)
