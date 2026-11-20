"""lumen-remote: manage the TV from a phone or computer, in a web browser.

Serves a small web app on the local network (off until enabled in
Settings > Remote access). A browser pairs once with the PIN shown on the TV,
then can act as a remote control, type text, open apps and links, manage web
apps, install APKs, update, and restart or shut down the device.

Everything here runs as the TV user, inside the TV session. It is plain HTTP,
meant for a home network: do not forward the port to the internet.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, apps as catalog, config, ctl
from .sway import HOME_WS, Sway

DEFAULT_PORT = 8080
AUTH_FILE = config.DATA_DIR / "remote.json"
COOKIE = "lumen_session"
MAX_JSON = 64 * 1024
MAX_APK = 1024 * 1024 * 1024

# Remote-control buttons -> key sent through the lumen-input daemon.
KEYS = {"up": "KEY_UP", "down": "KEY_DOWN", "left": "KEY_LEFT", "right": "KEY_RIGHT",
        "ok": "KEY_ENTER", "back": "KEY_ESC", "menu": "KEY_COMPOSE",
        "playpause": "KEY_SPACE", "backspace": "KEY_BACKSPACE"}
VOLUME = {"volup": ("set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "5%+"),
          "voldown": ("set-volume", "@DEFAULT_AUDIO_SINK@", "5%-"),
          "mute": ("set-mute", "@DEFAULT_AUDIO_SINK@", "toggle")}


class Auth:
    """The pairing PIN and the browsers that have paired.

    Stored in a file so the launcher (which shows the PIN) and this server
    share it. Only hashes of session tokens are kept."""

    MAX_FAILS = 5
    LOCK_SECONDS = 300
    MAX_SESSIONS = 20

    def __init__(self, path=None):
        self.path = Path(path or AUTH_FILE)
        self.fails = 0
        self.locked_until = 0.0
        self.mtime = None
        self.data = {"pin": "", "tokens": []}
        self.refresh()
        if not self.data["pin"]:
            self.new_pin()

    def refresh(self):
        """Pick up changes made by the other process (for example a new PIN)."""
        try:
            mtime = self.path.stat().st_mtime_ns
            if mtime != self.mtime:
                data = json.loads(self.path.read_text())
                self.data = {"pin": str(data.get("pin", "")), "tokens": list(data.get("tokens", []))}
                self.mtime = mtime
        except (OSError, ValueError):
            pass

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)
        self.mtime = self.path.stat().st_mtime_ns

    @property
    def pin(self):
        return self.data["pin"]

    def new_pin(self):
        """A fresh PIN. Every paired browser has to pair again."""
        self.data = {"pin": f"{secrets.randbelow(10 ** 6):06d}", "tokens": []}
        self._save()
        return self.pin

    @staticmethod
    def _hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def pair(self, pin, now=None):
        """Returns (session token, "") or (None, reason)."""
        now = time.monotonic() if now is None else now
        self.refresh()
        if now < self.locked_until:
            return None, f"Too many wrong PINs. Try again in {int(self.locked_until - now) + 1} s."
        if not hmac.compare_digest(str(pin).strip().encode(), self.pin.encode()):
            self.fails += 1
            if self.fails >= self.MAX_FAILS:
                self.fails = 0
                self.locked_until = now + self.LOCK_SECONDS
            return None, "Wrong PIN"
        self.fails = 0
        token = secrets.token_urlsafe(32)
        self.data["tokens"] = (self.data["tokens"] + [self._hash(token)])[-self.MAX_SESSIONS:]
        self._save()
        return token, ""

    def valid(self, token):
        if not token:
            return False
        self.refresh()
        digest = self._hash(token)
        return any(hmac.compare_digest(digest, known) for known in self.data["tokens"])


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def run(*cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, p.stdout
    except (OSError, subprocess.SubprocessError):
        return False, ""


def read(path):
    try:
        return Path(path).read_text(errors="replace").strip("\x00\n ")
    except OSError:
        return ""


def system_stats():
    mem = dict(re.findall(r"(\w+):\s+(\d+)", read("/proc/meminfo")))
    total, free = int(mem.get("MemTotal", 0)), int(mem.get("MemAvailable", 0))
    temp = read("/sys/class/thermal/thermal_zone0/temp")
    uptime = read("/proc/uptime").split()
    return {
        "model": read("/proc/device-tree/model") or "Unknown device",
        "memory_total_mb": total // 1024,
        "memory_used_mb": (total - free) // 1024,
        "temperature_c": round(int(temp) / 1000, 1) if temp.isdigit() else None,
        "uptime_s": int(float(uptime[0])) if uptime else 0,
    }


# Read-only commands whose output helps explain a problem. Fixed list: the
# web page can ask for the report but cannot choose what is run.
DIAGNOSTICS = [
    ("System", "uname -srm; getconf PAGESIZE; cat /proc/cmdline"),
    ("Boot configuration", "grep -v '^#' /boot/firmware/config.txt | grep -v '^$' | tail -n 25"),
    ("Memory and storage", "free -m; df -h / | tail -n 1"),
    ("Graphics devices", "ls -l /dev/dri/ 2>&1; ls /sys/class/drm/ 2>&1 | tr '\\n' ' '"),
    ("Session", "echo WAYLAND_DISPLAY=$WAYLAND_DISPLAY XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR "
                "SWAYSOCK=${SWAYSOCK:+set}; id; pgrep -a -u \"$(id -u)\" -f "
                "'sway|lumen|waydroid|chromium|flatpak' | cut -c1-150"),
    ("Android: status", "waydroid status 2>&1; echo; waydroid --version 2>&1"),
    ("Android: container service",
     "systemctl is-enabled waydroid-container 2>&1; systemctl is-active waydroid-container "
     "2>&1; systemctl status waydroid-container --no-pager -n 25 2>&1 | tail -n 30"),
    ("Android: kernel support",
     "grep -i binder /proc/filesystems 2>&1; ls -l /dev/binder* /dev/binderfs 2>&1 | head; "
     "lsmod | grep -i -E 'binder|ashmem' ; cat /proc/pressure/memory 2>&1 | head -n 2"),
    ("Android: network setup",
     "grep -n -E '^LXC_USE_NFT|^use_nft|IPTABLES_BIN=' "
     "/usr/lib/waydroid/data/scripts/waydroid-net.sh 2>&1 | head; "
     "for t in nft iptables iptables-legacy iptables-nft dnsmasq; do "
     "printf '%s: ' $t; command -v $t || PATH=$PATH:/usr/sbin command -v $t || echo missing; "
     "done; ip -br addr show waydroid0 2>&1"),
    ("Android: files", "ls -la /var/lib/waydroid/ 2>&1; ls -la /var/lib/waydroid/images 2>&1; "
                       "ls ~/.local/share/applications 2>&1 | head -n 30"),
    ("Android: an app entry",
     "f=$(ls ~/.local/share/applications/waydroid.*.desktop 2>/dev/null | head -n 1); "
     "[ -n \"$f\" ] && { echo \"$f\"; cat \"$f\"; }"),
    ("Android: configuration", "cat /var/lib/waydroid/waydroid.cfg 2>&1 | head -n 40"),
    ("Android: log", "tail -n 120 /var/lib/waydroid/waydroid.log 2>&1"),
    ("Android: setup log", "tail -n 40 /var/log/lumen-android-setup.log 2>&1"),
    ("Update log", "tail -n 25 /var/log/lumen-update.log 2>&1"),
    ("First-boot log", "tail -n 15 /var/log/lumen-firstboot.log 2>&1"),
    ("Kernel messages", "dmesg 2>&1 | grep -i -E 'binder|waydroid|lxc|v3d|oom|apparmor' "
                        "| tail -n 40"),
]


def diagnostics():
    parts = []
    for title, command in DIAGNOSTICS:
        try:
            p = subprocess.run(["sh", "-c", command], capture_output=True, text=True, timeout=20)
            output = (p.stdout + p.stderr).strip()
        except subprocess.SubprocessError:
            output = "(timed out)"
        parts.append(f"== {title}\n{output or '(nothing)'}")
    return {"report": "\n\n".join(parts)}


class Device:
    """What the web app can do. Each `do_<name>` is one API action."""

    def __init__(self, sway=None, send=ctl.send, config_path=None, downloads=None):
        self.sway = sway or Sway()
        self.launcher = catalog.Launcher(self.sway)
        self.send = send
        self.config_path = config_path
        self.downloads = Path(downloads or Path.home() / "Downloads")
        self.job = {"name": "", "status": "", "running": False}
        self.lock = threading.Lock()

    def cfg(self):
        return config.load(self.config_path)

    # --- reading ---

    def state(self):
        cfg = self.cfg()
        running = self.launcher.running()
        focused = self.sway.focused_workspace()
        return {
            "name": os.uname().nodename,
            "version": __version__,
            "commit": read(config.ROOT / "COMMIT")[:7],
            "showing": "Home" if focused in (None, HOME_WS) else focused,
            "apps": [{"id": a.id, "name": a.name, "kind": a.kind, "color": a.color,
                      "removable": a.kind == "web", "running": a.workspace in running}
                     for a in catalog.all_apps(cfg)
                     if a.kind != "system" or a.id in ("browser", "android")],
            "android": catalog.android_state(),
            "stats": system_stats(),
            "job": dict(self.job),
        }

    def log(self):
        path = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "lumen/session.log"
        return {"log": "\n".join(read(path).splitlines()[-200:])}

    # --- remote control ---

    def do_key(self, body):
        key = body.get("key")
        if key == "home":
            ok = self.send("home")
        elif key == "close":
            ok = self.send("close")
        elif key == "keyboard":
            ok = self.send("osk toggle")
        elif key in KEYS:
            ok = self.send(f"key {KEYS[key]}")
        elif key in VOLUME:
            ok = run("wpctl", *VOLUME[key], timeout=5)[0]
        else:
            raise ApiError("Unknown key")
        if not ok:
            raise ApiError("The TV did not respond", 503)

    def do_text(self, body):
        text = str(body.get("text", ""))[:500].replace("\n", " ").replace("\r", "")
        if text and not self.send("text " + text):
            raise ApiError("The TV did not respond", 503)

    # --- apps ---

    def _background(self, name, work):
        """Run something slow without holding the request open; shown as `job`."""
        with self.lock:
            if self.job["running"]:
                raise ApiError(f"Busy: {self.job['name']}", 409)
            self.job = {"name": name, "status": "Working…", "running": True}

        def runner():
            try:
                status = work() or "Done"
            except Exception as e:      # keep the server alive, report the failure
                status = f"Failed: {e}"
            self.job = {"name": name, "status": status, "running": False}

        threading.Thread(target=runner, daemon=True).start()

    def do_launch(self, body):
        app = next((a for a in catalog.all_apps(self.cfg()) if a.id == body.get("id")), None)
        if app is None:
            raise ApiError("No such app", 404)
        if app.kind == "web":
            error = self.launcher.launch_web(app)
        elif app.id == "browser":
            if self.launcher.is_running(app):
                self.sway.command(f"workspace {app.workspace}")
                error = None
            else:
                error = self.launcher.open_url(self.cfg()["browser_home"])
        elif app.kind == "flatpak":
            error = self.launcher.launch_flatpak(app)
        elif app.kind == "android" or app.id == "android":
            if catalog.android_state() != "ready":
                raise ApiError("Android is not set up on the TV yet")

            def start():
                result = catalog.android_helper("ensure")
                if result.returncode != 0:
                    return "Failed: " + (result.stderr.strip().splitlines() or ["Android"])[-1]
                self.launcher.show_android(app.package or None)   # none: Android's home
                return f"Opened {app.name}"

            return self._background(f"Opening {app.name}", start)
        else:
            raise ApiError("This app can only be opened on the TV")
        if error:
            raise ApiError(error, 500)

    def do_open_url(self, body):
        url = catalog.normalize_url(str(body.get("url", "")))
        if not catalog.is_web_url(url):
            raise ApiError("Enter a web address")
        error = self.launcher.open_url(url)
        if error:
            raise ApiError(error, 500)

    def do_add_webapp(self, body):
        name = str(body.get("name", "")).strip()[:40]
        url = catalog.normalize_url(str(body.get("url", "")))
        if not name or not catalog.is_web_url(url):
            raise ApiError("A name and a web address are needed")
        cfg = self.cfg()
        catalog.add_web_app(cfg, name, url)
        config.save(cfg, self.config_path)

    def do_remove_webapp(self, body):
        cfg = self.cfg()
        if not catalog.remove_web_app(cfg, body.get("id")):
            raise ApiError("No such web app", 404)
        config.save(cfg, self.config_path)

    def save_apk(self, name, stream, length):
        """Receive an uploaded APK and install it in Android."""
        if catalog.android_state() != "ready":
            raise ApiError("Android is not set up on the TV yet")
        if not 0 < length <= MAX_APK:
            raise ApiError("The file is empty or too large", 413)
        safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name or "")) or "app.apk"
        if not safe.lower().endswith(".apk"):
            raise ApiError("Only .apk files can be installed")
        self.downloads.mkdir(parents=True, exist_ok=True)
        target = self.downloads / safe
        with open(target, "wb") as f:
            remaining = length
            while remaining:
                chunk = stream.read(min(remaining, 1 << 20))
                if not chunk:
                    raise ApiError("Upload interrupted")
                f.write(chunk)
                remaining -= len(chunk)

        def install():
            result = catalog.android_helper("install", str(target))
            if result.returncode != 0:
                return "Failed: " + (result.stderr.strip().splitlines() or ["install"])[-1]
            return f"Installed {safe}"

        self._background(f"Installing {safe}", install)

    # --- system ---

    def do_update(self, _body):
        def update():
            ok, out = run(str(config.ROOT / "bin/lumen-update"), "check", timeout=40)
            values = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
            if not ok or not values.get("latest"):
                return "Failed: could not reach the update server"
            if values.get("installed") == values["latest"]:
                return "Already up to date"
            ok, _ = run("sudo", "-n", str(config.ROOT / "bin/lumen-update"), "apply", timeout=1800)
            return "Updated. Lumen TV is restarting." if ok else "Failed: see the update log on the TV"

        self._background("Updating Lumen TV", update)

    def do_power(self, body):
        action = {"restart": "reboot", "shutdown": "poweroff"}.get(body.get("action"))
        if not action:
            raise ApiError("Unknown action")
        # Answer first, then go down.
        threading.Timer(1.0, lambda: run("systemctl", action)).start()


def make_handler(auth, device):
    page = (config.PKG_DATA / "remote.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "LumenTV"

        def log_message(self, *_):
            pass

        def reply(self, status, body=b"", content_type="application/json", headers=()):
            if isinstance(body, (dict, list)):
                body = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def authed(self):
            cookies = dict(part.strip().split("=", 1) for part in
                           self.headers.get("Cookie", "").split(";") if "=" in part)
            return auth.valid(cookies.get(COOKIE))

        def json_body(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_JSON:
                raise ApiError("Request too large", 413)
            try:
                data = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                raise ApiError("Bad request") from None
            if not isinstance(data, dict):
                raise ApiError("Bad request")
            return data

        def guard(self, work):
            try:
                result = work()
                self.reply(200, result if result is not None else {"ok": True})
            except ApiError as e:
                self.close_connection = True    # the body may not have been read
                self.reply(e.status, {"error": str(e)})
            except (BrokenPipeError, ConnectionError):
                pass

        def do_GET(self):
            path = urllib.parse.urlsplit(self.path).path
            if self.headers.get("Content-Length"):
                self.close_connection = True    # we never read a GET body
            if path == "/":
                return self.reply(200, page, "text/html; charset=utf-8")
            if not self.authed():
                return self.reply(401, {"error": "Not paired"})
            if path == "/api/state":
                return self.guard(device.state)
            if path == "/api/log":
                return self.guard(device.log)
            if path == "/api/diagnostics":
                return self.guard(diagnostics)
            self.reply(404, {"error": "Not found"})

        def do_POST(self):
            path = urllib.parse.urlsplit(self.path).path
            # A custom header cannot be sent by another website's form or script
            # without our permission, which rules out cross-site requests.
            if self.headers.get("X-Lumen") != "1":
                self.close_connection = True
                return self.reply(403, {"error": "Forbidden"})
            if path == "/api/pair":
                def pair():
                    token, error = auth.pair(self.json_body().get("pin", ""))
                    if not token:
                        raise ApiError(error, 403)
                    self.reply(200, {"ok": True}, headers=[(
                        "Set-Cookie", f"{COOKIE}={token}; Path=/; Max-Age=31536000; "
                                      "HttpOnly; SameSite=Strict")])
                try:
                    return pair()
                except ApiError as e:
                    return self.reply(e.status, {"error": str(e)})
            if not self.authed():
                self.close_connection = True
                return self.reply(401, {"error": "Not paired"})
            if path == "/api/apk":
                name = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get(
                    "name", [""])[0]
                length = int(self.headers.get("Content-Length") or 0)
                return self.guard(lambda: device.save_apk(name, self.rfile, length))
            action = getattr(device, "do_" + path[len("/api/"):].replace("-", "_"), None) \
                if path.startswith("/api/") else None
            if action is None:
                self.close_connection = True
                return self.reply(404, {"error": "Not found"})
            self.guard(lambda: action(self.json_body()))

    return Handler


def serve(port, auth=None, device=None):
    """Start the server on a background thread and return it."""
    server = ThreadingHTTPServer(("0.0.0.0", port), make_handler(auth or Auth(),
                                                                device or Device()))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    """Follow the "remote_access" setting: listen while it is on."""
    server = None
    while True:
        cfg = config.load()
        wanted = bool(cfg.get("remote_access"))
        if wanted and server is None:
            try:
                server = serve(int(cfg.get("remote_port") or DEFAULT_PORT))
            except OSError:
                pass        # port busy: try again on the next round
        elif not wanted and server is not None:
            server.shutdown()
            server.server_close()
            server = None
        time.sleep(3)


if __name__ == "__main__":
    main()
