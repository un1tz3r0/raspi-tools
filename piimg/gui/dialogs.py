"""The command form, confirmations, and small prompts."""
from gi.repository import Adw, GLib, Gtk

from ..frontend import commands as C
from .forms import Form
from .markup import to_markup


def help_bar():
    """The line under a form that explains the focused field."""
    lb = Gtk.Label(xalign=0, wrap=True, margin_top=8, margin_bottom=8, margin_start=12, margin_end=12,
                   css_classes=["dim-label"])
    return lb


def say(label, text, kind=""):
    colour = {"error": "#e01b24", "warn": "#d4a000", "ok": "#2ec27e"}.get(kind)
    esc = GLib.markup_escape_text(text or "")
    label.set_markup(f"<span foreground='{colour}'><b>{esc}</b></span>" if colour else esc)
    for c in ("dim-label",):
        (label.remove_css_class if colour else label.add_css_class)(c)


class CommandForm:
    """The options of one command, shown in the main window under its help.

    .widget holds the options group (and, for `run`, the chosen recipe's
    variables); help for the focused field and errors go to on_say(text, kind).
    collect(target) checks the form and returns (argv, stdin, args, values) or
    None; values (dest -> form value) leave out secrets."""

    def __init__(self, cmd, last_values, on_say):
        self.cmd, self.say = cmd, on_say
        self.fields = C.fields_for(cmd, last_values)
        self.widget = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        self.form = Form([f for f in self.fields if f.type != "vars"], on_change=self._changed,
                         on_focus=lambda f: self.say(f.explain()), title="Options")
        self.widget.append(self.form.widget)
        self.vars_group = None
        self.varform = None
        if any(f.type == "vars" for f in self.fields):
            self._refresh_vars()

    def _changed(self, f):
        if f.data.get("kind") == "recipe":
            self._refresh_vars()

    def _refresh_vars(self):
        """For `run`: one field per variable of the chosen recipe."""
        if self.vars_group is not None:
            self.widget.remove(self.vars_group)
        name = next((self.form.value(f.key) for f in self.form.fields if f.data.get("kind") == "recipe"), "")
        fields, note = C.recipe_var_fields(name)
        if fields:
            self.varform = Form(fields, on_focus=lambda f: self.say(f.explain()), title="Recipe variables")
            self.vars_group = self.varform.widget
        else:
            self.varform = None
            self.vars_group = Adw.PreferencesGroup(title="Recipe variables", description=note)
        self.widget.append(self.vars_group)

    def focus_first(self):
        return self.form.focus_first()

    def values(self):
        values = self.form.values()
        if self.varform is not None:
            values["var"] = C.var_args(self.varform.values())
        return values

    def kept_values(self, values=None):
        """The values worth remembering: everything but secrets."""
        values = self.values() if values is None else values
        return {k: v for k, v in values.items()
                if not any(f.key == k and f.type == "secret" for f in self.fields)}

    def collect(self, target):
        values = self.values()
        missing = self.form.check_required()
        if missing:
            self.say(f"{missing.label} is required", "error")
            self.form.focus(missing.key)
            return None
        if self.varform is not None:
            vm = self.varform.check_required()
            if vm:
                self.say(f"variable {vm.label} is required", "error")
                self.varform.focus(vm.key)
                return None
        argv, stdin = C.build_argv(self.cmd, values, target)
        try:
            args = C.parse(argv)
        except C.ArgError as e:
            self.say(str(e), "error")
            return None
        return argv, stdin, args, self.kept_values(values)


def confirm(parent, title, messages, typed=None, ok_label="Run", danger=True, on_ok=None):
    """Ask before doing something; with `typed`, the user must type that word."""
    d = Adw.AlertDialog(heading=title, body="\n\n".join(messages))
    d.add_response("cancel", "Cancel")
    d.add_response("ok", ok_label)
    d.set_response_appearance("ok", Adw.ResponseAppearance.DESTRUCTIVE if danger
                              else Adw.ResponseAppearance.SUGGESTED)
    d.set_default_response("cancel")
    d.set_close_response("cancel")
    if typed:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.append(Gtk.Label(label=f"Type {typed} to confirm:", xalign=0))
        entry = Gtk.Entry(placeholder_text=typed, activates_default=True)
        box.append(entry)
        d.set_extra_child(box)
        d.set_response_enabled("ok", False)

        def changed(e):
            ok = e.get_text().strip() == typed
            d.set_response_enabled("ok", ok)
            if ok:
                d.set_default_response("ok")
        entry.connect("changed", changed)
    d.connect("response", lambda _d, r: r == "ok" and on_ok and on_ok())
    d.present(parent)
    return d


def prompt(parent, title, label, value="", ok_label="OK", help_text="", on_ok=None):
    """Ask for one line of text."""
    d = Adw.AlertDialog(heading=title, body=help_text)
    d.add_response("cancel", "Cancel")
    d.add_response("ok", ok_label)
    d.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
    d.set_default_response("ok")
    d.set_close_response("cancel")
    entry = Adw.EntryRow(title=label, text=value, activates_default=True)
    group = Adw.PreferencesGroup()
    group.add(entry)
    d.set_extra_child(group)
    d.connect("response", lambda _d, r: r == "ok" and entry.get_text().strip() and on_ok
              and on_ok(entry.get_text().strip()))
    d.present(parent)
    return d


def message(parent, title, text):
    d = Adw.AlertDialog(heading=title)
    d.set_body_use_markup(True)
    d.set_body(to_markup(text))
    d.add_response("close", "Close")
    d.present(parent)
    return d
