"""One form field, as both front ends draw it."""
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FieldDef:
    """One form field.

    type: str, secret, int, bool, choice, lines (one value per line), map
          (KEY=VALUE per line), text (multi-line), yaml (multi-line YAML),
          vars (a `run` command's recipe variables: the front end shows one
          field per variable of the chosen recipe instead)
    """
    key: str
    label: str
    type: str = "str"
    required: bool = False
    help: str = ""
    choices: tuple = ()
    value: Any = None
    placeholder: str = ""
    suggestions: tuple = ()
    data: dict = field(default_factory=dict)  # caller's own bookkeeping

    MULTILINE = ("lines", "map", "text", "yaml")

    @property
    def multiline(self):
        return self.type in self.MULTILINE

    def explain(self):
        bits = [f"{self.label}{' (required)' if self.required else ''}: {self.help or ''}".rstrip(": ")]
        if self.type in ("lines", "map"):
            bits.append("one per line" if self.type == "lines" else "KEY=VALUE, one per line")
        return " — ".join(b for b in bits if b)

    def is_empty(self, value):
        return value in ("", None, [], {})


def scalar_text(v):
    if v is True:
        return "true"
    if v is False:
        return "false"
    return "" if v is None else str(v)


def to_text(f, v):
    """A field's value as the text a multi-line box shows."""
    if isinstance(v, (list, tuple)):
        return "\n".join(map(str, v))
    if isinstance(v, dict):
        return "\n".join(f"{k}={scalar_text(x)}" for k, x in v.items())
    return "" if v is None else str(v)


def from_text(f, text):
    """What a multi-line box's text means: a list (lines), a dict (map) or the text."""
    if f.type == "lines":
        return [l.strip() for l in text.splitlines() if l.strip()]
    if f.type == "map":
        out = {}
        for l in text.splitlines():
            if l.strip():
                k, _, val = l.partition("=")
                out[k.strip()] = val.strip()
        return out
    return text


def missing_required(fields, values):
    """The first required field (not 'vars') with no value, or None."""
    for f in fields:
        if f.required and f.type != "vars" and f.is_empty(values.get(f.key)):
            return f
    return None
