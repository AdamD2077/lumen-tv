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
            Item("Sound", "Output and volume", "volume",
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
                 action=page("About", lambda p: about_items(shell), "info")),
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
    from . import audio
    percent, muted = audio.volume()
    level = "No sound device" if percent is None else "Muted" if muted else f"{percent}%"
    outputs = audio.outputs()
    current = next((o["name"] for o in outputs if o["current"]), "None found")

    def do(action, *args):
        action(*args)
        page.refresh()

    def choose_output():
        def build(sub_page):
            def pick(output):
                if audio.set_output(output["id"]):
                    shell.toast(f"Sound now plays through {output['name']}")
                else:
                    shell.toast("Could not switch the sound output")
                shell.pop()

            found = audio.outputs()
            return [Item(o["name"], icon="volume", value="Selected" if o["current"] else "",
                         action=lambda o=o: pick(o)) for o in found] or [
                Item("No sound outputs found", "Check that the TV or speaker is connected")]

        shell.push(ListPage(shell, "Sound output", build, glyph="volume",
                            subtitle="Where sound plays: the TV over HDMI, the headphone "
                                     "jack, or a Bluetooth speaker that is connected."))

    return [
        Item("Output", "Where sound plays", "volume", current, action=choose_output),
        Item("Volume up", icon="volume", value=level,
             action=lambda: do(audio.change_volume, 1)),
        Item("Volume down", icon="volume", action=lambda: do(audio.change_volume, -1)),
        Item("Mute", icon="volume", action=lambda: do(audio.toggle_mute)),
    ]


def display_items(shell, page):
    from . import sway as sw
    cfg = shell.cfg
    outputs = [o for o in shell.sway.outputs() if o.get("active") and o.get("current_mode")]

    def describe(setting):
        if setting == "auto":
            return "Automatic"
        if setting == "native":
            return "Highest"
        for out in outputs:
            for mode in out.get("modes", []):
                if sw.mode_key(mode) == setting:
                    return sw.mode_label(mode)
        return "Automatic"

    def choose(setting):
        previous = cfg["display_mode"]
        if setting == previous:
            return shell.pop()
        cfg["display_mode"] = setting
        shell.sway.setup_outputs(setting)
        shell.pop()
        confirm_display(shell, previous)

    def resolutions():
        def build(_p):
            current = cfg["display_mode"]
            mark = lambda s: "Selected" if s == current else ""
            items = [
                Item("Automatic", "Up to 1080p. Smoothest on a Raspberry Pi", "display",
                     mark("auto"), action=lambda: choose("auto")),
                Item("Highest", "The largest resolution the TV offers", "display",
                     mark("native"), action=lambda: choose("native")),
            ]
            seen = set()
            for out in outputs:
                for mode in sw.available_modes(out):
                    key = sw.mode_key(mode)
                    if key not in seen:
                        seen.add(key)
                        items.append(Item(sw.mode_label(mode), value=mark(key),
                                          action=lambda k=key: choose(k)))
            return items
        shell.push(ListPage(shell, "Resolution", build, glyph="display",
                            subtitle="Higher resolutions are sharper but make the "
                                     "interface and video work harder."))

    def toggle_animations():
        cfg["animations"] = not cfg["animations"]
        config.save(cfg)
        shell.apply_animations()
        page.refresh()

    items = [
        Item("Resolution", "Screen resolution and refresh rate", "display",
             describe(cfg["display_mode"]), action=resolutions),
        Item("Animations", "Turn off if moving around feels slow", "refresh",
             "On" if cfg["animations"] else "Off", action=toggle_animations),
    ]
    for out in outputs:
        mode = out["current_mode"]
        items.append(Item(out["name"], f"{out.get('make', '')} {out.get('model', '')}".strip(),
                          value=f"Now: {sw.mode_label(mode)}"))
    return items


def confirm_display(shell, previous, seconds=15):
    """Keep the new resolution only if the user can see the screen to say so."""
    from gi.repository import GLib
    state = {"timer": 0, "decided": False}

    def finish(keep):
        if state["decided"]:
            return
        state["decided"] = True
        if state["timer"]:
            GLib.source_remove(state["timer"])
        if keep:
            config.save(shell.cfg)
        else:
            shell.cfg["display_mode"] = previous
            shell.sway.setup_outputs(previous)
        if shell.page is page:
            shell.pop()

    def timeout():
        state["timer"] = 0
        if not state["decided"]:
            state["decided"] = True
            shell.cfg["display_mode"] = previous
            shell.sway.setup_outputs(previous)
            if shell.page is page:
                shell.pop()
            shell.toast("Resolution restored")
        return False

    page = ListPage(shell, "Keep this resolution?", lambda _p: [
        Item("Keep", icon="check", action=lambda: finish(True)),
        Item("Go back", "Return to the previous resolution", "close",
             action=lambda: finish(False)),
    ], subtitle=f"If you do nothing, the previous resolution returns in {seconds} seconds.",
        glyph="display")
    page.on_close = lambda: finish(False)        # Back means "go back"
    state["timer"] = GLib.timeout_add_seconds(seconds, timeout)
    shell.push(page)


def power_items(shell):
    return [
        Item("Restart", icon="refresh", action=lambda: run("systemctl", "reboot")),
        Item("Shut down", icon="power", action=lambda: run("systemctl", "poweroff")),
        Item("Exit to console", "Leave Lumen TV; log in again to come back", "close",
             action=lambda: shell.sway.command("exit")),
    ]


def about_items(shell=None):
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
        Item("Graphics", value=shell.graphics() if shell else "Unknown"),
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


ZOOM_STEPS = [1, 1.25, 1.5, 1.75, 2]


def webapp_edit_page(shell, app_id):
    """Edit one web app. Every change is saved at once."""
    def entry():
        return next((w for w in shell.cfg["web_apps"] if w.get("id") == app_id), None)

    def change(page, **fields):
        if catalog.edit_web_app(shell.cfg, app_id, **fields):
            shell.save()
        else:
            shell.toast("That is not a usable value")
        page.refresh()

    def build(page):
        web = entry()
        if web is None:
            return [Item("This web app no longer exists")]
        zoom = web.get("zoom") or 1
        tv = bool(web.get("user_agent"))

        def next_zoom():
            later = [z for z in ZOOM_STEPS if z > zoom]
            change(page, zoom=later[0] if later else ZOOM_STEPS[0])

        return [
            Item("Name", icon="info", value=web.get("name", ""),
                 action=lambda: shell.push(PromptPage(
                     shell, "Name", lambda text: change(page, name=text),
                     initial=web.get("name", ""), glyph="globe",
                     subtitle="What the app is called on the home screen"))),
            Item("Address", icon="globe", value=web.get("url", ""),
                 action=lambda: shell.push(PromptPage(
                     shell, "Address", lambda text: change(page, url=text),
                     initial=web.get("url", ""), glyph="globe",
                     subtitle="The web address this app opens"))),
            Item("TV interface", "Ask the site for its TV version, where it has one "
                 "(YouTube does)", "display", "On" if tv else "Off",
                 action=lambda: change(page, tv=not tv)),
            Item("Zoom", "Make the site larger for viewing from the sofa", "search",
                 f"{round(zoom * 100)}%", action=next_zoom),
        ]

    web = entry()
    return ListPage(shell, "Edit", build, glyph="globe",
                    subtitle=(web or {}).get("name", "") + ". Changes apply the next time "
                             "the app is opened.")


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

    return android_store_items(shell, page) + [
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


# --- store --------------------------------------------------------------

def android_store_items(shell, page):
    """Rows that install an Android app store, for those not installed yet."""
    from . import store
    if catalog.android_state() != "ready":
        return []
    have = {a.package for a in shell.apps if a.kind == "android"}
    items = []
    for package, (name, about) in store.ANDROID_STORES.items():
        if package in have:
            continue

        def work(report, package=package):
            path = store.download_android_store(
                package, report=lambda fraction, _t=None: report(fraction, "Downloading"))
            report(None, "Installing in Android…")
            return catalog.android_helper("install", str(path))

        def done(result, name=name):
            if getattr(result, "returncode", 1) == 0:
                shell.toast(f"{name} installed. It is on the home screen.")
                shell.reload_apps()
            elif isinstance(result, Exception):
                shell.toast(str(result) or f"{name} could not be downloaded")
            else:
                lines = (getattr(result, "stderr", "") or "").strip().splitlines()
                shell.toast(lines[-1] if lines else f"{name} could not be installed")
            page.refresh()

        items.append(Item(f"Install {name}", about, "android",
                          action=lambda w=work, d=done, n=name: shell.busy(
                              f"Installing {n}…", w, d, progress=True,
                              hint="Connecting to f-droid.org…")))
    return items


def store_page(shell):
    return ListPage(shell, "Store", lambda p: store_items(shell, p), glyph="store",
                    subtitle="Linux apps from Flathub. Only apps built for this "
                             "device are shown.")


def store_items(shell, page):
    from . import store
    from .widgets import run_async
    head = []
    if store.flatpak_available():
        head.append(Item("Search Flathub", "Find an app by name", "search",
                         action=lambda: shell.push(PromptPage(
                             shell, "Search", lambda q: shell.push(store_results(shell, q)),
                             subtitle="Search Flathub for apps", glyph="search"))))
    else:
        head.append(Item("Flatpak is not installed",
                         "Run Settings › System update to add app store support", "close"))
    head += android_store_items(shell, page)
    if catalog.android_state() != "ready":
        head.append(Item("Android app stores", "Set up Android first to add F-Droid or "
                         "Aurora Store", "android",
                         action=lambda: shell.push(android_page(shell))))
    if not store.flatpak_available():
        return head

    def show(result):
        if isinstance(result, Exception):
            return page.set_items(head + [Item("Flathub could not be reached",
                                               "Check the network connection", "close")])
        apps, have = result
        page.set_items(head + [store_row(shell, a, have) for a in apps])

    page.set_items(head + [Item("Loading popular apps…")])
    run_async(lambda: (store.search("", 30), store.installed()), show)


def store_row(shell, app, have):
    return Item(app["name"], app["summary"], "store",
                "Installed" if app["id"] in have else "",
                action=lambda: shell.push(store_app_page(shell, app)))


def store_results(shell, query):
    from . import store
    from .widgets import run_async

    def build(page):
        def show(result):
            if isinstance(result, Exception):
                return page.set_items([Item("Flathub could not be reached",
                                            "Check the network connection", "close")])
            apps, have = result
            page.set_items([store_row(shell, a, have) for a in apps]
                           or [Item("No apps found", "Try a different word")])

        page.loading("Searching…")
        run_async(lambda: (store.search(query, 40), store.installed()), show)

    return ListPage(shell, f"“{query}”", build, subtitle="Results from Flathub",
                    glyph="search")


def store_app_page(shell, app):
    from . import store

    def build(page):
        installed = app["id"] in store.installed()

        def finish(result, verb):
            ok, message = result if isinstance(result, tuple) else (False, "That did not work")
            shell.toast(f"{app['name']} {verb}" if ok else message)
            shell.reload_apps()
            page.refresh()

        def install():
            shell.busy(f"Installing {app['name']}…",
                       lambda report: store.install(app["id"], report),
                       lambda r: finish(r, "installed. It is on the home screen."),
                       hint="Preparing…", progress=True)

        def remove():
            shell.busy(f"Removing {app['name']}…", lambda: store.uninstall(app["id"]),
                       lambda r: finish(r, "removed"))

        def open_it():
            found = next((a for a in shell.apps if a.id == f"flatpak:{app['id']}"), None)
            if found:
                shell.open_app(found)

        if installed:
            return [Item("Open", icon="check", action=open_it),
                    Item("Remove", "Uninstall this app", "trash", action=remove)]
        return [Item("Install", app["id"], "plus", action=install)]

    return ListPage(shell, app["name"], build, subtitle=app["summary"], glyph="store")


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
        items.append(Item("Edit", "Name, address, TV interface and zoom", "settings",
                          action=lambda: shell.push(webapp_edit_page(shell, app.id))))
        items.append(Item("Remove", "Delete this web app from Lumen TV", "trash",
                          action=remove))
    elif app.kind == "android":
        items.append(Item("Hide", "Restore hidden apps from Settings › Web apps", "close",
                          action=hide))
    elif app.kind == "flatpak":
        def uninstall():
            from . import store

            def done(result):
                ok, message = result if isinstance(result, tuple) else (False, "")
                shell.toast(f"{app.name} removed" if ok else message or "Could not remove it")
                shell.pop()
                shell.reload_apps()

            shell.busy(f"Removing {app.name}…", lambda: store.uninstall(app.package), done)

        items.append(Item("Uninstall", "Remove this app from the device", "trash",
                          action=uninstall))
    return items
