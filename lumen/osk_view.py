"""On-screen keyboard window (GTK4 layer-shell overlay) and its controller."""

# gtk4-layer-shell has to be loaded before libwayland-client, i.e. before GTK.
from ctypes import CDLL

try:
    CDLL("libgtk4-layer-shell.so.0")
except OSError:
    pass

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gtk  # noqa: E402

try:
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell as LayerShell
except (ValueError, ImportError):
    LayerShell = None  # development outside the TV session: a plain window

from .config import PKG_DATA  # noqa: E402
from .osk import COLS, OskModel  # noqa: E402

HINT = "A  type      B  close      LB  shift      RB  symbols      Start  enter"


class OskWindow(Gtk.Window):
    def __init__(self, model, on_click):
        super().__init__(title="Lumen keyboard", decorated=False, resizable=False)
        self.model = model
        self.on_click = on_click
        self.at_bottom = True
        self.add_css_class("osk")
        self.set_focusable(False)

        provider = Gtk.CssProvider()
        provider.load_from_path(str(PKG_DATA / "osk.css"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self.layered = bool(LayerShell and LayerShell.is_supported())
        if self.layered:
            LayerShell.init_for_window(self)
            LayerShell.set_namespace(self, "lumen-osk")
            LayerShell.set_layer(self, LayerShell.Layer.OVERLAY)
            # Never take keyboard focus: the keys we type must reach the app.
            LayerShell.set_keyboard_mode(self, LayerShell.KeyboardMode.NONE)
            self._place()

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        panel.add_css_class("panel")
        self.grid = Gtk.Grid(row_spacing=8, column_spacing=8, column_homogeneous=True)
        panel.append(self.grid)
        hint = Gtk.Label(label=HINT)
        hint.add_css_class("hint")
        panel.append(hint)
        self.set_child(panel)
        self.buttons = []
        self.layer = None
        self.refresh()

    def _place(self):
        for edge, on in ((LayerShell.Edge.BOTTOM, self.at_bottom),
                         (LayerShell.Edge.TOP, not self.at_bottom)):
            LayerShell.set_anchor(self, edge, on)
            LayerShell.set_margin(self, edge, 40)

    def flip(self):
        """Move to the other screen edge, for when it covers the text field."""
        self.at_bottom = not self.at_bottom
        if self.layered:
            self._place()

    def _build(self):
        for row in self.buttons:
            for button in row:
                self.grid.remove(button)
        self.buttons = []
        for r, keys in enumerate(self.model.rows):
            x, row = 0, []
            for c, key in enumerate(keys):
                button = Gtk.Button(can_focus=False)
                button.add_css_class("key")
                if key.action != "char":
                    button.add_css_class("special")
                button.set_size_request(key.span * 38 - 8, 62)
                button.connect("clicked", lambda _b, r=r, c=c: self.on_click(r, c))
                self.grid.attach(button, x, r, key.span, 1)
                x += key.span
                row.append(button)
            assert x == COLS, f"keyboard row {r} is {x} units wide"
            self.buttons.append(row)
        self.layer = self.model.layer

    def refresh(self):
        if self.layer != self.model.layer:
            self._build()
        for r, keys in enumerate(self.model.rows):
            for c, key in enumerate(keys):
                button = self.buttons[r][c]
                button.set_label(self.model.label(key))
                selected = (r, c) == (self.model.row, self.model.col)
                (button.add_css_class if selected else button.remove_css_class)("sel")
                active = key.action == "shift" and self.model.shift
                (button.add_css_class if active else button.remove_css_class)("on")


class OskController:
    """What lumen-input's Mapper drives: model + window + the virtual keyboard."""

    TAPS = {"space": "KEY_SPACE", "left": "KEY_LEFT", "right": "KEY_RIGHT",
            "backspace": "KEY_BACKSPACE", "enter": "KEY_ENTER"}

    def __init__(self, out):
        self.out = out
        self.model = OskModel()
        self.window = OskWindow(self.model, self._clicked)

    @property
    def visible(self):
        return self.window.get_visible()

    def show(self):
        self.window.refresh()
        self.window.set_visible(True)

    def hide(self):
        self.window.set_visible(False)

    def flip(self):
        self.window.flip()

    def move(self, direction):
        self.model.move(direction)
        self.window.refresh()

    def shift(self):
        self.model.toggle_shift()
        self.window.refresh()

    def layer(self):
        self.model.toggle_layer()
        self.window.refresh()

    def press(self):
        action, text = self.model.press()
        if action == "char":
            self.out.type_char(text)
        elif action == "done":
            self.hide()
        elif action in self.TAPS:
            self.out.tap(self.TAPS[action])
        self.window.refresh()

    def _clicked(self, row, col):
        # Pointer / touch: select the key, then press it.
        self.model.row, self.model.col = row, col
        self.press()
