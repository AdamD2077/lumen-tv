"""Building blocks of the launcher UI: cards, rows, list pages, prompts."""

from dataclasses import dataclass
from typing import Callable, Optional

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk, Pango  # noqa: E402

from .config import PKG_DATA  # noqa: E402

# Layout is designed for a 1920x1080 canvas; sway scales it to the real screen.
# 6 cards + 5 gaps + both side margins = 1920 exactly.
CARD_W, CARD_H, GAP, SIDE = 276, 156, 24, 72
GRID_COLS = 6

V, H = Gtk.Orientation.VERTICAL, Gtk.Orientation.HORIZONTAL


def icon(name, size):
    image = Gtk.Image.new_from_file(str(PKG_DATA / "icons" / f"{name}.svg"))
    image.set_pixel_size(size)
    return image


def label(text, css, **props):
    widget = Gtk.Label(label=text, xalign=0, **props)
    widget.add_css_class(css)
    return widget


def animate(widget, adj, target, ms=220):
    """Ease a scroll adjustment to `target` (clamped once layout is known)."""
    token = object()
    adj._lumen_anim = token
    start = adj.get_value()
    began = []
    if not widget.get_settings().get_property("gtk-enable-animations"):
        ms = 0

    def tick(_widget, clock):
        if getattr(adj, "_lumen_anim", None) is not token:
            return False
        end = min(max(target, adj.get_lower()),
                  max(adj.get_lower(), adj.get_upper() - adj.get_page_size()))
        now = clock.get_frame_time()
        if not began:
            began.append(now)
        progress = min(1.0, (now - began[0]) / (ms * 1000)) if ms else 1.0
        adj.set_value(start + (end - start) * (1 - (1 - progress) ** 3))
        return progress < 1

    widget.add_tick_callback(tick)


def scroll_to_center(scroller, content, item):
    ok, rect = item.compute_bounds(content)
    if not ok:
        return
    adj = scroller.get_vadjustment()
    middle = rect.get_y() + rect.get_height() / 2
    animate(scroller, adj, middle - adj.get_page_size() / 2)


def vscroller(child):
    scroller = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.EXTERNAL)
    scroller.set_child(child)
    return scroller


class Card(Gtk.Box):
    """An app tile. Focus is our own `.focused` class, not GTK keyboard focus."""

    def __init__(self, app, shell):
        super().__init__(orientation=V, spacing=20)
        self.add_css_class("card")
        self.app = app
        self.nav_activate = lambda: shell.open_app(app)
        self.nav_menu = lambda: shell.app_menu(app)

        face = Gtk.Button(can_focus=False)
        face.add_css_class("face")
        face.add_css_class(f"acc-{app.css}")
        face.set_size_request(CARD_W, CARD_H)
        face.connect("clicked", lambda _b: shell.click(self))

        overlay = Gtk.Overlay()
        if app.icon_path:
            art = Gtk.Image.new_from_file(app.icon_path)
            art.set_pixel_size(84)
        elif app.icon:
            art = Gtk.Box(orientation=V, spacing=10, valign=Gtk.Align.CENTER)
            art.append(icon(app.icon, 52))
            art.append(Gtk.Label(label=app.name, css_classes=["tile-name"]))
        else:
            size = "wm-l" if len(app.name) <= 8 else "wm-m" if len(app.name) <= 13 else "wm-s"
            art = Gtk.Label(label=app.name, wrap=True, justify=Gtk.Justification.CENTER,
                            max_width_chars=14, css_classes=["wordmark", size])
        overlay.set_child(art)
        self.dot = Gtk.Box(halign=Gtk.Align.END, valign=Gtk.Align.START, visible=False)
        self.dot.add_css_class("dot")
        overlay.add_overlay(self.dot)
        face.set_child(overlay)
        self.append(face)

        if app.icon_path:
            # Icon-only tiles get their name underneath while focused.
            name = Gtk.Label(label=app.name, xalign=0, ellipsize=Pango.EllipsizeMode.END,
                             max_width_chars=22)
            name.add_css_class("card-name")
            self.append(name)


class AppRow(Gtk.Box):
    """A titled, horizontally scrolling strip of cards (one home-screen row)."""

    def __init__(self, title, cards):
        super().__init__(orientation=V)
        self.cards = cards
        self.append(label(title, "row-title", margin_start=SIDE))
        self.scroller = Gtk.ScrolledWindow()
        self.scroller.set_policy(Gtk.PolicyType.EXTERNAL, Gtk.PolicyType.NEVER)
        strip = Gtk.Box(spacing=GAP, margin_start=SIDE, margin_end=SIDE)
        strip.add_css_class("strip")
        for card in cards:
            strip.append(card)
        self.scroller.set_child(strip)
        self.append(self.scroller)

    def reveal(self, col):
        # Like Android TV: the focused card slides to the left edge.
        animate(self.scroller, self.scroller.get_hadjustment(), col * (CARD_W + GAP))


@dataclass
class Item:
    """One line of a list page."""
    title: str
    sub: str = ""
    icon: str = ""
    value: str = ""
    action: Optional[Callable] = None


class ListRow(Gtk.Button):
    def __init__(self, item, shell):
        super().__init__(can_focus=False)
        self.add_css_class("row")
        self.item = item
        self.nav_activate = item.action or (lambda: None)
        box = Gtk.Box(spacing=20)
        if item.icon:
            box.append(icon(item.icon, 30))
        text = Gtk.Box(orientation=V, spacing=2, hexpand=True, valign=Gtk.Align.CENTER)
        text.append(label(item.title, "row-label", ellipsize=Pango.EllipsizeMode.END))
        if item.sub:
            text.append(label(item.sub, "row-sub", ellipsize=Pango.EllipsizeMode.END))
        box.append(text)
        if item.value:
            box.append(label(item.value, "row-value"))
        self.set_child(box)
        self.connect("clicked", lambda _b: shell.click(self))


class ListPage(Gtk.Box):
    """Two-pane page: a big title on the left, a list of rows on the right.

    `builder(page)` returns the items. It may instead return None and call
    `page.set_items()` later, for lists that are loaded in the background."""

    modal = True
    keep_column = False

    def __init__(self, shell, title, builder, subtitle="", glyph=""):
        super().__init__(orientation=H)
        self.add_css_class("list-page")
        self.shell = shell
        self.builder = builder
        self.rows = []

        side = Gtk.Box(orientation=V, spacing=14, margin_start=SIDE, margin_top=40)
        side.set_size_request(560, -1)
        if glyph:
            side.append(icon(glyph, 56))
            side.get_last_child().set_halign(Gtk.Align.START)
        side.append(label(title, "page-title", wrap=True, max_width_chars=16))
        self.subtitle = label(subtitle, "page-sub", wrap=True, max_width_chars=34)
        side.append(self.subtitle)
        self.append(side)

        self.body = Gtk.Box(orientation=V, spacing=10, margin_top=40, margin_bottom=80,
                            margin_start=24, margin_end=SIDE)
        self.scroller = vscroller(self.body)
        self.append(self.scroller)
        self.refresh()

    def refresh(self):
        items = self.builder(self)
        if items is not None:
            self.set_items(items)

    def loading(self, text="Loading…"):
        self.set_items([Item(text)])

    def set_items(self, items):
        while child := self.body.get_first_child():
            self.body.remove(child)
        self.rows = [ListRow(item, self.shell) for item in items]
        for row in self.rows:
            self.body.append(row)
        self.shell.refocus(self)

    def nav_rows(self):
        return [[row] for row in self.rows]

    def reveal(self, item, _row, _col):
        scroll_to_center(self.scroller, self.body, item)


class PromptPage(ListPage):
    """Ask for a line of text. Typing works from a keyboard or the on-screen one."""

    def __init__(self, shell, title, on_submit, subtitle="", initial="", glyph=""):
        self.entry = Gtk.Entry(text=initial)
        self.entry.add_css_class("prompt")
        self.entry.nav_activate = self.submit
        self.on_submit = on_submit
        super().__init__(shell, title, lambda _p: [
            Item("Done", icon="check", action=self.submit),
            Item("Cancel", icon="close", action=shell.pop),
        ], subtitle, glyph)
        self.entry.set_position(-1)

    def set_items(self, items):
        super().set_items(items)
        self.body.prepend(self.entry)

    def nav_rows(self):
        return [[self.entry]] + super().nav_rows()

    def submit(self):
        text = self.entry.get_text().strip()
        self.shell.pop()
        if text:
            self.on_submit(text)


def run_async(work, done=None):
    """Run `work()` on a thread, then `done(result)` back on the UI thread."""
    import threading

    def runner():
        try:
            result = work()
        except Exception as e:  # report instead of killing the thread silently
            result = e
        if done:
            GLib.idle_add(lambda: done(result) and False)

    threading.Thread(target=runner, daemon=True).start()
