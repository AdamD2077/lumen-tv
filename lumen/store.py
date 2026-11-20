"""App stores: Flathub (Linux apps via Flatpak) and Android app-store installers.

Flatpak apps are installed for the TV user only (`flatpak --user`), so no
administrator rights are involved.
"""

import json
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


def install(app_id):
    """Install from Flathub. Returns (ok, message). Can take several minutes:
    the first app also downloads the shared runtime it needs."""
    if not APP_ID.match(app_id):
        return False, "Not a valid app"
    ok, _, err = _flatpak("remote-add", "--user", "--if-not-exists", "flathub", FLATHUB_REPO)
    if not ok:
        return False, _last_line(err, "Could not add the Flathub source")
    ok, _, err = _flatpak("install", "--user", "--noninteractive", "-y", "flathub", app_id,
                          timeout=3600)
    return ok, "Installed" if ok else _last_line(err, "The app could not be installed")


def uninstall(app_id):
    if not APP_ID.match(app_id):
        return False, "Not a valid app"
    ok, _, err = _flatpak("uninstall", "--user", "--noninteractive", "-y", app_id, timeout=600)
    if ok:
        _flatpak("uninstall", "--user", "--noninteractive", "-y", "--unused", timeout=600)
    return ok, "Removed" if ok else _last_line(err, "The app could not be removed")


def download_android_store(package, downloads=None, request=_request):
    """Download the current APK of an Android app store from F-Droid.
    Returns the file's path. Raises OSError on failure."""
    if package not in ANDROID_STORES:
        raise OSError("Unknown store")
    try:
        version = int(json.loads(request(FDROID_API.format(package)))["suggestedVersionCode"])
    except (ValueError, KeyError, TypeError) as e:
        raise OSError("Unexpected answer from F-Droid") from e
    data = request(FDROID_APK.format(package, version), timeout=300)
    if data[:2] != b"PK":           # an APK is a zip archive
        raise OSError("The download is not an Android app")
    folder = Path(downloads or Path.home() / "Downloads")
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{package}.apk"
    target.write_bytes(data)
    return target
