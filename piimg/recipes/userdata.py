"""First-boot account/hostname/ssh setup via cloud-init user-data.

Current Raspberry Pi OS has no default login: the first user comes from
cloud-init's NoCloud seed on the boot partition. Without this (or an
equivalent made by Raspberry Pi Imager) a gadget image can't be logged into.
"""
import json
import shutil
import subprocess
from . import register
from ..guest import PiImgError

DEFAULT_GROUPS = "users,adm,dialout,audio,netdev,video,plugdev,cdrom,games,input,gpio,spi,i2c,render,sudo"


def hash_password(pw):
    if not shutil.which("openssl"):
        raise PiImgError("openssl is needed to hash --password (or pass --password-hash)")
    r = subprocess.run(["openssl", "passwd", "-6", "-stdin"], input=pw, text=True,
                       capture_output=True, check=True)
    return r.stdout.strip()


@register
class UserData:
    name = "userdata"
    help = "create the first user, hostname and ssh via cloud-init user-data"

    def add_arguments(self, p):
        p.add_argument("--user", default="pi")
        g = p.add_mutually_exclusive_group()
        g.add_argument("--password", help="login password (hashed with openssl; visible in your shell history)")
        g.add_argument("--password-hash", help="already-hashed password (crypt format, e.g. $6$...)")
        p.add_argument("--ssh-key", action="append", default=[], metavar="FILE",
                       help="public key file to authorize (repeatable)")
        p.add_argument("--hostname")
        p.add_argument("--no-ssh", action="store_true", help="do not enable the ssh server")
        p.add_argument("--overwrite", action="store_true",
                       help="replace user-data even if it has non-comment content")

    def apply(self, img, args):
        path = f"{img.boot}/user-data"
        if img.exists(path) and not args.overwrite:
            live = [l for l in img.read_text(path).splitlines() if l.strip() and not l.lstrip().startswith("#")]
            if live:
                raise PiImgError(f"{path} already has settings; use --overwrite to replace it")
        if not (args.password or args.password_hash or args.ssh_key):
            raise PiImgError("Give --password, --password-hash or --ssh-key, or you will not be able to log in")

        user = {"name": args.user, "groups": DEFAULT_GROUPS, "shell": "/bin/bash",
                "sudo": "ALL=(ALL) NOPASSWD:ALL", "lock_passwd": not (args.password or args.password_hash)}
        pwhash = args.password_hash or (hash_password(args.password) if args.password else None)
        if pwhash:
            user["passwd"] = pwhash
        keys = [open(f).read().strip() for f in args.ssh_key]
        if keys:
            user["ssh_authorized_keys"] = keys
        doc = {"manage_etc_hosts": True, "enable_ssh": not args.no_ssh,
               "ssh_pwauth": bool(pwhash), "users": [user]}
        if args.hostname:
            doc["hostname"] = args.hostname
        # JSON is valid YAML, so no PyYAML dependency is needed
        img.write(path, "#cloud-config\n" + json.dumps(doc, indent=2) + "\n")
