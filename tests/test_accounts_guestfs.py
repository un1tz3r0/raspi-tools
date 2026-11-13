"""Offline account edits on a tiny scratch disk (needs libguestfs; ~10 s).

Run: uv run pytest -q tests/test_accounts_guestfs.py
"""
import pytest

guestfs_missing = False
try:
    from piimg.guest import _import_guestfs, _ensure_supermin_kernel
    _import_guestfs()
except Exception:  # noqa: BLE001
    guestfs_missing = True

pytestmark = pytest.mark.skipif(guestfs_missing, reason="libguestfs not available")

PASSWD = """root:x:0:0:root:/root:/bin/bash
pi:x:1000:1000::/home/pi:/usr/sbin/nologin
rpi-first-boot-wizard:x:109:65534:,,,,:/home/rpi-first-boot-wizard:/bin/bash
"""
SHADOW = "root:*:20732:0:99999:7:::\npi:!:20732:0:99999:7:::\nrpi-first-boot-wizard:!:20732::::::\n"
GROUP = "root:x:0:\nsudo:x:27:pi\nvideo:x:44:pi,vnc,rpi-first-boot-wizard\npi:x:1000:\nnetdev:x:102:pi,rpi-first-boot-wizard\n"
GSHADOW = "root:*::\nsudo:*::pi\nvideo:*::pi,vnc,rpi-first-boot-wizard\npi:!::\nnetdev:!::pi,rpi-first-boot-wizard\n"


class FakeImage:
    """Just enough of guest.Image on a guestfs handle with a bare ext4 root."""

    def __init__(self, g):
        from piimg.guest import Image
        self.g = g
        self.boot = "/boot/firmware"
        for name in ("read", "read_text", "write", "exists", "lexists", "listdir", "replace_text", "sub",
                     "symlink", "append_line_once"):
            setattr(self, name, getattr(Image, name).__get__(self))


@pytest.fixture(scope="module")
def img(tmp_path_factory):
    _ensure_supermin_kernel()
    path = tmp_path_factory.mktemp("acct") / "disk.img"
    g = _import_guestfs().GuestFS(python_return_dict=True)
    g.disk_create(str(path), "raw", 32 << 20)
    g.add_drive_opts(str(path), format="raw")
    g.launch()
    g.mkfs("ext4", "/dev/sda")
    g.mount("/dev/sda", "/")
    i = FakeImage(g)
    for p, text in (("/etc/passwd", PASSWD), ("/etc/shadow", SHADOW), ("/etc/group", GROUP),
                    ("/etc/gshadow", GSHADOW), ("/etc/subuid", "pi:100000:65536\n"),
                    ("/etc/subgid", "pi:100000:65536\n"), ("/etc/shells", "/bin/bash\n"),
                    ("/bin/bash", "")):
        i.write(p, text)
    g.mkdir_p("/home/pi")
    i.write("/home/pi/.profile", "x", uid=1000, gid=1000)
    g.mkdir_p("/home/rpi-first-boot-wizard")
    yield i
    g.shutdown()
    g.close()


def test_shell_rename_delete(img):
    from piimg import system as S
    S.set_shell(img, "pi", "/bin/bash")
    assert S.find_user(img, "pi")["shell"] == "/bin/bash"
    S.rename_user(img, "pi", "alice")
    assert S.find_user(img, "pi") is None
    u = S.find_user(img, "alice")
    assert (u["uid"], u["home"]) == (1000, "/home/alice")
    assert img.exists("/home/alice/.profile") and not img.exists("/home/pi")
    assert "alice:x:1000:" in img.read_text("/etc/group").splitlines()
    assert "sudo:x:27:alice" in img.read_text("/etc/group")
    assert "video:*::alice,vnc,rpi-first-boot-wizard" in img.read_text("/etc/gshadow")
    assert img.read_text("/etc/subuid").startswith("alice:")
    assert img.read_text("/etc/shadow").splitlines()[1].startswith("alice:!")
    S.delete_user(img, "rpi-first-boot-wizard")
    assert "wizard" not in img.read_text("/etc/passwd") + img.read_text("/etc/group") + img.read_text("/etc/gshadow")
    assert "video:x:44:alice,vnc" in img.read_text("/etc/group")
    assert not img.g.is_dir("/home/rpi-first-boot-wizard")


def test_rename_refuses_bad_names(img):
    from piimg import system as S
    from piimg.errors import PiImgError
    for bad in ("Root", "root", "1abc", "a" * 33, "with space"):
        with pytest.raises(PiImgError):
            S.rename_user(img, "alice", bad)
