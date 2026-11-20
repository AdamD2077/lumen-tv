"""gtk4-layer-shell, loaded the way it must be.

The library has to be loaded before libwayland-client, which means before GTK:
import this module ahead of anything that imports Gtk. `LayerShell` is None
where the library is missing (development outside the TV session); callers
then fall back to ordinary windows.
"""

from ctypes import CDLL

try:
    CDLL("libgtk4-layer-shell.so.0")
except OSError:
    pass

import gi  # noqa: E402

try:
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell as LayerShell
except (ValueError, ImportError):
    LayerShell = None
