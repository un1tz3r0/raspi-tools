"""Drive piimg gui headless and save screenshots: a development and test aid.

    gtk4-broadwayd :7 &
    GDK_BACKEND=broadway BROADWAY_DISPLAY=:7 python tests/gui_driver.py OUTDIR SCENARIO [TARGET [OVERLAY]]

Each scenario builds windows, does a few things through the same calls the
widgets make, saves OUTDIR/<name>.png at each stage and checks a few facts;
the exit status says whether they held. tests/test_gui.py runs it.
"""
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import piimg.gui as G  # noqa: E402

G.require()
from gi.repository import Adw, GLib, Graphene, Gtk  # noqa: E402

OUT = sys.argv[1]
SCENARIO = sys.argv[2]
TARGET = sys.argv[3] if len(sys.argv) > 3 else None
OVERLAY = sys.argv[4] if len(sys.argv) > 4 else None
failures = []


def check(cond, what):
    if not cond:
        failures.append(what)
        print("FAIL:", what, flush=True)


def wait(seconds=0.3, until=None, timeout=60):
    """Run the main loop for a while (or until until() is true)."""
    ctx = GLib.MainContext.default()
    end = time.monotonic() + (timeout if until else seconds)
    while time.monotonic() < end:
        while ctx.pending():
            ctx.iteration(False)
        if until and until():
            return True
        time.sleep(0.02)
    return not until


def shot(win, name):
    w, h = win.get_width(), win.get_height()
    snap = Gtk.Snapshot()
    Gtk.WidgetPaintable.new(win).snapshot(snap, w, h)
    node = snap.to_node()
    if node is None:
        print("empty snapshot", name)
        return
    tex = win.get_renderer().render_texture(node, Graphene.Rect().init(0, 0, w, h))
    path = os.path.join(OUT, name + ".png")
    tex.save_to_png(path)
    print("saved", path, flush=True)


def dialogs(win):
    d = win.get_visible_dialog()
    return d


def scenario_main(app):
    from piimg.gui.app import MainWindow
    from piimg.gui.opentarget import OpenDialog
    # as if started from a terminal: Python 3.14's argparse would colour the usage text
    os.environ["FORCE_COLOR"] = "1"
    win = MainWindow(app, TARGET, OVERLAY)
    os.environ.pop("FORCE_COLOR")
    win.present()
    wait(1.0)
    d = dialogs(win)
    if TARGET is None:
        check(d is not None, "open dialog shown without a target")
        shot(win, "open-dialog")
        d.close()
        wait(0.3)
    shot(win, "main-no-info")
    if TARGET:
        wait(until=lambda: win.info_child is None, timeout=240)
        check(win.info_rows_loaded(), "target info loaded")
        shot(win, "main")
    # filter and pick a command with a form
    win.search.set_text("print a file")
    wait(0.5)
    check(win.current is not None and win.current.name == "cat", "filter selects cat")
    # selecting a command shows its options inline: no dialog
    check(dialogs(win) is None, "selecting a command opens no dialog")
    check(win.cmd_form is not None and "path" in win.cmd_form.form.rows, "cat's form is in the window")
    help_text = win.cmd_help.get_text()
    check("\x1b" not in help_text and "[1;" not in help_text, "no ANSI codes in the help")
    check(help_text.count(win.current.help.strip()[:40]) == 1, "the description is shown once")
    shot(win, "form-cat")
    # typed values survive switching commands
    win.cmd_form.form.set_value("path", "/etc/hostname")
    if TARGET is None:
        win.run_current()
        wait(0.5)
        check(isinstance(dialogs(win), OpenDialog), "a command needing a target asks to open one")
        shot(win, "needs-target")
        dialogs(win).close()
        wait(0.3)
    else:
        win.cmd_form.form.set_value("path", "")
        win.run_current()   # user is required: nothing runs, the form says why
        wait(0.2)
        check(dialogs(win) is None, "missing required field runs nothing")
        check(win.field_help.get_visible() and "required" in win.field_help.get_text(), "says what is missing")
        shot(win, "form-required")
        win.cmd_form.form.set_value("path", "/etc/hostname")
    win.search.set_text("")
    wait(0.3)
    info = next(c for c in win.cmds if c.name == "info")
    win.list.select_row(next(r for r in _rows(win.list) if r.cmd is info))
    wait(0.2)
    check(win.cmd_form is None or not win.cmd_form.fields, "info has no options form")
    cat = next(c for c in win.cmds if c.name == "cat")
    win.list.select_row(next(r for r in _rows(win.list) if r.cmd is cat))
    wait(0.2)
    check(win.cmd_form.form.value("path") == "/etc/hostname", "cat's typed path is kept")
    if TARGET is None:
        # the run form shows the chosen recipe's variables
        run_cmd = next(c for c in win.cmds if c.name == "run")
        win.list.select_row(next(r for r in _rows(win.list) if r.cmd is run_cmd))
        wait(0.3)
        pd = win.cmd_form
        check(pd.varform is None, "no recipe: no variable fields")
        pd.form.set_value("recipe", "pitft")
        wait(0.3)
        check(pd.varform is not None and "display" in pd.varform.rows, "pitft's variables appear")
        shot(win, "form-run")
        pd.varform.set_value("display", "28c")
        got = pd.collect("/x/work.qcow2")
        check(got and "--var=display=28c" in got[0], f"the variable reaches the command line ({got})")
        win.close()
        return
    win.search.set_text("")
    wait(0.3)
    # a table command
    users = next(c for c in win.cmds if c.name == "autostart")
    win.list.select_row(next(r for r in _rows(win.list) if r.cmd is users))
    wait(0.2)
    shot(win, "main-users-selected")
    if TARGET:
        win.start_command(users)
        wait(0.5)
        rd = dialogs(win)
        check(rd is not None and rd.get_title() == "piimg autostart", "run dialog for autostart")
        wait(until=lambda: rd.rc is not None, timeout=240)
        wait(0.5)
        check(rd.rc == 0, f"autostart finished ok (rc {rd.rc})")
        check(rd.got_table, "autostart shows a table")
        shot(win, "run-autostart")
        rd.close()
        wait(0.3)
    # the open dialog
    win.action_open()
    wait(0.5)
    shot(win, "open-dialog")
    dialogs(win).close()
    wait(0.3)
    win.close()


def scenario_stop(app):
    """A long command, stopped: SIGINT reaches it and the dialog reports it."""
    from piimg.gui.app import MainWindow
    from piimg.gui.run import RunDialog
    win = MainWindow(app, TARGET, OVERLAY)
    win.present()
    wait(0.5)
    win.cancel_info()
    rd = RunDialog("piimg diagnose", ["diagnose", "--", win.work_path], layout="table")
    rd.present(win)
    wait(until=lambda: rd.tasks, timeout=120)
    wait(1.0)
    shot(win, "run-progress")
    rd.stop()
    wait(until=lambda: rd.rc is not None, timeout=120)
    check(rd.rc in (130, 1, -2), f"stopped command exits with 130 (got {rd.rc})")
    shot(win, "run-stopped")
    rd.close()
    win.close()


def scenario_picker(app):
    """Ctrl+E's action: the recipe picker, then a template in its own window."""
    from piimg.gui.app import MainWindow
    from piimg.gui.recipe import RecipePickDialog, RecipeWindow
    win = MainWindow(app, TARGET, OVERLAY)
    app.main_window = win
    win.present()
    wait(0.5)
    if win.get_visible_dialog():
        win.get_visible_dialog().close()
        wait(0.3)
    win.activate_action("win.recipe-editor", None)
    wait(0.5)
    d = win.get_visible_dialog()
    check(isinstance(d, RecipePickDialog), "the recipe picker opens")
    shot(win, "recipe-picker")
    from piimg.frontend.recipe import find_template
    d._pick(None, find_template("pitft"))
    wait(0.8)
    rw = [w for w in app.get_windows() if isinstance(w, RecipeWindow)]
    check(len(rw) == 1, "a recipe window opened")
    if rw:
        check(rw[0].model.path is None and rw[0].model.template is not None, "a template is untitled")
        check("template" in rw[0].wtitle.get_title(), "the title says it is a template")
        shot(rw[0], "recipe-template")
        rw[0].close()
    win.close()


def scenario_terminal(app):
    """A terminal command (edit) in the VTE dialog; the editor is `cat`, so nothing changes."""
    from gi.repository import Vte
    from piimg.gui.app import MainWindow
    from piimg.gui.terminal import TerminalDialog
    win = MainWindow(app, TARGET, OVERLAY)
    win.present()
    wait(0.5)
    win.cancel_info()
    done = []
    td = TerminalDialog("piimg edit", ["edit", "--editor=cat", "--", win.work_path, "/etc/hostname"],
                        on_done=done.append)
    td.present(win)
    wait(until=lambda: done, timeout=240)
    wait(0.5)
    text = td.term.get_text_format(Vte.Format.TEXT) or ""
    check(done == [0], f"edit exits 0 (got {done})")
    check("raspberrypi" in text, "the terminal shows the file")
    shot(win, "terminal")
    td.close()
    win.close()


def select_only(win, i):
    win.list.unselect_all()
    win.list.select_row(win.row_widgets[i])


def _rows(listbox):
    out, i = [], 0
    while (r := listbox.get_row_at_index(i)) is not None:
        out.append(r)
        i += 1
    return out


def scenario_recipe(app):
    import shutil
    from piimg.gui.recipe import InsertDialog, RecipeWindow, YamlDialog
    src = os.path.join(os.path.dirname(__file__), "..", "examples", "demo")
    demo = os.path.join(OUT, "demo")
    shutil.rmtree(demo, ignore_errors=True)
    shutil.copytree(src, demo)
    path = os.path.join(demo, "recipe.yaml")
    with open(path, "a") as f:  # a step that fails on purpose, to see it reported and selected
        f.write('  - when: {dry_run: true}\n    then: [{fail: "the GUI test stops the dry run here"}]\n')
    win = RecipeWindow(app, path)
    win.present()
    wait(0.8)
    m = win.model
    n0 = len(m.doc["steps"])
    shot(win, "recipe")
    select_only(win, 1)
    wait(0.3)
    check(win.cursor == 1 and win.form is not None, "selecting a step shows its form")
    shot(win, "recipe-step")
    # edit a field through the form
    win.form.set_value("name", "renamed step")
    wait(0.2)
    check(m.doc["steps"][0].get("name") == "renamed step", "form edit changes the step")
    check(m.dirty, "edit marks the recipe dirty")
    win.undo()
    wait(0.2)
    check(m.doc["steps"][0].get("name") != "renamed step", "undo reverts the edit")
    # multi-select two steps and duplicate them
    select_only(win, 1)
    win.list.select_row(win.row_widgets[2])
    wait(0.2)
    check(len(m.marks) == 2, "two rows selected = two marks")
    shot(win, "recipe-marked")
    win.duplicate()
    wait(0.2)
    check(len(m.doc["steps"]) == n0 + 2, "duplicate adds two steps")
    win.undo()
    check(len(m.doc["steps"]) == n0, "undo removes them")
    # insert dialog
    select_only(win, 1)
    wait(0.2)
    win.insert()
    wait(0.5)
    d = win.get_visible_dialog()
    check(isinstance(d, InsertDialog), "insert dialog shown")
    d.search.set_text("symli")
    wait(0.3)
    shot(win, "recipe-insert")
    d._insert()
    wait(0.5)
    check("symlink" in m.doc["steps"][1], "inserted a symlink step after the cursor")
    check(win.cursor == 2, "the new step is selected")
    shot(win, "recipe-inserted")
    # YAML edit of the step
    win.edit_yaml()
    wait(0.5)
    yd = win.get_visible_dialog()
    check(isinstance(yd, YamlDialog), "yaml dialog shown")
    shot(win, "recipe-yaml")
    yd.view.get_buffer().set_text("not: [valid\n")
    yd.apply()
    wait(0.3)
    check(win.get_visible_dialog() is yd, "invalid YAML keeps the dialog open")
    yd.view.get_buffer().set_text("echo: from-yaml\n")
    yd.apply()
    wait(0.3)
    check(m.doc["steps"][1].get("echo") == "from-yaml", "yaml edit applied")
    win.save()
    wait(0.2)
    check(not m.dirty and "from-yaml" in open(path).read(), "saved")
    win.check()
    wait(0.5)
    shot(win, "recipe-saved")
    if TARGET:
        if OVERLAY:
            from piimg.image import ensure_overlay
            ensure_overlay(TARGET, OVERLAY)
        win.image = OVERLAY or TARGET
        win._run(win.image, "all", True, {})
        wait(0.5)
        rd = win.get_visible_dialog()
        wait(until=lambda: rd.rc is not None, timeout=300)
        wait(0.5)
        shot(win, "recipe-dry-run")
        print("dry run rc", rd.rc, rd.errors, flush=True)
        if not rd.errors:
            from piimg.gui.forms import buffer_text
            print(buffer_text(rd.log_view.get_buffer())[-3000:], flush=True)
        # the copy's last step fails on purpose under --dry-run
        check(rd.rc == 1 and rd.errors, "dry run reports the failing step")
        r = m.rows[win.cursor]
        check(m.error_where and r.where == m.error_where, f"the failing step is selected ({m.error_where})")
        rd.close()
        wait(0.3)
        shot(win, "recipe-after-run")
    m.dirty = False
    win.close()


def _control(widget, column):
    """The editing widget for `column` inside a row (depth first)."""
    if getattr(widget, "column", None) == column:
        return widget
    child = widget.get_first_child()
    while child is not None:
        found = _control(child, column)
        if found is not None:
            return found
        child = child.get_next_sibling()
    return None


def _respond(d, response):
    """Answer an Adw.AlertDialog as a click on that button would."""
    d.emit("response", response)
    d.close()


def scenario_editor(app):
    """The Services editor: read, change, the guard on leaving, preview, commit, read again."""
    from piimg.gui.app import MainWindow
    from piimg.gui.run import RunDialog
    if TARGET and OVERLAY:
        from piimg.image import ensure_overlay
        ensure_overlay(TARGET, OVERLAY)
    win = MainWindow(app, TARGET, OVERLAY)
    win.present()
    wait(0.8)
    if dialogs(win) is not None:     # the open dialog, without a target
        dialogs(win).close()
        wait(0.3)
    names = [c.name for c in win.cmds]
    check("service" not in [c.name for c in win.cmds if not getattr(c, "is_editor", False)],
          "the service command is replaced by the editor")
    ed = next(c for c in win.cmds if getattr(c, "is_editor", False) and c.name == "services")
    check(names.count("services") == 1, "one services entry")
    win.list.select_row(next(r for r in _rows(win.list) if r.cmd is ed))
    wait(0.3)
    page = win.pages["services"]
    check(win.content_stack.get_visible_child() is page, "the editor page is shown")
    if TARGET is None:
        check(page.stack.get_visible_child_name() == "status", "no target: the page says so")
        shot(win, "editor-no-target")
        win.close()
        return
    wait(until=lambda: page.stack.get_visible_child_name() != "loading", timeout=240)
    check(page.stack.get_visible_child_name() == "list", "the units are listed")
    check(ed.loaded and len(ed.items) > 20, f"units loaded ({len(ed.items)})")
    shot(win, "editor-services")
    item = next(i for i in ed.items if i.orig["state"] == "disabled")
    row = page.rows[item.key]
    dd = _control(row, "state")
    choices = [dd.get_model().get_string(i) for i in range(dd.get_model().get_n_items())]
    dd.set_selected(choices.index("enabled"))
    wait(0.2)
    check(ed.dirty and item.get("state") == "enabled", "a change makes the editor dirty")
    check("changed" in row.get_css_classes() and row.undo.get_visible(), "the changed row is marked")
    check(page.commit_btn.get_sensitive(), "Commit is enabled")
    sidebar = next(r for r in _rows(win.list) if r.cmd is ed)
    check("changed" in sidebar.name_label.get_label(), "the list shows the editor has changes")
    page.filter.set_selected([f for f, _ in ed.filters].index("changed"))
    wait(0.2)
    shot(win, "editor-changed")
    # leaving asks; Keep Editing stays
    info = next(r for r in _rows(win.list) if r.cmd.name == "info")
    win.list.select_row(info)
    wait(0.3)
    d = dialogs(win)
    check(isinstance(d, Adw.AlertDialog), "leaving with changes asks")
    shot(win, "editor-leave")
    _respond(d, "cancel")
    wait(0.3)
    check(win.current is ed and win.list.get_selected_row() is sidebar, "Keep Editing stays on the editor")
    # preview: a dry run changes nothing
    win.editor_commit(page, dry=True)
    wait(0.5)
    rd = dialogs(win)
    check(isinstance(rd, RunDialog), "preview runs")
    wait(until=lambda: rd.rc is not None, timeout=300)
    wait(0.3)
    check(rd.rc == 0, f"preview ok (rc {rd.rc})")
    shot(win, "editor-preview")
    rd.close()
    wait(0.3)
    check(ed.dirty, "preview keeps the changes")
    # commit
    win.editor_commit(page)
    wait(0.3)
    d = dialogs(win)
    check(isinstance(d, Adw.AlertDialog) and item.key in d.get_body(), "commit lists the changes")
    shot(win, "editor-commit-confirm")
    _respond(d, "ok")
    wait(0.5)
    rd = dialogs(win)
    check(isinstance(rd, RunDialog), "commit runs")
    wait(until=lambda: rd.rc is not None, timeout=300)
    check(rd.rc == 0, f"commit ok (rc {rd.rc}) {rd.errors}")
    rd.close()
    wait(until=lambda: page.stack.get_visible_child_name() == "list" and not page.stale
         and ed.loaded and item.key in ed.by_key and ed.by_key[item.key] is not item, timeout=240)
    now = ed.by_key.get(item.key)
    check(now is not None and now.orig["state"] == "enabled", f"read again: {item.key} is enabled")
    check(not ed.dirty, "no changes left")
    page.filter.set_selected(0)
    wait(0.3)
    shot(win, "editor-committed")
    # leaving now does not ask
    win.list.select_row(info)
    wait(0.3)
    check(dialogs(win) is None and win.current is info.cmd, "leaving without changes does not ask")
    win.close()


def _open_editor(win, name):
    ed = next(c for c in win.cmds if getattr(c, "is_editor", False) and c.name == name)
    row = next((r for r in _rows(win.list) if r.cmd is ed), None)
    check(row is not None, f"{name} is in the list")
    if row is None:
        return None, None
    win.list.select_row(row)
    wait(0.3)
    page = win.pages[name]
    wait(until=lambda: page.stack.get_visible_child_name() not in ("loading",), timeout=300)
    check(page.listing(), f"{name}: read ({page.stack.get_visible_child_name()}: "
                          f"{page.status.get_title()} {page.status.get_description()})")
    return ed, page


def _commit(win, page, name):
    """Commit through the confirmation and the run dialog; wait for the page to be read again."""
    from piimg.gui.run import RunDialog
    check(page.commit_btn.get_sensitive(), f"{name}: Commit is enabled")
    win.editor_commit(page)
    wait(0.3)
    d = dialogs(win)
    check(isinstance(d, Adw.AlertDialog), f"{name}: commit asks first")
    if not isinstance(d, Adw.AlertDialog):
        return False
    shot(win, f"{name}-confirm")
    _respond(d, "ok")
    wait(0.5)
    rd = dialogs(win)
    check(isinstance(rd, RunDialog), f"{name}: commit runs")
    if not isinstance(rd, RunDialog):
        return False
    wait(until=lambda: rd.rc is not None, timeout=400)
    ok = rd.rc == 0
    check(ok, f"{name}: commit ok (rc {rd.rc}) {rd.errors}")
    rd.close()
    wait(0.3)
    wait(until=lambda: page.listing() and not page.stale and page.child is None, timeout=300)
    check(not page.editor.dirty, f"{name}: no changes left after the commit")
    return ok


def scenario_editors(app):
    """Every editor but services (scenario editor) on a target: change, commit, read again."""
    from piimg.gui.app import MainWindow
    if TARGET and OVERLAY:
        from piimg.image import ensure_overlay
        ensure_overlay(TARGET, OVERLAY)
    win = MainWindow(app, TARGET, OVERLAY)
    win.present()
    wait(0.8)

    # users: shell and a key for pi
    ed, page = _open_editor(win, "users")
    if ed:
        pi = ed.by_key.get("pi")
        check(pi is not None and "keys" in pi.values, "users: pi is listed with its keys")
        row = page.rows["pi"]
        check(isinstance(row, Adw.ExpanderRow), "users: a row expands to its fields")
        row.set_expanded(True)
        page._set(pi, "shell", "/bin/bash")
        page._set(pi, "keys", pi.get("keys") + "\nssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIguitest gui@test")
        page._set(pi, "name", "Bad Name")
        wait(0.2)
        check(page.problems.get_visible() and not page.commit_btn.get_sensitive(), "users: a bad name blocks Commit")
        shot(win, "users-problem")
        page._set(pi, "name", "pi")
        wait(0.2)
        shot(win, "users-changed")
        if _commit(win, page, "users"):
            pi = ed.by_key["pi"]
            check(pi.get("shell") == "/bin/bash" and "guitest" in pi.get("keys"), "users: read back")

    # system settings: hostname and ssh
    ed, page = _open_editor(win, "system")
    if ed:
        check(isinstance(page.rows["ssh"].get_last_child(), Gtk.Widget), "system: rows drawn")
        host, ssh = ed.by_key["hostname"], ed.by_key["ssh"]
        new_ssh = "off" if ssh.get("value") == "on" else "on"
        page._set(host, "value", "gui-test")
        page._set(ssh, "value", new_ssh)
        shot(win, "system-changed")
        if _commit(win, page, "system"):
            check(ed.by_key["hostname"].get("value") == "gui-test" and ed.by_key["ssh"].get("value") == new_ssh,
                  "system: read back")

    # wifi: add two networks, put the second first
    ed, page = _open_editor(win, "wifi")
    if ed:
        for ssid, pw in (("gui-home", "longenough"), ("gui-cafe", "")):
            d = page.add_dialog()
            wait(0.3)
            _d, form, add = page.add_form
            form.set_value("ssid", ssid)
            form.set_value("password", pw)
            add()
            wait(0.2)
        check(len([i for i in ed.items if i.new]) == 2, "wifi: two networks added")
        cafe = next(i for i in ed.items if i.new and i.get("ssid / interface") == "gui-cafe")
        page.move(cafe.key, -1)
        wait(0.2)
        shot(win, "wifi-changed")
        if _commit(win, page, "wifi"):
            keys = [i.key for i in ed.items]
            check(keys[:2] == ["gui-cafe", "gui-home"], f"wifi: read back in order ({keys})")

    # config.txt: one more line
    ed, page = _open_editor(win, "config-txt")
    if ed:
        buf = page.view.get_buffer()
        buf.set_text(ed.text + "dtoverlay=gui-test\n")
        wait(0.2)
        check(ed.dirty and "+dtoverlay=gui-test" in ed.describe_changes(), "config-txt: the change is seen")
        shot(win, "config-txt-changed")
        if _commit(win, page, "config-txt"):
            check("dtoverlay=gui-test" in ed.text, "config-txt: read back")

    # cmdline: a parameter added, one removed
    ed, page = _open_editor(win, "cmdline")
    if ed:
        page.add_dialog()
        wait(0.3)
        _d, form, add = page.add_form
        form.set_value("parameter", "gui.test=1")
        add()
        wait(0.2)
        quiet = next((i for i in ed.items if i.get("parameter") == "quiet"), None)
        if quiet is not None:
            page.remove(quiet.key)
        shot(win, "cmdline-changed")
        if _commit(win, page, "cmdline"):
            words = [i.get("parameter") for i in ed.items]
            check("gui.test=1" in words and "quiet" not in words, f"cmdline: read back ({words})")

    # overlay changes: listed; Commit asks, and we say no (it would write the base image)
    if OVERLAY:
        ed, page = _open_editor(win, "changes")
        if ed:
            check(len(ed.items) > 0 and page.commit_btn.get_sensitive(), "changes: the overlay's files are listed")
            shot(win, "changes")
            win.editor_commit(page)
            wait(0.3)
            d = dialogs(win)
            check(isinstance(d, Adw.AlertDialog), "changes: commit asks first")
            shot(win, "changes-confirm")
            if isinstance(d, Adw.AlertDialog):
                _respond(d, "cancel")
                wait(0.3)
            check(dialogs(win) is None, "changes: cancelled, nothing written")
    win.close()


def scenario_hosted(app):
    """What works without a target: the images folder editor and the preferences."""
    import tempfile
    from piimg.gui.app import MainWindow
    cfg = tempfile.mkdtemp(prefix="piimg-cfg-", dir=OUT)
    images = os.path.join(OUT, "images")
    os.makedirs(images, exist_ok=True)
    for n in ("keep.img", "drop.img"):
        with open(os.path.join(images, n), "wb") as f:
            f.write(b"\0" * 4096)
    os.environ["XDG_CONFIG_HOME"] = cfg
    from piimg import config
    config.set_value("images_dir", images)
    win = MainWindow(app, None, None)
    win.present()
    wait(0.8)
    if dialogs(win) is not None:
        dialogs(win).close()
        wait(0.3)
    ed, page = _open_editor(win, "images")
    if ed:
        check(set(ed.by_key) >= {"keep.img", "drop.img"}, f"images: listed ({list(ed.by_key)})")
        page.remove("drop.img")
        wait(0.2)
        check("removed" in page.rows["drop.img"].get_css_classes(), "images: marked for deletion")
        shot(win, "images-changed")
        if _commit(win, page, "images"):
            check(not os.path.exists(os.path.join(images, "drop.img"))
                  and os.path.exists(os.path.join(images, "keep.img")), "images: only drop.img deleted")
    # preferences
    d = app.preferences()
    wait(until=lambda: d.rows, timeout=60)
    check("images_dir" in d.rows and d.rows["images_dir"].get_text() == images, "preferences: settings shown")
    shot(win, "preferences")
    d.set_value("catalog_max_age_hours", "48")
    wait(until=lambda: config.get("catalog_max_age_hours") == "48", timeout=60)
    check(config.get("catalog_max_age_hours") == "48", "preferences: a setting is saved")
    d.unset("catalog_max_age_hours")
    wait(until=lambda: config.get("catalog_max_age_hours") == "24", timeout=60)
    check(config.get("catalog_max_age_hours") == "24", "preferences: back to the default")
    d.close()
    win.close()


def main():
    app = Adw.Application(application_id="io.github.un1tz3r0.piimg.test")
    from piimg.gui.app import PiImgApplication
    # reuse the real application's startup (CSS, actions, accelerators)
    app = PiImgApplication()

    def activate(_a):
        Gtk.Settings.get_default().set_property("gtk-enable-animations", False)
        try:
            globals()["scenario_" + SCENARIO](app)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
            failures.append("exception")
        app.quit()
    app.connect("activate", activate)
    PiImgApplication.do_activate = lambda self: None
    app.run([sys.argv[0]])
    print("FAILURES:", failures if failures else "none", flush=True)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
