#!/usr/bin/env python3
"""Open the launcher, walk through it and save PNG screenshots (development aid).

    tools/screenshot.py OUT_DIR

Needs a desktop session with a display of at least 1920x1080. It runs with the
default configuration and without sway, so nothing is launched or changed.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.pop("SWAYSOCK", None)
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()   # start from the default config

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gsk", "4.0")
gi.require_version("Graphene", "1.0")
from gi.repository import GLib, Graphene, Gsk, Gtk  # noqa: E402

from lumen import apps, osk_view, shell  # noqa: E402

apps.flatpak_dirs = lambda: []      # only the default apps, not this computer's own

# (file name, GDK key names to press before taking the shot)
SHOTS = [
    ("home", ""),
    ("home-row2", "Right Right Down"),
    ("apps", "Up Up Up Right Down"),
    ("settings", "Up Up Right Down Down"),
    ("app-menu", "Escape Menu"),
    ("search", "Escape Up Left Down"),
]


def pump(ms):
    loop = GLib.MainLoop()
    GLib.timeout_add(ms, loop.quit)
    loop.run()


def snap(widget, path, width=None):
    # The desktop may give us a narrower window than a TV. The content still
    # lays out at its full 1920 minimum width, so render it past the window edge.
    width, height = width or widget.get_width(), widget.get_height()
    snapshot = Gtk.Snapshot()
    widget.get_parent().snapshot_child(widget, snapshot)
    node = snapshot.to_node()
    if node is None:
        print("EMPTY", path)
        return
    renderer = Gsk.CairoRenderer()
    renderer.realize(None)
    rect = Graphene.Rect().init(0, 0, width, height)
    renderer.render_texture(node, rect).save_to_png(path)
    renderer.unrealize()
    print("wrote", path, f"{width}x{height}")


def shoot(app, out_dir):
    win = shell.Shell(app)
    win.get_child().set_size_request(1920, 1080)
    win.present()
    pump(3500)                       # let it start fetching web app icons...
    for _ in range(60):              # ...and finish
        if not win.fetching_icons:
            break
        pump(500)
    pump(600)
    for name, keys in SHOTS:
        for key in keys.split():
            win.on_key(key)
            pump(120)
        if name == "search":
            win.tab_pages["search"].entry.set_text("you")
        pump(700)
        snap(win.get_child(), os.path.join(out_dir, f"{name}.png"), 1920)

    class NoOutput:
        def tap(self, *_):
            pass
        type_char = tap

    osk = osk_view.OskController(NoOutput())
    osk.show()
    osk.model.move("right")
    osk.window.refresh()
    pump(600)
    panel = osk.window.get_child()
    snap(panel, os.path.join(out_dir, "keyboard.png"), panel.get_preferred_size()[1].width)
    osk.hide()


def main(out_dir):
    os.makedirs(out_dir, exist_ok=True)
    app = shell.Application()

    def activate(_app):
        try:
            shoot(app, out_dir)
        finally:
            app.quit()

    app.connect("activate", activate)
    app.run([])


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "screenshots")
