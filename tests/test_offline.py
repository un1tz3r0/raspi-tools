"""Fast tests: no libguestfs, no images, no devices. Run: uv run pytest -q"""
import json
import os

import pytest

from piimg import output, progress, recipe_schema as RS
from piimg.cli import iter_commands
from piimg.script import Runner
from piimg.tui import commands as C
from piimg.tui import recipe_editor as E


# ------------------------------------------------------------ recipe schema

def test_every_runner_action_has_a_schema():
    ops = {n[3:] for n in dir(Runner) if n.startswith("op_")}
    assert ops == set(RS.ACTIONS)


@pytest.mark.parametrize("act", sorted(RS.ACTIONS))
def test_new_step_round_trips_through_form_values(act):
    step = RS.new_step(act)
    assert RS.step_action(step) == act
    values = RS.arg_to_values(act, step[act])
    assert isinstance(values, dict)


def test_values_to_arg_uses_short_forms():
    assert RS.values_to_arg("remove", {"path": "/x"}) == "/x"
    assert RS.values_to_arg("modules", {"add": ["a", "b"]}) == ["a", "b"]
    assert RS.values_to_arg("mkdir", {"path": "/x", "mode": "0755", "owner": ""}) == {"path": "/x", "mode": "0755"}


# ------------------------------------------------------------ recipe editor model

DOC = """\
# a comment that must survive
name: t
vars:
  a: {default: 1}
steps:
  - echo: "x {{a}}"
  - when: {exists: /x}
    then:
      - remove: /y
    else: []
  - write: {path: /z, content: "{{literal}}"}   # trailing comment
"""


@pytest.fixture
def doc(tmp_path):
    p = tmp_path / "r.yaml"
    p.write_text(DOC)
    return E.load_doc(p)


def test_flatten_matches_runner_locations(doc):
    rows = E.flatten(doc)
    assert [r.kind for r in rows] == ["meta", "step", "step", "branch", "step", "branch", "step"]
    assert [r.where for r in rows if r.kind == "step"] == ["steps[1]", "steps[2]", "steps[2].then[1]", "steps[3]"]


def test_round_trip_keeps_comments(doc):
    text = E.dump(doc)
    assert "# a comment that must survive" in text
    assert "# trailing comment" in text


def test_checks(doc):
    rows = E.flatten(doc)
    assert all(not E.check_row(r, ["a"]) for r in rows)       # write content is literal: no var check
    rows[1].step["echo"] = "{{nope}}"
    assert "undeclared" in E.check_row(rows[1], ["a"])[0]
    assert E.check_row(E.Row("step", step={"bogus": 1}), []) == ["unknown action 'bogus'"]
    assert E.check_row(E.Row("step", step={"echo": 1, "remove": 2}), [])


def test_convert_field_values():
    f = RS.Field("x", "bool", default=True)
    assert E._convert(f, True) is None and E._convert(f, False) is False
    assert E._convert(RS.Field("x", "map"), {"iface": "usb0", "flag": "true", "n": "3"}) == \
        {"iface": "usb0", "flag": True, "n": 3}
    assert E._convert(RS.Field("x", "text"), "a\nb") == "a\nb\n"
    assert E._convert(RS.Field("x", "int"), "") is None


# ------------------------------------------------------------ cli <-> tui

def test_every_command_has_meta_and_a_handler():
    for words, p in iter_commands():
        assert hasattr(p, "piimg"), words
        assert p.get_default("func") is not None, words


def test_argv_round_trip_for_every_tui_command():
    for c in C.load_commands():
        values = {}
        for f in C.fields_for(c):
            if f.required:
                values[f.key] = f.choices[0] if f.choices else ("x" if f.type != "lines" else ["x"])
        argv, _ = C.build_argv(c, values, "img" if c.meta.target else None)
        ns = C.parse(argv)
        assert ns.func is c.meta.func or ns.func is not None, c.name


def test_secret_goes_to_stdin():
    c = next(c for c in C.load_commands() if c.name == "passwd")
    argv, stdin = C.build_argv(c, {"user": "pi", "password": "pw"}, "img")
    assert "pw" not in " ".join(argv) and stdin == "pw\n" and "--password-stdin" in argv


def test_mutually_exclusive_reported():
    with pytest.raises(C.ArgError):
        C.parse(["passwd", "--lock", "--unlock", "--", "img", "pi"])


def test_writes_depends_on_args():
    fsck = next(c for c in C.load_commands() if c.name == "fsck")
    assert not fsck.meta.writes_for(C.parse(["fsck", "--", "img"]))
    assert fsck.meta.writes_for(C.parse(["fsck", "--repair", "--", "img"]))


# ------------------------------------------------------------ output / progress

def test_table_plain_and_json(capsys):
    t = output.Table(["a", "status"], [["x", "ok"]], title="T", footer="f")
    output.emit(t, as_json=True)
    assert json.loads(capsys.readouterr().out)["table"]["rows"] == [["x", "ok"]]
    assert "status" in t.plain() and t.plain().endswith("f")


def test_json_progress_events(capsys):
    with progress.session("json"):
        with progress.task("step", total=10) as t:
            t.update(5)
    err = capsys.readouterr().err.splitlines()
    events = [json.loads(l[len(progress.EVENT_PREFIX):]) for l in err if l.startswith(progress.EVENT_PREFIX)]
    assert events[0]["event"] == "begin" and events[-1]["event"] == "end" and events[-1]["ok"]


# ------------------------------------------------------------ disks (no real devices)

def test_claim_refuses_disk_in_use(monkeypatch):
    from piimg import disks
    d = disks.Disk("sdz", "/dev/sdz", 1 << 30, True, False, "usb", "x", mounts=[("/dev/sdz2", "/")])
    monkeypatch.setattr(disks, "find_disk", lambda p: d)
    with pytest.raises(disks.PiImgError, match="refusing to modify"):
        with disks.claim("/dev/sdz"):
            pass


def test_claim_unmounts_automounts_and_locks(monkeypatch, tmp_path):
    from piimg import disks
    card = tmp_path / "card"
    card.write_bytes(b"\0" * 4096)
    d = disks.Disk("sdz", str(card), 4096, True, False, "usb", "x", mounts=[("/dev/sdz1", "/media/u/bootfs")])
    unmounted = []
    monkeypatch.setattr(disks, "find_disk", lambda p: d)
    monkeypatch.setattr(disks, "unmount", lambda dev, mp: unmounted.append(mp))
    with disks.claim(str(card)) as c:
        assert unmounted == ["/media/u/bootfs"]
        with disks.claim(str(card)) as inner:   # re-entrant
            assert inner is c
        import fcntl
        fd = os.open(card, os.O_RDONLY)
        with pytest.raises(BlockingIOError):     # someone else cannot take the lock
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.close(fd)
    assert not disks._claims


def test_readonly_claim_needs_read_access(monkeypatch):
    from piimg import disks
    d = disks.Disk("sdz", "/dev/sdz", 1, True, False, "usb", "x")
    monkeypatch.setattr(disks, "find_disk", lambda p: d)
    monkeypatch.setattr(os, "access", lambda p, m: False)
    with pytest.raises(disks.PiImgError, match="needs root"):
        with disks.claim("/dev/sdz", readonly=True):
            pass


# ------------------------------------------------------------ official image catalog (no network)

CATALOG = {"os_list": [
    {"name": "Raspberry Pi OS (64-bit)", "release_date": "2026-10-06", "extract_sha256": "a" * 64,
     "url": "https://downloads.raspberrypi.com/raspios_arm64/images/raspios_arm64-2026-10-06/"
            "2026-10-06-raspios-trixie-arm64.img.xz", "image_download_size": 10, "extract_size": 20,
     "devices": ["pi4-64bit"]},
    {"name": "Raspberry Pi OS (other)", "subitems": [
        {"name": "Lite (64-bit)", "release_date": "2026-10-06",
         "url": "https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2026-10-06/"
                "2026-10-06-raspios-trixie-arm64-lite.img.xz"},
        {"name": "Legacy Lite (64-bit)", "release_date": "2026-10-06",
         "url": "https://downloads.raspberrypi.com/raspios_oldstable_lite_arm64/images/"
                "raspios_oldstable_lite_arm64-2026-10-06/2026-10-06-raspios-bookworm-arm64-lite.img.xz"}]},
    {"name": "Other", "subitems": [{"name": "Ubuntu", "url": "https://cdimage.ubuntu.com/x.img.xz"}]},
]}


@pytest.fixture
def catalog_env(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "conf"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    (tmp_path / "cache" / "piimg").mkdir(parents=True)
    (tmp_path / "cache" / "piimg" / "os_list.json").write_text(json.dumps(CATALOG))
    from piimg import config
    config.set_value("images_dir", str(tmp_path / "imgs"))
    return tmp_path


def test_catalog_flattens_and_marks_official(catalog_env):
    from piimg import catalog
    cat = catalog.fetch_catalog()
    assert [e["slug"] for e in cat if e["official"]] == [
        "2026-10-06-raspios-trixie-arm64", "2026-10-06-raspios-trixie-arm64-lite",
        "2026-10-06-raspios-bookworm-arm64-lite"]
    assert not [e for e in cat if "ubuntu" in e["url"]][0]["official"]


def test_catalog_find(catalog_env):
    from piimg import catalog
    from piimg.errors import PiImgError
    assert catalog.find("trixie-arm64-lite")["series"] == "raspios_lite_arm64"
    assert catalog.find("2026-10-06-raspios-trixie-arm64.img.xz")["extract_sha256"] == "a" * 64
    with pytest.raises(PiImgError, match="several"):
        catalog.find("arm64-lite")
    with pytest.raises(PiImgError, match="no official image"):
        catalog.find("ubuntu")


def test_guess_url_from_official_name():
    from piimg.catalog import _guess_url
    assert _guess_url("2025-05-13-raspios-bookworm-arm64-lite.img.xz") == (
        "https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2025-05-13/"
        "2025-05-13-raspios-bookworm-arm64-lite.img.xz")
    assert _guess_url("random.img.xz") is None


def test_local_path_stays_in_images_folder(catalog_env):
    from piimg import catalog
    from piimg.errors import PiImgError
    (catalog_env / "imgs").mkdir()
    (catalog_env / "imgs" / "a.img.xz").write_bytes(b"x")
    assert catalog._local_path("a.img.xz").name == "a.img.xz"
    with pytest.raises(PiImgError, match="only files in the images folder"):
        catalog._local_path("/etc/passwd")
    with pytest.raises(PiImgError, match="only files in the images folder"):
        catalog._local_path("../conf/x")
