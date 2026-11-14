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
            await pilot.press("slash", *"passwd", "enter")
            names = [c.name for c in main.shown_cmds if c]
            assert names == ["passwd"]
            await pilot.press("enter")
            await pilot.pause()
            assert isinstance(app.screen, ParamsScreen)
    run(go())
