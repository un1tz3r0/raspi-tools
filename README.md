# raspi-tools

`piimg` customizes and repairs Raspberry Pi OS images and SD cards from a
Linux PC, without booting the Pi. It edits the image offline with libguestfs,
so most commands need no root and no emulator.

- **Set up a fresh image:** first account, password, locale, keyboard, wifi
  and ssh, without the first-boot wizard (`apply first-boot`). Also USB
  Ethernet gadget networking (`apply gadget-net`) and HAT setup recipes.
- **Repair a card from a Pi:** reset a password, read the Pi's logs, find out
  why it doesn't boot, show the screen or autostart, fsck, change boot mode
  and services.
- **Recipes:** YAML recipes for files, config edits and conditions, with an
  interactive editor.
- **Official images:** list, download and verify Raspberry Pi OS images.
- **Image handling:** copy-on-write overlays so the base image stays
  untouched, resize, flatten, and burn to a card.
- **TUI and GUI:** a terminal interface (`piimg tui`) and a GNOME desktop
  interface (`piimg gui`) for all of the above.

## Requirements

Linux (developed on Debian/Ubuntu) with Python 3.11+ and
[uv](https://docs.astral.sh/uv/). System packages:

```sh
sudo apt install python3-guestfs libguestfs-tools qemu-utils xz-utils
# optional: guestfs-tools (piimg diff), qemu-system-arm (live recipe steps)
```

For the desktop interface (`piimg gui`), also:

```sh
sudo apt install python3-gi gir1.2-adw-1 gir1.2-gtksource-5 gir1.2-vte-3.91
```

The libguestfs Python bindings are not on PyPI, so they come from apt, and so
does PyGObject. `pyproject.toml` therefore makes uv build the venv on the
system Python, where piimg finds them.

On Ubuntu, a normal user can't read the kernel in `/boot`, which libguestfs
needs. Either run `sudo chmod +r /boot/vmlinuz-*` (undone by kernel updates)
or see "Host quirk" in [docs/DESIGN.md](docs/DESIGN.md).

## Setup

```sh
git clone https://github.com/un1tz3r0/raspi-tools.git
cd raspi-tools
uv sync                 # creates .venv, installs piimg and its dependencies
uv run piimg --help
```

If uv is installed as a snap (`/snap/bin/uv`), programs started by `uv run`
carry the snap's AppArmor profile, and Ubuntu's profile for the guestfs tools
then refuses their signals: Stop or Ctrl-C cannot end `piimg diff`. Run
`.venv/bin/piimg` (or the app launcher entry from `piimg gui
--install-desktop`) instead of `uv run piimg` to avoid that.

`uv sync` installs the development tools too (pytest). Run the tests with
`uv run pytest`. The GUI tests need `mutter` (they run it headless); set
`PIIMG_TEST_IMAGE` to a Raspberry Pi OS image to include the tests that need
one (the image is only read). Pending work is in [TODO.md](TODO.md).

## Quick start

```sh
# get an image
uv run piimg images list
uv run piimg images download trixie-arm64-lite --unpack

# work on an overlay; the downloaded image is left untouched
uv run piimg overlay ~/Downloads/2026-10-06-raspios-trixie-arm64-lite.img work.qcow2
uv run piimg apply first-boot work.qcow2 --user alice --password-stdin \
    --locale en_US.UTF-8 --timezone Europe/London --keyboard gb --wifi-country GB --ssh
uv run piimg diagnose work.qcow2

# write it to a card (needs root; run the venv's piimg directly)
sudo .venv/bin/piimg burn work.qcow2 /dev/sdX

# or fix a card taken out of a Pi
sudo .venv/bin/piimg diagnose /dev/sdX
sudo .venv/bin/piimg passwd /dev/sdX pi

# or do it all interactively, in a terminal or on the desktop
uv run piimg tui work.qcow2
uv run piimg gui work.qcow2
uv run piimg gui --install-desktop     # add piimg to the app launcher
```

Commands that take an image also take a qcow2 overlay or a card (`/dev/sdX`,
`/dev/mmcblkN`). Cards need root. piimg refuses disks the running system uses.

## Documentation

- [docs/USAGE.md](docs/USAGE.md): every command, card repair, the TUI, the GUI
  and the recipe editor
- [docs/RECIPE-FORMAT.md](docs/RECIPE-FORMAT.md): the YAML recipe format
- [docs/DESIGN.md](docs/DESIGN.md): how it works, and notes on how current
  Raspberry Pi OS images boot

## License

Apache-2.0. This project started as a fork of
[hardillb/rpi-gadget-image-creator](https://github.com/hardillb/rpi-gadget-image-creator).
