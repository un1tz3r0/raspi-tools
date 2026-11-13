"""The run dialog: runs one piimg command as a child process and shows it.

    python -m piimg --progress=json --json COMMAND ...

stderr carries progress events (progress.EVENT_PREFIX + JSON), shown as one
bar per running step, plus messages; stdout carries the output: text lines
(ANSI colours kept) and tables ({"table": ...} lines). How output is laid out
depends on Meta.output: 'table' shows the first table as a grid with a detail
line for the highlighted row; 'log' and 'text' scroll in a pager.

Stop sends SIGINT to the child's process group (the CLI cleans up as on
Ctrl-C: qemu-img is stopped, libguestfs closes the image); pressing it again
sends SIGTERM, then SIGKILL.
"""
import asyncio
import json
import os
import signal
import sys
import time

from rich.table import Table as RTable
from rich.text import Text
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Label, ProgressBar, RichLog, Static

from ..output import STATUS_STYLES
from ..progress import EVENT_PREFIX, fmt_duration
from .widgets import ButtonRow, ConfirmScreen, StatusLine


def piimg_command(argv):
    return [sys.executable, "-m", "piimg", "--progress=json", "--json", *argv]


async def run_capture(argv, timeout=None):
    """Run a piimg command quietly; returns (rc, stdout lines, stderr lines without events)."""
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = await asyncio.create_subprocess_exec(*piimg_command(argv), stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.PIPE, env=env)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except BaseException:  # cancelled or timed out: don't leave it holding the image open
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    errs = [l for l in err.decode(errors="replace").splitlines() if not l.startswith(EVENT_PREFIX)]
    return proc.returncode, out.decode(errors="replace").splitlines(), errs


def parse_table(line):
    if not line.startswith('{"table"'):
        return None
    try:
        return json.loads(line)["table"]
    except (ValueError, KeyError):
        return None


def styled_cell(column, value):
    if column == "status":
        return Text(value, STATUS_STYLES.get(value.lower(), ""))
    return Text(value)


def rich_table(t):
    rt = RTable(title=t.get("title"), title_justify="left", header_style="bold", expand=False)
    for c in t["columns"]:
        rt.add_column(c, overflow="fold")
    for r in t["rows"]:
        rt.add_row(*[styled_cell(c, v) for c, v in zip(t["columns"], r)])
    if t.get("footer"):
        rt.caption, rt.caption_justify = t["footer"], "left"
    return rt


class TaskRow(Vertical):
    """One running step: description, bar, details."""

    def __init__(self, event):
        super().__init__(classes="task-row")
        self.event = event

    def compose(self) -> ComposeResult:
        with Horizontal(classes="task-head"):
            yield Label(Text(self.event["description"], "bold"), classes="task-desc")
            yield ProgressBar(total=self.event.get("total"), show_eta=False, classes="task-bar")
            yield Label("", classes="task-time")
        yield Label("", classes="task-detail")

    def apply(self, ev):
        self.event = ev
        bars = list(self.query(ProgressBar))
        if not self.is_mounted or not bars:
            return  # already finished and removed, or not composed yet
        if ev.get("total"):
            bars[0].update(total=ev["total"], progress=ev.get("completed", 0))
        self.query_one(".task-time", Label).update(Text(fmt_duration(ev.get("elapsed", 0)), "dim"))
        detail = Text()
        for i, (text, style) in enumerate(ev.get("parts") or []):
            if i:
                detail.append(" · ", "dim")
            detail.append(text, style)
        self.query_one(".task-detail", Label).update(detail)


class RunScreen(ModalScreen[int]):
    """Run `piimg ARGV` and show what it does. Dismisses with the exit status."""
    BINDINGS = [Binding("escape", "close", "Close"), Binding("ctrl+c", "stop", "Stop", show=False)]

    def __init__(self, title, argv, stdin=None, layout="log", abort="yes", cwd=None):
        super().__init__()
        self.title_text, self.argv, self.stdin, self.out_layout, self.abort = title, argv, stdin, layout, abort
        self.cwd = cwd
        self.proc = None
        self.rc = None
        self.started = None
        self.stops = 0
        self.tasks = {}
        self.table_rows = []
        self.table_cols = []
        self.got_table = False

    def compose(self) -> ComposeResult:
        with Vertical(id="run-dialog", classes="dialog wide"):
            yield Label(Text(self.title_text, "bold"), classes="dialog-title")
            yield Static(Text("$ piimg " + " ".join(self.argv), "dim"), id="cmdline")
            yield Vertical(id="tasks")
            if self.out_layout == "table":
                yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
                yield Static("", id="row-detail")
            yield RichLog(id="log", wrap=self.out_layout != "text", highlight=False, markup=False,
                          max_lines=20000, classes="table-log" if self.out_layout == "table" else "")
            yield StatusLine(id="run-status")
            with ButtonRow(classes="buttons"):
                if self.abort != "no":
                    yield Button("Stop", id="stop", variant="error")
                yield Button("Close", id="close", variant="primary", disabled=True)

    def on_mount(self):
        self.started = time.monotonic()
        self.set_interval(1.0, self._tick)
        self.run_worker(self._run(), exclusive=True)
        if self.abort == "no":
            self.query_one("#run-status", StatusLine).say("running; this step cannot be stopped safely", "yellow")

    def _tick(self):
        if self.rc is None:
            self.query_one("#run-status", StatusLine).say(
                f"running for {fmt_duration(time.monotonic() - self.started)}"
                + ("" if self.abort == "no" else " — Stop (Ctrl+C) interrupts it"), "")

    async def _run(self):
        log = self.query_one(RichLog)
        env = dict(os.environ, PYTHONUNBUFFERED="1", PIIMG_COLOR="1")
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *piimg_command(self.argv), cwd=self.cwd,
                stdin=asyncio.subprocess.PIPE if self.stdin else asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
                start_new_session=True, limit=1 << 20)
        except OSError as e:
            log.write(Text(f"cannot start piimg: {e}", "bold red"))
            self._finished(127)
            return
        if self.stdin:
            self.proc.stdin.write(self.stdin.encode())
            await self.proc.stdin.drain()
            self.proc.stdin.close()
        await asyncio.gather(self._pump(self.proc.stdout, self._out), self._pump(self.proc.stderr, self._err))
        self._finished(await self.proc.wait())

    async def _pump(self, stream, handle):
        while True:
            try:
                line = await stream.readline()
            except ValueError:  # a line longer than the limit
                line = await stream.read(1 << 16)
            if not line:
                return
            handle(line.decode("utf-8", "replace").rstrip("\n").rstrip("\r"))

    def _out(self, line):
        t = parse_table(line)
        log = self.query_one(RichLog)
        if t is None:
            log.write(Text.from_ansi(line) if "\x1b" in line else Text(line))
            return
        if self.out_layout == "table" and not self.got_table:
            self.got_table = True
            self._fill_table(t)
        else:
            log.write(rich_table(t))

    def _fill_table(self, t):
        dt = self.query_one(DataTable)
        self.table_cols, self.table_rows = t["columns"], t["rows"]
        dt.add_columns(*t["columns"])
        for r in t["rows"]:
            dt.add_row(*[styled_cell(c, v) for c, v in zip(t["columns"], r)])
        bits = [t.get("title") or "", t.get("footer") or ""]
        self.query_one("#row-detail", Static).update(Text(" — ".join(b for b in bits if b), "dim"))
        dt.focus()

    @on(DataTable.RowHighlighted)
    def _row(self, event):
        i = event.cursor_row
        if 0 <= i < len(self.table_rows):
            detail = Text()
            for c, v in zip(self.table_cols, self.table_rows[i]):
                if v:
                    detail.append(f"{c}: ", "bold")
                    detail.append(v + "\n")
            self.query_one("#row-detail", Static).update(detail)

    def _err(self, line):
        if line.startswith(EVENT_PREFIX):
            try:
                self._event(json.loads(line[len(EVENT_PREFIX):]))
            except ValueError:
                pass
            return
        style = "bold red" if line.startswith("piimg: ") and "note:" not in line else "yellow"
        self.query_one(RichLog).write(Text(line, style))

    def _event(self, ev):
        box = self.query_one("#tasks")
        tid = ev.get("id")
        if ev["event"] == "begin":
            row = TaskRow(ev)
            self.tasks[tid] = row
            box.mount(row)
            self.call_after_refresh(row.apply, ev)
        elif ev["event"] == "update" and tid in self.tasks:
            self.tasks[tid].apply(ev)
        elif ev["event"] == "end":
            row = self.tasks.pop(tid, None)
            if row is not None:
                row.remove()
            if ev.get("elapsed", 0) >= 1.5 or not ev.get("ok"):
                ok = ev.get("ok")
                line = Text("✔ " if ok else "✘ ", "green" if ok else "red")
                line.append(ev["description"])
                if ev.get("result"):
                    line.append(f" · {ev['result']}", "dim")
                line.append(f" · {fmt_duration(ev.get('elapsed', 0))}"
                            + ("" if ok else (" interrupted" if ev.get("interrupted") else " failed")),
                            "dim" if ok else "red")
                self.query_one(RichLog).write(line)

    def _finished(self, rc):
        self.rc = rc
        for row in list(self.tasks.values()):
            row.remove()
        self.tasks.clear()
        took = fmt_duration(time.monotonic() - self.started)
        status = self.query_one("#run-status", StatusLine)
        if rc == 0:
            status.say(f"finished in {took}", "bold green")
        elif rc in (130, -signal.SIGINT):
            status.say(f"stopped after {took}", "bold yellow")
        else:
            status.say(f"failed after {took} (exit status {rc})", "bold red")
        for b in self.query("#stop"):
            b.remove()
        close = self.query_one("#close", Button)
        close.disabled = False
        if self.out_layout == "table" and self.got_table:
            self.query_one(DataTable).focus()
        elif self.out_layout == "text":
            self.query_one(RichLog).focus()
        else:
            close.focus()

    # ---- stopping

    @on(Button.Pressed, "#stop")
    def action_stop(self):
        if self.rc is not None or self.proc is None:
            return
        if self.abort == "no":
            self.notify("This step cannot be stopped safely; wait for it to finish.", severity="warning")
            return
        if self.abort == "warn" and self.stops == 0:
            def answered(yes):
                if yes:
                    self._signal()
            self.app.push_screen(ConfirmScreen("Stop this command?",
                                               ["Stopping now may leave the image or card half changed."],
                                               ok_label="Stop"), answered)
            return
        self._signal()

    def _signal(self):
        sig = (signal.SIGINT, signal.SIGTERM, signal.SIGKILL)[min(self.stops, 2)]
        self.stops += 1
        try:
            os.killpg(self.proc.pid, sig)
        except ProcessLookupError:
            return
        label = {0: "Stop", 1: "Terminate", 2: "Kill"}.get(self.stops, "Kill")
        for b in self.query("#stop"):
            b.label = label
        self.query_one(RichLog).write(Text(f"-- sent {signal.Signals(sig).name}", "yellow"))

    @on(Button.Pressed, "#close")
    def action_close(self):
        if self.rc is None:
            self.notify("Still running: Stop it first.", severity="warning")
            return
        self.dismiss(self.rc)


CSS = """
#run-dialog { height: 90%; }
#tasks { height: auto; }
.task-row { height: auto; margin-bottom: 1; }
.task-head { height: 1; }
.task-desc { width: 1fr; }
.task-bar { width: 36; }
.task-time { width: 9; content-align: right middle; }
.task-detail { color: $text-muted; padding-left: 2; }
#log { height: 1fr; border: round $primary-darken-2; }
#log.table-log { height: 6; }
#table { height: 1fr; }
#row-detail { height: auto; max-height: 8; padding: 0 1; background: $boost; }
#cmdline { margin-bottom: 1; }
"""
