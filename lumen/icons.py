"""App icons: fetch a web app's own icon from its site, and tile colours.

A web app's icon is whatever the site itself advertises (the icon a phone
would use when you add the site to its home screen), downloaded once and
cached. Nothing is bundled with Lumen TV.
"""

import gzip
import http.client
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

ICON_DIR = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "lumen" / "icons"
MIN_SIZE = 64            # smaller icons look worse on a TV than the app's name does
SAVE_SIZE = 192
MAX_BYTES = 3 * 1024 * 1024
RETRY_AFTER = 7 * 24 * 3600
# Sites serve their proper icons to browsers, not to unknown clients.
USER_AGENT = ("Mozilla/5.0 (X11; Linux aarch64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36")


def icon_path(app_css, icon_dir=None):
    return Path(icon_dir or ICON_DIR) / f"{app_css}.png"


def _get(url, limit=MAX_BYTES, timeout=12):
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        raise ValueError("not a web address")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   "Accept-Language": "en"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(limit + 1)
            final = response.geturl()
    except http.client.HTTPException as e:      # e.g. a connection cut mid-page
        raise OSError(str(e)) from e
    if len(data) > limit:
        raise ValueError("too large")
    if data[:2] == b"\x1f\x8b":        # some servers compress without being asked
        try:
            data = gzip.decompress(data)
        except (OSError, EOFError) as e:
            raise ValueError("bad compressed data") from e
    return data, final


class _Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == "link":
            self.links.append({k.lower(): (v or "") for k, v in attrs})


def _declared_size(sizes, default):
    found = [int(n) for n in re.findall(r"(\d+)x\d+", sizes.lower())]
    return max(found) if found else default


def icon_candidates(html, base_url):
    """Icon URLs a page advertises, best first, plus the conventional fallbacks.

    Returns [(url, expected size, is_manifest)]."""
    parser = _Links()
    try:
        parser.feed(html)
    except Exception:       # malformed markup: use what was parsed so far
        pass
    found = []
    for link in parser.links:
        rel = link.get("rel", "").lower().split()
        href = link.get("href", "").strip()
        if not href or href.startswith("data:"):
            continue
        url = urllib.parse.urljoin(base_url, href)
        if "manifest" in rel:
            found.append((url, 512, True))
        elif "apple-touch-icon" in rel or "apple-touch-icon-precomposed" in rel:
            found.append((url, _declared_size(link.get("sizes", ""), 180), False))
        elif "icon" in rel:
            vector = href.lower().split("?")[0].endswith(".svg") or "svg" in link.get("type", "")
            found.append((url, 512 if vector else _declared_size(link.get("sizes", ""), 32),
                          False))
    found.sort(key=lambda c: -c[1])
    for path, size in (("/apple-touch-icon.png", 180), ("/favicon.ico", 48)):
        found.append((urllib.parse.urljoin(base_url, path), size, False))
    seen, unique = set(), []
    for candidate in found:
        if candidate[0] not in seen:
            seen.add(candidate[0])
            unique.append(candidate)
    return unique


def manifest_icons(text, manifest_url):
    """[(url, size)] from a web app manifest, largest first."""
    try:
        icons = json.loads(text).get("icons", [])
    except (ValueError, AttributeError):
        return []
    found = []
    for icon in icons if isinstance(icons, list) else []:
        if isinstance(icon, dict) and icon.get("src"):
            if "maskable" in str(icon.get("purpose", "")) and "any" not in str(icon.get("purpose")):
                continue        # maskable icons have padding meant to be cropped
            found.append((urllib.parse.urljoin(manifest_url, icon["src"]),
                          _declared_size(str(icon.get("sizes", "")), 0)))
    return sorted(found, key=lambda c: -c[1])


def _save_png(data, target):
    """Decode image bytes and save as a PNG. Returns its size in pixels, or 0."""
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, GLib
    try:
        loader = GdkPixbuf.PixbufLoader()
        # Vector icons would otherwise decode at their tiny nominal size.
        loader.connect("size-prepared", lambda ld, w, h: ld.set_size(SAVE_SIZE, SAVE_SIZE)
                       if data.lstrip()[:5] in (b"<?xml", b"<svg ") else None)
        loader.write(data)
        loader.close()
        pixbuf = loader.get_pixbuf()
    except GLib.Error:
        return 0
    if pixbuf is None:
        return 0
    size = min(pixbuf.get_width(), pixbuf.get_height())
    if size < MIN_SIZE:
        return size
    if max(pixbuf.get_width(), pixbuf.get_height()) > SAVE_SIZE:
        ratio = SAVE_SIZE / max(pixbuf.get_width(), pixbuf.get_height())
        pixbuf = pixbuf.scale_simple(max(1, round(pixbuf.get_width() * ratio)),
                                     max(1, round(pixbuf.get_height() * ratio)),
                                     GdkPixbuf.InterpType.BILINEAR)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".tmp")
    pixbuf.savev(str(tmp), "png", [], [])
    os.replace(tmp, target)
    return size


def fetch_icon(app_css, page_url, icon_dir=None, get=_get):
    """Download the site's icon into the cache. Returns its path or None."""
    target = icon_path(app_css, icon_dir)
    # The app's own page first, then the site's front page: an app such as a
    # TV interface often declares no icons of its own.
    root = urllib.parse.urljoin(page_url, "/")
    queue = []
    for page in dict.fromkeys((page_url, root)):
        try:
            html, final_url = get(page)
            found = icon_candidates(html.decode("utf-8", "replace"), final_url)
        except (OSError, ValueError):
            # Unreachable, or it refuses us; the well-known paths may still work.
            found = icon_candidates("", page)
        queue += [c for c in found if c not in queue]
    queue.sort(key=lambda c: -c[1])
    tried = 0
    while queue and tried < 10:
        url, _size, is_manifest = queue.pop(0)
        tried += 1
        try:
            data, final = get(url)
        except (OSError, ValueError):
            continue
        if is_manifest:
            queue[:0] = [(u, s, False) for u, s in manifest_icons(data.decode("utf-8", "replace"),
                                                                  final)[:2]]
            continue
        if _save_png(data, target) >= MIN_SIZE:
            return target
    return None


def missing(app_css, icon_dir=None, now=None):
    """True when we have no icon and have not recently failed to get one."""
    target = icon_path(app_css, icon_dir)
    if target.exists():
        return False
    marker = target.with_suffix(".none")
    try:
        return (now or time.time()) - marker.stat().st_mtime > RETRY_AFTER
    except OSError:
        return True


def mark_failed(app_css, icon_dir=None):
    marker = icon_path(app_css, icon_dir).with_suffix(".none")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.touch()


_colors = {}


def tile_color(path):
    """A background colour that suits an icon: its average colour, kept dark
    enough for white text. Returns "#rrggbb" or None."""
    try:
        key = (str(path), os.stat(path).st_mtime_ns)
    except OSError:
        return None
    if key in _colors:
        return _colors[key]
    import colorsys
    import gi
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf, GLib
    try:
        small = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), 12, 12, False)
    except GLib.Error:
        return None
    pixels, channels, stride = small.get_pixels(), small.get_n_channels(), small.get_rowstride()
    total, weight = [0.0, 0.0, 0.0], 0.0
    for y in range(small.get_height()):
        for x in range(small.get_width()):
            i = y * stride + x * channels
            r, g, b = pixels[i], pixels[i + 1], pixels[i + 2]
            alpha = pixels[i + 3] / 255 if channels == 4 else 1.0
            # Colourful pixels say more about an icon than its white or black parts.
            saturation = (max(r, g, b) - min(r, g, b)) / 255
            w = alpha * (0.15 + saturation)
            total = [total[0] + r * w, total[1] + g * w, total[2] + b * w]
            weight += w
    if weight == 0:
        return None
    h, light, s = colorsys.rgb_to_hls(*(c / weight / 255 for c in total))
    r, g, b = colorsys.hls_to_rgb(h, min(max(light, 0.26), 0.40), min(s, 0.75))
    color = f"#{int(r * 255):02x}{int(g * 255):02x}{int(b * 255):02x}"
    _colors[key] = color
    return color
