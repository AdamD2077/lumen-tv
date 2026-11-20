"""Tests for the parts of Lumen TV that need no display, controller or sway.

    python3 -m unittest discover tests
"""
import json
import os
import socket
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lumen import apps, config, input as pad, osk, sway  # noqa: E402


class FakeOut:
    def __init__(self):
        self.events = []

    def tap(self, name, shift=False):
        self.events.append(("tap", name))

    def key(self, name, down):
        self.events.append(("key", name, down))

    def move(self, dx, dy):
        self.events.append(("move", dx, dy))

    def wheel(self, step):
        self.events.append(("wheel", step))


class FakeOsk:
    def __init__(self):
        self.visible = False
        self.calls = []

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False

    def __getattr__(self, name):
        return lambda *args: self.calls.append((name, *args))


class FakeSystem:
    def __init__(self):
        self.calls = []

    def home(self):
        self.calls.append("home")

    def close_app(self):
        self.calls.append("close")


def make_pad(absinfo=None):
    out, keyboard, system = FakeOut(), FakeOsk(), FakeSystem()
    mapper = pad.Mapper(out, keyboard, system)
    state = pad.PadState(mapper, absinfo or {
        pad.ABS_X: (-32768, 32767), pad.ABS_Y: (-32768, 32767),
        pad.ABS_RX: (-32768, 32767), pad.ABS_RY: (-32768, 32767),
        pad.ABS_Z: (0, 255), pad.ABS_RZ: (0, 255)})
    return state, mapper, out, keyboard, system


class ControllerTest(unittest.TestCase):
    def test_buttons_become_keys(self):
        state, _, out, _, _ = make_pad()
        state.event(pad.EV_KEY, 0x130, 1, 0.0)   # A
        state.event(pad.EV_KEY, 0x130, 0, 0.1)
        state.event(pad.EV_KEY, 0x131, 1, 0.2)   # B
        self.assertEqual(out.events, [("tap", "KEY_ENTER"), ("tap", "KEY_ESC")])

    def test_dpad_repeats_while_held(self):
        state, _, out, _, _ = make_pad()
        state.event(pad.EV_ABS, pad.ABS_HAT0X, 1, 0.0)
        self.assertEqual(out.events, [("tap", "KEY_RIGHT")])
        self.assertTrue(state.tick(0.2))                 # before the repeat delay
        self.assertEqual(len(out.events), 1)
        state.tick(0.4)
        state.tick(0.45)                                  # faster than the repeat rate
        state.tick(0.52)
        self.assertEqual(len(out.events), 3)
        state.event(pad.EV_ABS, pad.ABS_HAT0X, 0, 0.6)
        self.assertFalse(state.tick(1.5))
        self.assertEqual(len(out.events), 3)

    def test_left_stick_navigates_once_per_push(self):
        state, _, out, _, _ = make_pad()
        for value in (10000, 25000, 32767):               # one continuous push down
            state.event(pad.EV_ABS, pad.ABS_Y, value, 0.0)
        self.assertEqual(out.events, [("tap", "KEY_DOWN")])

    def test_right_stick_moves_pointer_with_deadzone(self):
        state, _, out, _, _ = make_pad()
        state.event(pad.EV_ABS, pad.ABS_RX, 3000, 0.0)   # inside the deadzone
        self.assertFalse(state.tick(0.016))
        state.event(pad.EV_ABS, pad.ABS_RX, 32767, 0.1)
        self.assertTrue(state.tick(0.116))
        self.assertTrue(state.tick(0.132))
        moves = [e for e in out.events if e[0] == "move"]
        self.assertTrue(moves and all(m[1] > 0 and m[2] == 0 for m in moves))

    def test_trigger_axis_is_a_held_mouse_button(self):
        state, _, out, _, _ = make_pad()
        state.event(pad.EV_ABS, pad.ABS_RZ, 250, 0.0)
        state.event(pad.EV_ABS, pad.ABS_RZ, 255, 0.1)    # still down: no second press
        state.event(pad.EV_ABS, pad.ABS_RZ, 0, 0.2)
        self.assertEqual(out.events, [("key", "BTN_LEFT", True), ("key", "BTN_LEFT", False)])

    def test_simple_pad_uses_z_axes_as_right_stick(self):
        state, _, out, _, _ = make_pad({pad.ABS_X: (0, 255), pad.ABS_Y: (0, 255),
                                        pad.ABS_Z: (0, 255), pad.ABS_RZ: (0, 255)})
        state.event(pad.EV_ABS, pad.ABS_Z, 255, 0.0)
        state.tick(0.016)
        state.tick(0.032)
        self.assertTrue(any(e[0] == "move" for e in out.events))
        self.assertFalse(any(e[0] == "key" for e in out.events))

    def test_select_short_press_goes_home_long_press_closes(self):
        state, _, _, _, system = make_pad()
        state.event(pad.EV_KEY, 0x13a, 1, 0.0)
        state.event(pad.EV_KEY, 0x13a, 0, 0.2)
        state.event(pad.EV_KEY, 0x13a, 1, 1.0)
        state.event(pad.EV_KEY, 0x13a, 0, 2.0)
        self.assertEqual(system.calls, ["home", "close"])

    def test_shoulder_buttons_scroll_while_held(self):
        state, mapper, out, _, _ = make_pad()
        state.event(pad.EV_KEY, 0x137, 1, 0.0)            # RB
        self.assertTrue(mapper.tick(0.03))                # too soon for another step
        mapper.tick(0.08)
        state.event(pad.EV_KEY, 0x137, 0, 0.1)
        self.assertFalse(mapper.tick(0.2))
        self.assertEqual(out.events, [("wheel", -1), ("wheel", -1)])

    def test_keyboard_open_redirects_the_controller(self):
        state, _, out, keyboard, _ = make_pad()
        state.event(pad.EV_KEY, 0x133, 1, 0.0)            # north: open keyboard
        self.assertTrue(keyboard.visible)
        state.event(pad.EV_ABS, pad.ABS_HAT0Y, 1, 0.1)    # moves the key selection
        state.event(pad.EV_KEY, 0x130, 1, 0.2)            # A types
        state.event(pad.EV_KEY, 0x134, 1, 0.3)            # west: backspace
        state.event(pad.EV_KEY, 0x131, 1, 0.4)            # B closes
        self.assertEqual(keyboard.calls, [("move", "down"), ("press",)])
        self.assertEqual(out.events, [("tap", "KEY_BACKSPACE")])
        self.assertFalse(keyboard.visible)


class KeyboardTest(unittest.TestCase):
    def test_every_row_fills_the_grid(self):
        for name, rows in osk.LAYERS.items():
            for row in rows:
                self.assertEqual(sum(k.span for k in row), osk.COLS, name)

    def test_every_character_can_be_typed(self):
        for rows in osk.LAYERS.values():
            for row in rows:
                for key in row:
                    if key.action == "char":
                        self.assertIn(key.label, osk.CHARMAP)
                        self.assertIn(key.label.upper(), osk.CHARMAP)

    def test_charmap_uses_real_key_codes(self):
        try:
            from evdev import ecodes
        except ImportError:
            self.skipTest("python-evdev not installed")
        for key_name, _shift in osk.CHARMAP.values():
            self.assertTrue(hasattr(ecodes, key_name), key_name)
        for name in list(pad.Mapper.NORMAL.values()) + list(pad.Mapper.NAV_KEYS.values()):
            self.assertTrue(hasattr(ecodes, name), name)
        for code, name in pad.BUTTONS.items():           # our constants match the kernel's
            self.assertIn(code, ecodes.BTN)
        self.assertEqual((pad.KEY_OK, pad.KEY_BACK, pad.KEY_EXIT),
                         (ecodes.KEY_OK, ecodes.KEY_BACK, ecodes.KEY_EXIT))

    def test_typing_shift_and_layers(self):
        model = osk.OskModel()
        model.row, model.col = 1, 0
        self.assertEqual(model.press(), ("char", "q"))
        model.toggle_shift()
        self.assertEqual(model.press(), ("char", "Q"))
        self.assertEqual(model.press(), ("char", "q"))    # shift lasts one letter
        model.toggle_layer()
        self.assertEqual(model.press(), ("char", "@"))

    def test_moving_wraps_and_tracks_columns(self):
        model = osk.OskModel()
        model.row, model.col = 1, 9                       # "p"
        model.move("right")
        self.assertEqual(model.key.label, "q")
        model.row, model.col = 3, 9                       # "." sits above Done
        model.move("down")
        self.assertEqual(model.key.action, "done")
        model.move("down")                                # wraps to the top row
        self.assertEqual(model.key.label, "0")
        for _ in range(200):                              # never leaves the layout
            for direction in ("up", "left", "down", "down", "right"):
                model.move(direction)
                self.assertIsNotNone(model.key)


class SwayTest(unittest.TestCase):
    def test_ipc_round_trip(self):
        path = os.path.join(tempfile.mkdtemp(), "sway.sock")
        server = socket.socket(socket.AF_UNIX)
        server.bind(path)
        server.listen(1)
        seen = []

        def serve():
            conn, _ = server.accept()
            with conn:
                seen.append(conn.recv(4096))
                conn.sendall(sway.pack(sway.RUN_COMMAND, json.dumps([{"success": True}])))

        thread = threading.Thread(target=serve)
        thread.start()
        self.assertTrue(sway.Sway(path).command("workspace home"))
        thread.join()
        server.close()
        self.assertEqual(seen[0], b"i3-ipc" + (14).to_bytes(4, sys.byteorder)
                         + (0).to_bytes(4, sys.byteorder) + b"workspace home")

    def test_no_sway_is_harmless(self):
        offline = sway.Sway(path="")
        self.assertFalse(offline.available)
        self.assertFalse(offline.command("workspace home"))
        self.assertEqual(offline.workspace_names(), set())

    TV_4K = {"name": "HDMI-A-1", "active": True,
             "current_mode": {"width": 3840, "height": 2160, "refresh": 60000},
             "modes": [{"width": 3840, "height": 2160, "refresh": 60000},
                       {"width": 3840, "height": 2160, "refresh": 30000},
                       {"width": 2560, "height": 1440, "refresh": 59951},
                       {"width": 1920, "height": 1080, "refresh": 120000},
                       {"width": 1920, "height": 1080, "refresh": 60000},
                       {"width": 1920, "height": 1080, "refresh": 59940},
                       {"width": 1920, "height": 1080, "refresh": 50000},
                       {"width": 1280, "height": 720, "refresh": 60000},
                       {"width": 640, "height": 480, "refresh": 60000}]}

    def test_4k_tv_is_driven_at_1080p_by_default(self):
        self.assertEqual(sway.output_command(self.TV_4K),
                         "output HDMI-A-1 mode 1920x1080@60.000Hz scale 1")
        self.assertEqual(sway.output_command(self.TV_4K, "auto"),
                         sway.output_command(self.TV_4K))

    def test_native_and_explicit_resolutions(self):
        tv = self.TV_4K
        # Already in the wanted mode: only the scale is set.
        self.assertEqual(sway.output_command(tv, "native"), "output HDMI-A-1 scale 2")
        self.assertEqual(sway.output_command(tv, "2560x1440@59951"),
                         "output HDMI-A-1 mode 2560x1440@59.951Hz scale 1.333")
        self.assertEqual(sway.output_command(tv, "1920x1080@120000"),
                         "output HDMI-A-1 mode 1920x1080@120.000Hz scale 1")
        self.assertEqual(sway.output_command(tv, "1280x720@60000"),
                         "output HDMI-A-1 mode 1280x720@60.000Hz scale 0.667")
        # A mode this TV does not have (settings carried over from another TV).
        self.assertEqual(sway.output_command(tv, "1600x900@60000"),
                         sway.output_command(tv, "auto"))
        # "native" after "auto" has switched the TV to 1080p goes back up.
        at_1080 = {**tv, "current_mode": {"width": 1920, "height": 1080, "refresh": 60000}}
        self.assertEqual(sway.output_command(at_1080, "native"),
                         "output HDMI-A-1 mode 3840x2160@60.000Hz scale 2")
        self.assertEqual(sway.output_command(at_1080, "auto"), "output HDMI-A-1 scale 1")

    def test_offered_modes_are_deduplicated_and_tv_sized(self):
        labels = [sway.mode_label(m) for m in sway.available_modes(self.TV_4K)]
        self.assertEqual(labels, [
            "3840 × 2160 · 60 Hz", "3840 × 2160 · 30 Hz", "2560 × 1440 · 60 Hz",
            "1920 × 1080 · 120 Hz", "1920 × 1080 · 60 Hz", "1920 × 1080 · 50 Hz",
            "1280 × 720 · 60 Hz"])
        for mode in sway.available_modes(self.TV_4K):        # every offer can be applied
            self.assertIn("mode " if sway.mode_key(mode) != "3840x2160@60000" else "scale",
                          sway.output_command(self.TV_4K, sway.mode_key(mode)))

    def test_720p_tv_is_scaled_down(self):
        out = {"name": "HDMI-A-1", "active": True, "modes": [],
               "current_mode": {"width": 1280, "height": 720, "refresh": 60000}}
        self.assertEqual(sway.output_command(out), "output HDMI-A-1 scale 0.667")
        self.assertIsNone(sway.output_command({"name": "x", "active": False}))

    def test_small_only_display_keeps_its_best_mode(self):
        out = {"name": "DSI-1", "active": True,
               "current_mode": {"width": 2560, "height": 1440, "refresh": 60000},
               "modes": [{"width": 2560, "height": 1440, "refresh": 60000}]}
        self.assertEqual(sway.output_command(out), "output DSI-1 scale 1.333")


class AppsTest(unittest.TestCase):
    def test_android_apps_come_from_waydroid_desktop_files(self):
        folder = Path(tempfile.mkdtemp())
        (folder / "waydroid.org.videolan.vlc.desktop").write_text(
            "[Desktop Entry]\nType=Application\nName=VLC\n"
            "Exec=waydroid app launch org.videolan.vlc\nIcon=/nonexistent/vlc.png\n")
        (folder / "waydroid.com.hidden.desktop").write_text(
            "[Desktop Entry]\nName=Hidden\nNoDisplay=true\n")
        (folder / "waydroid.broken.desktop").write_text("not a desktop file")
        (folder / "Waydroid.desktop").write_text("[Desktop Entry]\nName=Waydroid\n")
        found = apps.android_apps([folder])
        self.assertEqual([(a.id, a.name, a.package) for a in found],
                         [("android:org.videolan.vlc", "VLC", "org.videolan.vlc")])
        self.assertEqual(found[0].workspace, "android")
        self.assertEqual(found[0].icon_path, "")          # missing icon file is ignored

    def test_catalogue_respects_hidden_and_has_unique_css_names(self):
        cfg = config.load(path="/nonexistent")
        cfg["hidden"] = ["netflix"]
        catalogue = apps.all_apps(cfg)
        ids = [a.id for a in catalogue]
        self.assertNotIn("netflix", ids)
        self.assertIn("youtube", ids)
        self.assertIn("settings", ids)
        self.assertEqual(len({a.css for a in catalogue}), len(catalogue))

    def test_browser_command(self):
        cmd = apps.chromium_cmd("chromium", "https://example.org", "p", kiosk=True,
                                user_agent="TV UA", zoom=1.25)
        self.assertEqual(cmd[0], "chromium")
        self.assertEqual(cmd[-2:], ["--kiosk", "https://example.org"])
        self.assertIn("--user-agent=TV UA", cmd)
        self.assertIn("--force-device-scale-factor=1.25", cmd)
        self.assertNotIn("--kiosk", apps.chromium_cmd("chromium", "https://example.org"
                                                      , kiosk=False))

    def test_files_open_in_the_right_viewer(self):
        self.assertEqual(apps.file_cmd("/media/usb/Holiday/IMG 1.JPG")[:4],
                         ["imv", "-f", "-n", "/media/usb/Holiday/IMG 1.JPG"])
        self.assertEqual(apps.file_cmd("/x/film.mkv")[0], "mpv")
        self.assertIsNone(apps.file_cmd("/x/notes.txt"))

    def test_url_and_slug_helpers(self):
        self.assertEqual(apps.normalize_url(" example.org/tv "), "https://example.org/tv")
        self.assertEqual(apps.normalize_url("http://nas.local:8096"), "http://nas.local:8096")
        self.assertEqual(apps.slugify("My Jellyfin!"), "my-jellyfin")
        self.assertRegex(apps.color_for("anything"), r"^#[0-9a-f]{6}$")
        self.assertEqual(apps.darken("#ff8000", 0.5), "#7f4000")


class FlatpakAppsTest(unittest.TestCase):
    def test_apps_come_from_flatpak_exports(self):
        share = Path(tempfile.mkdtemp())
        (share / "applications").mkdir()
        icon_dir = share / "icons/hicolor/128x128/apps"
        icon_dir.mkdir(parents=True)
        (icon_dir / "org.videolan.VLC.png").write_bytes(b"not really a png")
        (share / "applications/org.videolan.VLC.desktop").write_text(
            "[Desktop Entry]\nName=VLC\nComment=Media player\nIcon=org.videolan.VLC\n"
            "Exec=flatpak run org.videolan.VLC\nX-Flatpak=org.videolan.VLC\n")
        (share / "applications/org.videolan.VLC.extra.desktop").write_text(
            "[Desktop Entry]\nName=VLC (second entry)\nX-Flatpak=org.videolan.VLC\n")
        (share / "applications/hidden.desktop").write_text(
            "[Desktop Entry]\nName=Hidden\nNoDisplay=true\nX-Flatpak=org.example.Hidden\n")
        (share / "applications/plain.desktop").write_text("[Desktop Entry]\nName=Not flatpak\n")
        found = apps.flatpak_apps([share])
        self.assertEqual([(a.id, a.name, a.package, a.kind) for a in found],
                         [("flatpak:org.videolan.VLC", "VLC", "org.videolan.VLC", "flatpak")])
        self.assertTrue(found[0].icon_path.endswith("128x128/apps/org.videolan.VLC.png"))
        self.assertEqual(found[0].workspace, "app-flatpak-org-videolan-vlc")
        self.assertRegex(found[0].color, r"^#[0-9a-f]{6}$")     # unreadable icon: fallback


class IconsTest(unittest.TestCase):
    PAGE = """<html><head>
      <link rel="shortcut icon" href="/favicon.ico">
      <link rel="icon" sizes="32x32" href="/i32.png">
      <link rel="apple-touch-icon" sizes="180x180" href="https://cdn.example/touch.png">
      <link rel="icon" type="image/svg+xml" href="logo.svg">
      <link rel="manifest" href="/app.webmanifest">
      <link rel="stylesheet" href="/style.css">
      <link rel="icon" href="data:image/png;base64,AAAA">
    </head></html>"""

    def setUp(self):
        from lumen import icons
        self.icons = icons
        self.dir = Path(tempfile.mkdtemp())

    def png(self, size, color=0x2060c0ff):
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        pixbuf = GdkPixbuf.Pixbuf.new(GdkPixbuf.Colorspace.RGB, True, 8, size, size)
        pixbuf.fill(color)
        return bytes(pixbuf.save_to_bufferv("png", [], [])[1])

    def test_candidates_are_ranked_and_resolved(self):
        found = self.icons.icon_candidates(self.PAGE, "https://example.org/tv/")
        self.assertEqual([c[0] for c in found], [
            "https://example.org/tv/logo.svg", "https://example.org/app.webmanifest",
            "https://cdn.example/touch.png", "https://example.org/favicon.ico",
            "https://example.org/i32.png", "https://example.org/apple-touch-icon.png"])
        self.assertTrue(found[1][2])                               # the manifest
        self.assertEqual(self.icons.icon_candidates("<<<not html", "https://x.org/")[-1][0],
                         "https://x.org/favicon.ico")

    def test_manifest_icons(self):
        manifest = json.dumps({"icons": [
            {"src": "a-192.png", "sizes": "192x192"},
            {"src": "/b-512.png", "sizes": "512x512", "purpose": "any maskable"},
            {"src": "mask.png", "sizes": "1024x1024", "purpose": "maskable"},
            {"nosrc": 1}, "junk"]})
        self.assertEqual(self.icons.manifest_icons(manifest, "https://x.org/m/app.json"),
                         [("https://x.org/b-512.png", 512), ("https://x.org/m/a-192.png", 192)])
        self.assertEqual(self.icons.manifest_icons("not json", "https://x.org/"), [])

    def test_fetch_skips_tiny_and_broken_icons(self):
        big, tiny = self.png(300), self.png(32)
        site = {
            "https://example.org/tv": self.PAGE.encode(),
            "https://example.org/": b"<html></html>",
            "https://example.org/logo.svg": b"this is not an image",
            "https://example.org/app.webmanifest": json.dumps(
                {"icons": [{"src": "/tiny.png", "sizes": "512x512"}]}).encode(),
            "https://example.org/tiny.png": tiny,           # lies about its size
            "https://cdn.example/touch.png": big,
        }
        asked = []

        def get(url):
            asked.append(url)
            if url not in site:
                raise OSError("404")
            return site[url], url

        path = self.icons.fetch_icon("example", "https://example.org/tv", self.dir, get=get)
        self.assertEqual(path, self.dir / "example.png")
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
        _, width, height = GdkPixbuf.Pixbuf.get_file_info(str(path))
        self.assertEqual((width, height), (self.icons.SAVE_SIZE, self.icons.SAVE_SIZE))
        self.assertNotIn("https://example.org/i32.png", asked)     # stopped once it had one
        self.assertEqual(self.icons.tile_color(path), self.icons.tile_color(path))
        self.assertRegex(self.icons.tile_color(path), r"^#[0-9a-f]{6}$")

    def test_unreachable_site_gives_no_icon_and_is_not_retried_at_once(self):
        def get(url):
            raise OSError("offline")

        self.assertIsNone(self.icons.fetch_icon("gone", "https://gone.invalid", self.dir, get=get))
        self.assertTrue(self.icons.missing("gone", self.dir))
        self.icons.mark_failed("gone", self.dir)
        self.assertFalse(self.icons.missing("gone", self.dir))
        later = __import__("time").time() + self.icons.RETRY_AFTER + 60
        self.assertTrue(self.icons.missing("gone", self.dir, now=later))
        with self.assertRaises(ValueError):
            self.icons._get("file:///etc/passwd")
        self.assertIsNone(self.icons.tile_color(self.dir / "nothing.png"))


class StoreTest(unittest.TestCase):
    def setUp(self):
        from lumen import store
        self.store = store

    def test_search_asks_for_this_devices_architecture(self):
        sent = []

        def request(url, body=None, timeout=20):
            sent.append((url, body))
            return json.dumps({"hits": [
                {"app_id": "org.videolan.VLC", "name": "VLC", "summary": "Media player",
                 "installs_last_month": 10},
                {"app_id": "bad id; rm -rf", "name": "Evil"},
                {"app_id": "org.noname.App"},
                {"name": "No id"}]}).encode()

        found = self.store.search("vlc", arch="aarch64", request=request)
        self.assertEqual(found, [{"id": "org.videolan.VLC", "name": "VLC",
                                  "summary": "Media player", "installs": 10}])
        self.assertEqual(sent[0][1]["filters"], [{"filterType": "arches", "value": "aarch64"}])
        self.assertEqual(sent[0][1]["query"], "vlc")
        with self.assertRaises(OSError):
            self.store.search("x", request=lambda *a, **k: b"<html>error</html>")

    def test_install_refuses_ids_that_are_not_app_ids(self):
        for bad in ("", "--system", "a b", "x;reboot", "../../etc", "-y"):
            self.assertEqual(self.store.install(bad), (False, "Not a valid app"), bad)
            self.assertEqual(self.store.uninstall(bad), (False, "Not a valid app"), bad)

    def test_android_store_download(self):
        folder = Path(tempfile.mkdtemp())
        asked = []

        def request(url, body=None, timeout=20):
            asked.append(url)
            return b'{"packageName":"org.fdroid.fdroid","suggestedVersionCode":2000053}'

        def fetch(content):
            def run(url, target, report=None):
                asked.append(url)
                Path(target).parent.mkdir(parents=True, exist_ok=True)
                Path(target).write_bytes(content)
                return Path(target)
            return run

        apk = fetch(b"PK\x03\x04 pretend apk")
        path = self.store.download_android_store("org.fdroid.fdroid", folder, request, apk)
        self.assertEqual(path, folder / "org.fdroid.fdroid.apk")
        self.assertEqual(asked[1], "https://f-droid.org/repo/org.fdroid.fdroid_2000053.apk")
        with self.assertRaises(OSError):
            self.store.download_android_store("com.evil.app", folder, request, apk)
        with self.assertRaises(OSError):                 # an error page instead of an APK
            self.store.download_android_store("com.aurora.store", folder, request,
                                              fetch(b"<html>Not found</html>"))
        self.assertFalse((folder / "com.aurora.store.apk").exists())

    def test_download_reports_progress_and_leaves_nothing_behind_on_failure(self):
        import http.server
        import threading
        payload = b"x" * (700 * 1024)

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                if self.path == "/file":
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                else:
                    self.send_error(404)

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_address[1]}"
        folder = Path(tempfile.mkdtemp())
        seen = []
        path = self.store.download(base + "/file", folder / "a.bin",
                                   lambda fraction, text=None: seen.append(fraction))
        self.assertEqual(path.read_bytes(), payload)
        self.assertEqual(seen[-1], 1.0)
        self.assertGreaterEqual(len(seen), 2)
        self.assertEqual(seen, sorted(seen))
        with self.assertRaises(OSError):
            self.store.download(base + "/missing", folder / "b.bin")
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["a.bin"])

    def test_flatpak_progress_is_followed_across_output_chunks(self):
        progress = self.store.flatpak_progress
        state, fraction = progress("Looking for matches…\n")
        self.assertIsNone(fraction)
        # What flatpak really prints: redraws with escape codes, split anywhere.
        state, fraction = progress(
            "Installing 1/4…\x1b[K\r  0%  0 bytes/s\x1b]9;4;1;0\x1b\\\r 37%\x1b]9;4;1;37\x1b\\", state)
        self.assertAlmostEqual(fraction, 0.37 / 4)
        state, fraction = progress("\r100%  913.7 kB/s", state)
        self.assertAlmostEqual(fraction, 0.25)
        state, fraction = progress("\nInstalling 2/4…", state)
        self.assertAlmostEqual(fraction, 0.25)             # new part starts at its 0%
        state, fraction = progress("\r 50%\nInstalling 4/4…\r100%", state)
        self.assertAlmostEqual(fraction, 1.0)
        self.assertIsNone(progress("999%")[1])              # a percentage with no part
        # A single download is not numbered.
        state, fraction = progress("Installing… | |   0%  0 bytes/s\x1b]9;4;1;0\x1b\\\rInstalling… |▍|  42%")
        self.assertAlmostEqual(fraction, 0.42)
        self.assertEqual(self.store.ESCAPES.sub("", "a\x1b]9;4;1;37\x1b\\b\x1b[Kc"), "abc")


class ConfigTest(unittest.TestCase):
    def test_saved_values_override_defaults_and_missing_ones_are_filled(self):
        path = Path(tempfile.mkdtemp()) / "sub" / "config.json"
        cfg = config.load(path)
        self.assertEqual(cfg["display_mode"], "auto")
        cfg["display_mode"] = "native"
        cfg["favorites"].append("plex")
        config.save(cfg, path)
        again = config.load(path)
        self.assertEqual(again["display_mode"], "native")
        self.assertIn("plex", again["favorites"])
        self.assertNotIn("plex", config.DEFAULTS["favorites"])   # defaults not mutated
        # Settings saved by versions before 0.4 keep their meaning.
        path.write_text('{"prefer_1080p": false}')
        self.assertEqual(config.load(path)["display_mode"], "native")
        path.write_text('{"prefer_1080p": true}')
        old = config.load(path)
        self.assertEqual(old["display_mode"], "auto")
        self.assertNotIn("prefer_1080p", old)
        path.write_text("{ broken")
        self.assertEqual(config.load(path)["favorites"], config.DEFAULTS["favorites"])


class SettingsParsingTest(unittest.TestCase):
    def setUp(self):
        try:
            from lumen import settings
        except (ImportError, ValueError):
            self.skipTest("GTK 4 not available")
        self.settings = settings

    def test_wifi_list(self):
        nets = self.settings.parse_wifi(
            " :Home\\:5G:48:WPA2\n"
            "*:Home:80:WPA2 WPA3\n"
            " :Home:35:WPA2\n"
            " :Cafe:91:\n"
            " ::70:WPA2\n")
        self.assertEqual([n["ssid"] for n in nets], ["Home", "Cafe", "Home:5G"])
        self.assertTrue(nets[0]["active"])
        self.assertEqual(nets[0]["signal"], 80)
        self.assertFalse(nets[1]["secure"])
        self.assertTrue(nets[2]["secure"])


class RemoteAuthTest(unittest.TestCase):
    def setUp(self):
        from lumen import remote
        self.remote = remote
        self.path = Path(tempfile.mkdtemp()) / "remote.json"
        self.auth = remote.Auth(self.path)

    def test_pin_pairs_and_tokens_are_stored_hashed(self):
        self.assertRegex(self.auth.pin, r"^\d{6}$")
        token, error = self.auth.pair(self.auth.pin)
        self.assertTrue(token and not error)
        self.assertTrue(self.auth.valid(token))
        self.assertFalse(self.auth.valid(token + "x"))
        self.assertFalse(self.auth.valid(""))
        self.assertFalse(self.auth.valid(None))
        self.assertNotIn(token, self.path.read_text())
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_wrong_pins_lock_pairing_for_a_while(self):
        wrong = "000000" if self.auth.pin != "000000" else "111111"
        for _ in range(self.auth.MAX_FAILS):
            self.assertEqual(self.auth.pair(wrong, now=100.0)[0], None)
        token, error = self.auth.pair(self.auth.pin, now=101.0)     # correct, but locked
        self.assertIsNone(token)
        self.assertIn("Too many", error)
        token, _ = self.auth.pair(self.auth.pin, now=100.0 + self.auth.LOCK_SECONDS + 1)
        self.assertTrue(token)

    def test_new_pin_from_the_tv_signs_everyone_out(self):
        token, _ = self.auth.pair(self.auth.pin)
        launcher_side = self.remote.Auth(self.path)       # the launcher's own instance
        self.assertEqual(launcher_side.pin, self.auth.pin)
        old = self.auth.pin
        while launcher_side.new_pin() == old:
            pass
        self.assertFalse(self.auth.valid(token))
        self.assertIsNone(self.auth.pair(old)[0])
        self.assertTrue(self.auth.pair(launcher_side.pin)[0])


class RemoteApiTest(unittest.TestCase):
    """Talks to the real HTTP server on localhost; the TV side is faked."""

    def setUp(self):
        import http.client
        from lumen import remote
        self.remote, self.http = remote, http.client
        tmp = Path(tempfile.mkdtemp())
        self.config_path = tmp / "config.json"
        self.sent = []
        self.auth = remote.Auth(tmp / "remote.json")
        self.device = remote.Device(sway=sway.Sway(path=""), send=lambda c: self.sent.append(c) or True,
                                    config_path=self.config_path, downloads=tmp / "dl")
        self.server = remote.serve(0, self.auth, self.device)
        self.port = self.server.server_address[1]
        self.cookie = None
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def call(self, method, path, body=None, headers=None, cookie=True):
        conn = self.http.HTTPConnection("127.0.0.1", self.port, timeout=5)
        send = {"X-Lumen": "1"} if headers is None else dict(headers)
        if cookie and self.cookie:
            send["Cookie"] = self.cookie
        data = json.dumps(body).encode() if isinstance(body, dict) else body
        conn.request(method, path, body=data, headers=send)
        response = conn.getresponse()
        raw = response.read()
        conn.close()
        return response, (json.loads(raw) if response.getheader("Content-Type") == "application/json" else raw)

    def pair(self):
        response, _ = self.call("POST", "/api/pair", {"pin": self.auth.pin})
        self.assertEqual(response.status, 200)
        self.cookie = response.getheader("Set-Cookie").split(";")[0]
        self.assertIn("HttpOnly", response.getheader("Set-Cookie"))
        self.assertIn("SameSite=Strict", response.getheader("Set-Cookie"))

    def test_page_is_public_but_everything_else_needs_pairing(self):
        response, page = self.call("GET", "/")
        self.assertEqual(response.status, 200)
        self.assertIn(b"Lumen TV", page)
        self.assertNotIn(self.auth.pin.encode(), page)
        for method, path in (("GET", "/api/state"), ("GET", "/api/log"),
                             ("POST", "/api/key"), ("POST", "/api/power"),
                             ("POST", "/api/add-webapp"), ("POST", "/api/apk?name=a.apk")):
            response, _ = self.call(method, path, {"key": "ok", "action": "restart"})
            self.assertEqual(response.status, 401, path)
        self.assertEqual(self.sent, [])
        response, body = self.call("POST", "/api/pair", {"pin": "nope"})
        self.assertEqual((response.status, body["error"]), (403, "Wrong PIN"))
        response, _ = self.call("POST", "/api/key", {"key": "ok"},
                                headers={"X-Lumen": "1", "Cookie": "lumen_session=forged"})
        self.assertEqual(response.status, 401)

    def test_cross_site_requests_are_refused_even_when_paired(self):
        self.pair()
        response, _ = self.call("POST", "/api/key", {"key": "ok"}, headers={})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.sent, [])

    def test_remote_keys_and_text(self):
        self.pair()
        for key in ("up", "ok", "back", "home", "keyboard"):
            self.assertEqual(self.call("POST", "/api/key", {"key": key})[0].status, 200)
        self.assertEqual(self.sent, ["key KEY_UP", "key KEY_ENTER", "key KEY_ESC", "home",
                                     "osk toggle"])
        self.assertEqual(self.call("POST", "/api/key", {"key": "KEY_POWER"})[0].status, 400)
        self.call("POST", "/api/text", {"text": "hello\nworld"})
        self.assertEqual(self.sent[-1], "text hello world")       # one line, no injection
        self.assertEqual(self.call("POST", "/api/nothing", {})[0].status, 404)
        self.assertEqual(self.call("POST", "/api/key", b"not json")[0].status, 400)

    def test_tv_not_responding_is_an_error(self):
        self.pair()
        self.device.send = lambda _c: False
        response, body = self.call("POST", "/api/key", {"key": "ok"})
        self.assertEqual(response.status, 503)
        self.assertIn("did not respond", body["error"])

    def test_state_and_web_app_management(self):
        self.pair()
        response, state = self.call("GET", "/api/state")
        self.assertEqual(response.status, 200)
        ids = [a["id"] for a in state["apps"]]
        self.assertIn("youtube", ids)
        self.assertIn("browser", ids)
        self.assertNotIn("settings", ids)                 # TV-only screens are not offered
        self.assertEqual(state["showing"], "Home")
        self.assertGreater(state["stats"]["memory_total_mb"], 0)

        self.assertEqual(self.call("POST", "/api/add-webapp",
                                   {"name": "Jellyfin", "url": "nas.local:8096"})[0].status, 200)
        self.assertEqual(self.call("POST", "/api/add-webapp",
                                   {"name": "Jellyfin", "url": "other.local"})[0].status, 200)
        saved = config.load(self.config_path)
        self.assertEqual([(w["id"], w["url"]) for w in saved["web_apps"][-2:]],
                         [("jellyfin", "https://nas.local:8096"), ("jellyfin-2", "https://other.local")])
        self.assertIn("jellyfin", saved["favorites"])
        for bad in ({"name": "", "url": "x.org"}, {"name": "X", "url": "javascript:alert(1)"},
                    {"name": "X", "url": ""}):
            self.assertEqual(self.call("POST", "/api/add-webapp", bad)[0].status, 400, bad)

        self.assertEqual(self.call("POST", "/api/remove-webapp", {"id": "jellyfin"})[0].status, 200)
        saved = config.load(self.config_path)
        self.assertNotIn("jellyfin", [w["id"] for w in saved["web_apps"]])
        self.assertNotIn("jellyfin", saved["favorites"])
        self.assertEqual(self.call("POST", "/api/remove-webapp", {"id": "jellyfin"})[0].status, 404)

    def test_links_and_uploads_are_validated(self):
        self.pair()
        for bad in ("", "file:///etc/passwd", "javascript:alert(1)"):
            self.assertEqual(self.call("POST", "/api/open-url", {"url": bad})[0].status, 400, bad)
        self.assertEqual(self.call("POST", "/api/power", {"action": "format"})[0].status, 400)
        # No Android on the test machine: an upload must be refused, and nothing written.
        response, body = self.call("POST", "/api/apk?name=../../evil.apk", b"x" * 10)
        self.assertEqual(response.status, 400)
        self.assertFalse((self.device.downloads).exists())


class UpdateTest(unittest.TestCase):
    """Runs the real lumen-update script against a local git repository."""

    SCRIPT = Path(__file__).parent.parent / "bin" / "lumen-update"

    def setUp(self):
        import shutil
        import subprocess
        if not shutil.which("git"):
            self.skipTest("git not installed")
        if os.geteuid() == 0:
            self.skipTest("the test overrides are ignored for root")
        self.run_cmd = subprocess.run
        self.tmp = Path(tempfile.mkdtemp())
        self.upstream = self.tmp / "upstream"
        self.upstream.mkdir()
        self.git("init", "-q", "-b", "main")
        # Stand-in installer: records that it ran, and which version it installed.
        (self.upstream / "install.sh").write_text(
            'mkdir -p "$LUMEN_HOME"\n'
            'git -C "$(dirname "$0")" rev-parse HEAD > "$LUMEN_HOME/COMMIT"\n'
            'echo "update=$LUMEN_UPDATE" > "$LUMEN_HOME/ran"\n')
        self.first = self.commit("one")
        conf = self.tmp / "update.conf"
        conf.write_text(f"# comment\nREPO={self.upstream}\nBRANCH=main\n")
        self.env = {**os.environ, "LUMEN_UPDATE_CONF": str(conf),
                    "LUMEN_HOME": str(self.tmp / "installed"),
                    "LUMEN_UPDATE_SRC": str(self.tmp / "src")}

    def git(self, *args):
        return self.run_cmd(["git", "-C", str(self.upstream), "-c", "user.name=t",
                             "-c", "user.email=t@example.invalid", *args],
                            check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, text):
        (self.upstream / "file").write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", text)
        return self.git("rev-parse", "HEAD")

    def update(self, action, **env):
        return self.run_cmd(["bash", str(self.SCRIPT), action], capture_output=True,
                            text=True, env={**self.env, **env})

    def check(self):
        from lumen.settings import parse_update_check
        result = self.update("check")
        self.assertEqual(result.returncode, 0, result.stderr)
        return parse_update_check(result.stdout)

    def test_check_apply_and_check_again(self):
        from lumen.settings import update_available
        self.assertEqual(self.check(), ("unknown", self.first))
        self.assertTrue(update_available("unknown", self.first))

        result = self.update("apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.tmp / "installed/ran").read_text().strip(), "update=1")
        self.assertEqual(self.check(), (self.first, self.first))
        self.assertFalse(update_available(self.first, self.first))

        second = self.commit("two")                       # a new release appears
        self.assertEqual(self.check(), (self.first, second))
        self.assertEqual(self.update("apply").returncode, 0)
        self.assertEqual(self.check(), (second, second))
        self.assertFalse((self.tmp / "src.new").exists())

    def test_unreachable_server_is_reported_not_installed(self):
        conf = self.tmp / "bad.conf"
        conf.write_text(f"REPO={self.tmp / 'nowhere'}\n")
        for action in ("check", "apply"):
            result = self.update(action, LUMEN_UPDATE_CONF=str(conf))
            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(result.stderr.strip())
        self.assertFalse((self.tmp / "installed").exists())

    def test_download_without_installer_is_rejected(self):
        self.git("rm", "-q", "install.sh")
        self.git("commit", "-q", "-m", "broken")
        result = self.update("apply")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("does not look like Lumen TV", result.stderr)
        self.assertFalse((self.tmp / "installed").exists())

    def test_missing_configuration(self):
        result = self.update("check", LUMEN_UPDATE_CONF=str(self.tmp / "none.conf"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("No update source configured", result.stderr)

    def test_shipped_configuration_points_at_a_repository(self):
        text = (Path(__file__).parent.parent / "config/update.conf").read_text()
        self.assertRegex(text, r"(?m)^REPO=https://github\.com/[\w-]+/[\w.-]+\.git$")
        self.assertRegex(text, r"(?m)^BRANCH=main$")


if __name__ == "__main__":
    unittest.main()
