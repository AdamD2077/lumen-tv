"""The app catalogue (web, Android, built-in) and how each kind is launched."""

import colorsys
import configparser
import hashlib
import os
import re
import shutil
import subprocess
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from . import config, icons
from .sway import HOME_WS, Sway

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".svg", ".avif", ".heic"}
MEDIA_EXT = {".mp4", ".mkv", ".webm", ".avi", ".mov", ".m4v", ".ts", ".mpg", ".mpeg",
             ".mp3", ".flac", ".ogg", ".opus", ".m4a", ".wav", ".aac"}


@dataclass
class App:
    id: str
    name: str
    kind: str                 # "web" | "android" | "flatpak" | "system"
    color: str = "#3b82f6"
    desc: str = ""
    url: str = ""
    user_agent: str = ""
    zoom: float = 0
    package: str = ""
    icon_path: str = ""       # image file (Android apps)
    icon: str = ""            # built-in icon name (system apps)

    @property
    def css(self):
        return re.sub(r"[^a-z0-9]", "-", self.id.lower())

    @property
    def kind_label(self):
        return {"web": "Web app", "android": "Android app", "flatpak": "App",
                "system": "Lumen TV"}[self.kind]

    @property
    def workspace(self):
        if self.kind == "android":
            return "android"
        if self.kind == "web" or self.id == "browser":
            return f"web-{self.css}"
        return f"app-{self.css}"


def color_for(text):
    """A stable, pleasant colour for apps that do not bring their own."""
    hue = int(hashlib.sha1(text.encode()).hexdigest()[:4], 16) / 0xFFFF
    r, g, b = colorsys.hls_to_rgb(hue, 0.42, 0.55)
    return f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"


def darken(color, amount=0.5):
    r, g, b = (int(color[i:i + 2], 16) for i in (1, 3, 5))
    return "#" + "".join(f"{int(c * (1 - amount)):02x}" for c in (r, g, b))


def slugify(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "app"


def normalize_url(text):
    text = text.strip()
    if text and not re.match(r"^[a-z][a-z0-9+.-]*://", text, re.I):
        text = "https://" + text
    return text


def add_web_app(cfg, name, url):
    """Add a web app to the config (and the favorites row). Returns its id."""
    taken = {a.id for a in all_apps({**cfg, "hidden": []})}
    app_id = base = slugify(name)
    n = 2
    while app_id in taken:
        app_id, n = f"{base}-{n}", n + 1
    cfg["web_apps"].append({"id": app_id, "name": name, "url": normalize_url(url)})
    cfg["favorites"].append(app_id)
    return app_id


def remove_web_app(cfg, app_id):
    """Remove a web app and its sign-in data. Returns False if there is none."""
    kept = [w for w in cfg["web_apps"] if w.get("id") != app_id]
    if len(kept) == len(cfg["web_apps"]):
        return False
    cfg["web_apps"] = kept
    cfg["favorites"] = [f for f in cfg["favorites"] if f != app_id]
    shutil.rmtree(config.DATA_DIR / "chromium" / slugify(app_id), ignore_errors=True)
    return True


def is_web_url(url):
    """True for a well-formed http(s) address with a host."""
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port                      # raises ValueError when it is not a number
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.hostname)


# --- catalogue ------------------------------------------------------------

def web_apps(cfg):
    apps = []
    for w in cfg.get("web_apps", []):
        if not w.get("id") or not w.get("url"):
            continue
        host = re.sub(r"^[a-z]+://(www\.)?", "", w["url"]).split("/")[0]
        app = App(
            id=w["id"], name=w.get("name", w["id"]), kind="web", url=w["url"],
            desc=w.get("desc") or host,
            user_agent=w.get("user_agent", ""), zoom=w.get("zoom", 0))
        # The site's own icon, once the launcher has fetched it (see icons.py).
        icon = icons.icon_path(app.css)
        app.icon_path = str(icon) if icon.exists() else ""
        app.color = w.get("color") or icons.tile_color(icon) or color_for(w["id"])
        apps.append(app)
    return apps


def system_apps():
    return [
        App("browser", "Browser", "system", "#2563eb", icon="globe",
            desc="Browse the web. Move the pointer with the right stick."),
        App("files", "Files", "system", "#d97706", icon="folder",
            desc="Photos, videos and music from this device or a USB drive."),
        App("store", "Store", "system", "#7c3aed", icon="store",
            desc="Find and install apps from Flathub, and Android app stores."),
        App("android", "Android", "system", "#16a34a", icon="android",
            desc="Open the Android home screen, install and manage apps."),
        App("settings", "Settings", "system", "#475569", icon="settings",
            desc="Network, controllers, sound, display and power."),
    ]


# Entries Waydroid lists that are not something to open from a home screen.
ANDROID_NOT_APPS = {"com.android.inputmethod.latin"}


def android_dirs():
    return [Path.home() / ".local/share/applications"]


def android_apps(dirs=None):
    """Android apps, read from the .desktop entries Waydroid generates."""
    apps = []
    for d in dirs or android_dirs():
        for path in sorted(Path(d).glob("waydroid.*.desktop")):
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                parser.read(path, encoding="utf-8")
                entry = parser["Desktop Entry"]
            except (configparser.Error, KeyError, OSError):
                continue
            # Waydroid marks every app NoDisplay=true (to keep them out of desktop
            # menus), so that flag says nothing about whether to show an app here.
            package = path.name[len("waydroid."):-len(".desktop")]
            if package in ANDROID_NOT_APPS:
                continue
            icon = entry.get("Icon", "")
            icon = icon if os.path.isfile(icon) else ""
            apps.append(App(
                id=f"android:{package}", name=entry.get("Name", package), kind="android",
                package=package, desc=package, icon_path=icon,
                color=(icons.tile_color(icon) if icon else None) or color_for(package)))
    apps.sort(key=lambda a: a.name.lower())
    return apps


def flatpak_dirs():
    return [Path.home() / ".local/share/flatpak/exports/share",
            Path("/var/lib/flatpak/exports/share")]


def flatpak_icon(share, name):
    """Find an exported app icon, preferring large bitmaps."""
    if os.path.isabs(name):
        return name if os.path.isfile(name) else ""
    for size in ("512x512", "256x256", "128x128", "scalable", "96x96", "64x64"):
        for ext in ("png", "svg"):
            path = Path(share) / "icons/hicolor" / size / "apps" / f"{name}.{ext}"
            if path.is_file():
                return str(path)
    return ""


def flatpak_apps(dirs=None):
    """Flatpak apps, read from the .desktop entries Flatpak exports."""
    apps = {}
    for share in dirs or flatpak_dirs():
        for path in sorted(Path(share, "applications").glob("*.desktop")):
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            try:
                parser.read(path, encoding="utf-8")
                entry = parser["Desktop Entry"]
            except (configparser.Error, KeyError, OSError):
                continue
            app_id = entry.get("X-Flatpak", "")
            if (not app_id or f"flatpak:{app_id}" in apps
                    or entry.get("NoDisplay", "false").lower() == "true"):
                continue
            icon = flatpak_icon(share, entry.get("Icon", app_id))
            apps[f"flatpak:{app_id}"] = App(
                id=f"flatpak:{app_id}", name=entry.get("Name", app_id), kind="flatpak",
                package=app_id, desc=entry.get("Comment", "") or app_id, icon_path=icon,
                color=(icons.tile_color(icon) if icon else None) or color_for(app_id))
    return sorted(apps.values(), key=lambda a: a.name.lower())


def installed_stamp():
    """Changes whenever an Android or Flatpak app (or its icon) is added or
    removed. Cheap enough to poll: it only looks at a few folders' timestamps."""
    folders = list(android_dirs()) + [Path(d) / "applications" for d in flatpak_dirs()]
    folders.append(Path.home() / ".local/share/waydroid/data/icons")
    stamp = []
    for folder in folders:
        try:
            stamp.append(os.stat(folder).st_mtime_ns)
        except OSError:
            stamp.append(None)
    return tuple(stamp)


def all_apps(cfg):
    hidden = set(cfg.get("hidden", []))
    apps = web_apps(cfg) + android_apps() + flatpak_apps() + system_apps()
    return [a for a in apps if a.id not in hidden]


# --- Android (Waydroid) ---------------------------------------------------

def android_state():
    """One of "missing", "uninitialized", "ready"."""
    if not shutil.which("waydroid"):
        return "missing"
    if not os.path.exists("/var/lib/waydroid/waydroid.cfg"):
        return "uninitialized"
    return "ready"


def android_helper(*args, **kwargs):
    return subprocess.run([str(config.ROOT / "bin/lumen-android"), *args],
                          capture_output=True, text=True, **kwargs)


# --- launching ------------------------------------------------------------

def chromium_bin():
    return shutil.which("chromium") or shutil.which("chromium-browser")


def chromium_cmd(exe, url, profile="default", kiosk=True, user_agent="", zoom=0):
    cmd = [
        exe, "--ozone-platform=wayland", "--no-first-run", "--no-default-browser-check",
        "--noerrdialogs", "--disable-session-crashed-bubble", "--disable-features=Translate",
        "--autoplay-policy=no-user-gesture-required", "--password-store=basic",
        f"--user-data-dir={config.DATA_DIR / 'chromium' / profile}",
    ]
    if user_agent:
        cmd.append(f"--user-agent={user_agent}")
    if zoom:
        cmd.append(f"--force-device-scale-factor={zoom:g}")
    if kiosk:
        cmd.append("--kiosk")
    return cmd + [url]


def file_cmd(path):
    """Command that opens a local file, or None if we have no viewer for it."""
    path = Path(path)
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        # -n starts on this picture; passing the folder lets left/right browse it.
        return ["imv", "-f", "-n", str(path), str(path.parent)]
    if ext in MEDIA_EXT:
        return ["mpv", "--fs", "--hwdec=auto-safe",
                f"--input-conf={config.ROOT / 'config/mpv-input.conf'}", str(path)]
    return None


class Launcher:
    """Starts apps, each on its own sway workspace so it fills the screen.

    Going home just switches back to the launcher's workspace; the app keeps
    running and re-opening it resumes where it was."""

    def __init__(self, sway=None):
        self.sway = sway or Sway()

    def running(self):
        return self.sway.workspace_names() - {HOME_WS}

    def is_running(self, app):
        return app.workspace in self.running()

    def _spawn(self, cmd, workspace, env=None):
        self.sway.command(f"workspace {workspace}")
        try:
            subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True,
                             env={**os.environ, **(env or {})})
            return None
        except OSError as e:
            self.sway.go_home()
            return f"Could not start {cmd[0]}: {e.strerror}"

    def _close_web(self, keep=None):
        # One browser at a time: Chromium is the heaviest thing on the system.
        old = [ws for ws in self.running() if ws.startswith("web-") and ws != keep]
        for ws in old:
            self.sway.command(f'[workspace="{ws}"] kill')
        if old:
            # Let the old browser exit, or a new one sharing its profile would
            # hand its URL to the dying process and quit.
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and set(old) & self.running():
                time.sleep(0.05)
            time.sleep(0.3)

    def open_url(self, url, workspace="web-browser", profile="default", kiosk=False,
                 user_agent="", zoom=1.25):
        exe = chromium_bin()
        if not exe:
            return "Chromium is not installed"
        self._close_web()
        return self._spawn(chromium_cmd(exe, url, profile, kiosk, user_agent, zoom), workspace)

    def launch_web(self, app):
        if self.is_running(app):
            self.sway.command(f"workspace {app.workspace}")
            return None
        # A custom user agent needs its own browser process, hence its own profile.
        profile = app.css if app.user_agent else "default"
        return self.open_url(app.url, app.workspace, profile, kiosk=True,
                             user_agent=app.user_agent, zoom=app.zoom)

    def open_file(self, path):
        cmd = file_cmd(path)
        if not cmd:
            return "No viewer for this kind of file"
        if not shutil.which(cmd[0]):
            return f"{cmd[0]} is not installed"
        return self._spawn(cmd, "app-media",
                           env={"imv_config": str(config.ROOT / "config/imv.conf")})

    def launch_flatpak(self, app):
        if self.is_running(app):
            self.sway.command(f"workspace {app.workspace}")
            return None
        if not shutil.which("flatpak"):
            return "Flatpak is not installed"
        return self._spawn(["flatpak", "run", app.package], app.workspace)

    def show_android(self, package=None):
        """Bring Android to the front. Call once `lumen-android ensure` succeeded."""
        self.sway.command("workspace android")
        cmd = ["waydroid", "app", "launch", package] if package else ["waydroid", "show-full-ui"]
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)

    def close(self, app):
        self.sway.command(f'[workspace="{app.workspace}"] kill')
