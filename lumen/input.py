"""lumen-input: makes a game controller drive the whole system.

It grabs every connected gamepad and re-emits its input through one virtual
keyboard + mouse, so the launcher, the browser and Android apps all just see
arrow keys, Enter, Escape and a pointer. It also owns the on-screen keyboard.

  D-pad / left stick   move            A   select        B   back
  right stick          pointer         RT / R3  click    LT  right click
  LB / RB              scroll          Y*  play/pause    X*  keyboard
  Start                menu            Select / Guide    home (hold: close app)

  * by position: "north" button opens the keyboard, "west" is play/pause.

With the keyboard open: A types, B closes, west = backspace, north = space,
LB = shift, RB = symbols, Start = enter, L3 = move keyboard to the other edge.
"""

import os
import socket
import time

from .ctl import SOCKET_PATH

NAV_THRESHOLD = 0.6     # stick deflection that counts as a direction press
DEADZONE = 0.22         # pointer stick deadzone
REPEAT_DELAY = 0.38
REPEAT_RATE = 0.1
POINTER_SPEED = 1500    # px/s at full deflection
SCROLL_RATE = 0.07
LONG_PRESS = 0.7

# Linux input event codes (stable kernel ABI), spelled out so the mapping
# logic can be tested without python-evdev.
EV_KEY, EV_ABS = 1, 3
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0, 1, 2, 3, 4, 5
ABS_GAS, ABS_BRAKE, ABS_HAT0X, ABS_HAT0Y = 9, 10, 16, 17
BTN_SOUTH = 0x130
KEY_OK, KEY_SELECT, KEY_EXIT, KEY_BACK = 0x160, 0x161, 174, 158

BUTTONS = {
    0x130: "a", 0x131: "b", 0x133: "north", 0x134: "west",
    0x136: "lb", 0x137: "rb", 0x138: "lt", 0x139: "rt",
    0x13a: "select", 0x13b: "start", 0x13c: "mode", 0x13d: "thumbl", 0x13e: "thumbr",
}
DPAD_BUTTONS = {0x220: "up", 0x221: "down", 0x222: "left", 0x223: "right"}
REMOTE_KEYS = {KEY_OK: "KEY_ENTER", KEY_SELECT: "KEY_ENTER",
               KEY_EXIT: "KEY_ESC", KEY_BACK: "KEY_ESC"}


class Mapper:
    """Turns controller buttons into actions. Pure logic: `out` is the virtual
    keyboard/mouse, `osk` the on-screen keyboard, `system` does home/close."""

    NORMAL = {"a": "KEY_ENTER", "b": "KEY_ESC", "west": "KEY_SPACE", "start": "KEY_COMPOSE"}
    NAV_KEYS = {"up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT", "right": "KEY_RIGHT"}

    def __init__(self, out, osk, system):
        self.out, self.osk, self.system = out, osk, system
        self.held = {}
        self.next_scroll = 0.0

    def nav(self, direction):
        if self.osk.visible:
            self.osk.move(direction)
        else:
            self.out.tap(self.NAV_KEYS[direction])

    def pointer(self, dx, dy):
        self.out.move(dx, dy)

    def button(self, name, pressed, now):
        # Mouse buttons follow the trigger so that holding it drags.
        if name in ("rt", "thumbr"):
            return self.out.key("BTN_LEFT", pressed)
        if name == "lt":
            return self.out.key("BTN_RIGHT", pressed)

        since = self.held.pop(name, None)
        if pressed:
            self.held[name] = now

        if name in ("select", "mode"):
            if not pressed and since is not None:
                self.osk.hide()
                if now - since >= LONG_PRESS:
                    self.system.close_app()
                else:
                    self.system.home()
            return
        if not pressed:
            return

        if self.osk.visible:
            self._osk_button(name)
        elif name == "north":
            self.osk.show()
        elif name in ("lb", "rb"):
            self.next_scroll = now  # tick() does the scrolling, with repeat
            self.tick(now)
        elif name in self.NORMAL:
            self.out.tap(self.NORMAL[name])

    def _osk_button(self, name):
        if name == "a":
            self.osk.press()
        elif name == "b":
            self.osk.hide()
        elif name == "west":
            self.out.tap("KEY_BACKSPACE")
        elif name == "north":
            self.out.tap("KEY_SPACE")
        elif name == "lb":
            self.osk.shift()
        elif name == "rb":
            self.osk.layer()
        elif name == "thumbl":
            self.osk.flip()
        elif name == "start":
            self.out.tap("KEY_ENTER")
            self.osk.hide()

    def tick(self, now):
        """Scroll while LB/RB is held. Returns True while it needs more ticks."""
        if self.osk.visible:
            return False
        step = ("lb" in self.held) - ("rb" in self.held)
        if step and now >= self.next_scroll:
            self.out.wheel(step)
            self.next_scroll = now + SCROLL_RATE
        return bool(step)


class PadState:
    """Per-controller state: axis normalisation, direction repeat, pointer."""

    def __init__(self, mapper, absinfo):
        """absinfo: {abs code: (min, max)} for the axes the device has."""
        self.mapper = mapper
        self.absinfo = absinfo
        # Xbox/PlayStation pads report the right stick on RX/RY and the triggers
        # on Z/RZ; simpler pads put the right stick on Z/RZ.
        if ABS_RX in absinfo and ABS_RY in absinfo:
            self.right = (ABS_RX, ABS_RY)
            self.triggers = {ABS_Z: "lt", ABS_RZ: "rt"}
        else:
            self.right = (ABS_Z, ABS_RZ)
            self.triggers = {}
        self.triggers.update({ABS_BRAKE: "lt", ABS_GAS: "rt"})
        self.axes = {}
        self.sources = {"hat": set(), "stick": set(), "btn": set()}
        self.dirs = {}            # direction -> time of next repeat
        self.trigger_down = {}
        self.frac = [0.0, 0.0]
        self.last_tick = None

    def _norm(self, code, value, signed=True):
        lo, hi = self.absinfo.get(code, (-32768, 32767))
        if hi <= lo:
            return 0.0
        unit = (value - lo) / (hi - lo)
        return unit * 2 - 1 if signed else unit

    @staticmethod
    def _axis_dirs(x, y, threshold):
        dirs = set()
        if x <= -threshold:
            dirs.add("left")
        elif x >= threshold:
            dirs.add("right")
        if y <= -threshold:
            dirs.add("up")
        elif y >= threshold:
            dirs.add("down")
        return dirs

    def event(self, etype, code, value, now):
        if etype == EV_KEY:
            if value not in (0, 1):
                return
            if code in BUTTONS:
                self.mapper.button(BUTTONS[code], bool(value), now)
            elif code in DPAD_BUTTONS:
                (self.sources["btn"].add if value else self.sources["btn"].discard)(
                    DPAD_BUTTONS[code])
        elif etype == EV_ABS:
            if code in self.triggers and code not in self.right:
                down = self._norm(code, value, signed=False) > 0.5
                if down != self.trigger_down.get(code, False):
                    self.trigger_down[code] = down
                    self.mapper.button(self.triggers[code], down, now)
                return
            if code in (ABS_HAT0X, ABS_HAT0Y):
                self.axes[code] = value
                self.sources["hat"] = self._axis_dirs(
                    self.axes.get(ABS_HAT0X, 0), self.axes.get(ABS_HAT0Y, 0), 1)
            else:
                self.axes[code] = self._norm(code, value)
                if code in (ABS_X, ABS_Y):
                    self.sources["stick"] = self._axis_dirs(
                        self.axes.get(ABS_X, 0), self.axes.get(ABS_Y, 0), NAV_THRESHOLD)
        self._update_dirs(now)

    def _update_dirs(self, now):
        want = set().union(*self.sources.values())
        for d in list(self.dirs):
            if d not in want:
                del self.dirs[d]
        for d in want:
            if d not in self.dirs:
                self.dirs[d] = now + REPEAT_DELAY
                self.mapper.nav(d)

    def tick(self, now):
        """Advance repeats and the pointer. Returns True while still active."""
        dt = min(now - self.last_tick, 0.05) if self.last_tick is not None else 0.016
        self.last_tick = now
        for d, due in self.dirs.items():
            if now >= due:
                self.dirs[d] = now + REPEAT_RATE
                self.mapper.nav(d)

        moving = False
        step = [0, 0]
        for i, code in enumerate(self.right):
            v = self.axes.get(code, 0.0)
            mag = (abs(v) - DEADZONE) / (1 - DEADZONE)
            if mag <= 0:
                self.frac[i] = 0.0
                continue
            moving = True
            # Squared response: fine control near the centre, fast at the edge.
            self.frac[i] += (1 if v > 0 else -1) * mag * mag * POINTER_SPEED * dt
            step[i] = int(self.frac[i])
            self.frac[i] -= step[i]
        if step[0] or step[1]:
            self.mapper.pointer(step[0], step[1])
        if not (moving or self.dirs):
            self.last_tick = None
        return moving or bool(self.dirs)


# --- everything below needs a real system (evdev, uinput, GTK) -------------

class Out:
    """The virtual keyboard + mouse all controller input is re-emitted through."""

    def __init__(self):
        from evdev import UInput, ecodes
        from .osk import CHARMAP
        self.e = ecodes
        self.charmap = CHARMAP
        names = {k for k, _ in CHARMAP.values()} | set(Mapper.NORMAL.values()) | set(
            Mapper.NAV_KEYS.values()) | {"KEY_BACKSPACE", "KEY_LEFTSHIFT", "BTN_LEFT", "BTN_RIGHT"}
        self.ui = UInput(
            {ecodes.EV_KEY: sorted(getattr(ecodes, n) for n in names),
             ecodes.EV_REL: [ecodes.REL_X, ecodes.REL_Y, ecodes.REL_WHEEL]},
            name="Lumen Virtual Input")

    def key(self, name, down):
        self.ui.write(self.e.EV_KEY, getattr(self.e, name), int(down))
        self.ui.syn()

    def tap(self, name, shift=False):
        if shift:
            self.key("KEY_LEFTSHIFT", True)
        self.key(name, True)
        self.key(name, False)
        if shift:
            self.key("KEY_LEFTSHIFT", False)

    def type_char(self, ch):
        if ch in self.charmap:
            self.tap(*self.charmap[ch])

    def move(self, dx, dy):
        if dx:
            self.ui.write(self.e.EV_REL, self.e.REL_X, dx)
        if dy:
            self.ui.write(self.e.EV_REL, self.e.REL_Y, dy)
        self.ui.syn()

    def wheel(self, step):
        self.ui.write(self.e.EV_REL, self.e.REL_WHEEL, step)
        self.ui.syn()


class Daemon:
    RESCAN_SECONDS = 2

    def __init__(self):
        from gi.repository import GLib
        from .osk_view import OskController
        from .sway import Sway
        self.GLib = GLib
        self.sway = Sway()
        self.out = Out()
        self.osk = OskController(self.out)
        self.mapper = Mapper(self.out, self.osk, self)
        self.pads = {}       # path -> (device, PadState, watch id)
        self.remotes = {}    # path -> (device, watch id)
        self.ignored = set()
        self.timer = None
        self._listen()
        self.scan()
        GLib.timeout_add_seconds(self.RESCAN_SECONDS, self.scan)

    # home/close are what the Mapper calls on `system`
    def home(self):
        self.sway.go_home()

    def close_app(self):
        self.sway.close_focused_app()

    # --- devices ---

    def scan(self):
        import evdev
        e = evdev.ecodes
        paths = set(evdev.list_devices())
        self.ignored &= paths
        for path in paths - self.ignored - set(self.pads) - set(self.remotes):
            try:
                dev = evdev.InputDevice(path)
                caps = dev.capabilities()
            except OSError:
                continue  # not readable yet; try again on the next scan
            keys = set(caps.get(e.EV_KEY, []))
            if BTN_SOUTH in keys:
                try:
                    # Exclusive access, or the browser's Gamepad API would
                    # handle every press a second time.
                    dev.grab()
                except OSError:
                    pass
                absinfo = {code: (info.min, info.max) for code, info in caps.get(e.EV_ABS, [])}
                watch = self.GLib.io_add_watch(dev.fd, self.GLib.IO_IN | self.GLib.IO_HUP,
                                               self._on_pad, path)
                self.pads[path] = (dev, PadState(self.mapper, absinfo), watch)
            elif KEY_OK in keys:
                # TV remote (HDMI-CEC): arrows already work, OK and Back need help.
                watch = self.GLib.io_add_watch(dev.fd, self.GLib.IO_IN | self.GLib.IO_HUP,
                                               self._on_remote, path)
                self.remotes[path] = (dev, watch)
            else:
                dev.close()
                self.ignored.add(path)
        return True

    def _drop(self, table, path):
        entry = table.pop(path, None)
        if entry:
            try:
                entry[0].close()
            except OSError:
                pass

    def _on_pad(self, _fd, _cond, path):
        dev, state, _ = self.pads[path]
        now = time.monotonic()
        try:
            for ev in dev.read():
                state.event(ev.type, ev.code, ev.value, now)
        except OSError:
            self._drop(self.pads, path)
            return False
        if self.timer is None:
            self.timer = self.GLib.timeout_add(16, self._tick)
        return True

    def _on_remote(self, _fd, _cond, path):
        dev, _ = self.remotes[path]
        try:
            for ev in dev.read():
                if ev.type == EV_KEY and ev.value == 1 and ev.code in REMOTE_KEYS:
                    self.out.tap(REMOTE_KEYS[ev.code])
        except OSError:
            self._drop(self.remotes, path)
            return False
        return True

    def _tick(self):
        now = time.monotonic()
        active = self.mapper.tick(now)
        for _, state, _ in list(self.pads.values()):
            active = state.tick(now) or active
        if not active:
            self.timer = None  # sleep until the next controller event
        return active

    # --- control socket ---

    def _listen(self):
        try:
            os.unlink(SOCKET_PATH)
        except OSError:
            pass
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(SOCKET_PATH)
        self.server.listen(4)
        self.GLib.io_add_watch(self.server.fileno(), self.GLib.IO_IN, self._on_client)

    def _on_client(self, *_):
        try:
            conn, _ = self.server.accept()
            with conn:
                conn.settimeout(0.3)
                line = conn.recv(256).decode(errors="replace").strip()
        except OSError:
            return True
        self.handle(line)
        return True

    def handle(self, line):
        if line == "home":
            self.home()
        elif line == "close":
            self.close_app()
        elif line == "osk show":
            self.osk.show()
        elif line == "osk hide":
            self.osk.hide()
        elif line == "osk toggle":
            self.osk.hide() if self.osk.visible else self.osk.show()
        elif line == "osk auto":
            # Text field focused: only pop up if someone is holding a controller.
            if self.pads:
                self.osk.show()


def main():
    from gi.repository import GLib
    Daemon()
    GLib.MainLoop().run()


if __name__ == "__main__":
    main()
