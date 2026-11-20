"""Minimal sway IPC client (the i3 IPC protocol over $SWAYSOCK)."""

import json
import os
import socket
import struct

MAGIC = b"i3-ipc"
HEADER = struct.Struct("=6sII")
RUN_COMMAND, GET_WORKSPACES, SUBSCRIBE, GET_OUTPUTS = 0, 1, 2, 3

HOME_WS = "home"


def pack(msg_type, payload=""):
    body = payload.encode()
    return HEADER.pack(MAGIC, len(body), msg_type) + body


def _read_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("sway closed the connection")
        buf += chunk
    return buf


def read_message(sock):
    """Read one message, returning (type, decoded JSON)."""
    magic, length, msg_type = HEADER.unpack(_read_exact(sock, HEADER.size))
    if magic != MAGIC:
        raise ConnectionError("bad sway IPC magic")
    return msg_type, json.loads(_read_exact(sock, length) or b"null")


class Sway:
    """Every call is a no-op (returning an empty result) outside a sway session,
    so the shell can also run in a plain window for development."""

    def __init__(self, path=None):
        self.path = path or os.environ.get("SWAYSOCK")

    @property
    def available(self):
        return bool(self.path)

    def _connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(2)
        sock.connect(self.path)
        return sock

    def _request(self, msg_type, payload=""):
        if not self.available:
            return None
        try:
            with self._connect() as sock:
                sock.sendall(pack(msg_type, payload))
                return read_message(sock)[1]
        except (OSError, ValueError):
            return None

    def command(self, cmd):
        reply = self._request(RUN_COMMAND, cmd)
        return bool(reply) and all(r.get("success") for r in reply)

    def workspaces(self):
        return self._request(GET_WORKSPACES) or []

    def workspace_names(self):
        return {w["name"] for w in self.workspaces()}

    def focused_workspace(self):
        for w in self.workspaces():
            if w.get("focused"):
                return w["name"]
        return None

    def outputs(self):
        return self._request(GET_OUTPUTS) or []

    def subscribe(self, events):
        """Return a connected socket that receives the given events, or None."""
        if not self.available:
            return None
        try:
            sock = self._connect()
            sock.sendall(pack(SUBSCRIBE, json.dumps(events)))
            read_message(sock)
            sock.settimeout(None)
            return sock
        except (OSError, ValueError):
            return None

    def go_home(self):
        self.command(f"workspace {HOME_WS}")

    def close_focused_app(self):
        """Close whatever app is in front (never the launcher) and go home."""
        ws = self.focused_workspace()
        if ws and ws != HOME_WS:
            self.command(f'[workspace="{ws}"] kill')
        self.go_home()

    def setup_outputs(self, setting="auto"):
        """Apply the display setting to every output (see `pick_mode`) and scale
        it so the UI is always laid out on a 1920x1080 canvas."""
        for out in self.outputs():
            cmd = output_command(out, setting)
            if cmd:
                self.command(cmd)


SMOOTH_HZ = 60500      # in mHz: what "auto" and "native" will not exceed


def mode_key(mode):
    return f"{mode['width']}x{mode['height']}@{mode['refresh']}"


def mode_label(mode):
    return f"{mode['width']} × {mode['height']} · {round(mode['refresh'] / 1000)} Hz"


def available_modes(out):
    """The modes worth offering: 720p and up, one per resolution and refresh
    rate, largest first."""
    best = {}
    for mode in out.get("modes", []):
        if mode["height"] < 720:
            continue
        key = (mode["width"], mode["height"], round(mode["refresh"] / 1000))
        if key not in best or mode["refresh"] > best[key]["refresh"]:
            best[key] = mode
    return sorted(best.values(),
                  key=lambda m: (m["width"] * m["height"], m["refresh"]), reverse=True)


def pick_mode(out, setting="auto"):
    """Which mode a display setting means for this output, or None to leave it.

    "auto"       the largest mode up to 1080p: easy on a Pi's GPU
    "native"     the largest mode the display offers
    "WxH@mHz"    exactly that mode (falls back to "auto" if it is not offered)"""
    modes = available_modes(out)
    if not modes:
        return None
    if setting not in ("auto", "native"):
        for mode in out.get("modes", []):
            if mode_key(mode) == setting:
                return mode
        setting = "auto"
    if setting == "auto":
        capped = [m for m in modes if m["height"] <= 1080 and m["width"] <= 1920]
        modes = capped or modes[-1:]
    # Prefer ordinary refresh rates; a 120 Hz mode has to be chosen explicitly.
    calm = [m for m in modes if m["refresh"] <= SMOOTH_HZ] or modes
    return max(calm, key=lambda m: (m["width"] * m["height"], m["refresh"]))


def output_command(out, setting="auto"):
    cur = out.get("current_mode") or {}
    if not out.get("active") or not cur.get("height"):
        return None
    mode = pick_mode(out, setting) or cur
    parts = []
    if mode_key(mode) != mode_key(cur):
        parts.append(f"mode {mode['width']}x{mode['height']}@{mode['refresh'] / 1000:.3f}Hz")
    # Fit a 1920x1080 logical canvas inside the output.
    scale = round(min(mode["width"] / 1920, mode["height"] / 1080), 3)
    parts.append(f"scale {max(scale, 0.25):g}")
    return f"output {out['name']} " + " ".join(parts)
