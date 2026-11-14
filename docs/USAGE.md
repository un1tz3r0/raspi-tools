# piimg usage

The full command tour. For installation see the [README](../README.md); for
recipe files see [RECIPE-FORMAT.md](RECIPE-FORMAT.md); for how it works and
what was found out about current Raspberry Pi OS images see
[DESIGN.md](DESIGN.md). `uv run piimg COMMAND --help` documents every option.

## Working on image files

Run from the repo root. Nothing here needs root except `burn` and working on a
card directly (see below).

```
uv run piimg unpack ~/Downloads/2026-10-06-raspios-trixie-arm64-lite.img.xz base.img
uv run piimg overlay base.img work.qcow2          # edits go to the overlay; base stays pristine
uv run piimg apply userdata   work.qcow2 --user pi --password 'secret' --hostname gadgetpi
uv run piimg apply gadget-net work.qcow2          # USB Ethernet gadget (shared + client profiles)
uv run piimg run work.qcow2 pitft --var display=35r   # recipe files: see docs/RECIPE-FORMAT.md
uv run piimg recipes                              # bundled recipe files
uv run piimg shell work.qcow2                     # explore/edit files interactively (guestfish)
uv run piimg clean work.qcow2                     # remove leftovers if a live run was killed
uv run piimg ls  work.qcow2 /boot/firmware        # inspect
uv run piimg cat work.qcow2 /boot/firmware/config.txt
uv run piimg resize work.qcow2 +2G                # grow disk + root fs; only the overlay is written
uv run piimg flatten work.qcow2 final.img         # overlay + base -> one standalone (sparse) raw image
uv run piimg flatten work.qcow2 final.qcow2 --compress   # ...or a standalone compressed qcow2
uv run piimg burn work.qcow2                      # list the disks it could write to
sudo .venv/bin/piimg burn work.qcow2 /dev/sdX --expand       # write to a card, grow root to fill it
```

`resize` grows the overlay's virtual disk (the base image is not touched: space
past the end of the base reads as zeros), then the last partition and its ext4
filesystem. Use it when a `live` step needs room for packages. Shrinking is not
supported.

`burn` writes a raw image or an overlay (merged with its base on the fly) to a
whole disk. It refuses disks the running system uses, asks you to type the
device name to confirm (`--yes` skips that), and unmounts desktop automounts of
the card first. `--expand` then grows the root partition to fill the card.
Raspberry Pi OS also does that by itself on first boot while `cmdline.txt` has
`resize`, so `--expand` is for when you want the space before first boot.
Under `sudo`, run the venv's `piimg` directly (`uv` is usually not on root's
PATH); without root, `burn` prints the exact command to use.

Two kinds of recipe: Python (`piimg/recipes/*.py`, `apply`) and YAML recipe
files (`run`), which can describe files, patches, conditionals and, as a last
resort, a script to run inside the booted image. The YAML format is meant for
translating the setup scripts that HAT/hardware vendors supply; see
[docs/RECIPE-FORMAT.md](docs/RECIPE-FORMAT.md), which includes a translation
table. Bundled recipe files (`piimg recipes`): `pitft` (Adafruit PiTFT) and
`waveshare-35dsi` (Waveshare 3.5inch DSI LCD (H); it downloads Waveshare's
overlay, checked by SHA-256). The old `raspi-img-pitft` is now `piimg/recipes/pitft.yaml`; the old
`raspi-img-shell` is `piimg shell`, and `raspi-img-grow` is `piimg resize` (which
also grows the partition and filesystem, and works on overlays).

## First-boot setup without the wizard (`piimg apply first-boot`)

A fresh Raspberry Pi OS image has a placeholder account `pi` with no password
and the shell `/usr/sbin/nologin`; on first boot the desktop wizard (piwiz) or,
on Lite, a console prompt (userconfig.service) asks for a user name and
password. `first-boot` does the same changes offline, so the card boots
straight into a working system:

```
piimg apply first-boot work.qcow2 --user alice --password-stdin <<< 'secret' \
      --locale en_US.UTF-8 --timezone America/New_York --keyboard us \
      --wifi-country US --wifi-ssid HomeNet --wifi-password '...' \
      --hostname kiosk --ssh --browser chromium --boot desktop-autologin
```

What it does, step for step what the wizard and userconf-pi do on the Pi:
rename `pi` (passwd, shadow, group, gshadow, subuid/subgid, home directory),
shell `/bin/bash`, password (or `--ssh-key` only); then `cancel-rename`: boot
behaviour (raspi-config B1-B4; by default what the wizard would pick), remove
the wizard's autostart, account and sudoers file and the sshd rename banner,
disable userconfig.service, re-enable getty@tty1; locale (compiled by a
one-shot unit on first boot if it is new), time zone, keyboard (console and
labwc), wifi country and network, default browser, hostname, ssh, Bluetooth
auto-pairing. It refuses to run if the boot partition's cloud-init
`user-data` creates users itself (`apply userdata`): cloud-init would then set
up the account again on first boot. The stock, all-comment `user-data` does
not touch accounts (checked by booting the image under QEMU).

Not done offline (they need the running Pi): installing updates and language
packs, and removing the browser you did not choose; use a `live` recipe step if
you need them. Just changing the shell of an existing user is
`piimg passwd IMAGE pi --shell /bin/bash` (the wizard still runs at first boot then).

## Official images (`piimg images`)

```
piimg images list                          # current Raspberry Pi OS images (the list Raspberry Pi Imager uses)
piimg images list --archive --series lite_arm64   # every past release of a series
piimg images download trixie-arm64-lite    # a unique part of the name is enough; SERIES@DATE for old releases
piimg images download trixie-arm64 --unpack       # ...and unpack to .img, checking that hash too
piimg images local                         # what is in the images folder, verified or not, newer release available
piimg images verify [FILE...]              # recheck against the published SHA-256
piimg images delete FILE...
piimg config images_dir ~/Images           # where downloads go (default ~/Downloads); `piimg config` shows all settings
```

Downloads resume if interrupted, are hashed while they arrive and must match
the `.sha256` published next to them on downloads.raspberrypi.com before they
get their final name. Unpacked images are checked against the extracted-image
hash in the Imager list. What has been verified is remembered in
`.piimg-images.json` in the images folder (a file changed since then shows as
"changed since verified"). Settings live in `~/.config/piimg/config.toml`;
under sudo, the invoking user's settings and folder are used. The TUI lists
these commands under "Official images", and its picker shows the images folder.

## Repairing a Pi's SD card in place

Every command that takes an IMAGE also takes a card taken out of a Pi
(`/dev/sdX`, `/dev/mmcblkN`). Reading or writing a card needs root, so run the
venv's `piimg` under sudo:

```
sudo .venv/bin/piimg disks                            # cards and USB disks; which ones look like Pi OS
sudo .venv/bin/piimg diagnose /dev/sdc                # why won't it boot / show the screen / log in / autostart?
sudo .venv/bin/piimg passwd /dev/sdc pi               # reset a forgotten password (asks for it)
sudo .venv/bin/piimg users /dev/sdc                   # accounts, password state, ssh keys
sudo .venv/bin/piimg logs /dev/sdc -p err             # errors from the last boot (the Pi's own journal)
sudo .venv/bin/piimg logs /dev/sdc --boot -1 -u lightdm
sudo .venv/bin/piimg autostart /dev/sdc               # everything that starts by itself: services, autologin, kiosk
sudo .venv/bin/piimg service /dev/sdc disable kiosk   # (or mask / enable / unmask / show)
sudo .venv/bin/piimg services /dev/sdc --all          # every unit: enabled, disabled, static, masked
sudo .venv/bin/piimg settings /dev/sdc                # hostname, boot mode, ssh, I2C/SPI/serial, locale, ... (as raspi-config shows them)
sudo .venv/bin/piimg users /dev/sdc --keys            # with the authorized ssh keys themselves
sudo .venv/bin/piimg ssh-key /dev/sdc pi --remove --key "ssh-ed25519 AAAA... old@laptop"
sudo .venv/bin/piimg wifi /dev/sdc oldnet --remove    # delete a NetworkManager profile
sudo .venv/bin/piimg boot-mode /dev/sdc console       # console | console-autologin | desktop | desktop-autologin
sudo .venv/bin/piimg config-txt /dev/sdc --set hdmi_force_hotplug=1
sudo .venv/bin/piimg cmdline /dev/sdc --add video=HDMI-A-1:1280x720@60 --remove quiet
sudo .venv/bin/piimg wifi /dev/sdc MyNet --country GB # asks for the wifi password
sudo .venv/bin/piimg ssh-key /dev/sdc pi ~/.ssh/id_ed25519.pub
sudo .venv/bin/piimg fsck /dev/sdc [--repair]         # check (on a throwaway snapshot), or repair
sudo .venv/bin/piimg edit /dev/sdc /etc/rc.local      # with $EDITOR
sudo .venv/bin/piimg run /dev/sdc pitft --var display=28c
sudo .venv/bin/piimg resize /dev/sdc                  # grow the root filesystem to fill the card
sudo .venv/bin/piimg flatten /dev/sdc backup.img      # back up the card
```

While piimg works on a card it refuses disks the running system uses (anything
mounted outside `/media`, `/run/media`, `/mnt`, swap, LVM, LUKS, RAID),
unmounts the desktop's automounts of the card first, and holds an exclusive
lock on it so udev and the desktop leave it alone. To try changes without
touching the card, put an overlay on it:

```
sudo .venv/bin/piimg overlay /dev/sdc fix.qcow2       # the card is only read from now on
sudo .venv/bin/piimg passwd fix.qcow2 pi              # ...changes go to fix.qcow2
sudo .venv/bin/piimg diff fix.qcow2                   # what changed
sudo .venv/bin/piimg commit fix.qcow2                 # write the changes to the card (or just delete fix.qcow2)
```

## Interactive interface (`piimg tui`)

```
sudo .venv/bin/piimg tui                              # pick a card or image
sudo .venv/bin/piimg tui /dev/sdc                     # work on the card directly
sudo .venv/bin/piimg tui /dev/sdc fix.qcow2           # card read-only, changes go to fix.qcow2
uv run piimg tui work.qcow2                           # images need no root
```

The left pane lists every command (filter with `/`); the right pane shows the
highlighted command's help and a summary of the target. Enter runs it: commands
with options open a form first (status line at the bottom explains the focused
field; Run / Back), commands that change a card directly or that are risky ask
for confirmation, and the run dialog shows a progress bar per slow step, the
output as a scrolling log or a table, and Stop for commands that can be
interrupted. For `run`, the form shows the chosen recipe's variables.

Keys: Tab / Shift+Tab next / previous widget; Ctrl+Left/Right/Up/Down between
panes (and the editor's menu bar); Up/Down between the fields of a form or the
rows of a list (in a multi-line box they move the cursor and leave the box at
its first / last line); Left/Right between buttons; Esc goes back. Ctrl+arrows
are taken for pane switching, so they do not jump words inside text boxes.

### Editors

Some entries in the list are marked *editor*: users, system, services, wifi,
config-txt, cmdline, images and (with an overlay) changes. They stand in for
the commands that list and change those things one at a time (the table under
[Desktop interface](#desktop-interface-piimg-gui) says what each one covers). Enter opens one: it
reads the target, shows the items in a table with the highlighted one's
details and a form below, and marks every change (`●` changed, `+` new, `−`
to remove). Nothing is written until you commit: all the changes then run as
one recipe (`piimg run`) in the run dialog. config-txt is a text box instead
of a table. Leaving an editor, or quitting, with changes asks whether to
commit them, discard them or keep editing.

| key | in an editor |
|---|---|
| Space | change the highlighted item's value (next state, on/off) |
| Enter | edit it in the form below; Esc returns to the list |
| `a` / Insert, `d` / Delete | add an item; remove one (again: keep it) |
| Alt+Up / Alt+Down | move it (wifi priority, cmdline order) |
| `u`, `U` | revert the item, revert everything |
| `x` | the item's action (show a unit file, verify an image) |
| `/`, `f` | filter by text; next view (e.g. enabled, masked, changed) |
| Ctrl+S, F5 | commit the changes (asks first); preview them as a dry run |
| Ctrl+R | read the target again |

### Recipe editor

`e` in the TUI, or `piimg recipe-edit [FILE | BUNDLED-NAME] [--image IMG]`. A
bundled recipe's name (e.g. `pitft`) opens it as a template: you edit a copy and
save it under a new name. The left pane is the recipe: its name and variables,
then its steps, with `when`/`then`/`else` blocks indented. The right pane is a
form for the selected step's arguments, applied as you type; `F2` edits a step
(or, on the first row, the whole recipe) as YAML. Comments and layout of the
steps you don't touch are kept when saving.

| key | in the step list |
|---|---|
| `a` / Insert | insert a step after the cursor (a dialog lists every action with its help) |
| `d` / Delete | delete the marked steps, or the one under the cursor |
| Space, Shift+Up/Down, Esc | mark / unmark one step, mark a range, clear the marks |
| Alt+Up / Alt+Down | move them within their block |
| Ctrl+D | duplicate them |
| `c` / `x` / `v` | copy / cut / paste after the cursor (also how to move steps between blocks) |
| Ctrl+Z / Ctrl+Y | undo / redo |
| Ctrl+S, F5 | save; run or dry-run the recipe (or only the marked steps) on an image |

Rows with problems (unknown action, missing required field, undeclared
`{{variable}}`) get a ⚠; after a failed run the failing step gets a ✘ and the
cursor jumps to it.

## Desktop interface (`piimg gui`)

```
uv run piimg gui                                  # pick a card or image
uv run piimg gui work.qcow2                       # open an image or overlay
uv run piimg gui /dev/sdc fix.qcow2               # card read-only, changes go to fix.qcow2
uv run piimg gui --recipe my.yaml                 # only the recipe editor
uv run piimg gui --install-desktop                # add it to the app launcher
```

The same as the TUI, with GTK 4 and libadwaita (needs `python3-gi
gir1.2-adw-1 gir1.2-gtksource-5 gir1.2-vte-3.91`). The sidebar lists every
command, grouped and filterable (Ctrl+F); the main pane shows the selected
command's help, its options as a form, and a summary of the target. Enter or
a double-click in the list moves to the form; the line under it explains the
focused field, and required fields are marked until filled. What you type in a
form is kept when you switch commands (except passwords). Run (Ctrl+Return)
runs the command with those options. Risky commands
ask first (`burn` makes you type the device name). The run dialog shows a
progress bar per slow step, the output as a log or a table (select a row for
its details), and Stop: SIGINT, then SIGTERM, then SIGKILL. `shell` and `edit`
run in a terminal inside the window.

**Editors.** Some entries in the list (marked "editor") are pages for
changing a set of things rather than single commands. Selecting one reads
the current state; change what you want (the undo button on a row puts it
back, Revert puts everything back), then **Commit**: it lists the changes,
asks, and makes them all in one run (as a recipe, `piimg run`), then reads
again. **Preview** runs the same changes as a dry run. While there are
uncommitted changes the entry says "changed", and leaving it (another
command, another target, closing the window) asks whether to commit,
discard or keep editing.

| Editor | Stands in for | What you can change |
|---|---|---|
| users | `users`, `passwd`, `ssh-key` | per account (expand it): new password (hashed before it leaves the GUI), locked, shell, name, authorized keys (one per line); delete an account |
| system | `hostname`, `boot-mode`, `settings` | hostname, boot mode, wifi country, ssh, I2C, SPI, serial port and console, 1-Wire, locale, time zone, keyboard (raspi-config's offline translations) |
| services | `services`, `service` | each unit's state: enabled, disabled, masked, unmasked |
| wifi | `wifi` | add networks, remove profiles, connect automatically, and the order the Pi tries them in (the arrows; it sets their priorities) |
| config-txt | `config-txt` | the whole file, in a text editor |
| cmdline | `cmdline` | the kernel parameters: edit, add, remove, reorder |
| images | `images local`, `images delete` | your images folder: delete images (Verify checks one); needs no target |
| changes | `diff`, `commit` | nothing: it lists what the open overlay changes, and Commit writes it into the base image or card (asks first). Only with an overlay |

piimg's own settings (`piimg config`) are under Preferences in the main menu
(Ctrl+,).

**Root.** Run `piimg gui` as yourself, not with sudo. Image files and overlays
never need root. When a command must open a card you cannot open, it runs
through `pkexec` and polkit asks for your password. By default it asks each
time; to have it remembered for about five minutes, install piimg's polkit
action once with `sudo .venv/bin/piimg gui --install-polkit` (undo with
`--uninstall-polkit`). Like sudo's timestamp, that lets programs you run in
those minutes start piimg as root without asking. Reading the summary of such a card waits
for you to press Read. Overlays on a card (`gui /dev/sdc fix.qcow2`) are
created as root but stay your files; after that, commands only read the card,
so they still need root, but `commit` is the only one that writes it.

Keys: Ctrl+O open, Ctrl+F filter, Ctrl+Return run the selected command (or
commit an editor's changes),
Ctrl+R read the target again, Ctrl+E recipe editor, Ctrl+?
all shortcuts, Ctrl+Q quit.

### Recipe editor (GUI)

Ctrl+E in the main window, or `piimg gui --recipe FILE | BUNDLED-NAME`. Each
recipe opens in its own window, with its own file, undo history and unsaved
state; several can be open at once. Steps are on the left, the selected
step's form on the right (applied as you type). Click, Ctrl+click and
Shift+click (or Shift+arrows) select steps; list operations work on the
selected steps, and insert and paste put new steps after the last one
clicked (at the start of a `then:`/`else:` branch when that row is selected).

| key | |
|---|---|
| Ctrl+I / Insert | insert a step (the dialog lists every action with its help; type to filter) |
| Delete | delete the selected steps |
| Alt+Up / Alt+Down | move them within their block |
| Ctrl+D | duplicate them |
| Ctrl+C / Ctrl+X / Ctrl+V | copy / cut / paste steps (in the step list) |
| Ctrl+Z / Ctrl+Shift+Z | undo / redo (in the step list; the toolbar buttons work anywhere) |
| Escape | select only one step again |
| F2 | edit the step (or, on the first row, the whole recipe) as YAML |
| F7 | check: go to the first problem |
| Ctrl+S / Ctrl+Shift+S | save / save as |
| F5 | run or dry-run, by default on the main window's target |
| Ctrl+W | close (asks to save unsaved changes) |

Rows with problems get a ⚠ and a tooltip; after a failed run the failing step
gets a ✘ and is selected.
