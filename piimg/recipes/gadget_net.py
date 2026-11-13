"""USB Ethernet gadget networking, done offline.

This reproduces what Raspberry Pi OS's own `rpi-usb-gadget on` does on the
device (trixie ships that package), as plain file edits so no emulation or
first boot is needed:
  - /etc/modules-load.d/usb-gadget.conf  -> load g_ether
  - config.txt: dtoverlay=dwc2,dr_mode=peripheral
  - NetworkManager profiles "USB Gadget (client)" / "(shared)" for usb0
  - dnsmasq lease snippet for the shared profile
  - enable rpi-usb-gadget-ics.service (switches client<->shared automatically)
"""
from . import register
from ..guest import PiImgError

OVERLAY = "dtoverlay=dwc2,dr_mode=peripheral"
DNSMASQ_SNIPPET = """\
# Written by piimg (mirrors rpi-usb-gadget) - safe to delete

# controls server behaviour (send NAKs immediately, faster failover)
dhcp-authoritative
# 2 minute leases (clients should renew every 1 minute)
dhcp-option=51,120
# suppresses dnsmasq's DHCP chatter in logs
quiet-dhcp
"""


@register
class GadgetNet:
    name = "gadget-net"
    help = "USB Ethernet gadget (the Pi shows up as a network adapter on the host)"

    def add_arguments(self, p):
        p.add_argument("--iface", default="usb0", help="gadget interface name (default: usb0)")
        p.add_argument("--address", default="10.12.194.1/28",
                       help="Pi's address when it shares its connection (default: 10.12.194.1/28)")
        p.add_argument("--shared-only", action="store_true",
                       help="only create the 'shared' profile (Pi runs DHCP+NAT); no client profile "
                            "and no client<->shared auto-switch service")

    def apply(self, img, args):
        if not img.exists("/usr/lib/systemd/system/rpi-usb-gadget-ics.service") and not args.shared_only:
            raise PiImgError("This image has no rpi-usb-gadget package (pre-trixie?). "
                             "Use --shared-only, which only needs NetworkManager.")
        if not img.exists("/etc/NetworkManager"):
            raise PiImgError("This image has no NetworkManager; only NM-based images "
                             "(bookworm and later) are supported by this recipe.")

        img.write("/etc/modules-load.d/usb-gadget.conf", "g_ether\n")
        img.config_txt_add(OVERLAY, section="all")

        img.write("/etc/NetworkManager/dnsmasq-shared.d/90-rpi-usb-gadget-lease.conf", DNSMASQ_SNIPPET)

        ipv6 = {"method": "disabled"}
        img.nm_connection("USB Gadget (shared)", {
            "connection": {"type": "ethernet", "interface-name": args.iface,
                           "autoconnect": "true", "autoconnect-priority": 10},
            "ethernet": {},
            "ipv4": {"address1": args.address, "method": "shared"},
            "ipv6": ipv6,
            "proxy": {},
        })
        if not args.shared_only:
            img.nm_connection("USB Gadget (client)", {
                "connection": {"type": "ethernet", "interface-name": args.iface,
                               "autoconnect": "false", "autoconnect-priority": 100},
                "ethernet": {},
                "ipv4": {"method": "auto", "may-fail": "true", "route-metric": 100, "dhcp-timeout": 6},
                "ipv6": ipv6,
                "proxy": {},
            })
            img.enable_unit("rpi-usb-gadget-ics.service")
