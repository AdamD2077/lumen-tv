"""The launch screen: shown the moment an app is opened, until its window is up.

Apps take a few seconds to start. Without this the screen would sit black for
that time, which reads as "nothing happened". The splash is a layer-shell
overlay, so it stays on top while sway switches workspace underneath it.
"""

from .layershell import LayerShell

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk  # noqa: E402

from .widgets import V, icon  # noqa: E402

GIVE_UP_SECONDS = 12


class Splash(Gtk.Window):
    def __init__(self):
        super().__init__(title="Lumen TV", decorated=False)
        self.add_css_class("splash")
        self.set_focusable(False)
        self.layered = bool(LayerShell and LayerShell.is_supported())
        if self.layered:
            LayerShell.init_for_window(self)
            LayerShell.set_namespace(self, "lumen-splash")
            LayerShell.set_layer(self, LayerShell.Layer.OVERLAY)
            LayerShell.set_keyboard_mode(self, LayerShell.KeyboardMode.NONE)
            LayerShell.set_exclusive_zone(self, -1)
            for edge in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM,
                         LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
                LayerShell.set_anchor(self, edge, True)
        else:
            self.set_default_size(960, 540)

        self.backdrop = Gtk.Box(hexpand=True, vexpand=True)
        self.backdrop.add_css_class("splash-backdrop")
        self.content = Gtk.Box(orientation=V, spacing=22, hexpand=True,
                               halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        self.content.add_css_class("splash-content")
        self.art = Gtk.Box(halign=Gtk.Align.CENTER)
        self.name = Gtk.Label()
        self.name.add_css_class("splash-name")
        spinner = Gtk.Box(halign=Gtk.Align.CENTER)
        spinner.add_css_class("spinner")
        for widget in (self.art, self.name, spinner):
            self.content.append(widget)
        self.backdrop.append(self.content)
        self.set_child(self.backdrop)
        self.accent = None
        self.timer = 0

    def show_for(self, app):
        if self.accent:
            self.backdrop.remove_css_class(self.accent)
        self.accent = f"acc-{app.css}"
        self.backdrop.add_css_class(self.accent)
        while child := self.art.get_first_child():
            self.art.remove(child)
        if app.icon_path:
            picture = Gtk.Image.new_from_file(app.icon_path)
            picture.set_pixel_size(132)
            frame = Gtk.Box(css_classes=["app-icon"])
            frame.set_overflow(Gtk.Overflow.HIDDEN)
            frame.append(picture)
            self.art.append(frame)
        elif app.icon:
            self.art.append(icon(app.icon, 110))
        self.name.set_label(app.name)
        self.backdrop.remove_css_class("leaving")
        self._cancel()
        # Never get stuck on screen if the app fails to show a window.
        self.timer = GLib.timeout_add_seconds(GIVE_UP_SECONDS, self._hide)
        self.set_visible(True)

    def _cancel(self):
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0

    def _hide(self):
        self.timer = 0
        self.set_visible(False)
        return False

    def _leave(self):
        self.backdrop.add_css_class("leaving")       # fades out (see style.css)
        self.timer = GLib.timeout_add(220, self._hide)
        return False

    def dismiss(self, delay_ms=0):
        """Fade out, after giving the new window `delay_ms` to draw itself."""
        if not self.get_visible():
            return
        self._cancel()
        if delay_ms:
            self.timer = GLib.timeout_add(delay_ms, self._leave)
        else:
            self._leave()
