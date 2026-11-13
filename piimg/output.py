"""Results of commands that report data rather than doing things.

A command returns a Table (or a list of them); the CLI prints it:

  text (default)  a rich table on a terminal, aligned plain columns otherwise
  json (--json)   one line per table: {"table": {"title", "columns", "rows"}}
                  (what the TUI reads to fill its table views)

A column named "status" holds ok / warn / FAIL / info / note, and is coloured.
"""
import json
import sys

STATUS_STYLES = {"ok": "green", "warn": "yellow", "fail": "bold red", "info": "cyan", "note": "dim"}


class Table:
    def __init__(self, columns, rows=None, title=None, footer=None):
        self.columns = list(columns)
        self.rows = [list(r) for r in (rows or [])]
        self.title = title
        self.footer = footer  # one closing line of text, or None

    def add(self, *row):
        self.rows.append(list(row))

    def to_json(self):
        return {"title": self.title, "columns": self.columns,
                "rows": [["" if v is None else str(v) for v in r] for r in self.rows],
                "footer": self.footer}

    def plain(self):
        cells = [self.columns] + [["" if v is None else str(v) for v in r] for r in self.rows]
        widths = [max(len(r[i]) for r in cells) for i in range(len(self.columns))]
        out = [self.title] if self.title else []
        for n, r in enumerate(cells):
            out.append("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
            if n == 0:
                out.append("  ".join("-" * w for w in widths))
        if self.footer:
            out.append(self.footer)
        return "\n".join(out)

    def rich(self):
        from rich.table import Table as RTable
        t = RTable(title=self.title, title_justify="left", show_lines=False, header_style="bold")
        status_col = self.columns.index("status") if "status" in self.columns else None
        for c in self.columns:
            t.add_column(c, overflow="fold")
        for r in self.rows:
            cells = ["" if v is None else str(v) for v in r]
            style = STATUS_STYLES.get(cells[status_col].lower()) if status_col is not None else None
            if style:
                cells[status_col] = f"[{style}]{cells[status_col]}[/]"
            t.add_row(*cells)
        if self.footer:
            t.caption, t.caption_justify = self.footer, "left"
        return t


def emit(result, as_json=False):
    """Print a command's result: None, a str, a Table or a list of those."""
    if result is None:
        return
    if isinstance(result, (list, tuple)):
        for r in result:
            emit(r, as_json)
        return
    if isinstance(result, Table):
        if as_json:
            print(json.dumps({"table": result.to_json()}), flush=True)
        elif sys.stdout.isatty():
            from rich.console import Console
            Console().print(result.rich())
        else:
            print(result.plain())
        return
    print(result, flush=True)
