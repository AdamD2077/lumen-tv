"""lumen-shell: the launcher. A full-screen, D-pad driven home screen."""

import os
import socket
import sys
import time
import urllib.parse

from . import layershell  # noqa: F401  (must come before GTK is imported)

import gi  # noqa: E402

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

from . import apps as catalog  # noqa: E402
from . import config, ctl, settings  # noqa: E402
from .quick import QuickPanel  # noqa: E402
from .splash import Splash  # noqa: E402
from .sway import HOME_WS, Sway, read_message  # noqa: E402
from .widgets import (GRID_COLS, GAP, SIDE, AppRow, Card, Item, ListPage, ListRow,  # noqa: E402
                      V, animate, icon, label, run_async, scroll_to_center, vscroller)

TABS = [("search", "Search", "search"), ("home", "Home", ""), ("apps", "Apps", ""),
        ("settings", "Settings", "")]

KEYS = {
    "Up": "up", "Down": "down", "Left": "left", "Right": "right",
    "KP_Up": "up", "KP_Down": "down", "KP_Left": "left", "KP_Right": "right",
    "Return": "ok", "KP_Enter": "ok", "space": "ok",
    "Escape": "back", "BackSpace": "back", "XF86Back": "back",
    "Menu": "menu", "F10": "menu", "XF86HomePage": "home", "Home": "home",
}


class HomePage(Gtk.Box):
    modal = False
    keep_column = False

    def __init__(self, shell):
        super().__init__(orientation=V)
        self.shell = shell
        self.content = Gtk.Box(orientation=V, margin_bottom=120)
        self.scroller = vscroller(self.content)
        self.append(self.scroller)
        self.rows = []

        self.hero = Gtk.Box(orientation=V, spacing=6, margin_start=SIDE, valign=Gtk.Align.END)
        self.hero.set_size_request(-1, 270)
        self.hero.add_css_class("hero")
        self.hero_step = 0
        self.hero_kind = label("", "hero-kind")
        self.hero_title = label("", "hero-title")
        self.hero_desc = label("", "hero-desc", wrap=True, max_width_chars=60)
        self.hero.append(Gtk.Box(vexpand=True))     # push the text to the bottom
        for widget in (self.hero_kind, self.hero_title, self.hero_desc):
            self.hero.append(widget)
        self.build()

    def build(self):
        while child := self.content.get_first_child():
            self.content.remove(child)
        shell = self.shell
        by_id = {a.id: a for a in shell.apps}
        favorites = [by_id[i] for i in shell.cfg["favorites"] if i in by_id]
        groups = [
            ("Favorites", favorites),
            ("Apps", [a for a in shell.apps if a.kind == "flatpak"]),
            ("Android apps", [a for a in shell.apps if a.kind == "android"]),
            ("Web apps", [a for a in shell.apps if a.kind == "web"]),
            ("This device", [a for a in shell.apps if a.kind == "system"]),
        ]
        self.content.append(self.hero)
        self.rows = []
        for title, group in groups:
            if group:
                row = AppRow(title, [Card(a, shell) for a in group])
                self.rows.append(row)
                self.content.append(row)

    def cards(self):
        return [card for row in self.rows for card in row.cards]

    def nav_rows(self):
        return [row.cards for row in self.rows]

    def reveal(self, item, row, col):
        self.rows[row].reveal(col)
        adj = self.scroller.get_vadjustment()
        if row == 0:
            target = 0
        else:
            ok, rect = self.rows[row].compute_bounds(self.content)
            if not ok:
                return
            target = rect.get_y() - 12      # this row's title sits right under the top bar
        animate(self.scroller, adj, target)

    def item_focused(self, card):
        app = card.app
        if app.name != self.hero_title.get_label():
            # Alternate between two identical animations: switching the name
            # is what makes the text glide in again for every new app.
            self.hero.remove_css_class(f"hero-in-{self.hero_step}")
            self.hero_step = 1 - self.hero_step
            self.hero.add_css_class(f"hero-in-{self.hero_step}")
        self.hero_kind.set_label(app.kind_label.upper())
        self.hero_title.set_label(app.name)
        self.hero_desc.set_label(app.desc)
        self.shell.set_glow(app)


class GridPage(Gtk.Box):
    """Cards in a grid, in titled sections. Used by Apps and by Search."""

    modal = False
    keep_column = True

    def __init__(self, shell):
        super().__init__(orientation=V)
        self.shell = shell
        self.content = Gtk.Box(orientation=V, margin_bottom=120, margin_top=8)
        self.scroller = vscroller(self.content)
        self.append(self.scroller)
        self.grid_rows = []

    def fill(self, sections):
        """sections: [(title, [App])]"""
        while child := self.content.get_first_child():
            self.content.remove(child)
        self.grid_rows = []
        for title, group in sections:
            if not group:
                continue
            self.content.append(label(title, "row-title", margin_start=SIDE))
            for i in range(0, len(group), GRID_COLS):
                cards = [Card(a, self.shell) for a in group[i:i + GRID_COLS]]
                strip = Gtk.Box(spacing=GAP, margin_start=SIDE, margin_end=SIDE)
                strip.add_css_class("strip")
                for card in cards:
                    strip.append(card)
                self.content.append(strip)
                self.grid_rows.append(cards)

    def cards(self):
        return [card for row in self.grid_rows for card in row]

    def nav_rows(self):
        return self.grid_rows

    def reveal(self, item, _row, _col):
        scroll_to_center(self.scroller, self.content, item)

    def item_focused(self, item):
        if isinstance(item, Card):
            self.shell.set_glow(item.app)


class AppsPage(GridPage):
    def __init__(self, shell):
        super().__init__(shell)
        self.build()

    def build(self):
        apps = self.shell.apps
        self.fill([
            ("Apps", [a for a in apps if a.kind == "flatpak"]),
            ("Android apps", [a for a in apps if a.kind == "android"]),
            ("Web apps", [a for a in apps if a.kind == "web"]),
            ("This device", [a for a in apps if a.kind == "system"]),
        ])


class SearchPage(GridPage):
    def __init__(self, shell):
        super().__init__(shell)
        self.entry = Gtk.Entry(placeholder_text="Search apps and the web",
                               margin_start=SIDE, margin_end=SIDE, margin_top=24)
        self.entry.add_css_class("prompt")
        self.entry.nav_activate = self.submit
        self.entry.connect("changed", lambda _e: self.build())
        self.prepend(self.entry)
        self.web_row = None
        self.build()

    def build(self):
        query = self.entry.get_text().strip()
        shell = self.shell
        matches = [a for a in shell.apps if query.lower() in a.name.lower()] if query else []
        self.fill([("Apps", matches)])
        self.web_row = None
        if query:
            self.web_row = ListRow(Item(f"Search the web for “{query}”", icon="globe",
                                        action=lambda: shell.web_search(query)), shell)
            self.web_row.set_margin_start(SIDE)
            self.web_row.set_margin_end(SIDE)
            self.web_row.set_margin_top(28)
            self.content.append(self.web_row)

    def nav_rows(self):
        rows = [[self.entry]] + self.grid_rows
        if self.web_row:
            rows.append([self.web_row])
        return rows

    def reveal(self, item, row, col):
        if row > 0:
            super().reveal(item, row, col)

    def submit(self):
        if not self.entry.get_text().strip():
            ctl.send("osk auto")
        else:
            self.shell.move(1, 0)


class Shell(Gtk.ApplicationWindow):
    def __init__(self, application, cfg=None, sway=None):
        super().__init__(application=application, title="Lumen TV")
        self.cfg = cfg if cfg is not None else config.load()
        self.sway = sway or Sway()
        self.launcher = catalog.Launcher(self.sway)
        self.apps = catalog.all_apps(self.cfg)
        self.set_default_size(1920, 1080)
        self.set_decorated(False)

        self.focused = None
        self.focus_row = 0          # -1: top bar, >= 0: row of the current page
        self.cols = {}              # (page, row) -> remembered column
        self.busy_cancel = None
        self.toast_timer = None
        self.css = Gtk.CssProvider()
        self.app_css = Gtk.CssProvider()
        display = Gdk.Display.get_default()
        self.css.load_from_path(str(config.PKG_DATA / "style.css"))
        for provider in (self.css, self.app_css):
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self.load_app_css()

        # background glow < content < toast / busy overlays
        root = Gtk.Overlay()
        root.add_css_class("root")
        self.glow = Gtk.Box()
        self.glow.add_css_class("glow")
        self.glow_class = None
        root.set_child(self.glow)
        main = Gtk.Box(orientation=V)
        root.add_overlay(main)
        main.append(self.build_topbar())
        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE,
                               transition_duration=230, vexpand=True)
        main.append(self.stack)

        self.toast_label = Gtk.Label(halign=Gtk.Align.CENTER, valign=Gtk.Align.END,
                                     margin_bottom=56, visible=False)
        self.toast_label.add_css_class("toast")
        root.add_overlay(self.toast_label)
        self.busy_box = Gtk.Box(orientation=V, spacing=22, visible=False)
        self.busy_box.add_css_class("busy")
        inner = Gtk.Box(orientation=V, spacing=22, valign=Gtk.Align.CENTER, vexpand=True)
        # Our own CSS spinner: Gtk.Spinner needs an icon theme, which a lean
        # system image does not ship.
        self.spinner = Gtk.Box(halign=Gtk.Align.CENTER)
        self.spinner.add_css_class("spinner")
        self.busy_label = Gtk.Label()
        self.busy_label.add_css_class("busy-label")
        self.busy_hint = Gtk.Label()
        self.busy_hint.add_css_class("row-sub")
        self.busy_bar = Gtk.Box(halign=Gtk.Align.CENTER, visible=False)
        self.busy_bar.add_css_class("progress")
        self.busy_bar.set_size_request(self.BAR_WIDTH, -1)
        self.busy_fill = Gtk.Box(halign=Gtk.Align.START)
        self.busy_fill.add_css_class("progress-fill")
        self.busy_bar.append(self.busy_fill)
        for widget in (self.spinner, self.busy_label, self.busy_bar, self.busy_hint):
            inner.append(widget)
        self.busy_box.append(inner)
        root.add_overlay(self.busy_box)
        self.set_child(root)

        self.tab_pages = {
            "search": SearchPage(self), "home": HomePage(self), "apps": AppsPage(self),
            "settings": ListPage(self, "Settings", settings.root_items(self), glyph="settings"),
        }
        self.tab_pages["settings"].modal = False
        for name, page in self.tab_pages.items():
            self.stack.add_named(page, name)
        self.pages = [self.tab_pages["home"]]   # root tab + pushed sub-pages
        self.tab = "home"
        self.stack.set_visible_child(self.pages[0])

        keys = Gtk.EventControllerKey()
        keys.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", self.on_key_pressed)
        self.add_controller(keys)

        self.update_tabs()
        self.update_clock()
        GLib.timeout_add_seconds(10, self.update_clock)
        self.update_network()
        GLib.timeout_add_seconds(30, self.update_network)
        self.watch_sway()
        self.connect("map", lambda _w: GLib.idle_add(self.apply_focus))
        self.sway.setup_outputs(self.cfg["display_mode"])
        self.apply_animations()
        # Look for an update shortly after start-up, without getting in the way.
        GLib.timeout_add_seconds(45, self.check_for_update)
        self.splash = Splash()
        self.quick = QuickPanel(self)
        self.listen()
        self.fetching_icons = False
        GLib.timeout_add_seconds(3, self.fetch_icons)
        # The web remote can change the configuration (for example add a web app).
        self.config_mtime = self.config_stamp()
        self.apps_stamp = catalog.installed_stamp()
        self.apps_timer = 0
        GLib.timeout_add_seconds(2, self.watch_config)
        if self.cfg["android_autostart"] and catalog.android_state() == "ready":
            run_async(lambda: catalog.android_helper("ensure"))

    # --- chrome -----------------------------------------------------------

    def build_topbar(self):
        bar = Gtk.Box(spacing=8, margin_start=SIDE - 22, margin_end=SIDE, margin_top=28,
                      margin_bottom=8)
        bar.add_css_class("topbar")
        self.tab_buttons = []
        for name, title, glyph in TABS:
            button = Gtk.Button(can_focus=False)
            button.add_css_class("tab")
            button.set_child(icon(glyph, 26) if glyph else Gtk.Label(label=title))
            button.tab = name
            button.nav_activate = lambda: self.move(1, 0)
            button.connect("clicked", lambda b: self.click(b))
            self.tab_buttons.append(button)
            bar.append(button)
        bar.append(Gtk.Box(hexpand=True))
        hint = Gtk.Label(label="Hold Start for quick settings")
        hint.add_css_class("status")
        bar.append(hint)
        self.net_label = Gtk.Label()
        self.net_label.add_css_class("status")
        bar.append(self.net_label)
        self.clock = Gtk.Label()
        self.clock.add_css_class("clock")
        bar.append(self.clock)
        return bar

    def update_clock(self):
        self.clock.set_label(time.strftime("%H:%M"))
        return True

    def update_network(self):
        run_async(settings.network_summary, lambda text: self.net_label.set_label(
            text if isinstance(text, str) else ""))
        return True

    def update_tabs(self):
        for button in self.tab_buttons:
            (button.add_css_class if button.tab == self.tab else button.remove_css_class)(
                "current")

    def load_app_css(self):
        rules = []
        for app in self.apps:
            dark = catalog.darken(app.color, 0.55)
            rules.append(f".acc-{app.css} {{ background-image: "
                         f"linear-gradient(135deg, {app.color}, {dark}); }}")
            rules.append(f".glow-{app.css} {{ background-image: radial-gradient("
                         f"circle at 78% -10%, alpha({app.color}, 0.42), "
                         f"alpha({app.color}, 0) 62%); }}")
        self.app_css.load_from_string("\n".join(rules))

    def set_glow(self, app):
        if self.glow_class:
            self.glow.remove_css_class(self.glow_class)
        self.glow_class = f"glow-{app.css}"
        self.glow.add_css_class(self.glow_class)

    def toast(self, text):
        self.toast_label.set_label(text)
        self.toast_label.set_visible(True)
        if self.toast_timer:
            GLib.source_remove(self.toast_timer)

        def hide():
            self.toast_label.set_visible(False)
            self.toast_timer = None
            return False

        self.toast_timer = GLib.timeout_add(3500, hide)

    BAR_WIDTH = 560

    def busy(self, text, work, done=None, hint="", progress=False):
        """Block the UI behind a spinner while `work()` runs on a thread.

        With `progress`, the work is called as `work(report)` and can call
        `report(fraction, text)` from its thread to drive a progress bar:
        a fraction of None means "working, amount unknown"."""
        self.busy_label.set_label(text)
        self.busy_hint.set_label(hint)
        self.busy_bar.set_visible(False)
        self.busy_box.set_visible(True)
        ticket = object()
        self.busy_cancel = ticket

        def show(fraction, note):
            if self.busy_cancel is ticket:
                self.busy_bar.set_visible(fraction is not None)
                if fraction is not None:
                    fraction = min(max(fraction, 0.0), 1.0)
                    self.busy_fill.set_size_request(round(self.BAR_WIDTH * fraction), -1)
                    percent = f"{round(fraction * 100)}%"
                    note = f"{note} · {percent}" if note else percent
                if note:
                    self.busy_hint.set_label(note)
            return False

        last = [None]

        def report(fraction, note=None):
            # Called very often from the worker; only pass on visible changes.
            key = (None if fraction is None else round(fraction * 100), note)
            if key != last[0]:
                last[0] = key
                GLib.idle_add(show, fraction, note)

        if progress:
            job = work
            work = lambda: job(report)  # noqa: E731

        def finished(result):
            if self.busy_cancel is ticket:
                self.end_busy()
                if done:
                    done(result)

        run_async(work, finished)

    def check_for_update(self):
        def done(status):
            if isinstance(status, tuple) and not status[2] and settings.update_available(
                    status[0], status[1]):
                self.set_update_ready(True)
                self.toast("A Lumen TV update is available: Settings › System update")

        run_async(settings.update_status, done)
        return False

    def listen(self):
        """Accept commands from the controller daemon, key bindings and the
        web remote (`lumenctl quick`)."""
        try:
            os.unlink(ctl.SHELL_SOCKET)
        except OSError:
            pass
        try:
            self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.server.bind(ctl.SHELL_SOCKET)
            self.server.listen(4)
        except OSError:
            return

        def on_client(*_):
            try:
                conn, _ = self.server.accept()
                with conn:
                    conn.settimeout(0.3)
                    command = conn.recv(256).decode(errors="replace").strip()
            except OSError:
                return True
            if command == "quick":
                self.quick.toggle()
            return True

        GLib.io_add_watch(self.server.fileno(), GLib.IO_IN, on_client)

    def apply_animations(self):
        self.get_settings().set_property("gtk-enable-animations",
                                         bool(self.cfg["animations"]))

    def graphics(self):
        """How the launcher is being drawn: worth knowing when it feels slow."""
        renderer = self.get_renderer()
        name = type(renderer).__name__ if renderer else ""
        if "Cairo" in name:
            return "Software (slow)"
        if "Vulkan" in name:
            return "GPU (Vulkan)"
        return "GPU (OpenGL)" if name else "Unknown"

    def set_update_ready(self, ready):
        """Mark the Settings tab while an update is waiting to be installed."""
        tab = next(b for b in self.tab_buttons if b.tab == "settings")
        tab.get_child().set_label("Settings  •" if ready else "Settings")

    def restart_soon(self):
        """Quit after the toast is readable; the session starts the new version."""
        GLib.timeout_add(2500, lambda: self.get_application().quit() and False)

    def end_busy(self):
        self.busy_cancel = None
        self.busy_box.set_visible(False)

    # --- pages ------------------------------------------------------------

    @property
    def page(self):
        return self.pages[-1]

    def set_tab(self, name):
        if name == self.tab and len(self.pages) == 1:
            return
        for page in self.pages[1:]:
            self.stack.remove(page)
        names = [t[0] for t in TABS]
        forward = names.index(name) > names.index(self.tab)
        self.tab = name
        self.pages = [self.tab_pages[name]]
        self.show_page(self.page, Gtk.StackTransitionType.SLIDE_LEFT if forward
                       else Gtk.StackTransitionType.SLIDE_RIGHT)
        self.update_tabs()

    def show_page(self, page, transition):
        """Switch page with a slide, so it is clear where you went."""
        self.stack.set_transition_type(transition)
        self.stack.set_visible_child(page)

    def push(self, page):
        self.stack.add_child(page)
        self.pages.append(page)
        self.show_page(page, Gtk.StackTransitionType.SLIDE_LEFT)
        self.focus_row = 0
        self.apply_focus()

    def pop(self):
        if len(self.pages) < 2:
            return
        page = self.pages.pop()
        closing = getattr(page, "on_close", None)
        if closing:
            page.on_close = None
            closing()
        self.show_page(self.page, Gtk.StackTransitionType.SLIDE_RIGHT)
        # Remove after the slide so the old page does not vanish abruptly.
        GLib.timeout_add(320, lambda: self.stack.remove(page) and False)
        # Restore the row we came from before anything re-applies focus.
        self.focus_row = self.cols.get((self.page, "row"), 0)
        if hasattr(self.page, "builder"):
            self.page.refresh()    # the sub-page may have changed what this list shows
        self.apply_focus()

    def go_home(self):
        self.set_tab("home")
        self.focus_row = 0
        self.apply_focus()

    # --- focus ------------------------------------------------------------

    def nav_rows(self):
        return self.page.nav_rows()

    def current_item(self):
        if self.focus_row < 0:
            return self.tab_buttons[[t[0] for t in TABS].index(self.tab)]
        rows = self.nav_rows()
        if not rows:
            return None
        self.focus_row = min(self.focus_row, len(rows) - 1)
        row = rows[self.focus_row]
        col = min(self.cols.get((self.page, self.focus_row), 0), len(row) - 1)
        self.cols[(self.page, self.focus_row)] = col
        return row[col]

    def apply_focus(self):
        item = self.current_item()
        if item is None and self.focus_row >= 0 and not self.page.modal:
            self.focus_row = -1     # empty page: fall back to the top bar
            item = self.current_item()
        old, self.focused = self.focused, item
        if old is not item:
            if old:
                old.remove_css_class("focused")
            was_entry, is_entry = isinstance(old, Gtk.Entry), isinstance(item, Gtk.Entry)
            if is_entry:
                item.grab_focus_without_selecting()
                if not was_entry:
                    ctl.send("osk auto")
            else:
                self.set_focus(None)
                if was_entry:
                    ctl.send("osk hide")
        if item is None:
            return
        item.add_css_class("focused")
        if self.focus_row >= 0:
            self.cols[(self.page, "row")] = self.focus_row
            col = self.cols[(self.page, self.focus_row)]
            self.page.reveal(item, self.focus_row, col)
            if hasattr(self.page, "item_focused"):
                self.page.item_focused(item)

    def refocus(self, page):
        """A page rebuilt its widgets; re-apply focus if it is the one showing."""
        if getattr(self, "pages", None) and page is self.page:
            self.focused = None
            self.apply_focus()

    def move(self, d_row, d_col):
        if self.focus_row < 0:
            if d_col:
                names = [t[0] for t in TABS]
                index = min(max(names.index(self.tab) + d_col, 0), len(names) - 1)
                self.set_tab(names[index])
            elif d_row > 0 and self.nav_rows():
                self.focus_row = 0
        elif d_col:
            row = self.nav_rows()[self.focus_row]
            col = self.cols.get((self.page, self.focus_row), 0)
            self.cols[(self.page, self.focus_row)] = min(max(col + d_col, 0), len(row) - 1)
        else:
            target = self.focus_row + d_row
            if target < 0:
                if not self.page.modal:
                    self.focus_row = -1
            elif target < len(self.nav_rows()):
                if self.page.keep_column:
                    self.cols[(self.page, target)] = self.cols.get(
                        (self.page, self.focus_row), 0)
                self.focus_row = target
        self.apply_focus()

    def click(self, item):
        """Pointer / touch: focus the item, then activate it."""
        if item in self.tab_buttons:
            self.set_tab(item.tab)
            self.focus_row = -1
            return self.apply_focus()
        for r, row in enumerate(self.nav_rows()):
            if item in row:
                self.focus_row = r
                self.cols[(self.page, r)] = row.index(item)
                self.apply_focus()
                item.nav_activate()
                return

    @staticmethod
    def press(item):
        """A brief "pushed in" look, so every press is visibly acknowledged."""
        item.add_css_class("pressed")
        GLib.timeout_add(140, lambda: item.remove_css_class("pressed") and False)

    def on_key_pressed(self, _controller, keyval, _keycode, _state):
        return self.on_key(Gdk.keyval_name(keyval) or "")

    def on_key(self, name):
        """Handle one key by GDK name. Returns True when it was consumed."""
        action = KEYS.get(name)
        if self.busy_cancel is not None:
            if action == "back":
                self.end_busy()
            return True
        editing = isinstance(self.focused, Gtk.Entry)
        if editing and (action in ("left", "right") or name in ("space", "BackSpace", "Home")):
            return False                      # these belong to the text field
        if action is None:
            return False
        if action in ("up", "down", "left", "right"):
            d_row = {"up": -1, "down": 1}.get(action, 0)
            d_col = {"left": -1, "right": 1}.get(action, 0)
            self.move(d_row, d_col)
        elif action == "ok":
            if self.focused is not None:
                self.press(self.focused)
                self.focused.nav_activate()
        elif action == "menu":
            menu = getattr(self.focused, "nav_menu", None)
            if menu:
                menu()
        elif action == "back":
            if len(self.pages) > 1:
                self.pop()
            elif self.tab != "home" or self.focus_row != 0:
                self.go_home()
        elif action == "home":
            self.go_home()
        return True

    # --- apps -------------------------------------------------------------

    def reload_apps(self):
        self.apps = catalog.all_apps(self.cfg)
        self.load_app_css()
        self.tab_pages["home"].build()
        self.tab_pages["apps"].build()
        self.tab_pages["search"].build()
        self.refresh_running()
        self.fetch_icons()          # a newly added web app may need its icon
        self.focused = None
        self.apply_focus()

    def save(self):
        config.save(self.cfg)
        self.reload_apps()

    def fetch_icons(self):
        """Download the icons of web apps that do not have one yet (in the
        background), then redraw the tiles."""
        from . import icons
        wanted = [a for a in self.apps if a.kind == "web" and not a.icon_path
                  and icons.missing(a.css)]
        if not wanted or self.fetching_icons:
            return False
        self.fetching_icons = True

        def fetch(app):
            if icons.fetch_icon(app.css, app.url):
                return 1
            icons.mark_failed(app.css)          # do not ask the site again for a week
            return 0

        def work():
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(4) as pool:
                return sum(pool.map(fetch, wanted))

        def done(got):
            self.fetching_icons = False
            if isinstance(got, int) and got:
                self.reload_apps()

        run_async(work, done)
        return False

    @staticmethod
    def config_stamp():
        try:
            return config.CONFIG_FILE.stat().st_mtime_ns
        except OSError:
            return None

    @staticmethod
    def catalogue_signature(apps):
        return [(a.id, a.name, a.icon_path) for a in apps]

    def apps_settled(self):
        """An app was installed or removed somewhere (inside Android, by the
        Store, from the web remote): show it, without waiting for a restart."""
        self.apps_timer = 0
        fresh = catalog.all_apps(self.cfg)
        if self.catalogue_signature(fresh) != self.catalogue_signature(self.apps):
            self.reload_apps()
        return False

    def watch_config(self):
        installed = catalog.installed_stamp()
        if installed != self.apps_stamp:
            self.apps_stamp = installed
            # Wait for things to settle: an app's entry and its icon arrive
            # separately, a moment apart.
            if self.apps_timer:
                GLib.source_remove(self.apps_timer)
            self.apps_timer = GLib.timeout_add(1500, self.apps_settled)
        stamp = self.config_stamp()
        if stamp != self.config_mtime:
            self.config_mtime = stamp
            changed = config.load()
            if changed != self.cfg:         # not one of our own saves
                self.cfg = changed
                self.reload_apps()
        return True

    def refresh_running(self):
        running = self.launcher.running()
        for page in ("home", "apps", "search"):
            for card in self.tab_pages[page].cards():
                card.dot.set_visible(card.app.workspace in running
                                     and card.app.kind != "android")

    WINDOW_EVENT = 0x80000003

    def watch_sway(self):
        """Follow sway: hide the launch screen when an app's window appears,
        and refresh the "running" dots and the app list when we come home."""
        sock = self.sway.subscribe(["workspace", "window"])
        if sock is None:
            return
        self.sway_events = sock
        known = [a.id for a in self.apps]

        def on_event(*_):
            nonlocal known
            try:
                kind, data = read_message(sock)
            except (OSError, ValueError):
                return False
            change = (data or {}).get("change")
            if kind == self.WINDOW_EVENT:
                if change == "new":
                    self.splash.dismiss(350)        # give it a moment to draw
                if change in ("new", "close"):
                    self.refresh_running()
                return True
            if change == "focus" and ((data.get("current") or {}).get("name") == HOME_WS):
                self.splash.dismiss()
                now = [a.id for a in catalog.all_apps(self.cfg)]
                if now != known:       # e.g. an app was installed inside Android
                    known = now
                    self.reload_apps()
                    return True
            if change in ("focus", "init", "empty"):
                self.refresh_running()
            return True

        GLib.io_add_watch(sock.fileno(), GLib.IO_IN, on_event)

    def open_app(self, app):
        if app.id == "settings":
            self.set_tab("settings")
            self.focus_row = 0
            return self.apply_focus()
        if app.id == "files":
            return self.push(settings.files_page(self))
        if app.id == "store":
            return self.push(settings.store_page(self))
        if app.kind == "android":
            return self.open_android(app.package, app)
        if self.launcher.is_running(app):        # already open: just switch to it
            return self.sway.command(f"workspace {app.workspace}")
        if app.kind == "flatpak":
            start = lambda: self.launcher.launch_flatpak(app)  # noqa: E731
        elif app.id == "browser":
            start = lambda: self.launcher.open_url(self.cfg["browser_home"])  # noqa: E731
        else:
            start = lambda: self.launcher.launch_web(app)  # noqa: E731
        self.launch(app, start)

    def launch(self, app, start):
        """Show the launch screen at once, then start the app off the UI thread
        (closing a previous browser can take a moment)."""
        self.splash.show_for(app)

        def started(error):
            if error:
                self.splash.dismiss()
                self.toast(error if isinstance(error, str) else f"Could not open {app.name}")
            elif not self.sway.available:
                self.splash.dismiss(900)     # development: no window event will come

        run_async(start, started)

    def open_android(self, package=None, app=None):
        state = catalog.android_state()
        if state != "ready":
            return self.push(settings.android_page(self))

        def done(result):
            if getattr(result, "returncode", 1) == 0:
                if app:
                    self.splash.show_for(app)
                    self.splash.dismiss(1600)    # Android's window may already exist
                self.launcher.show_android(package)
            else:
                lines = (getattr(result, "stderr", "") or str(result)).strip().splitlines()
                self.toast(lines[-1] if lines else "Android did not start")

        self.busy("Starting Android…", lambda: catalog.android_helper("ensure"), done,
                  hint="The first start after boot takes a little while")

    def web_search(self, query):
        url = self.cfg["search_url"].format(urllib.parse.quote_plus(query))
        self.report(self.launcher.open_url(url))

    def report(self, error):
        if error:
            self.toast(error)

    def app_menu(self, app):
        self.push(ListPage(self, app.name, lambda _p: settings.app_menu_items(self, app),
                           subtitle=app.desc))


class Application(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=config.APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)

    def do_activate(self):
        Shell(self).present()


def main():
    sys.exit(Application().run(sys.argv))


if __name__ == "__main__":
    main()
