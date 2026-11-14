# Recipe files

A recipe is a YAML file describing changes to an image. Run one with

    python3 -m piimg run work.qcow2 path/to/recipe.yaml [--var NAME=VALUE ...] [--dry-run]
    python3 -m piimg run work.qcow2 pitft --var display=35r      # bundled recipe, by name
    python3 -m piimg recipes                                      # list bundled recipes
    python3 -m piimg recipe-edit my-recipe.yaml --image work.qcow2 # edit (and test) in the TUI editor

`--dry-run` opens the image read-only, evaluates every condition and prints
what each step would do. Examples: `piimg/recipes/pitft.yaml` (small, uses
variables and branches) and `examples/demo/recipe.yaml` (every kind of step).

## Shape

```yaml
name: Human readable title
vars:                          # optional; passed with --var NAME=VALUE
  rotation: {default: 90, choices: [0, 90, 180, 270]}
  token:    {required: true}
steps:
  - <action>: <argument>       # exactly one action per step
```

Every step may also carry `name:` (a label), `when:` (run only if true) and
`ignore_errors: true`. Steps run in order and stop at the first error.

`{{var}}` is replaced in all strings (paths, lines, ...) except `write`
content (see below) and `live` scripts. `{{var|fallback}}` uses a fallback if
the variable is unset. `{{boot}}` is always defined: the boot partition's
mount point in the image (`/boot/firmware` on bookworm/trixie, `/boot` before).

Quote file modes (`"0755"`). Unquoted YAML `0755` also works, but `755` is
read as decimal.

## Conditions (`when:`)

One key each; combine with `all` / `any` / `not`.

| condition | true when |
|---|---|
| `exists: PATH` | path exists (a dangling symlink counts) |
| `grep: {path, pattern}` | regex matches somewhere in the file (false if the file is missing) |
| `package: NAME` | the Debian package is installed (read from `dpkg/status`) |
| `os_release: {VERSION_CODENAME: trixie}` | every key matches `/etc/os-release` |
| `var: {name, equals \| in \| set}` | recipe variable test |
| `not: COND`, `all: [..]`, `any: [..]` | boolean combinations |

Branching: put `then:` and/or `else:` (lists of steps) next to `when:`.

```yaml
- when: {grep: {path: "{{boot}}/config.txt", pattern: '^dtparam=spi=on'}}
  then: [{echo: "already on"}]
  else: [{raspi_config: ["do_spi 0"]}]
```

## Actions

All paths are paths inside the image.

| action | argument |
|---|---|
| `echo` | message |
| `fail` | message; aborts the recipe (use with `when`) |
| `mkdir` | `PATH` or `{path, mode, owner}` |
| `write` | `{path, content \| content_file, mode, owner, if_exists: overwrite\|skip\|error, template: bool}`. `content` is literal (no `{{ }}` replacement) unless `template: true`, so shell scripts are safe. `content_file` is relative to the recipe file |
| `copy` | `{src, dest, mode, owner}`: a file, or a whole directory's contents (modes and symlinks kept). `src` is relative to the recipe file |
| `symlink` | `{path, target}` (replaces an existing link) |
| `remove` | `PATH` (recursive) |
| `chmod` / `chown` | `{path, mode}` / `{path, owner: "user:group"}`. Names are resolved from the *image's* passwd/group: a user that only cloud-init creates at first boot does not exist yet, so use numbers (e.g. `1000:1000`) |
| `line` | `{path, line, match, state: present\|absent}`: like Ansible lineinfile. With `match`, the first matching line is replaced (else `line` is appended) |
| `sub` | `{path, pattern, replace, count, must_match}`: regex substitution (Python syntax, multiline) |
| `config_txt` | `{add: [..], remove: [..], set: {key: value}, section: all, content: "..."}`. `set` is raspi-config's `set_config_var` (rewrites commented or active `key=` lines); `content` replaces the whole file first |
| `cmdline` | `{add: [..], remove: [..], set: "..."}` kernel parameters in `cmdline.txt`. A trailing `=` in `remove` removes any value; `set` replaces the whole line first |
| `modules` | module name(s) to load at boot (`/etc/modules-load.d/piimg.conf`) |
| `systemd` | `{enable: [..], disable: [..], mask: [..], unmask: [..]}`: creates/removes the symlinks `systemctl` would, no running systemd needed; applied in the order unmask, disable, mask, enable |
| `user` | `{name: pi, password_hash: $6$..., lock: true, unlock: true, shell: /bin/bash, add_keys: [..], remove_keys: [..], rename: NEW, delete: true}` changes an existing account (create the first one with `first_boot`/`userdata`). Hash a password with `openssl passwd -6`; `delete` also removes the home folder unless `remove_home: false` |
| `wifi` | `{add: [{ssid, password, hidden, priority}], remove: [profile], set: {profile: {priority: N, autoconnect: false}}, country: GB}` NetworkManager profiles, like `piimg wifi` |
| `raspi_config` | list of `raspi-config nonint` calls, e.g. `["do_i2c 0", "do_hostname mypi"]`. **0 = enable, 1 = disable**, as in raspi-config. Supported: `do_i2c do_spi do_ssh do_serial_hw do_serial_cons do_onewire do_hostname do_boot_behaviour B1..B4 do_wifi_country XX do_change_locale do_change_timezone do_configure_keyboard` (B2/B4 autologin user: the recipe variable `autologin_user`, else the first regular user). Anything else errors and tells you to use `live` |
| `recipe` | another recipe file (relative path or bundled name), or `{name, vars}` |
| `python_recipe` | a built-in Python recipe: `{name: gadget-net, args: {iface: usb0}}` |
| `live` | boot the image and run a script as root. See below |

## `live`: running code inside the image (last resort)

```yaml
- live:
    script: |                  # or script_file: path/relative/to/recipe
      apt-get update && apt-get install -y i2c-tools
    network: true              # default true
    timeout: 1800              # seconds
    env: {FOO: bar}
    allow_failure: false       # true = a non-zero exit does not abort
    template: false            # true = {{var}} replaced in the script
```

Use it only for things that must execute arm code: `apt-get install` (package
maintainer scripts), compiling, running a vendor installer you have not
translated. Prefer the offline actions above whenever you can; a live step is
slow (an `apt-get update` plus one small package took 6 minutes under
emulation).

How it works (`piimg/live.py`): the script and a tiny init are injected into
the image, QEMU (`raspi3b`) boots the image's own kernel with
`init=/usr/lib/piimg/live-init`, **not systemd**, so cloud-init's first-boot
pass is not consumed. The guest runs your script, then signals completion twice:
a `PIIMG-DONE rc=N` line on the console and a `/var/lib/piimg/done` marker file
that the host reads back from the filesystem after the VM stops. The script's
exit code becomes the step's result. Everything injected is removed afterwards;
if the host process is killed mid-run, `piimg clean IMAGE` removes the leftovers.

Properties and limits:

- Console output streams to your terminal. **Input does not work** (serial
  receive is broken in this QEMU/Pi-kernel combination), so scripts must be
  non-interactive: use `-y`, `DEBIAN_FRONTEND=noninteractive`, and so on.
- Networking is QEMU user-mode NAT over an emulated USB NIC (`usb0`,
  10.0.2.15). The guest clock is set from the host (the emulated Pi has no RTC;
  without this apt rejects every signature).
- systemd is not running: `systemctl enable` works (it only makes symlinks),
  but starting services or `systemctl is-active` does not. Packages' "restart
  the service" steps are skipped, which is what you want in an image.
- 64-bit images only (needs `kernel8.img`; QEMU machine `raspi3b`).
- The image is modified in place, so apply recipes to a qcow2 overlay.

## Translating a vendor / HAT setup script

Walk the script top to bottom and replace each idiom:

| the script does | use |
|---|---|
| `raspi-config nonint do_i2c 0` (spi, ssh, serial, 1-wire, hostname) | `raspi_config: ["do_i2c 0"]` |
| `echo "dtoverlay=foo" >> /boot/config.txt` (also `/boot/firmware/config.txt`) | `config_txt: {add: "dtoverlay=foo"}` (right section, no duplicates; `{{boot}}` handles the path) |
| `sed -i 's/$/ foo=1/' /boot/cmdline.txt` | `cmdline: {add: foo=1}` |
| `echo i2c-dev >> /etc/modules` | `modules: [i2c-dev]` |
| `cat > /etc/foo <<EOF ... EOF`, `cp`, `install -m` | `write` / `copy` |
| `ln -s`, `chmod`, `chown`, `mkdir -p`, `rm -rf` | `symlink`, `chmod`, `chown`, `mkdir`, `remove` |
| `sed -i 's/a/b/' file`, `grep -q x file \|\| echo x >> file` | `sub`, `line` |
| `systemctl enable foo` | `systemd: {enable: [foo.service]}` |
| `if grep -q ...; then ... else ... fi` | `when` / `then` / `else` |
| `apt-get install pkg` | `live` (or, if the device has internet at first boot, cloud-init `packages:` via the `userdata` recipe) |
| `git clone ... && make && make install`, `pip install`, `curl ... \| bash` | `live`, or put the commands in a `script:` unchanged |
| `reboot`, `systemctl start foo`, `modprobe`, `dtoverlay` (runtime), `i2cdetect` | drop it: it only matters on a running device; the config edits above take effect on the device's first boot |
| sets a password / creates a user | the `userdata` recipe (cloud-init); there is no default user on current images |

A vendor script that is mostly the last few rows can be pasted into one `live`
step unchanged. Translate the rest offline and keep `live` for only the lines
that need it: it keeps image builds fast and reproducible.
