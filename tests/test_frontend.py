"""The toolkit-neutral front-end models (piimg.frontend): no Textual, no GTK."""
import json
import os
import signal
import subprocess
import sys
import textwrap

import pytest

from piimg import config
from piimg.frontend import commands as C
from piimg.frontend import proc as P
from piimg.frontend import recipe as M
from piimg.frontend import target as T
from piimg.frontend.fields import FieldDef, from_text, missing_required, to_text

RECIPE = """\
# keep me
name: t
vars:
  a: {default: 1, help: an a}
steps:
  - echo: "one {{a}}"
  - echo: two
  - when: {exists: /x}
    then:
      - remove: /y
  - remove: /z
"""


@pytest.fixture
def model(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text(RECIPE)
    return M.RecipeModel(p)


def steps(m):
    return [M.RS.step_action(s) + ":" + str(s[M.RS.step_action(s)])[:10] for s in m.doc["steps"]]


# ------------------------------------------------------------ imports

def test_frontend_and_gui_dont_import_the_other_toolkit():
    code = textwrap.dedent("""
        import sys
        import piimg.frontend.commands, piimg.frontend.recipe, piimg.frontend.proc, piimg.frontend.target
        assert "textual" not in sys.modules and "gi" not in sys.modules, "frontend pulled in a toolkit"
        import piimg.tui.app, piimg.tui.recipe_editor
        assert "gi" not in sys.modules, "the TUI imported gi"
        print("ok")
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    gui = textwrap.dedent("""
        import sys
        import piimg.gui as G
        try:
            G.require()
        except Exception:
            print("skip"); raise SystemExit(0)
        import piimg.gui.app, piimg.gui.recipe
        assert "textual" not in sys.modules, "the GUI imported textual"
        print("ok")
    """)
    r = subprocess.run([sys.executable, "-c", gui], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# ------------------------------------------------------------ fields / commands

def test_field_text_round_trip():
    f = FieldDef("k", "k", "map")
    assert from_text(f, to_text(f, {"a": "1", "b": True})) == {"a": "1", "b": "true"}
    f = FieldDef("k", "k", "lines")
    assert from_text(f, to_text(f, ["x", "y"])) == ["x", "y"]
    req = FieldDef("r", "r", required=True)
    assert missing_required([req], {"r": ""}) is req and missing_required([req], {"r": "x"}) is None


def test_visible_hides_overlay_only_commands_and_filters():
    cmds = C.load_commands()
    names = [c.name for _, c in C.visible(cmds)]
    assert "commit" not in names and "info" in names
    assert "commit" in [c.name for _, c in C.visible(cmds, has_overlay=True)]
    assert [c.name for _, c in C.visible(cmds, "passwd")] == ["passwd"]
    assert not any(c.name in ("tui", "gui", "recipe-edit") for c in cmds)


def test_confirmation_typed_and_yes_flag(monkeypatch):
    burn = next(c for c in C.load_commands() if c.name == "burn")
    monkeypatch.setattr(burn.meta, "typed_confirm", lambda a: "sdz")
    argv, _ = C.build_argv(burn, {"device": "/dev/sdz"}, "img")
    conf = C.confirmation(burn, argv, C.parse(argv))
    assert conf.needed and conf.typed == "sdz" and "--yes" in conf.argv
    assert any("ERASES" in m for m in conf.messages)


def test_confirmation_for_a_direct_card():
    hn = next(c for c in C.load_commands() if c.name == "hostname")
    argv, _ = C.build_argv(hn, {"name": "x"}, "/dev/sdz")
    conf = C.confirmation(hn, argv, C.parse(argv), direct_card="/dev/sdz")
    assert conf.needed and "directly" in conf.messages[0]


def test_recipe_var_fields_for_a_bundled_recipe():
    fields, note = C.recipe_var_fields("pitft")
    assert note == "" or not fields
    assert C.recipe_var_fields("")[1].startswith("(choose")
    assert C.var_args({"a": "1", "b": ""}) == ["a=1"]


# ------------------------------------------------------------ proc

def test_events_and_tables():
    from piimg.progress import EVENT_PREFIX
    ev = {"event": "end", "id": 1, "description": "Copying", "ok": False, "elapsed": 3.0}
    line = EVENT_PREFIX + json.dumps(ev)
    assert P.is_event(line) and P.parse_event(line) == ev
    assert P.parse_event(EVENT_PREFIX + "{bad") is None
    assert "failed" in P.end_line(ev).plain
    assert P.end_line({"ok": True, "elapsed": 0.2}) is None
    assert P.first_table(["x", '{"table": {"columns": ["a"], "rows": []}}'])["columns"] == ["a"]
    assert P.finished(0, 1)[1] == "ok" and P.finished(130, 1)[1] == "stopped" and P.finished(2, 1)[1] == "failed"
    assert P.last_error(["x", "piimg: boom"]) == "boom"


def test_root_command_goes_through_pkexec_and_the_supervisor(monkeypatch):
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    monkeypatch.setattr(P.shutil, "which", lambda name: f"/usr/bin/{name}")
    cmd = P.piimg_command(["info", "/dev/sdz"], root=True, env={"PIIMG_COLOR": "1", "SECRET": "x"}, cwd="/w")
    assert cmd[:4] == ["/usr/bin/pkexec", "/usr/bin/env", "-C", "/w"]
    assert "PIIMG_COLOR=1" in cmd and not any(c.startswith("SECRET") for c in cmd)
    assert "piimg.frontend.supervise" in cmd and cmd[-2:] == ["info", "/dev/sdz"]
    term = P.piimg_command(["shell", "x"], root=True, json_mode=False, supervise=False)
    assert "piimg.frontend.supervise" not in term and "--json" not in term
    assert P.piimg_command(["info", "x"])[1:3] == ["-m", "piimg"]


def test_supervisor_passes_stdin_and_signals():
    from piimg.frontend.supervise import encode
    script = ("import sys, signal, time\n"
              "print('got', sys.stdin.readline().strip(), flush=True)\n"
              "time.sleep(30)\n")
    p = subprocess.Popen([sys.executable, "-m", "piimg.frontend.supervise", sys.executable, "-c", script],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=False)
    p.stdin.write(P.stdin_orders("secret\n"))
    p.stdin.flush()
    assert p.stdout.readline().strip() == b"got secret"
    p.stdin.write(P.stop_order(signal.SIGINT))
    p.stdin.flush()
    assert p.wait(10) == 130  # killed by SIGINT: 128 + 2, which the run dialog calls "stopped"
    # the front end going away stops the command too
    p = subprocess.Popen([sys.executable, "-m", "piimg.frontend.supervise", sys.executable, "-c",
                          "import time; time.sleep(30)"], stdin=subprocess.PIPE)
    p.stdin.write(encode(close=True))
    p.stdin.close()
    assert p.wait(10) != 0


def test_invoking_user_under_pkexec(monkeypatch):
    import pwd
    me = pwd.getpwuid(os.getuid())
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    for v in ("SUDO_USER", "SUDO_UID"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("PKEXEC_UID", str(me.pw_uid))
    assert config.invoking_user().pw_uid == me.pw_uid
    assert str(config.user_home()) == me.pw_dir


def test_target_helpers(tmp_path):
    (tmp_path / "a.img").write_bytes(b"x")
    (tmp_path / "b.qcow2").write_bytes(b"x")
    (tmp_path / "c.txt").write_bytes(b"x")
    found, _ = T.candidates([tmp_path])
    files = [(c.name, c.kind) for c in found if c.kind != "disk"]
    assert files == [("a.img", "image"), ("b.qcow2", "overlay")]
    assert T.suggest_overlay("/dev/sdc") == "sdc-card-changes.qcow2"
    assert T.can_open(str(tmp_path / "a.img"), True)


# ------------------------------------------------------------ the recipe model

def test_model_rows_and_marks(model):
    assert [r.kind for r in model.rows] == ["meta", "step", "step", "step", "branch", "step", "branch", "step"]
    model.mark_range(1, 2)
    assert len(model.selection(1)) == 2
    model.clear_marks()
    assert [r.where for r in model.selection(2)] == ["steps[2]"]


def test_model_operations_and_undo(model):
    i, _ = model.move(2, -1)
    assert model.rows[i].step["echo"] == "two" and model.dirty
    assert model.undo() and model.doc["steps"][0]["echo"] == "one {{a}}"
    assert model.redo() and model.doc["steps"][0]["echo"] == "two"
    model.undo()
    i, msg = model.insert_new(1, "mkdir")
    assert model.rows[i].where == "steps[2]" and "mkdir" in model.doc["steps"][1]
    model.undo()
    model.set_marks([1, 2])
    i, msg = model.duplicate(1)
    assert len(model.doc["steps"]) == 6 and "duplicated 2" in msg
    model.undo()
    model.copy(5)                   # the remove inside then:
    i, _ = model.paste(7)           # after the last step
    assert model.doc["steps"][-1] == {"remove": "/y"}
    i, _ = model.cut(1)
    assert model.doc["steps"][0]["echo"] == "two"
    # insert on a branch row goes to the start of that branch
    branch = next(j for j, r in enumerate(model.rows) if r.kind == "branch")
    model.insert_new(branch, "echo")
    assert M.RS.step_action(model.doc["steps"][1]["then"][0]) == "echo"
    # moving out of a block is refused, with a reason
    j = next(j for j, r in enumerate(model.rows) if r.where == "steps[2].then[1]")
    assert model.move(j, -1)[1]


def test_model_field_edits_group_undo(model):
    r = model.rows[2]
    pane = model.pane(r)
    vals = {f.key: f.value for f in pane.fields}
    vals["arg:message"] = "tw"
    model.apply_field(r, "arg:message", vals)
    vals["arg:message"] = "twelve"
    model.apply_field(r, "arg:message", vals)
    assert model.doc["steps"][1]["echo"] == "twelve"
    assert len(model.undo_stack) == 1           # one undo step per field
    model.undo()
    assert model.doc["steps"][1]["echo"] == "two"
    meta = model.rows[0]
    with pytest.raises(ValueError):
        model.apply_field(meta, "vars", {"vars": "[1, 2]", "name": "t"})
    model.apply_field(meta, "vars", {"vars": "b: {}", "name": "t"})
    assert "undeclared" in " ".join(model.rows[1].issues)


def test_model_yaml_save_and_run(model, tmp_path):
    r, title, text = model.yaml_target(1)
    assert "echo" in text
    with pytest.raises(ValueError):
        model.replace_yaml(r, "- not a mapping")
    i = model.replace_yaml(r, "echo: replaced\n")
    assert model.rows[i].step["echo"] == "replaced"
    note = model.save()
    assert note is None and not model.dirty
    assert "# keep me" in model.path.read_text() and "replaced" in model.path.read_text()
    tmp = model.run_file(1, selected_only=True)
    try:
        assert M.parse_yaml(open(tmp).read())["steps"] == [{"echo": "replaced"}]
        assert os.path.dirname(tmp) == str(model.path.parent)
    finally:
        os.unlink(tmp)
    argv = model.run_argv("img", "r.yaml", True, {"a": "2"})
    assert argv == ["run", "--dry-run", "--var=a=2", "--", "img", "r.yaml"]
    i = model.run_finished(1, ["piimg: steps[3].then[1]: boom"], selected_only=False)
    assert model.rows[i].where == "steps[3].then[1]" and model.error_where == "steps[3].then[1]"


def test_template_saves_elsewhere(tmp_path):
    t = M.find_template("pitft")
    assert t is not None
    m = M.RecipeModel(template=t)
    assert m.path is None and "template" in m.title()
    note = m.save(tmp_path / "mine.yaml")
    assert (tmp_path / "mine.yaml").exists() and m.template is None
    assert note is None or "relative" in note


def test_needs_root_only_for_cards_we_cannot_open(monkeypatch):
    monkeypatch.setattr(T, "can_open", lambda p, w: not (p.startswith("/dev/") and w))
    assert not P.needs_root([("/x/a.img", True), (None, True)])
    assert not P.needs_root([("/dev/sdz", False)])
    assert P.needs_root([("/x/a.img", False), ("/dev/sdz", True)])


def test_desktop_entry(monkeypatch, tmp_path):
    from piimg import gui
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    msg = gui.install_desktop()
    p = tmp_path / "applications" / f"{gui.APP_ID}.desktop"
    assert p.exists() and str(p) in msg
    text = p.read_text()
    assert f"Exec={sys.executable} -m piimg gui %f" in text
    gui.install_desktop(remove=True)
    assert not p.exists()


def test_var_conditions_count_as_use():
    assert M.used_vars({"when": {"var": {"name": "display", "equals": "22"}}, "echo": "{{x}}"}) == {"display", "x"}
    m = M.RecipeModel(template=M.find_template("pitft"))
    assert "Declared but not used" not in m.pane(m.rows[0]).note.plain


# ---------------------------------------------------------------- editors

from piimg.frontend import editors as E  # noqa: E402

SERVICES = {"columns": ["unit", "state", "wanted by", "unit file", "origin", "description"],
            "rows": [["ssh.service", "disabled", "", "/usr/lib/systemd/system/ssh.service", "package", "SSH"],
                     ["cron.service", "enabled", "multi-user.target", "/usr/lib/systemd/system/cron.service",
                      "package", "Cron"],
                     ["dbus.service", "static", "", "/usr/lib/systemd/system/dbus.service", "package", "D-Bus"],
                     ["foo.service", "masked", "", "/dev/null", "local", ""],
                     ["mine.service", "enabled", "multi-user.target", "/etc/systemd/system/mine.service",
                      "local", "Mine"],
                     ["gone.service", "BROKEN (no unit file)", "multi-user.target", "", "package", ""]],
            "footer": "default target: graphical.target"}


@pytest.fixture
def services():
    ed = E.ServicesEditor()
    ed.load(SERVICES)
    return ed


def test_editor_replaces_its_commands_in_the_list():
    cmds = C.load_commands()
    eds = E.make_editors()
    out = E.list_entries(cmds, eds)
    names = [c.name for c in out]
    assert "service" not in names and names.count("services") == 1
    ed = out[names.index("services")]
    assert ed is eds[0] and ed.meta.group == "Repair & configure"
    # it sits among its own group, not in the middle of another
    groups = [c.meta.group for c in out]
    assert groups == sorted(groups, key=lambda g: groups.index(g))


def test_services_choices_follow_what_systemd_allows(services):
    ch = {i.key: services.choices(i, "state") for i in services.items}
    assert ch["ssh.service"] == ["enabled", "disabled", "masked"]
    assert ch["dbus.service"] == ["static", "masked"]
    assert ch["foo.service"] == ["masked", "unmasked", "enabled"]
    assert "masked" not in ch["mine.service"]          # its own file is in /etc/systemd/system
    assert ch["gone.service"][0] == "broken"
    with pytest.raises(ValueError):
        services.set("dbus.service", "state", "enabled")


def test_services_changes_become_one_systemd_step(services, tmp_path):
    assert not services.dirty and services.steps() == []
    services.set("ssh.service", "state", "enabled")
    services.set("cron.service", "state", "masked")
    services.set("foo.service", "state", "enabled")
    services.set("dbus.service", "state", "masked")
    assert services.dirty and services.count_text() == "6 units, 4 changed"
    assert "ssh.service: state disabled → enabled" in services.describe_changes()
    (step,) = services.steps()
    assert step == {"systemd": {"unmask": ["foo.service"], "disable": ["cron.service"],
                                "mask": ["cron.service", "dbus.service"],
                                "enable": ["ssh.service", "foo.service"]}}
    # the recipe is one the runner and the recipe editor accept
    argv, text = services.commit("/x/w.qcow2")
    assert argv == ["run", "--", "/x/w.qcow2", E.RECIPE]
    path = services.write_recipe(text, "services")
    assert os.stat(path).st_mode & 0o077 == 0     # it may hold a wifi password
    try:
        m = M.RecipeModel(path)
        steps = [r for r in m.rows if r.kind == "step"]
        assert m.problems() == [] and len(steps) == 1 and "systemd" in steps[0].step
    finally:
        os.unlink(path)
    assert E.Editor.commit_argv("/x/w.qcow2", path, dry=True) == ["run", "--dry-run", "--", "/x/w.qcow2", path]
    C.parse(E.Editor.commit_argv("/x/w.qcow2", path))
    services.revert("ssh.service")
    assert not services.by_key["ssh.service"].changed() and services.dirty
    services.revert()
    assert not services.dirty


def test_services_filters(services):
    shown = lambda f, n="": [i.key for i in services.items if services.matches(i, n, f)]  # noqa: E731
    assert "dbus.service" not in shown("startable") and "dbus.service" in shown("all")
    assert shown("masked") == ["foo.service"]
    assert shown("all", "cron") == ["cron.service"]
    services.set("dbus.service", "state", "masked")
    assert "dbus.service" in shown("startable") and shown("changed") == ["dbus.service"]


def _table(cols, rows, footer=None):
    return {"columns": cols, "rows": rows, "footer": footer}


def _valid_recipe(ed):
    path = ed.write_recipe(ed.recipe_text(), ed.name)
    try:
        m = M.RecipeModel(path)
        return m.problems() == [] and [r.step for r in m.rows if r.kind == "step"]
    finally:
        os.unlink(path)


def test_list_entries_hides_and_places_every_editor():
    cmds = C.load_commands()
    out = E.list_entries(cmds, E.make_editors(), hide=("config",))
    names = [c.name for c in out]
    for gone in ("passwd", "ssh-key", "service", "hostname", "boot-mode", "settings", "images local",
                 "images delete", "diff", "commit", "config"):
        assert gone not in names, gone
    for ed in ("users", "system", "services", "wifi", "config-txt", "cmdline", "images", "changes"):
        assert names.count(ed) == 1, ed
    groups = [c.meta.group for c in out]
    assert groups == sorted(groups, key=groups.index)          # no group is split
    changes = out[names.index("changes")]
    assert [n for _g, c in C.visible(out, "", False) for n in [c.name]].count("changes") == 0
    assert changes in [c for _g, c in C.visible(out, "", True)]


def test_users_editor(monkeypatch):
    ed = E.UsersEditor()
    ed.load(_table(["user", "uid", "shell", "password", "ssh keys", "groups", "note", "keys"],
                   [["root", "0", "/bin/bash", "none", "", "", "", ""],
                    ["pi", "1000", "/usr/sbin/nologin", "locked", "1", "sudo", "", "ssh-ed25519 AAAA old@x"]]))
    root, pi = ed.by_key["root"], ed.by_key["pi"]
    assert not ed.editable(root, "name") and not ed.removable(root) and ed.removable(pi)
    assert pi.get("locked") == "on" and pi.get("new password") == ""
    ed.set("pi", "new password", "s3cret")
    ed.set("pi", "locked", "off")
    ed.set("pi", "shell", "/bin/bash")
    ed.set("pi", "keys", "ssh-ed25519 AAAA new@y\n")
    ed.set("pi", "name", "alice")
    assert ed.problems() == []
    text = "\n".join(ed.describe_changes())
    assert "s3cret" not in text and "new password" in text and "rename pi → alice" in text
    import piimg.recipes.userdata as U
    monkeypatch.setattr(U, "hash_password", lambda pw: "$6$salt$hash")
    (step,) = ed.steps()
    assert step == {"user": {"name": "pi", "password_hash": "$6$salt$hash", "unlock": True,
                             "shell": "/bin/bash", "remove_keys": ["ssh-ed25519 AAAA old@x"],
                             "add_keys": ["ssh-ed25519 AAAA new@y"], "rename": "alice"}}
    assert "s3cret" not in ed.recipe_text()
    ed.set("pi", "name", "Bad Name")
    assert ed.problems()
    ed.revert()
    ed.remove("pi")
    assert ed.steps() == [{"user": {"name": "pi", "delete": True}}]
    assert _valid_recipe(ed)


def test_system_editor():
    ed = E.SystemEditor()
    ed.load(_table(["setting", "value", "label", "kind", "help"],
                   [["hostname", "raspberrypi", "Hostname", "text", ""],
                    ["boot mode", "desktop", "Boot into", "choice", ""],
                    ["ssh", "off", "SSH server", "switch", ""],
                    ["wifi country", "", "Wi-Fi country", "text", ""]]))
    assert ed.kind(ed.by_key["ssh"], "value") == "switch" and ed.kind(ed.by_key["boot mode"], "value") == "choice"
    assert "console-autologin" in ed.choices(ed.by_key["boot mode"], "value")
    with pytest.raises(ValueError):
        ed.set("boot mode", "value", "nonsense")
    ed.set("hostname", "value", "kiosk-1")
    ed.set("boot mode", "value", "console-autologin")
    ed.set("ssh", "value", "on")
    ed.set("wifi country", "value", "gb")
    assert ed.problems() == []
    (step,) = ed.steps()
    assert step == {"raspi_config": ["do_hostname kiosk-1", "do_boot_behaviour B2", "do_ssh 0",
                                     "do_wifi_country GB"]}
    ed.set("hostname", "value", "not valid!")
    assert ed.problems()
    ed.set("hostname", "value", "ok")
    assert _valid_recipe(ed)


def test_wifi_editor_order_add_remove():
    ed = E.WifiEditor()
    cols = ["connection", "type", "ssid / interface", "autoconnect", "file", "priority", "hidden", "security"]
    ed.load(_table(cols, [["home", "wifi", "home", "true", "/x/home.nmconnection", "10", "false", "wpa-psk"],
                          ["work", "wifi", "work", "true", "/x/work.nmconnection", "20", "false", "wpa-psk"],
                          ["cafe", "wifi", "cafe", "false", "/x/cafe.nmconnection", "0", "false", "none"]]))
    assert [i.key for i in ed.items] == ["work", "home", "cafe"]      # highest priority first
    assert not ed.dirty
    ed.move("cafe", -1)
    assert ed.order_changed and ed.dirty
    ed.move("cafe", 1)
    assert not ed.dirty
    with pytest.raises(ValueError):
        ed.add({"ssid": "x", "password": "short"})
    new = ed.add({"ssid": "phone", "password": "longenough", "hidden": True})
    ed.move(new.key, -3)
    ed.remove("home")
    ed.set("cafe", "autoconnect", "on")
    (step,) = ed.steps()
    w = step["wifi"]
    assert w["remove"] == ["home"]
    assert w["add"] == [{"ssid": "phone", "priority": 30, "password": "longenough", "hidden": True}]
    assert "work" not in w["set"]                               # still priority 20
    assert w["set"]["cafe"] == {"priority": 10, "autoconnect": True}
    assert all("longenough" not in l for l in ed.describe_changes())
    assert _valid_recipe(ed)
    ed.revert()
    assert not ed.dirty and [i.key for i in ed.items] == ["work", "home", "cafe"]


def test_text_and_cmdline_editors():
    ed = E.ConfigTxtEditor()
    ed.load_output(["# hi", "dtparam=audio=on"])
    assert not ed.dirty
    ed.text = "# hi\ndtparam=audio=on\ndtoverlay=dwc2\n"
    assert ed.dirty and ed.describe_changes() == ["+dtoverlay=dwc2"]
    assert ed.steps() == [{"config_txt": {"content": ed.text}}]
    assert _valid_recipe(ed)
    ed.revert()
    assert not ed.dirty

    cl = E.CmdlineEditor()
    cl.load_output(["console=serial0,115200 console=tty1 root=PARTUUID=x rootwait quiet"])
    assert [cl.label(i) for i in cl.items] == ["console", "console", "root", "rootwait", "quiet"]
    cl.remove("p4")
    cl.set("p3", "parameter", "rootwait")
    new = cl.add({"parameter": "modules-load=dwc2"})
    cl.move(new.key, -1)    # past the removed quiet
    cl.move(new.key, -1)
    assert cl.line() == "console=serial0,115200 console=tty1 root=PARTUUID=x modules-load=dwc2 rootwait"
    assert cl.steps() == [{"cmdline": {"set": cl.line()}}]
    with pytest.raises(ValueError):
        cl.add({"parameter": "two words"})
    cl.remove("p2")
    assert cl.problems() and "root=" in cl.problems()[0]
    cl.revert()
    assert not cl.dirty and len(cl.items) == 5
    assert _valid_recipe(cl)


def test_images_and_overlay_editors():
    ed = E.ImagesEditor()
    assert ed.query_argv(None) == ["images", "local"] and ed.meta.target is None
    ed.load(_table(["file", "size", "release", "image", "verified", "note"],
                   [["a.img.xz", "1G", "", "", "yes", ""], ["b.img", "3G", "", "", "no", ""]]))
    ed.remove("b.img")
    assert ed.commit(None) == (["images", "delete", "--", "b.img"], None)
    C.parse(ed.commit(None)[0])

    ov = E.OverlayEditor()
    ov.load_output(["- l 0777         58 /etc/a.wants/x.service -> /usr/lib/x.service",
                    "+ l 0777         58 /etc/a.wants/x.service -> /usr/lib/x.service",
                    "# changed: st_mtime_sec",
                    "+ - 0644         12 /etc/hostname",
                    "- d 0755       4096 /home/old"])
    got = {i.key: i.get("change") for i in ov.items}
    assert got == {"/etc/a.wants/x.service": "changed", "/etc/hostname": "added", "/home/old": "removed"}
    assert not ov.dirty and ov.commit("/x/o.qcow2") == (["commit", "--", "/x/o.qcow2"], None)
    assert [i.key for i in ov.items if ov.matches(i, "", "added")] == ["/etc/hostname"]
