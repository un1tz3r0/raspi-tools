"""The TUI driven headless with Textual's pilot: no images, no child commands."""
import asyncio

import pytest

pytest.importorskip("textual")

from piimg.frontend import proc as P  # noqa: E402
from piimg.tui.recipe_editor import RecipeEditorApp, RecipeEditorScreen  # noqa: E402

RECIPE = """\
name: t
vars:
  a: {default: 1}
steps:
  - echo: "one {{a}}"
  - echo: two
  - remove: /x
"""


def run(coro):
    return asyncio.run(coro)


def test_recipe_editor_keys(tmp_path):
    path = tmp_path / "r.yaml"
    path.write_text(RECIPE)

    async def go():
        app = RecipeEditorApp(path)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            ed = app.screen
            assert isinstance(ed, RecipeEditorScreen)
            assert [r.kind for r in ed.rows] == ["meta", "step", "step", "step"]
            await pilot.press("down", "down")          # on "echo: two"
            await pilot.press("alt+up")                # move it up
            assert ed.doc["steps"][0]["echo"] == "two"
            await pilot.press("ctrl+z")
            assert ed.doc["steps"][0]["echo"] == "one {{a}}"
            await pilot.press("space", "down", "space")  # mark two steps
            assert len(ed.marks) == 2
            await pilot.press("d")
            assert len(ed.doc["steps"]) == 1 and ed.dirty
            await pilot.press("ctrl+z")
            assert len(ed.doc["steps"]) == 3
            await pilot.press("c", "v")                # copy + paste after the cursor
            assert len(ed.doc["steps"]) == 4
            await pilot.press("a")                     # insert dialog
            await pilot.pause()
            await pilot.press("enter")                 # first entry: if
            await pilot.pause()
            assert any("when" in s and "then" in s for s in ed.doc["steps"])
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert not ed.dirty
            assert "then" in path.read_text()
    run(go())


def test_recipe_editor_form_edit(tmp_path):
    path = tmp_path / "r.yaml"
    path.write_text(RECIPE)

    async def go():
        app = RecipeEditorApp(path)
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            ed = app.screen
            await pilot.press("down", "down", "enter")  # into the form of "echo: two"
            await pilot.pause()
            await pilot.press("end", "!")
            await pilot.pause()
            assert ed.doc["steps"][1]["echo"] == "two!"
            await pilot.press("ctrl+z")
            assert ed.doc["steps"][1]["echo"] == "two"
    run(go())


def test_main_screen_lists_and_filters(tmp_path, monkeypatch):
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)

    async def fake_capture(argv, timeout=None, root=False):
        return 0, ['{"table": {"title": "t", "columns": ["item", "value"], "rows": [["os", "test"]]}}'], []
    monkeypatch.setattr(P, "run_capture", fake_capture)
    from piimg.tui.app import MainScreen, ParamsScreen, PiImgApp

    async def go():
        app = PiImgApp(str(img))
        async with app.run_test(size=(140, 40)) as pilot:
            await pilot.pause()
            await pilot.pause()
            assert isinstance(app.screen, MainScreen)
            main = app.screen
            assert any(c and c.name == "info" for c in main.shown_cmds)
            await pilot.press("slash", *"logs", "enter")
            names = [c.name for c in main.shown_cmds if c]
            assert names == ["logs"]
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ParamsScreen)
    run(go())



# ------------------------------------------------------------------ editors

SERVICES = {"columns": ["unit", "state", "wanted by", "origin", "description", "unit file"],
            "rows": [["ssh.service", "enabled", "multi-user.target", "debian", "OpenBSD Secure Shell server",
                      "/usr/lib/systemd/system/ssh.service"],
                     ["bluetooth.service", "enabled", "bluetooth.target", "debian", "Bluetooth service",
                      "/usr/lib/systemd/system/bluetooth.service"],
                     ["fake.service", "static", "", "debian", "static one", "/usr/lib/systemd/system/fake.service"]]}


class Fakes:
    """Stands in for the child commands: the editors' queries, and the run dialog."""

    def __init__(self, monkeypatch, outputs):
        self.outputs, self.queries, self.runs = outputs, [], []
        fakes = self

        async def capture(argv, timeout=None, root=False):
            fakes.queries.append(list(argv))
            for words, out in fakes.outputs.items():
                if tuple(argv[:len(words)]) == words:
                    return 0, out() if callable(out) else out, []
            return 0, ['{"table": {"title": "t", "columns": ["item", "value"], "rows": [["os", "test"]]}}'], []
        monkeypatch.setattr(P, "run_capture", capture)

        from textual.screen import ModalScreen
        from textual.widgets import Static
        import piimg.tui.run as R

        class FakeRun(ModalScreen):
            def __init__(self, title, argv, stdin=None, layout="log", abort="yes", cwd=None):
                super().__init__()
                recipe = next((a for a in argv if a.endswith(".yaml")), None)
                fakes.runs.append((title, list(argv), open(recipe).read() if recipe else None))

            def compose(self):
                yield Static("running")

            def on_mount(self):
                self.call_after_refresh(self.finish)

            def finish(self):
                self.dismiss(0)
        monkeypatch.setattr(R, "RunScreen", FakeRun)


def table(t):
    import json
    return [json.dumps({"table": t})]


async def settle(pilot, n=4):
    for _ in range(n):
        await pilot.pause()


async def open_editor(pilot, app, name):
    from piimg.tui.editor import EditorScreen
    await settle(pilot)
    main = app.screen
    await pilot.press("slash", *name, "enter")
    idx = next(i for i, c in enumerate(main.shown_cmds) if c and c.name == name)
    main.query_one("#commands").highlighted = idx
    await pilot.press("enter")
    await settle(pilot, 6)
    assert isinstance(app.screen, EditorScreen), app.screen
    return app.screen


def test_services_editor_guard_and_commit(tmp_path, monkeypatch):
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)
    fakes = Fakes(monkeypatch, {("services",): table(SERVICES)})
    from piimg.tui.app import MainScreen, PiImgApp
    from piimg.tui.editor import AskDiscardScreen
    from piimg.tui.widgets import ConfirmScreen

    async def go():
        app = PiImgApp(str(img))
        async with app.run_test(size=(150, 45)) as pilot:
            await settle(pilot)
            main = app.screen
            names = [c.name for c in main.shown_cmds if c]
            assert "services" in names and "service" not in names and "passwd" not in names
            ed_screen = await open_editor(pilot, app, "services")
            ed = ed_screen.ed
            assert ["services", "--all", "--", str(img)] in fakes.queries
            assert [i.key for i in ed_screen.shown] == ["ssh.service", "bluetooth.service"]  # static hidden
            await pilot.press("space")                 # ssh: enabled -> disabled
            assert ed.by_key["ssh.service"].get("state") == "disabled" and ed.dirty
            assert "changed" in str(ed_screen.query_one("#ed-head").render())
            # leaving asks; Discard drops the change
            await pilot.press("escape")
            await settle(pilot)
            assert isinstance(app.screen, AskDiscardScreen)
            app.screen.query_one("#discard").press()
            await settle(pilot)
            assert isinstance(app.screen, MainScreen) and not ed.dirty
            # back again: no new query (nothing changed the target)
            n = len(fakes.queries)
            ed_screen = await open_editor(pilot, app, "services")
            assert len(fakes.queries) == n
            # change, then commit: confirm, the recipe runs once, the editor reads again
            await pilot.press("down", "space")         # bluetooth: enabled -> disabled
            await pilot.press("ctrl+s")
            await settle(pilot)
            assert isinstance(app.screen, ConfirmScreen)
            app.screen.query_one("#ok").press()
            await settle(pilot, 8)
            title, argv, recipe = fakes.runs[-1]
            assert argv[:2] == ["run", "--"] and argv[2] == str(img) and title == "Commit: Services"
            assert "systemd" in recipe and "bluetooth.service" in recipe and "ssh.service" not in recipe
            assert not os.path.exists(argv[3])           # the recipe file is removed afterwards
            assert not ed.dirty and len(fakes.queries) > n
    import os
    run(go())


def test_config_txt_and_quit_guard(tmp_path, monkeypatch):
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)
    fakes = Fakes(monkeypatch, {("config-txt",): ["[all]", "dtparam=audio=on"]})
    from piimg.tui.app import PiImgApp
    from piimg.tui.editor import AskDiscardScreen

    async def go():
        app = PiImgApp(str(img))
        async with app.run_test(size=(150, 45)) as pilot:
            ed_screen = await open_editor(pilot, app, "config-txt")
            ed = ed_screen.ed
            assert ed.text == "[all]\ndtparam=audio=on\n"
            ta = ed_screen.query_one("#ed-text")
            ta.move_cursor(ta.document.end)
            await pilot.press(*"dtoverlay=vc4-kms-v3d")
            await settle(pilot)
            assert ed.dirty and "+dtoverlay=vc4-kms-v3d" in ed.describe_changes()
            await pilot.press("f5")                    # preview: a dry run, no questions
            await settle(pilot, 6)
            title, argv, recipe = fakes.runs[-1]
            assert argv[:2] == ["run", "--dry-run"] and "dtoverlay=vc4-kms-v3d" in recipe and ed.dirty
            await pilot.press("ctrl+q")                # quitting asks too
            await settle(pilot)
            assert isinstance(app.screen, AskDiscardScreen)
            app.screen.query_one("#cancel").press()
            await settle(pilot)
            assert app.screen is ed_screen and ed.dirty
    run(go())


def test_cmdline_add_move_remove_and_problems(tmp_path, monkeypatch):
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)
    fakes = Fakes(monkeypatch, {("cmdline",): ["console=serial0,115200 root=PARTUUID=ab-02 rootwait"]})
    from piimg.tui.app import PiImgApp
    from piimg.tui.editor import AddScreen

    async def go():
        app = PiImgApp(str(img))
        async with app.run_test(size=(150, 45)) as pilot:
            ed_screen = await open_editor(pilot, app, "cmdline")
            ed = ed_screen.ed
            await pilot.press("a")
            await settle(pilot)
            assert isinstance(app.screen, AddScreen)
            await pilot.press(*"quiet", "enter")
            await settle(pilot)
            assert ed.line() == "console=serial0,115200 quiet root=PARTUUID=ab-02 rootwait"
            await pilot.press("alt+down")
            assert ed.line() == "console=serial0,115200 root=PARTUUID=ab-02 quiet rootwait"
            await pilot.press("up", "d")                # remove root=
            await settle(pilot)
            assert ed.problems() and "Cannot commit" in str(ed_screen.query_one("#ed-status").render())
            await pilot.press("ctrl+s")                 # refused: no root=
            await settle(pilot)
            assert not fakes.runs
            await pilot.press("d")                      # keep it after all
            assert not ed.problems()
            # edit a parameter in place, in the form under the list
            ed_screen.query_one("#ed-table").move_cursor(row=0)
            await settle(pilot)
            await pilot.press("enter")
            await settle(pilot)
            await pilot.press("end", *",9600")
            await settle(pilot)
            assert ed.items[0].get("parameter") == "console=serial0,115200,9600"
            await pilot.press("escape")                 # form -> list
            await settle(pilot)
            assert app.screen.focused is ed_screen.query_one("#ed-table")
    run(go())


def test_users_editor_form(tmp_path, monkeypatch):
    img = tmp_path / "x.img"
    img.write_bytes(b"\0" * 1024)
    users = {"columns": ["user", "uid", "shell", "password", "ssh keys", "groups", "note", "keys"],
             "rows": [["root", "0", "/bin/bash", "locked", "0", "", "", ""],
                      ["pi", "1000", "/bin/bash", "set", "0", "sudo", "admin", ""]]}
    Fakes(monkeypatch, {("users",): table(users)})
    from piimg.tui.app import PiImgApp

    async def go():
        app = PiImgApp(str(img))
        async with app.run_test(size=(150, 50)) as pilot:
            ed_screen = await open_editor(pilot, app, "users")
            ed = ed_screen.ed
            await pilot.press("down", "enter")          # pi's form: its first field is the name
            await settle(pilot)
            await pilot.press("end", "2")
            await settle(pilot)
            assert ed.by_key["pi"].get("name") == "pi2"
            await pilot.press("down", *"secret1")       # new password
            await settle(pilot)
            assert ed.by_key["pi"].get("new password") == "secret1"
            changes = ed.describe_changes()
            assert "rename pi → pi2" in changes and "pi: new password" in changes
            assert all("secret1" not in c for c in changes)
            await pilot.press("escape", "up")           # root: no rename, no delete
            await settle(pilot)
            assert "name" not in [f.key for f in ed_screen.form.fields]
            await pilot.press("d")
            await settle(pilot)
            assert not ed.by_key["root"].removed
    run(go())
