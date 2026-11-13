"""piimg - customize Raspberry Pi OS images offline. See docs/DESIGN.md."""
from .guest import Image, PiImgError

__all__ = ["Image", "PiImgError"]
