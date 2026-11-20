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

    def test_4k_tv_is_driven_at_1080p(self):
        out = {"name": "HDMI-A-1", "active": True,
               "current_mode": {"width": 3840, "height": 2160, "refresh": 60000},
               "modes": [{"width": 3840, "height": 2160, "refresh": 60000},
                         {"width": 1920, "height": 1080, "refresh": 120000},
                         {"width": 1920, "height": 1080, "refresh": 60000},
                         {"width": 1920, "height": 1080, "refresh": 50000}]}
        self.assertEqual(sway.output_command(out),
                         "output HDMI-A-1 mode 1920x1080@60.000Hz scale 1")
        self.assertEqual(sway.output_command(out, prefer_1080p=False),
                         "output HDMI-A-1 scale 2")

    def test_720p_tv_is_scaled_down(self):
        out = {"name": "HDMI-A-1", "active": True, "modes": [],
               "current_mode": {"width": 1280, "height": 720, "refresh": 60000}}
        self.assertEqual(sway.output_command(out), "output HDMI-A-1 scale 0.667")
        self.assertIsNone(sway.output_command({"name": "x", "active": False}))


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


class ConfigTest(unittest.TestCase):
    def test_saved_values_override_defaults_and_missing_ones_are_filled(self):
        path = Path(tempfile.mkdtemp()) / "sub" / "config.json"
        cfg = config.load(path)
        self.assertTrue(cfg["prefer_1080p"])
        cfg["prefer_1080p"] = False
        cfg["favorites"].append("plex")
        config.save(cfg, path)
        again = config.load(path)
        self.assertFalse(again["prefer_1080p"])
        self.assertIn("plex", again["favorites"])
        self.assertNotIn("plex", config.DEFAULTS["favorites"])   # defaults not mutated
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
