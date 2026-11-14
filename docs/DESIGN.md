# Design notes

Goal: a general "customize a Raspberry Pi OS image" tool. USB-gadget networking
(the original purpose of this repo) becomes one *recipe* among many.

## Guiding principle: offline first, emulation only as a last resort

**Do everything we can by editing the image offline with libguestfs. Only fall
back to executing code inside the image if that is truly unavoidable and cannot
be faked or done declaratively.** Most customization is file placement and
config edits, which need no emulation at all. Typing heredocs into a
serial console (what the old `expect` scripts did) is slow and fragile and is
deliberately gone.

There is no Docker. It was only a way to pin qemu and its libraries. We use the
host's packages and document them (see the README, "Requirements").

### Tier 1 - offline edits (default)

libguestfs (Python `guestfs` bindings) opens the `.img` as a user, no root:
read/write files, set modes/ownership, make symlinks, edit `config.txt` /
`cmdline.txt`, drop systemd units and `.d` snippets, and enable a unit by
creating the `wants/` symlink that `systemctl enable` would create.

Things that look like they need a running system but usually do not:

| Looks like it needs to run...      | Do this instead                                    |
|------------------------------------|----------------------------------------------------|
| `systemctl enable foo`             | create the symlink under `/etc/systemd/system/*.wants/` |
| `useradd` / set password           | write `userconf.txt`/cloud-init `user-data`, or edit passwd/shadow directly |
| `apt-get install pkg`              | cloud-init `packages:` (installs on first boot on the device), or download the `.deb`s and unpack/stage them |
| first-boot setup scripts           | cloud-init `runcmd:` / `write_files:`, or a oneshot systemd unit |
| enable ssh                         | cloud-init / `ssh` flag file on the boot partition |

Known limit: the libguestfs appliance is x86, so its `sh`/`command` calls fail
with `Exec format error` on an arm image. Do not try to run image binaries
through libguestfs.

### Tier 2 - the `live` step: boot the image and run a script (implemented)

For things that must execute arm code (package maintainer scripts, compiling,
untranslated vendor installers). See `docs/RECIPE-FORMAT.md` for usage. Design
decisions, all verified on the trixie arm64 lite image:

- **Boot with `init=`, not systemd.** A normal boot runs cloud-init's
  first-boot pass and would use up the `user-data` we wrote. Our own tiny init
  mounts the basics, brings up networking, runs the script, and powers off.
- **Two completion signals**: a `PIIMG-DONE rc=N` console line (streamed live)
  and a `/var/lib/piimg/done` marker file read back offline after the VM stops.
  If they disagree, or the marker is missing, the step fails. Everything
  injected is removed in a `finally`; `piimg clean` handles a killed host.
- **QEMU `raspi3b` with the image's own `kernel8.img`** boots in ~4 s and the
  whole boot-run-stop cycle is ~15 s. SD card sizes need not be a power of 2.
- **Console**: the Pi 3 mini UART cannot register under QEMU (no clock model:
  `unable to register 8250 port`). The PL011 works and Linux names it
  `ttyAMA1`, so `console=ttyAMA1` with QEMU's *first* `-serial`.
  **Serial input does not work** (the prompt shows but typed characters never
  arrive), so no interactive boot; scripts must be non-interactive.
- **Networking**: `-netdev user` + `usb-net`; the guest sees `usb0`
  (`cdc_ether`), 10.0.2.15, gateway 10.0.2.2, DNS 10.0.2.3. curl reached
  deb.debian.org; `apt-get update` + `install i2c-tools` succeeded.
- **Guest clock** is set from the host: the emulated Pi has no RTC and boots
  in 1970, which made apt reject the security and raspberrypi.com signatures.
- **Cost**: slow. `apt-get update` + one small package took about 6 minutes
  (TCG emulation); the qcow2 overlay grew by ~350 MiB, which flattening with
  `qemu-img convert` compacts.
- Not done: 32-bit images (`qemu-system-arm`, `raspi2b`, `kernel7.img`);
  a rootless chroot/binfmt backend (faster, but needs uid mapping for apt).
  Ideas: download .debs on the host to avoid the guest's slow `apt-get update`.

A chroot via qemu-user + binfmt also works for running arm binaries (verified
earlier: `dpkg`, `apt-get`, `systemctl --version`, and cloud-init's schema
validator all ran under a loop-mounted, chrooted image). It was used only as a
validator because mounting needs root.

## Resizing, flattening and burning

- **`resize` grows only the overlay.** A qcow2 overlay's virtual size may exceed
  its backing file's; reads past the backing file's end return zeros. So
  `qemu-img resize work.qcow2 +2G` leaves the base untouched, and the partition
  table and ext4 writes that follow land in the overlay. Verified: base sha256
  unchanged, the overlay grew by ~22 MiB for +2 GiB.
- **Partition growth** (`image.grow_last_partition`, shared by `resize` and
  `burn --expand`): libguestfs, partition not mounted. `e2fsck -fp` first (it
  aborts before anything is changed if the fs is damaged), then
  `parted resizepart` on the last partition (start sector and number kept),
  then `resize2fs`. parted keeps the MBR disk ID, so `root=PARTUUID=` stays
  valid. End sector: the last sector of the disk, capped at 2^32-1 on MBR; on
  GPT the backup header is moved first (`part_expand_gpt`) and 33 sectors are
  left for it.
- **The Pi resizes itself on first boot** while `cmdline.txt` has ` resize`:
  initramfs `local-premount/resize_early` runs `parted resizepart 2 <end>`
  (same cap as ours), and `local-bottom/set_partuuid` gives the card a new
  random disk ID, fixes fstab/cmdline.txt and removes ` resize`. Pre-growing
  does not conflict with that: its resizepart becomes a no-op. We do not remove
  ` resize`, so cloned cards still get distinct disk IDs.
- **`flatten`** is `qemu-img convert` from the overlay (the whole backing chain
  is read) into `OUT.part`, renamed when complete. Raw output is sparse.
- **`burn`** is `qemu-img convert -n -t none` straight to the disk. `-n`: write
  into the existing device; `-t none`: O_DIRECT, so progress tracks data that
  reached the card, not the page cache. qemu-img's `-p` output (`(12.34/100%)`,
  about every 1%) drives the progress bar. Throughout writing and `--expand`,
  piimg holds `flock(LOCK_EX)` on the whole disk: udev defers events for a
  locked disk (https://systemd.io/BLOCK_DEVICE_LOCKING/), so the desktop cannot
  probe or automount half-written partitions. qemu's own image locks are OFD
  `fcntl` locks, which are independent of `flock`, so qemu-img and libguestfs
  still open the disk. Afterwards: `fsync`, `BLKFLSBUF`, unlock, `BLKRRPART`.
  Before writing, an `O_EXCL` open of the whole disk is the kernel's own check
  that no partition is still mounted or claimed.
- **libguestfs under sudo** (`--expand`): root would build its own appliance
  (~2 GB in `/var/tmp/.guestfs-0`, slow; the first real run failed with ENOSPC
  on a nearly full disk). Instead `guest._borrow_sudo_users_appliance()` points
  `LIBGUESTFS_PATH` at a temp dir of symlinks to `$SUDO_USER`'s built appliance
  plus a `README.fixed` marker (libguestfs's "fixed appliance" mode). Its root
  disk is attached `snapshot=on`, so the user's copy is never written.
- First real burn (trixie desktop overlay, 6.3 GiB, to a 50 GB SD card in the
  laptop's mmc slot): ~35 MiB/s, 2:45.
- Partly tested on a real block device (development machine has no passwordless
  sudo). The write and expand path was tested against a regular file standing
  in for the card, with the same qemu-img arguments, flock and libguestfs calls.

## Working on a Pi's card in place (`disks.py`)

- Any IMAGE argument may be a whole disk. `Image.open()` then calls
  `disks.claim()`: refuse disks in use by the running system (same rules as
  `burn`), unmount desktop automounts (`udisksctl` as a user, `umount` as
  root), hold `flock(LOCK_EX)` for the whole session, and on release `fsync`
  + `BLKFLSBUF`, plus `BLKRRPART` only if partitions changed (`resize`).
  Claims are re-entrant per process: a second `open()`+`flock()` of the same
  disk in one process would conflict with the first. `live` holds the claim
  from injection to cleanup. `burn` claims the disk itself and opens its
  `Image` with `claim=False`.
- Read-only access (diagnose, logs, an overlay's backing card) only checks
  permission and warns if the card is mounted (stale reads).
- The format of a block device is always `raw`; qemu-img is not asked to probe it.
- A card can be the base of a qcow2 overlay: `qemu-img create -b /dev/sdc -F raw`.
  `commit` writes the overlay back (`qemu-img commit`, card claimed);
  `diff` is `virt-diff` between base and overlay.
- Not tested on a real device (no root on the development machine); the claim
  logic is unit-tested against a regular file standing in for the card.

## Repairs and diagnosis (`system.py`)

Offline translations of what one would do on the running Pi: `/etc/shadow`
edits (`passwd`; hashes with `openssl passwd -6`), raspi-config's
`do_boot_behaviour` (default.target link, getty@tty1 autologin drop-in,
lightdm `autologin-user`), `do_wifi_country` (`cfg80211.ieee80211_regdom=` on
the kernel command line, saved rfkill state `0`), NetworkManager keyfiles.
`diagnose` resolves `root=` and fstab specs against the partitions' blkid
data (PARTUUID/UUID/LABEL), checks kernel/firmware/initramfs/overlay files
named by config.txt, flags KMS-ignored legacy display settings, and summarises
the last boot from the journal. The journal is read with libguestfs's
`journal_*` API: seek to the end with `journal_skip`, step back `--scan`
entries and read forward (~1600 entries/s); boots are grouped by `_BOOT_ID`.
`fsck` without `--repair` runs on libguestfs's throwaway read-only snapshot
and forces a full ext4 check by setting the mount count to the maximum there.

## First-boot setup (`recipes/first_boot.py`)

What a fresh Raspberry Pi OS (trixie, 2026-10-06) does at first boot, read from
the image (`userconf-pi` 0.19, `piwiz` 1.8, cloud-init's Pi OS distro class):

- The image ships `pi` (uid 1000, password `!`, shell nologin) and, on desktop
  images, `rpi-first-boot-wizard` (uid 109, NOPASSWD sudo), lightdm and getty
  autologin as the wizard, `/etc/xdg/autostart/piwiz.desktop`, the sshd banner
  `/etc/ssh/sshd_config.d/rename_user.conf`, `/var/lib/userconf-pi/autologin`
  (the boot behaviour to restore), userconfig.service enabled.
- piwiz runs `raspi-config nonint do_change_locale_rc_gui`, `..._timezone_rc_gui`,
  `do_change_keyboard_rc_gui pc105 L V`, `do_wifi_country`, `do_browser`,
  NetworkManager for wifi, PackageKit for updates / language packs / removing
  the other browser, `/usr/lib/userconf-pi/userconf OLD NEW HASH` and finally
  `cancel-rename NEW`.
- `userconf` = usermod -l/-m -d, groupmod -n, subuid/subgid, sudoers.d,
  `usermod -s /bin/bash`, `chpasswd -e`, `cancel-rename`. `cancel-rename` =
  boot behaviour B3/B4 (B1/B2 on console images) from the autologin flag,
  remove the wizard autostart, delete the wizard user (via a self-deleting
  del-wizard.service), remove the sshd banner, disable userconfig, enable getty@tty1.
- cloud-init (NoCloud seed on the boot partition) runs on first boot. Pi OS's
  distro class implements the *first* `add_user` by running `userconf NAME
  [HASH]` and masking userconfig.service; generic `create_user` then applies
  `lock_passwd` (default true). That only happens when user-data lists users:
  booting the stock image (all-comment user-data) under QEMU, users_groups ran
  in 5 ms, created nothing, and `pi`, the wizard and userconfig were untouched.
  So `first-boot` leaves user-data alone and refuses to run when it has `users:`.
- Booting this image in QEMU with normal systemd needs `systemd.watchdog_sec=0`:
  `40-rpi-enable-watchdog.conf` arms the hardware watchdog and QEMU's raspi3b
  resets at once.

## The front ends (`piimg/frontend/`, `piimg/tui/`, `piimg/gui/`)

- `piimg/frontend/` holds everything the TUI (Textual) and the GUI (GTK 4 /
  libadwaita) share, and imports neither toolkit: `fields.FieldDef`, the
  command forms (`commands.py`), running a child and reading its output
  (`proc.py`), the open target (`target.py`) and the recipe editor's model
  (`recipe.RecipeModel`: rows, checks, marks, clipboard, undo/redo and every
  list operation, each taking the cursor row and returning the row to show
  next). Styled text is `rich.text.Text`; the GUI turns it into Pango markup.
  A test checks that the front end imports no toolkit, the TUI no `gi` and
  the GUI no `textual`.
- Commands come from the CLI's argparse tree (`cli.iter_commands`); each
  subparser carries a `cli.Meta` (group, output layout, whether it writes,
  confirmation text, abortability) and arguments carry hints (`kind`,
  `stdin_flag`). Forms are generated from the actions; the built command line
  is validated with a raising ArgumentParser before running.
- Commands run as `python -m piimg --progress=json --json ...` children in
  their own session: progress events on stderr (`progress._Json`), tables as
  JSON lines on stdout. Stop sends SIGINT to the process group (the CLI's own
  Ctrl-C cleanup runs), then SIGTERM, then SIGKILL. Secrets go through stdin
  (`--password-stdin`), never argv.
- The recipe editor edits ruamel.yaml round-trip structures in place, so
  untouched steps keep comments and flow style; `recipe_schema.py` describes
  every action's fields (a test checks it covers every `Runner.op_*`). Runs
  write the edited recipe to a hidden temp file next to the original so
  relative `copy`/`content_file` paths still resolve.
- The GUI finds the distro's PyGObject the way `guest.py` finds libguestfs
  (`sysimport.import_system`). It runs as the user. A command that must open
  a card the user cannot open runs as
  `pkexec env -C CWD VARS... python -m piimg.frontend.supervise python -m piimg ...`:
  pkexec clears the environment and starts in `/`, and the user cannot signal
  a root process, so the small supervisor passes stdin and stop requests
  (JSON lines on its stdin) to the command. piimg finds the user's settings
  and libguestfs appliance from `PKEXEC_UID` as from `SUDO_UID`
  (`config.invoking_user`), and overlays and flattened images it creates as
  root are given back to the user. Terminal commands (`shell`, `edit`) run in
  a VTE terminal without the supervisor: the terminal stops them.
- Editors (`frontend/editors.py`) are the read-change-commit pages: a query
  command's table is the initial state, each item keeps its original values
  (so the editor knows what changed and can revert), and Commit turns the
  changes into a recipe run once with `piimg run` (one start of the image,
  the run dialog's progress, `--dry-run` as Preview, all or nothing as far
  as the recipe goes). An editor stands in for the commands it replaces in a
  front end's list (`list_entries`). The GUI keeps the editor on screen
  while it has changes: leaving it asks to commit, discard or stay. Editors:
  users, system (raspi-config settings, read by `settings`), services, wifi,
  config-txt (a text editor), cmdline (an ordered list), images (the images
  folder; no target, commits with `images delete`), changes (review only:
  the overlay's diff, committed with `commit`). Secrets stay out of the
  recipe where they can: passwords are hashed in the GUI; a new wifi
  network's password is in the recipe (as NetworkManager stores it), which
  is written mode 0600 and deleted after the run. Only the GUI draws them so
  far; the TUI still lists the plain commands.
- GUI tests (`tests/test_gui.py`) run `tests/gui_driver.py` under
  `mutter --headless`; broadway does not tick libadwaita's dialog animations
  without a browser attached. Set `PIIMG_TEST_IMAGE` to an image to run the
  scenarios that need one; it is only read.

## Findings about current images (Raspberry Pi OS trixie, 2026-10-06)

- Boot partition is `/boot/firmware` (FAT, partition 1). `config.txt`,
  `cmdline.txt`, `user-data`, `meta-data`, `network-config` live there.
  The old scripts wrote `/boot/config.txt`: that is wrong now.
- First boot is configured by **cloud-init** (NoCloud seed =
  `file:///boot/firmware`). There is no default `pi`/`raspberry` login in the
  image, so the old expect login flow cannot work. Networking is
  NetworkManager (+ netplan via cloud-init), not dhcpcd/`interfaces.d`.
  The old `dhcpcd.conf` / `interfaces.d/usb0` steps are obsolete.
- Trixie already ships **`rpi-usb-gadget`** (CLI + `g_ether` modprobe config +
  internet-sharing service). The gadget recipe should build on that (plus the
  `dwc2` overlay in `config.txt` and `modules-load=dwc2` in `cmdline.txt`)
  before reinventing `usb-gadget.sh`. Still to read: what `rpi-usb-gadget`
  expects so we configure it correctly offline.
- The `dwc2` overlay exists in the image (`dwc2.dtbo`).
- `cmdline.txt` uses `root=PARTUUID=...`; resizing the image does not change
  the PARTUUID, but the partition table must stay consistent.

## Host quirk: libguestfs without root on Ubuntu

Ubuntu ships `/boot/vmlinuz-*` as mode 600, so supermin cannot build the
appliance as a normal user (`supermin exited with error status 1`). Rootless
workaround, no sudo: `apt-get download linux-image-$(uname -r)`, `dpkg-deb -x`
it into a user directory, then export `SUPERMIN_KERNEL`,
`SUPERMIN_KERNEL_VERSION=$(uname -r)` and `SUPERMIN_MODULES=/lib/modules/$(uname -r)`.
The tool should do this itself (or print the exact fix) when libguestfs fails.
(`sudo chmod +r /boot/vmlinuz-*` also works but is undone by kernel updates.)

## Vendoring

- Nothing from the old Docker image is needed any more (its `fs/` tree and
  tarballs are not in this repo).
- Base images are too large to commit. Pin download URL + sha256 and cache.
- Only if tier 3 returns: vendor `qemu-rpi-kernel` (kernel + DTB, ~5 MB) with
  checksums.
