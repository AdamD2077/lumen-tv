"""Settings, the file browser and per-app menus, all built from list pages."""

import os
import re
import subprocess
from pathlib import Path

from . import __version__, config
from . import apps as catalog
from .widgets import Item, ListPage, PromptPage


def run(*cmd, timeout=30):
    """Run a command, returning (ok, stdout). Missing tools just count as failure."""
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, p.stdout
    except (OSError, subprocess.SubprocessError):
        return False, ""


def split_terse(line):
    """Split one line of `nmcli -t` output, where literal colons are escaped."""
    return [part.replace("\\:", ":").replace("\\\\", "\\")
            for part in re.split(r"(?<!\\):", line)]


# --- root ---------------------------------------------------------------

def root_items(shell):
    def page(title, builder, glyph, subtitle=""):
        return lambda: shell.push(ListPage(shell, title, builder, subtitle, glyph))

    def build(_page):
        return [
            Item("Network", "Wi-Fi and connection status", "wifi",
                 action=page("Network", lambda p: wifi_items(shell, p), "wifi")),
            Item("Controllers & Bluetooth", "Pair a gamepad, remote or speaker", "bluetooth",
                 action=page("Bluetooth", lambda p: bluetooth_items(shell, p), "bluetooth",
                             "Put the controller in pairing mode, then scan.")),
            Item("Sound", "Volume", "volume",
                 action=page("Sound", lambda p: sound_items(shell, p), "volume")),
            Item("Display", "Resolution", "display",
                 action=page("Display", lambda p: display_items(shell, p), "display")),
            Item("Android", "Apps, APK install, memory", "android",
                 action=lambda: shell.push(android_page(shell))),
            Item("Web apps", "Add sites to the home screen", "globe",
                 action=page("Web apps", lambda p: webapp_items(shell, p), "globe")),
            Item("Remote access", "Control and manage this TV from a phone or computer",
                 "display", "On" if shell.cfg["remote_access"] else "Off",
                 action=page("Remote access", lambda p: remote_items(shell, p), "display",
                             "Open the address in a web browser on the same network, "
                             "then enter the PIN.")),
            Item("System update", "Get the latest version of Lumen TV", "refresh",
                 action=page("System update", lambda p: update_items(shell, p), "refresh",
                             "Updates are downloaded from the Lumen TV project page.")),
            Item("About", f"Lumen TV {__version__}", "info",
                 action=page("About", lambda p: about_items(), "info")),
            Item("Power", "Restart or shut down", "power",
                 action=page("Power", lambda p: power_items(shell), "power")),
        ]
    return build


# --- network ------------------------------------------------------------

def network_summary():
    ok, out = run("nmcli", "-t", "-f", "TYPE,STATE,CONNECTION", "device", timeout=5)
    if not ok:
        return ""
    for line in out.splitlines():
        kind, state, name = (split_terse(line) + ["", "", ""])[:3]
        if state == "connected" and kind in ("wifi", "ethernet"):
            return "Wired" if kind == "ethernet" else name
    return "Offline"


def parse_wifi(out):
    """nmcli wifi list -> [{ssid, signal, secure, active}], strongest first, no duplicates."""
    best = {}
    for line in out.splitlines():
        parts = split_terse(line)
        if len(parts) < 4 or not parts[1]:
            continue
        net = {"active": parts[0] == "*", "ssid": parts[1],
               "signal": int(parts[2] or 0), "secure": parts[3] not in ("", "--")}
        old = best.get(net["ssid"])
        if not old or net["active"] or (net["signal"] > old["signal"] and not old["active"]):
            best[net["ssid"]] = net
    return sorted(best.values(), key=lambda n: (not n["active"], -n["signal"]))


def wifi_items(shell, page):
    def load():
        _, out = run("nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list")
        _, saved = run("nmcli", "-t", "-f", "NAME", "connection", "show")
        return parse_wifi(out), {split_terse(s)[0] for s in saved.splitlines()}

    def connect(net, password=None):
        cmd = ["nmcli", "device", "wifi", "connect", net["ssid"]]
        if password:
            cmd += ["password", password]

        def done(result):
            shell.toast(f"Connected to {net['ssid']}" if result[0]
                        else f"Could not connect to {net['ssid']}")
            shell.update_network()
            page.refresh()

        shell.busy(f"Connecting to {net['ssid']}…", lambda: run(*cmd, timeout=45), done)

    def choose(net, saved):
        if net["active"]:
            return
        if net["secure"] and net["ssid"] not in saved:
            shell.push(PromptPage(shell, net["ssid"], lambda pw: connect(net, pw),
                                  subtitle="Enter the Wi-Fi password", glyph="wifi"))
        else:
            connect(net)

    def show(result):
        if isinstance(result, Exception):
            return page.set_items([Item("Could not read Wi-Fi networks")])
        nets, saved = result
        items = [Item("Scan again", icon="refresh", action=page.refresh)]
        for net in nets:
            detail = ("Secured" if net["secure"] else "Open") + f" · signal {net['signal']}%"
            items.append(Item(net["ssid"], detail, "wifi",
                              "Connected" if net["active"] else "",
                              action=lambda n=net: choose(n, saved)))
        if not nets:
            items.append(Item("No Wi-Fi networks found"))
        page.set_items(items)

    page.loading("Looking for networks…")
    from .widgets import run_async
    run_async(load, show)


# --- bluetooth ----------------------------------------------------------

def bluetooth_items(shell, page):
    def load():
        _, out = run("bluetoothctl", "devices")
        devices = []
        for line in out.splitlines():
            m = re.match(r"Device ([0-9A-F:]{17}) (.+)", line)
            if not m or m.group(2).replace("-", ":") == m.group(1):
                continue    # unnamed devices only show their address; skip the noise
            _, info = run("bluetoothctl", "info", m.group(1))
            devices.append({"mac": m.group(1), "name": m.group(2),
                            "paired": "Paired: yes" in info,
                            "connected": "Connected: yes" in info})
        return sorted(devices, key=lambda d: (not d["connected"], not d["paired"], d["name"]))

    def scan():
        run("bluetoothctl", "power", "on")
        shell.busy("Scanning for devices…",
                   lambda: run("bluetoothctl", "--timeout", "10", "scan", "on", timeout=20),
                   lambda _r: page.refresh())

    def toggle(dev):
        mac = dev["mac"]

        def work():
            if dev["connected"]:
                return run("bluetoothctl", "disconnect", mac)[0]
            if not dev["paired"]:
                run("bluetoothctl", "pair", mac, timeout=40)
            run("bluetoothctl", "trust", mac)
            return run("bluetoothctl", "connect", mac, timeout=40)[0]

        def done(ok):
            verb = "Disconnected" if dev["connected"] else "Connected"
            shell.toast(f"{verb}: {dev['name']}" if ok is True
                        else f"Could not connect to {dev['name']}")
            page.refresh()

        shell.busy(f"{'Disconnecting' if dev['connected'] else 'Connecting'} {dev['name']}…",
                   work, done)

    def show(devices):
        items = [Item("Scan for devices", "Takes about ten seconds", "refresh", action=scan)]
        if isinstance(devices, list):
            for dev in devices:
                state = "Connected" if dev["connected"] else "Paired" if dev["paired"] else ""
                items.append(Item(dev["name"], dev["mac"], "bluetooth", state,
                                  action=lambda d=dev: toggle(d)))
        page.set_items(items)

    page.loading()
    from .widgets import run_async
    run_async(load, show)


# --- sound / display / power / about -------------------------------------

def sound_items(shell, page):
    sink = "@DEFAULT_AUDIO_SINK@"
    ok, out = run("wpctl", "get-volume", sink, timeout=5)
    m = re.search(r"([\d.]+)", out)
    level = f"{round(float(m.group(1)) * 100)}%" if ok and m else "unknown"
    if "MUTED" in out:
        level += " (muted)"

    def do(*args):
        run("wpctl", *args, timeout=5)
        page.refresh()

    return [
        Item("Volume up", icon="volume", value=level,
             action=lambda: do("set-volume", "-l", "1.0", sink, "5%+")),
        Item("Volume down", icon="volume",
             action=lambda: do("set-volume", sink, "5%-")),
        Item("Mute", icon="volume", action=lambda: do("set-mute", sink, "toggle")),
    ]


def display_items(shell, page):
    def toggle():
        shell.cfg["prefer_1080p"] = not shell.cfg["prefer_1080p"]
        config.save(shell.cfg)
        shell.sway.setup_outputs(shell.cfg["prefer_1080p"])
        page.refresh()

    items = [Item("Limit to 1080p", "Smoother on a Raspberry Pi than rendering in 4K",
                  "display", "On" if shell.cfg["prefer_1080p"] else "Off", action=toggle)]
    for out in shell.sway.outputs():
        mode = out.get("current_mode") or {}
        if out.get("active") and mode:
            items.append(Item(out["name"], f"{out.get('make', '')} {out.get('model', '')}".strip(),
                              value=f"{mode['width']}×{mode['height']} "
                                    f"@ {round(mode['refresh'] / 1000)} Hz"))
    return items


def power_items(shell):
    return [
        Item("Restart", icon="refresh", action=lambda: run("systemctl", "reboot")),
        Item("Shut down", icon="power", action=lambda: run("systemctl", "poweroff")),
        Item("Exit to console", "Leave Lumen TV; log in again to come back", "close",
             action=lambda: shell.sway.command("exit")),
    ]


def about_items():
    def read(path):
        try:
            return Path(path).read_text(errors="replace").strip("\x00\n ")
        except OSError:
            return ""

    mem = re.search(r"MemTotal:\s+(\d+)", read("/proc/meminfo"))
    _, ips = run("hostname", "-I", timeout=5)
    os_name = re.search(r'PRETTY_NAME="(.*)"', read("/etc/os-release"))
    return [
        Item("Lumen TV", value=__version__),
        Item("Device", value=read("/proc/device-tree/model") or "Unknown"),
        Item("Memory", value=f"{int(mem.group(1)) / 1048576:.1f} GB" if mem else "Unknown"),
        Item("System", value=os_name.group(1) if os_name else "Unknown"),
        Item("IP address", value=ips.split()[0] if ips.split() else "Not connected"),
    ]


# --- remote access ------------------------------------------------------

def remote_items(shell, page):
    from . import remote
    enabled = shell.cfg["remote_access"]

    def toggle():
        shell.cfg["remote_access"] = not enabled
        config.save(shell.cfg)
        page.refresh()

    items = [Item("Remote access", "Anyone on your network who knows the PIN can control "
                  "this TV", "display", "On" if enabled else "Off", action=toggle)]
    if enabled:
        auth = remote.Auth()
        _, ips = run("hostname", "-I", timeout=5)
        port = shell.cfg.get("remote_port") or remote.DEFAULT_PORT
        address = f"http://{ips.split()[0]}:{port}" if ips.split() else "Not connected"

        def new_pin():
            auth.new_pin()
            shell.toast("New PIN set. Paired devices must pair again.")
            page.refresh()

        items += [
            Item("Address", "Type this into a browser on your phone or computer",
                 "globe", address),
            Item("PIN", icon="check", value=" ".join(auth.pin)),
            Item("Change PIN", "Also signs out every paired device", "refresh",
                 action=new_pin),
        ]
    return items


# --- system update ------------------------------------------------------

def parse_update_check(output):
    """`lumen-update check` output -> (installed, latest) commit ids."""
    values = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    return values.get("installed", "unknown"), values.get("latest", "")


def update_status():
    """Ask the update server. Returns (installed, latest, error message)."""
    try:
        p = subprocess.run([str(config.ROOT / "bin/lumen-update"), "check"],
                           capture_output=True, text=True, timeout=40)
    except (OSError, subprocess.SubprocessError):
        return "unknown", "", "Could not check for updates"
    installed, latest = parse_update_check(p.stdout)
    error = "" if p.returncode == 0 and latest else (
        p.stderr.strip().splitlines() or ["Could not check for updates"])[-1]
    return installed, latest, error


def update_available(installed, latest):
    return bool(latest) and installed != latest


def update_items(shell, page):
    def apply():
        def work():
            try:
                return subprocess.run(
                    ["sudo", "-n", str(config.ROOT / "bin/lumen-update"), "apply"],
                    capture_output=True, text=True, timeout=1800)
            except (OSError, subprocess.SubprocessError) as e:
                return e

        def done(result):
            if getattr(result, "returncode", 1) == 0:
                shell.toast("Update installed. Restarting Lumen TV…")
                shell.restart_soon()
            else:
                lines = (getattr(result, "stderr", "") or "").strip().splitlines()
                shell.toast(lines[-1] if lines else "The update could not be installed")
                page.refresh()

        shell.busy("Updating Lumen TV…", work, done,
                   hint="This can take a few minutes. Please keep the device powered on.")

    def show(status):
        if isinstance(status, Exception):
            status = ("unknown", "", "Could not check for updates")
        installed, latest, error = status
        current = Item("Installed version", value=f"{__version__} ({installed[:7]})")
        check = Item("Check again", icon="refresh", action=page.refresh)
        if error:
            items = [Item(error, "Check the network connection and try again", "close"),
                     check, current]
        elif update_available(installed, latest):
            items = [Item("Update now", f"A new version is available ({latest[:7]})",
                          "check", action=apply), check, current]
        else:
            items = [Item("Lumen TV is up to date", icon="check"), check, current]
        shell.set_update_ready(not error and update_available(installed, latest))
        page.set_items(items)

    page.loading("Checking for updates…")
    from .widgets import run_async
    run_async(update_status, show)


# --- web apps -----------------------------------------------------------

def webapp_items(shell, page):
    def add():
        def with_name(name):
            def with_url(url):
                catalog.add_web_app(shell.cfg, name, url)
                shell.save()
                shell.toast(f"{name} added to the home screen")
            shell.push(PromptPage(shell, "Address", with_url, glyph="globe",
                                  subtitle=f"The web address of {name}",
                                  initial="https://"))
        shell.push(PromptPage(shell, "Name", with_name, glyph="globe",
                              subtitle="What should the app be called?"))

    def restore():
        have = {w["id"] for w in shell.cfg["web_apps"]}
        shell.cfg["web_apps"] += [w for w in config.DEFAULT_WEB_APPS if w["id"] not in have]
        shell.cfg["hidden"] = []
        shell.save()
        shell.toast("Default apps restored")

    return [
        Item("Add a web app", "Any site becomes a full-screen app", "plus", action=add),
        Item("Restore default and hidden apps", icon="refresh", action=restore),
    ]


# --- android ------------------------------------------------------------

def find_files(roots, extensions, limit=200):
    found = []
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            if dirpath[len(str(root)):].count(os.sep) >= 3:
                dirnames[:] = []
            found += [Path(dirpath) / f for f in filenames
                      if Path(f).suffix.lower() in extensions]
            if len(found) >= limit:
                return sorted(found)[:limit]
    return sorted(found)


def storage_roots():
    user = os.environ.get("USER", "")
    roots = [Path.home()]
    for base in (Path("/media") / user, Path("/run/media") / user):
        if base.is_dir():
            roots += sorted(p for p in base.iterdir() if p.is_dir())
    return roots


def android_page(shell):
    return ListPage(shell, "Android", lambda p: android_items(shell, p), glyph="android",
                    subtitle="Android apps run in a container (Waydroid) next to Lumen TV.")


def android_items(shell, page):
    state = catalog.android_state()
    if state == "missing":
        return [Item("Android support is not installed",
                     "Run the Lumen TV installer without --no-android")]

    def helper(text, *args, hint="", then=None):
        def done(result):
            ok = getattr(result, "returncode", 1) == 0
            lines = (getattr(result, "stderr", "") or "").strip().splitlines()
            shell.toast("Done" if ok else (lines[-1] if lines else "That did not work"))
            page.refresh()
            if ok and then:
                then()
        shell.busy(text, lambda: catalog.android_helper(*args), done, hint=hint)

    if state == "uninitialized":
        hint = "Downloads about 1 GB. Please keep the device powered on."
        return [
            Item("Set up Android", "Download the Android system image", "android",
                 action=lambda: helper("Downloading Android…", "setup", hint=hint)),
            Item("Set up Android with Google apps", "Includes the Play Store; "
                 "the device must be registered with Google before sign-in works", "android",
                 action=lambda: helper("Downloading Android…", "setup", "--gapps", hint=hint)),
        ]

    def install_apk():
        def build(_p):
            apks = find_files(storage_roots(), {".apk"})
            if not apks:
                return [Item("No APK files found",
                             "Copy .apk files to a USB drive or the home folder")]
            return [Item(a.name, str(a.parent), "file",
                         action=lambda a=a: helper(f"Installing {a.name}…", "install", str(a),
                                                   then=shell.reload_apps))
                    for a in apks]
        shell.push(ListPage(shell, "Install APK", build, glyph="android",
                            subtitle="APK files on this device and on USB drives."))

    def toggle_autostart():
        shell.cfg["android_autostart"] = not shell.cfg["android_autostart"]
        config.save(shell.cfg)
        page.refresh()

    return [
        Item("Open Android home screen", icon="android",
             action=lambda: shell.open_android()),
        Item("Install an APK", "From a USB drive or the home folder", "plus",
             action=install_apk),
        Item("Start Android at boot", "Apps open instantly, at the cost of memory",
             "refresh", "On" if shell.cfg["android_autostart"] else "Off",
             action=toggle_autostart),
        Item("Stop Android", "Frees memory until the next Android app is opened", "power",
             action=lambda: helper("Stopping Android…", "stop")),
    ]


# --- files --------------------------------------------------------------

def files_page(shell, folder=None):
    def open_file(path):
        if path.suffix.lower() == ".apk":
            return shell.push(android_page(shell))
        shell.report(shell.launcher.open_file(path))

    def build(_page):
        if folder is None:
            roots = storage_roots()
            return [Item("Home" if r == Path.home() else r.name, str(r),
                         "folder" if r == Path.home() else "usb",
                         action=lambda r=r: shell.push(files_page(shell, r)))
                    for r in roots]
        try:
            entries = sorted((e for e in folder.iterdir() if not e.name.startswith(".")),
                             key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return [Item("This folder cannot be opened")]
        items = []
        for entry in entries:
            ext = entry.suffix.lower()
            if entry.is_dir():
                items.append(Item(entry.name, icon="folder",
                                  action=lambda e=entry: shell.push(files_page(shell, e))))
            elif ext in catalog.IMAGE_EXT:
                items.append(Item(entry.name, icon="image", action=lambda e=entry: open_file(e)))
            elif ext in catalog.MEDIA_EXT:
                items.append(Item(entry.name, icon="film", action=lambda e=entry: open_file(e)))
            elif ext == ".apk":
                items.append(Item(entry.name, "Android app package", "android",
                                  action=lambda e=entry: open_file(e)))
        return items or [Item("Nothing to play here")]

    title = "Files" if folder is None else folder.name or str(folder)
    subtitle = ("Photos, videos and music. USB drives appear here when plugged in."
                if folder is None else str(folder))
    return ListPage(shell, title, build, subtitle, "folder")


# --- per-app menu -------------------------------------------------------

def app_menu_items(shell, app):
    cfg = shell.cfg
    favorite = app.id in cfg["favorites"]

    def toggle_favorite():
        (cfg["favorites"].remove if favorite else cfg["favorites"].append)(app.id)
        shell.pop()
        shell.save()

    def close():
        shell.launcher.close(app)
        shell.pop()

    def hide():
        cfg["hidden"].append(app.id)
        if favorite:
            cfg["favorites"].remove(app.id)
        shell.pop()
        shell.save()

    def remove():
        catalog.remove_web_app(cfg, app.id)
        shell.pop()
        shell.save()

    def open_it():
        shell.pop()
        shell.open_app(app)

    items = [
        Item("Open", icon="check", action=open_it),
        Item("Remove from favorites" if favorite else "Add to favorites", icon="star",
             action=toggle_favorite),
    ]
    if shell.launcher.is_running(app) and app.kind != "android":
        items.append(Item("Close app", "It is still running in the background", "close",
                          action=close))
    if app.kind == "web":
        items.append(Item("Remove", "Delete this web app from Lumen TV", "trash",
                          action=remove))
    elif app.kind == "android":
        items.append(Item("Hide", "Restore hidden apps from Settings › Web apps", "close",
                          action=hide))
    return items
