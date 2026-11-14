"""piimg gui under a headless compositor (mutter --headless), driven by gui_driver.py.

Skipped without PyGObject/libadwaita or mutter. The scenarios that need a
real Raspberry Pi OS image run when PIIMG_TEST_IMAGE names one; it is only
read (they work on a qcow2 overlay in a temporary folder).
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).parent


def _gi_ok():
    r = subprocess.run([sys.executable, "-c", "import piimg.gui as G; G.require()"], capture_output=True)
    return r.returncode == 0


pytestmark = [
    pytest.mark.skipif(shutil.which("mutter") is None, reason="needs mutter for a headless display"),
    pytest.mark.skipif(not _gi_ok(), reason="needs PyGObject, libadwaita, GtkSource and VTE"),
]


@pytest.fixture(scope="module")
def display(tmp_path_factory):
    runtime = tmp_path_factory.mktemp("xdg")
    os.chmod(runtime, 0o700)
    name = f"piimg-test-{os.getpid()}"
    env = dict(os.environ, XDG_RUNTIME_DIR=str(runtime))
    env.pop("DISPLAY", None)
    proc = subprocess.Popen(["mutter", "--headless", "--no-x11", f"--wayland-display={name}",
                             "--virtual-monitor", "1280x860"], env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)
    sock = runtime / name
    for _ in range(100):
        if sock.exists():
            break
        time.sleep(0.1)
    else:
        proc.kill()
        pytest.skip("mutter did not start")
    yield {"XDG_RUNTIME_DIR": str(runtime), "WAYLAND_DISPLAY": name, "GDK_BACKEND": "wayland"}
    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()


def drive(display, out, scenario, *args, timeout=600):
    env = dict(os.environ, **display)
    env.pop("DISPLAY", None)
    r = subprocess.run([sys.executable, str(HERE / "gui_driver.py"), str(out), scenario, *map(str, args)],
                       env=env, capture_output=True, text=True, timeout=timeout)
    assert r.returncode == 0 and "FAILURES: none" in r.stdout, r.stdout[-3000:] + r.stderr[-3000:]
    return r


def test_main_window_without_a_target(display, tmp_path):
    drive(display, tmp_path, "main")
    assert (tmp_path / "main-no-info.png").exists()


def test_recipe_editor_from_the_main_window(display, tmp_path):
    drive(display, tmp_path, "picker")


def test_recipe_editor_without_an_image(display, tmp_path):
    drive(display, tmp_path, "recipe")


def test_services_editor_without_an_image(display, tmp_path):
    drive(display, tmp_path, "editor")


def test_images_folder_and_preferences(display, tmp_path):
    drive(display, tmp_path, "hosted")


@pytest.fixture
def image(tmp_path):
    img = os.environ.get("PIIMG_TEST_IMAGE")
    if not img or not os.path.exists(img):
        pytest.skip("set PIIMG_TEST_IMAGE to a Raspberry Pi OS image to run this")
    return img, tmp_path / "work.qcow2"


@pytest.mark.parametrize("scenario", ["main", "stop", "terminal", "recipe", "editor", "editors"])
def test_with_an_image(display, tmp_path, image, scenario):
    drive(display, tmp_path, scenario, *image)
