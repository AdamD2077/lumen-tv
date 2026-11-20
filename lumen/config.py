"""Paths and the user configuration file (~/.config/lumen/config.json)."""

import copy
import json
import os
from pathlib import Path

APP_ID = "tv.lumen.shell"


def _xdg(var, default):
    return Path(os.environ.get(var) or Path.home() / default)


CONFIG_DIR = _xdg("XDG_CONFIG_HOME", ".config") / "lumen"
DATA_DIR = _xdg("XDG_DATA_HOME", ".local/share") / "lumen"
CONFIG_FILE = CONFIG_DIR / "config.json"
PKG_DATA = Path(__file__).parent / "data"
# Repo/install root (holds bin/ and config/).
ROOT = Path(os.environ.get("LUMEN_HOME") or Path(__file__).parent.parent)

# YouTube only serves its remote-friendly "leanback" UI to TV browsers.
TV_USER_AGENT = (
    "Mozilla/5.0 (SMART-TV; Linux; Tizen 5.0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) SamsungBrowser/2.2 Chrome/63.0.3239.84 TV Safari/537.36"
)

DEFAULT_WEB_APPS = [
    {"id": "youtube", "name": "YouTube", "url": "https://www.youtube.com/tv",
     "color": "#e0201b", "user_agent": TV_USER_AGENT,
     "desc": "Videos, music and live streams in the TV interface."},
    {"id": "netflix", "name": "Netflix", "url": "https://www.netflix.com",
     "color": "#b1060f", "desc": "Films and series."},
    {"id": "primevideo", "name": "Prime Video", "url": "https://www.primevideo.com",
     "color": "#1a98ff", "desc": "Films, series and live sport."},
    {"id": "disneyplus", "name": "Disney+", "url": "https://www.disneyplus.com",
     "color": "#0f3fd1", "desc": "Films and series."},
    {"id": "twitch", "name": "Twitch", "url": "https://www.twitch.tv",
     "color": "#9146ff", "desc": "Live streams and gaming."},
    {"id": "spotify", "name": "Spotify", "url": "https://open.spotify.com",
     "color": "#1db954", "desc": "Music and podcasts."},
    {"id": "plex", "name": "Plex", "url": "https://app.plex.tv",
     "color": "#e5a00d", "desc": "Your own media library."},
]

DEFAULTS = {
    "favorites": ["youtube", "netflix", "spotify", "browser", "files"],
    "hidden": [],
    "web_apps": DEFAULT_WEB_APPS,
    "prefer_1080p": True,
    "android_autostart": False,
    "remote_access": False,
    "remote_port": 8080,
    "search_url": "https://duckduckgo.com/?q={}",
    "browser_home": "https://duckduckgo.com",
}


def load(path=None):
    """Return the config, with defaults filled in for anything missing."""
    cfg = copy.deepcopy(DEFAULTS)
    try:
        with open(path or CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg.update(data)
    except (OSError, ValueError):
        pass
    return cfg


def save(cfg, path=None):
    path = Path(path or CONFIG_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, path)
