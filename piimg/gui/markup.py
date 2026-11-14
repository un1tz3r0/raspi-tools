"""rich.text.Text (what the frontend models produce) -> Pango markup and TextBuffer tags."""
from gi.repository import GLib, Gtk, Pango
from rich.style import Style
from rich.text import Text

# colours readable on both the light and the dark libadwaita background
PALETTE = {
    "red": "#e01b24", "green": "#2ec27e", "yellow": "#d4a000", "blue": "#3584e4", "magenta": "#c061cb",
    "cyan": "#1f9ead", "white": "#deddda", "black": "#3d3846", "bright_red": "#f66151",
    "bright_green": "#57e389", "bright_yellow": "#e5a50a", "bright_blue": "#62a0ea",
    "bright_magenta": "#dc8add", "bright_cyan": "#4fc8d6", "grey": "#9a9996", "gray": "#9a9996",
}
ANSI = ("black", "red", "green", "yellow", "blue", "magenta", "cyan", "white", "grey", "bright_red",
        "bright_green", "bright_yellow", "bright_blue", "bright_magenta", "bright_cyan", "white")
DIM_ALPHA = "60%"


def _colour(c):
    if c is None or c.is_default:
        return None
    name = c.name.lower()
    if c.number is not None and c.number < 16:
        name = ANSI[c.number]
    if name in PALETTE:
        return PALETTE[name]
    if name.startswith("#"):
        return name
    try:
        t = c.get_truecolor()
        return f"#{t.red:02x}{t.green:02x}{t.blue:02x}"
    except Exception:  # noqa: BLE001
        return None


def _attrs(style):
    """A rich style (or style string) -> Pango span attributes."""
    if not style:
        return {}
    if isinstance(style, str):
        try:
            style = Style.parse(style)
        except Exception:  # noqa: BLE001 - not a style: plain text
            return {}
    a = {}
    if style.bold:
        a["weight"] = "bold"
    if style.italic:
        a["style"] = "italic"
    if style.underline:
        a["underline"] = "single"
    if style.strike:
        a["strikethrough"] = "true"
    fg, bg = _colour(style.color), _colour(style.bgcolor)
    if style.reverse:
        fg, bg = bg, fg or "#77767b"
    if fg:
        a["foreground"] = fg
    if bg:
        a["background"] = bg
    if style.dim:
        a["alpha"] = DIM_ALPHA
    return a


def to_markup(text):
    """rich Text (or a plain str) -> Pango markup."""
    if isinstance(text, str):
        return GLib.markup_escape_text(text)
    out = []
    for seg_text, style in _segments(text):
        esc = GLib.markup_escape_text(seg_text)
        a = _attrs(style)
        out.append(f"<span {' '.join(f'{k}={v!r}' for k, v in a.items())}>{esc}</span>" if a else esc)
    return "".join(out)


def _segments(text):
    """(str, Style) pieces of a rich Text, with spans resolved."""
    for seg in text.render(_console(), end=""):
        if seg.text:
            yield seg.text, seg.style


_CONSOLE = None


def _console():
    global _CONSOLE
    if _CONSOLE is None:
        from rich.console import Console
        _CONSOLE = Console(width=10_000, color_system="truecolor", force_terminal=False)
    return _CONSOLE


def label(text, **kw):
    """A Gtk.Label showing rich Text, wrapped and selectable unless told otherwise."""
    lb = Gtk.Label(xalign=0, wrap=kw.pop("wrap", True), selectable=kw.pop("selectable", False), **kw)
    lb.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    lb.set_markup(to_markup(text))
    return lb


class TagCache:
    """TextBuffer tags for rich styles, made once per style."""

    def __init__(self, buffer):
        self.buffer = buffer
        self.tags = {}

    def tag(self, style):
        key = str(style)
        if key in self.tags:
            return self.tags[key]
        a = _attrs(style)
        props = {}
        if a.get("weight") == "bold":
            props["weight"] = Pango.Weight.BOLD
        if a.get("style") == "italic":
            props["style"] = Pango.Style.ITALIC
        if a.get("underline"):
            props["underline"] = Pango.Underline.SINGLE
        if a.get("strikethrough"):
            props["strikethrough"] = True
        if "foreground" in a:
            props["foreground"] = a["foreground"]
        if "background" in a:
            props["background"] = a["background"]
        tag = None
        if props or "alpha" in a:
            tag = self.buffer.create_tag(None, **props)
            if "alpha" in a and "foreground" not in a:
                tag.set_property("foreground-rgba", _dim_rgba())
        self.tags[key] = tag
        return tag

    def append(self, text):
        """Append rich Text (or str) at the end of the buffer."""
        b = self.buffer
        if isinstance(text, str):
            text = Text(text)
        for seg_text, style in _segments(text):
            end = b.get_end_iter()
            tag = self.tag(style) if style else None
            if tag is not None:
                b.insert_with_tags(end, seg_text, tag)
            else:
                b.insert(end, seg_text)


def _dim_rgba():
    from gi.repository import Gdk
    c = Gdk.RGBA()
    c.parse("rgba(128,128,128,0.9)")
    return c


def strip(text):
    return text.plain if isinstance(text, Text) else str(text)
