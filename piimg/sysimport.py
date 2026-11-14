"""Import Python packages that come from the distro, even inside a venv.

Some bindings piimg uses are compiled extensions for the distro Python that
are not on PyPI (libguestfs: python3-guestfs) or are awkward to build there
(PyGObject: python3-gi needs the GObject-introspection and cairo headers). A
plain `uv sync` venv does not include them. If the normal import fails, look
for them in the distro's package directories and add that directory to the
*end* of sys.path, so venv packages still win. This only works when the venv
runs the same Python minor version the distro package was built for.
"""
import importlib
import sys
from pathlib import Path


def _candidates():
    v = f"{sys.version_info.major}.{sys.version_info.minor}"
    return [
        "/usr/lib/python3/dist-packages",                  # Debian/Ubuntu
        f"/usr/lib/python{v}/site-packages",               # Fedora/Arch (noarch dir)
        f"/usr/lib64/python{v}/site-packages",             # Fedora/RHEL (arch dir)
        f"/usr/local/lib/python{v}/dist-packages",
        f"/usr/local/lib/python{v}/site-packages",
    ]


def import_system(name, marker=None):
    """Import module `name`, falling back to the distro's package directories.

    marker: the file that shows a directory holds the package (default
    NAME.py, or NAME/__init__.py for a package). Raises ImportError if it
    cannot be found or loaded anywhere.
    """
    try:
        return importlib.import_module(name)
    except ImportError as e:
        first = e
    markers = [marker] if marker else [f"{name}.py", f"{name}/__init__.py"]
    for d in _candidates():
        if d in sys.path or not any((Path(d) / m).is_file() for m in markers):
            continue
        sys.path.append(d)
        try:
            return importlib.import_module(name)
        except ImportError:
            sys.path.remove(d)  # wrong ABI / broken install; try the next one
            for k in [k for k in sys.modules if k == name or k.startswith(name + ".")]:
                del sys.modules[k]
    raise first
