"""App stores: Flathub (Linux apps via Flatpak) and Android app-store installers.

Flatpak apps are installed for the TV user only (`flatpak --user`), so no
administrator rights are involved.
"""

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

FLATHUB_REPO = "https://dl.flathub.org/repo/flathub.flatpakrepo"
FLATHUB_SEARCH = "https://flathub.org/api/v2/search"
FDROID_API = "https://f-droid.org/api/v1/packages/{}"
FDROID_APK = "https://f-droid.org/repo/{}_{}.apk"
APP_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{2,254}$")
USER_AGENT = "LumenTV"

# Android app stores that can be installed without a Google account.
ANDROID_STORES = {
    "org.fdroid.fdroid": ("F-Droid", "Free and open-source Android apps"),
    "com.aurora.store": ("Aurora Store", "Apps from Google Play, without a Google account"),
}


def flatpak_available():
    return bool(shutil.which("flatpak"))


def machine_arch():
    import platform
    return {"arm64": "aarch64"}.get(platform.machine(), platform.machine())


def _request(url, body=None, timeout=20):
    headers = {"User-Agent": USER_AGENT}
    if body is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(body).encode()
    with urllib.request.urlopen(urllib.request.Request(url, body, headers),
                                timeout=timeout) as response:
        return response.read()


def parse_search(data):
    """Flathub search response -> [{id, name, summary, installs}]."""
    apps = []
    for hit in data.get("hits", []) if isinstance(data, dict) else []:
        app_id = hit.get("app_id")
        if isinstance(app_id, str) and APP_ID.match(app_id) and hit.get("name"):
            apps.append({"id": app_id, "name": str(hit["name"]),
                         "summary": str(hit.get("summary") or ""),
                         "installs": int(hit.get("installs_last_month") or 0)})
    return apps


def search(query="", limit=40, arch=None, request=_request):
    """Search Flathub for apps built for this device. An empty query lists the
    most popular. Raises OSError when Flathub cannot be reached."""
    body = {"query": query, "hits_per_page": limit,
            "filters": [{"filterType": "arches", "value": arch or machine_arch()}]}
    try:
        return parse_search(json.loads(request(FLATHUB_SEARCH, body)))
    except ValueError as e:
        raise OSError("Unexpected answer from Flathub") from e


def _flatpak(*args, timeout=60):
    try:
        p = subprocess.run(["flatpak", *args], capture_output=True, text=True, timeout=timeout)
        return p.returncode == 0, p.stdout, p.stderr
    except (OSError, subprocess.SubprocessError) as e:
        return False, "", str(e)


def installed():
    """Ids of the Flatpak apps installed on this device."""
    ok, out, _ = _flatpak("list", "--app", "--columns=application")
    return {line.strip() for line in out.splitlines() if APP_ID.match(line.strip())} if ok else set()


def _last_line(text, fallback):
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return re.sub(r"^error:\s*", "", lines[-1]) if lines else fallback


# Terminal control sequences: "OSC ... ST" (window title, progress) and CSI.
ESCAPES = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[A-Za-z]")


def flatpak_progress(text, state=None):
    """Follow flatpak's terminal output ("Installing 2/5…", "37%").

    `state` carries (step, steps, percent) between chunks. Returns the new
    state and the overall fraction done, or None before anything is known."""
    step, steps, percent = state or (0, 0, 0)
    text = ESCAPES.sub("\n", text)
    pattern = r"(?:Installing|Updating|Uninstalling)(?: (\d+)/(\d+))?…|(\d{1,3})%"
    for match in re.finditer(pattern, text):
        if match.group(3):
            percent = min(int(match.group(3)), 100)
        elif match.group(1):
            step, steps, percent = int(match.group(1)), int(match.group(2)), 0
        elif not steps:
            step, steps = 1, 1          # a single download is not numbered
    if not steps or step > steps:
        return (step, steps, percent), None
    return (step, steps, percent), ((step - 1) + percent / 100) / steps


def _run_in_terminal(cmd, on_output, timeout):
    """Run a command inside a pseudo-terminal: flatpak only reports download
    progress when it believes it is talking to one. Returns (exit code, text)."""
    import pty
    import select
    import time
    master, slave = pty.openpty()
    try:
        process = subprocess.Popen(
            cmd, stdin=slave, stdout=slave, stderr=slave, close_fds=True,
            env={**os.environ, "COLUMNS": "120", "LINES": "40", "TERM": "xterm",
                 "LC_ALL": "C.UTF-8"})
    except OSError as e:
        os.close(master)
        os.close(slave)
        return 1, str(e)
    os.close(slave)
    collected, deadline = [], time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], 0.5)
            if ready:
                try:
                    data = os.read(master, 4096)
                except OSError:         # the terminal closes when the command ends
                    break
                if not data:
                    break
                text = data.decode("utf-8", "replace")
                collected.append(text)
                on_output(text)
            elif process.poll() is not None:
                break
        else:
            process.kill()
    finally:
        os.close(master)
    return process.wait(), "".join(collected)


def install(app_id, report=None):
    """Install from Flathub. Returns (ok, message). Can take several minutes:
    the first app also downloads the shared runtime it needs.

    `report(fraction, text)` is called as the download progresses."""
    report = report or (lambda fraction, text=None: None)
    if not APP_ID.match(app_id):
        return False, "Not a valid app"
    ok, _, err = _flatpak("remote-add", "--user", "--if-not-exists", "flathub", FLATHUB_REPO)
    if not ok:
        return False, _last_line(err, "Could not add the Flathub source")
    report(None, "Preparing…")
    state = [None]

    def on_output(text):
        state[0], fraction = flatpak_progress(text, state[0])
        if fraction is not None:
            step, steps, _ = state[0]
            report(fraction, f"Part {step} of {steps}" if steps > 1 else None)

    code, output = _run_in_terminal(
        ["flatpak", "install", "--user", "-y", "flathub", app_id], on_output, 3600)
    if code == 0:
        return True, "Installed"
    errors = [line for line in ESCAPES.sub("", output).splitlines() if "error:" in line.lower()]
    if any("Nothing matches" in line for line in errors):
        return False, "This app is not available for this device"
    return False, _last_line("\n".join(errors), "The app could not be installed")


def uninstall(app_id):
    if not APP_ID.match(app_id):
        return False, "Not a valid app"
    ok, _, err = _flatpak("uninstall", "--user", "--noninteractive", "-y", app_id, timeout=600)
    if ok:
        _flatpak("uninstall", "--user", "--noninteractive", "-y", "--unused", timeout=600)
    return ok, "Removed" if ok else _last_line(err, "The app could not be removed")


def download(url, target, report=None, timeout=60):
    """Download a file, calling `report(fraction)` as it arrives."""
    report = report or (lambda fraction, text=None: None)
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(target.suffix + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response, \
                open(partial, "wb") as out:
            total = int(response.headers.get("Content-Length") or 0)
            done = 0
            while chunk := response.read(256 * 1024):
                out.write(chunk)
                done += len(chunk)
                report(min(done / total, 1.0) if total else None)
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)
    return target


def download_android_store(package, downloads=None, request=_request, fetch=download,
                           report=None):
    """Download the current APK of an Android app store from F-Droid.
    Returns the file's path. Raises OSError on failure."""
    if package not in ANDROID_STORES:
        raise OSError("Unknown store")
    try:
        version = int(json.loads(request(FDROID_API.format(package)))["suggestedVersionCode"])
    except (ValueError, KeyError, TypeError) as e:
        raise OSError("Unexpected answer from F-Droid") from e
    folder = Path(downloads or Path.home() / "Downloads")
    target = fetch(FDROID_APK.format(package, version), folder / f"{package}.apk", report)
    with open(target, "rb") as f:
        if f.read(2) != b"PK":          # an APK is a zip archive
            target.unlink()
            raise OSError("The download is not an Android app")
    return target
