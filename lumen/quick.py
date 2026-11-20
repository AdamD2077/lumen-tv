"""Quick settings: a side panel for the things you change mid-film.

It slides in over whatever is on screen (an app, a video, the launcher), so
volume and sound output can be changed without leaving. It is a layer-shell
overlay that takes the keyboard while open; closing it hands control back.

  Up / Down     move            Left / Right   change the value
  A             select          B              close
"""

from .layershell import LayerShell

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

from . import audio, ctl  # noqa: E402
from .sway import HOME_WS  # noqa: E402
from .widgets import V, icon, label  # noqa: E402

BAR_WIDTH = 220


class QuickRow(Gtk.Box):
    def __init__(self, glyph, title, on_ok=None, on_step=None):
        super().__init__(spacing=18)
        self.add_css_class("row")
        self.on_ok, self.on_step = on_ok, on_step
        self.append(icon(glyph, 28))
        self.title = label(title, "row-label", hexpand=True)
        self.append(self.title)
        self.extra = Gtk.Box(spacing=14, valign=Gtk.Align.CENTER)
        self.append(self.extra)
        self.value = label("", "row-value")
        self.append(self.value)


class QuickPanel(Gtk.Window):
    def __init__(self, shell):
        super().__init__(title="Lumen TV quick settings", decorated=False)
        self.shell = shell
        self.add_css_class("quick")
        self.layered = bool(LayerShell and LayerShell.is_supported())
        if self.layered:
            LayerShell.init_for_window(self)
            LayerShell.set_namespace(self, "lumen-quick")
            LayerShell.set_layer(self, LayerShell.Layer.OVERLAY)
            LayerShell.set_keyboard_mode(self, LayerShell.KeyboardMode.EXCLUSIVE)
            LayerShell.set_exclusive_zone(self, -1)
            for edge in (LayerShell.Edge.TOP, LayerShell.Edge.BOTTOM,
                         LayerShell.Edge.LEFT, LayerShell.Edge.RIGHT):
                LayerShell.set_anchor(self, edge, True)
        else:
            self.set_default_size(960, 720)

        # A dimmed view of what is behind, with the panel on the right.
        scrim = Gtk.Box(hexpand=True, vexpand=True)
        scrim.add_css_class("quick-scrim")
        scrim.append(Gtk.Box(hexpand=True))
        self.panel = Gtk.Box(orientation=V, spacing=10)
        self.panel.add_css_class("quick-panel")
        self.panel.set_size_request(560, -1)
        self.panel.set_hexpand(False)      # stay a side panel: rows inside want to expand
        scrim.append(self.panel)
        self.set_child(scrim)

        self.panel.append(label("Quick settings", "quick-title"))
        self.volume_row = QuickRow("volume", "Volume", on_ok=self.mute, on_step=self.volume)
        self.bar = Gtk.Box(valign=Gtk.Align.CENTER)
        self.bar.add_css_class("progress")
        self.bar.set_size_request(BAR_WIDTH, -1)
        self.fill = Gtk.Box(halign=Gtk.Align.START)
        self.fill.add_css_class("progress-fill")
        self.bar.append(self.fill)
        self.volume_row.extra.append(self.bar)
        self.output_row = QuickRow("display", "Sound output", on_ok=lambda: self.output(1),
                                   on_step=self.output)
        self.keyboard_row = QuickRow("search", "On-screen keyboard", on_ok=self.keyboard)
        self.close_row = QuickRow("close", "Close this app", on_ok=self.close_app)
        self.settings_row = QuickRow("settings", "Settings", on_ok=self.open_settings)
        self.power_row = QuickRow("power", "Power", on_ok=self.open_power)
        self.rows = [self.volume_row, self.output_row, self.keyboard_row, self.close_row,
                     self.settings_row, self.power_row]
        for row in self.rows:
            self.panel.append(row)
        self.panel.append(Gtk.Box(vexpand=True))
        self.panel.append(label("◀ ▶  change      A  select      B  close", "quick-hint"))
        self.index = 0
        self.outputs = []

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", lambda _c, keyval, _code, _state:
                     self.on_key(Gdk.keyval_name(keyval) or ""))
        self.add_controller(keys)

    # --- showing ---

    def toggle(self):
        if self.get_visible():
            self.set_visible(False)
        else:
            self.open()

    def open(self):
        focused = self.shell.sway.focused_workspace()
        self.close_row.set_visible(focused not in (None, HOME_WS))
        self.show_volume(audio.volume())
        self.outputs = audio.outputs()
        current = next((o["name"] for o in self.outputs if o["current"]), "")
        self.output_row.value.set_label(current or "None found")
        self.focus(0)
        self.set_visible(True)

    def visible_rows(self):
        return [row for row in self.rows if row.get_visible()]

    def focus(self, index):
        rows = self.visible_rows()
        self.index = min(max(index, 0), len(rows) - 1)
        for i, row in enumerate(rows):
            (row.add_css_class if i == self.index else row.remove_css_class)("focused")

    def on_key(self, name):
        row = self.visible_rows()[self.index]
        if name in ("Up", "KP_Up"):
            self.focus(self.index - 1)
        elif name in ("Down", "KP_Down"):
            self.focus(self.index + 1)
        elif name in ("Left", "KP_Left", "Right", "KP_Right"):
            if row.on_step:
                row.on_step(1 if "Right" in name else -1)
        elif name in ("Return", "KP_Enter", "space"):
            if row.on_ok:
                row.on_ok()
        elif name in ("Escape", "BackSpace", "Menu", "XF86Back"):
            self.set_visible(False)
        elif name in ("XF86AudioRaiseVolume", "XF86AudioLowerVolume"):
            self.volume(1 if "Raise" in name else -1)
        return True

    # --- actions ---

    def show_volume(self, state):
        percent, muted = state
        if percent is None:
            self.volume_row.value.set_label("No sound device")
            self.fill.set_size_request(0, -1)
            return
        self.volume_row.value.set_label("Muted" if muted else f"{percent}%")
        self.fill.set_size_request(round(BAR_WIDTH * min(percent, 100) / 100), -1)
        (self.bar.add_css_class if muted else self.bar.remove_css_class)("muted")

    def volume(self, step):
        self.show_volume(audio.change_volume(step))

    def mute(self):
        self.show_volume(audio.toggle_mute())

    def output(self, step):
        """Switch to the next (or previous) sound output."""
        if not self.outputs:
            return
        now = next((i for i, o in enumerate(self.outputs) if o["current"]), -1)
        chosen = self.outputs[(now + step) % len(self.outputs)]
        if audio.set_output(chosen["id"]):
            for output in self.outputs:
                output["current"] = output is chosen
            self.output_row.value.set_label(chosen["name"])
            self.show_volume(audio.volume())      # each output has its own volume

    def keyboard(self):
        self.set_visible(False)       # give the keyboard back to the app first
        ctl.send("osk toggle")

    def close_app(self):
        self.set_visible(False)
        self.shell.sway.close_focused_app()

    def open_settings(self):
        self.set_visible(False)
        self.shell.sway.go_home()
        self.shell.set_tab("settings")
        self.shell.focus_row = 0
        self.shell.apply_focus()

    def open_power(self):
        from . import settings
        from .widgets import ListPage
        self.open_settings()
        self.shell.push(ListPage(self.shell, "Power", lambda _p: settings.power_items(self.shell),
                                 glyph="power"))
