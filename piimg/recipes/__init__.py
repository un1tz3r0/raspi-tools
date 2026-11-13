"""Recipes: small, declarative image customizations.

A recipe is a class with `name`, `help`, `add_arguments(parser)` and
`apply(img, args)`. Register it with @register. `img` is an open
piimg.Image; recipes must only edit files (offline first, see DESIGN.md).
"""
REGISTRY = {}


def register(cls):
    REGISTRY[cls.name] = cls()
    return cls


from . import first_boot, gadget_net, userdata  # noqa: E402,F401  (register built-ins)
