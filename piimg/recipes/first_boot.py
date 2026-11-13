"""Do what Raspberry Pi OS's first-boot setup does, offline.

A fresh Raspberry Pi OS image has a placeholder account `pi` (uid 1000, no
password, shell nologin) that nobody can log in as. On first boot one of these
turns it into a real account:

  - desktop images: lightdm logs in `rpi-first-boot-wizard`, whose desktop runs
    `piwiz` (country, language, time zone, keyboard, user name and password,
    wifi, default browser, updates, Bluetooth pairing);
  - console images: userconfig.service asks for a user name and password on tty8;
  - either: /boot/firmware/userconf.txt (`name:hash`, Raspberry Pi Imager's
    older method), or cloud-init's user-data (Imager's current method).

All of them end in userconf-pi: `/usr/lib/userconf-pi/userconf` renames `pi`,
sets its shell to /bin/bash and its password, and `cancel-rename` removes the
wizard (its autostart, its account, the sshd rename banner), disables
userconfig.service, re-enables getty@tty1 and sets the boot behaviour
(raspi-config B1-B4). This recipe does the same file changes without booting,
plus the wizard's localisation settings, so the card boots straight into a
usable system. See docs/DESIGN.md "First-boot setup".
"""
import sys

from . import register
from ..guest import PiImgError

WIZARD = "rpi-first-boot-wizard"


@register
class FirstBoot:
    name = "first-boot"
    help = "do the first-boot wizard's setup offline: user, password, shell, locale, keyboard, wifi, browser"

    def add_arguments(self, p):
        p.add_argument("--user", help="name for the first account (default: keep its current name, "
                                      "usually 'pi'); it is renamed like the wizard does")
        g = p.add_mutually_exclusive_group()
        a = g.add_argument("--password", help="login password (visible in your shell history; prefer the "
                                              "prompt or --password-stdin)")
        a.kind, a.stdin_flag = "secret", "--password-stdin"
        g.add_argument("--password-hash", help="an already hashed password ($6$..., $y$...)")
        a = g.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
        a.tui = False
        p.add_argument("--ssh-key", action="append", default=[], metavar="FILE",
                       help="public key file to authorize for the user (repeatable)")
        p.add_argument("--boot", choices=("console", "console-autologin", "desktop", "desktop-autologin"),
                       help="what to boot into (default: what the wizard would choose: desktop or console "
                            "as the image boots now, with autologin if the image had it)")
        p.add_argument("--locale", help="e.g. en_US.UTF-8, de_DE.UTF-8 (compiled on first boot if new)")
        p.add_argument("--timezone", help="e.g. Europe/Berlin, America/New_York")
        p.add_argument("--keyboard", help="keyboard layout, e.g. us, de, fr, gb")
        p.add_argument("--keyboard-variant", default="", help="layout variant, e.g. nodeadkeys (default none)")
        p.add_argument("--keyboard-model", default="pc105", help="keyboard model (default pc105)")
        p.add_argument("--wifi-country", help="two-letter country for wifi, e.g. US, DE, GB")
        p.add_argument("--wifi-ssid", help="wifi network to connect to")
        a = p.add_argument("--wifi-password", help="its password (WPA); leave out for an open network")
        a.kind = "secret"
        p.add_argument("--hostname", help="new hostname")
        p.add_argument("--browser", choices=("chromium", "firefox"), help="default web browser (desktop)")
        p.add_argument("--ssh", action="store_true", help="enable the ssh server")
        p.add_argument("--bt-autopair", action="store_true",
                       help="pair a Bluetooth keyboard and mouse automatically on next boot")

    # ------------------------------------------------------------------

    def apply(self, img, args):
        from .. import system as S
        from .userdata import hash_password

        say = print
        first = next((u for u in S.passwd_entries(img) if u["uid"] == 1000), None)
        if first is None:
            raise PiImgError("This image has no first account (uid 1000) to set up")
        old, new = first["name"], args.user or first["name"]

        pwhash = args.password_hash
        if args.password_stdin:
            line = sys.stdin.readline().rstrip("\r\n")
            pwhash = hash_password(line) if line else None
        elif args.password:
            pwhash = hash_password(args.password)
        if not pwhash and not args.ssh_key:
            raise PiImgError("Give --password, --password-stdin, --password-hash or --ssh-key, or nobody "
                             "will be able to log in")

        self._check_cloud_init(img)
        # Decide the boot mode before anything changes what the image looks like now.
        desktop_now = S.default_target(img).startswith("graphical") and img.exists(S.LIGHTDM_CONF)
        autologin_flag = img.exists("/var/lib/userconf-pi/autologin")
        mode = args.boot or ("desktop" if desktop_now else "console") + ("-autologin" if autologin_flag else "")

        # --- userconf: rename, shell, password
        if new != old:
            say(S.rename_user(img, old, new))
        say(S.set_shell(img, new, "/bin/bash"))
        if pwhash:
            say(S.set_password(img, new, pwhash=pwhash, unlock=True).splitlines()[0])
        if args.ssh_key:
            keys = []
            for f in args.ssh_key:
                keys += [l for l in open(f).read().splitlines() if l.strip()]
            say(S.add_ssh_keys(img, new, keys).splitlines()[0])

        # --- cancel-rename: the wizard goes away
        say(S.set_boot_mode(img, mode, new))
        removed = []
        for path in ("/etc/xdg/autostart/piwiz.desktop", "/etc/sudoers.d/010_wiz-nopasswd",
                     "/etc/ssh/sshd_config.d/rename_user.conf", "/var/lib/userconf-pi/autologin",
                     "/etc/systemd/system/del-wizard.service"):
            if img.lexists(path):
                img.g.rm(path)
                removed.append(path)
        if img.g.is_dir("/var/lib/userconf-pi") and not img.listdir("/var/lib/userconf-pi"):
            img.g.rmdir("/var/lib/userconf-pi")
        if S.find_user(img, WIZARD):
            say(S.delete_user(img, WIZARD))
        img.disable_unit("userconfig.service")
        img.disable_unit("del-wizard.service")
        if img.find_unit("getty@tty1.service"):
            img.enable_unit("getty@tty1.service")
        for f in (f"{img.boot}/userconf.txt", f"{img.boot}/userconf"):
            if img.exists(f):
                img.g.rm(f)
                removed.append(f)
        say("first-boot wizard removed" + (f" ({', '.join(removed)})" if removed else ""))


        # --- the wizard's other pages
        if args.locale:
            if S.set_locale(img, args.locale):
                S.schedule_locale_gen(img)
                say(f"locale {args.locale} (compiled on first boot)")
            else:
                say(f"locale {args.locale}")
        if args.timezone:
            S.set_timezone(img, args.timezone)
            say(f"time zone {args.timezone}")
        if args.keyboard:
            S.set_keyboard(img, args.keyboard, args.keyboard_model, args.keyboard_variant, user=new)
            say(f"keyboard {args.keyboard_model} {args.keyboard}" + (f" ({args.keyboard_variant})"
                                                                   if args.keyboard_variant else ""))
        if args.wifi_ssid:
            say(S.add_wifi(img, args.wifi_ssid, args.wifi_password or None, args.wifi_country))
        elif args.wifi_country:
            S.set_wifi_country(img, args.wifi_country)
            say(f"wifi country {args.wifi_country.upper()}")
        if args.hostname:
            say(S.set_hostname(img, args.hostname).splitlines()[0])
        if args.browser:
            S.set_browser(img, args.browser, new)
            say(f"default browser {args.browser}")
        if args.ssh:
            img.enable_unit("ssh.service")
            say("ssh enabled")
        if args.bt_autopair:
            img.write(f"{img.boot}/btautopair", "")
            say("Bluetooth keyboard/mouse auto-pairing on next boot")

    @staticmethod
    def _check_cloud_init(img):
        """A user-data that creates users makes cloud-init run userconf-pi on first boot
        (Pi OS's cloud-init renames uid 1000 to the first user listed and locks its
        password unless lock_passwd: false), which would undo or fight this recipe."""
        from ..system import cloud_init_userdata
        if not img.exists("/usr/bin/cloud-init"):
            return
        doc = cloud_init_userdata(img)
        if doc and doc.get("users"):
            raise PiImgError(f"{img.boot}/user-data creates users itself, so cloud-init would set up the "
                             "first account again on first boot. Use either `apply userdata` or "
                             "`apply first-boot`, not both")
