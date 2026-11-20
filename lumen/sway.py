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

    def setup_outputs(self, prefer_1080p=True):
        """Pick a TV-friendly mode and scale so the UI is always laid out at 1080p.

        Rendering at 1080p instead of 4K keeps a Pi's GPU comfortable."""
        for out in self.outputs():
            cmd = output_command(out, prefer_1080p)
            if cmd:
                self.command(cmd)


def output_command(out, prefer_1080p=True):
    cur = out.get("current_mode") or {}
    width, height = cur.get("width", 0), cur.get("height", 0)
    if not out.get("active") or not height:
        return None
    parts = []
    if prefer_1080p and height > 1080:
        full_hd = [m for m in out.get("modes", [])
                   if (m["width"], m["height"]) == (1920, 1080) and m["refresh"] <= 60500]
        if full_hd:
            best = max(full_hd, key=lambda m: m["refresh"])
            parts.append(f"mode 1920x1080@{best['refresh'] / 1000:.3f}Hz")
            width, height = 1920, 1080
    # Fit a 1920x1080 logical canvas inside the output.
    scale = round(min(width / 1920, height / 1080), 3)
    parts.append(f"scale {max(scale, 0.25):g}")
    return f"output {out['name']} " + " ".join(parts)
