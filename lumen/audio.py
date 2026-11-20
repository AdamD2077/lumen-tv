"""Sound: volume and choosing the output, through PipeWire's `wpctl`."""

import re
import subprocess

SINK = "@DEFAULT_AUDIO_SINK@"
STEP = 5
_SINK_LINE = re.compile(r"^[^\w*]*(\*)?\s*(\d+)\.\s+(.*?)\s+\[vol:\s*([\d.]+)(\s+MUTED)?\]\s*$")


def _wpctl(*args):
    try:
        p = subprocess.run(["wpctl", *args], capture_output=True, text=True, timeout=5)
        return p.returncode == 0, p.stdout
    except (OSError, subprocess.SubprocessError):
        return False, ""


def parse_volume(text):
    """`wpctl get-volume` output -> (percent, muted), or (None, False)."""
    match = re.search(r"Volume:\s*([\d.]+)", text)
    if not match:
        return None, False
    return round(float(match.group(1)) * 100), "MUTED" in text


def parse_outputs(status):
    """The audio outputs listed by `wpctl status`:
    [{"id", "name", "current", "volume", "muted"}]."""
    outputs, in_audio, in_sinks = [], False, False
    for line in status.splitlines():
        if not line.startswith((" ", "\t")) and line.strip():
            in_audio = line.strip() == "Audio"     # top-level section: Audio, Video, …
            in_sinks = False
            continue
        if not in_audio:
            continue
        heading = re.search(r"[├└]─\s*(\w+):", line)
        if heading:
            in_sinks = heading.group(1) == "Sinks"
            continue
        match = _SINK_LINE.match(line) if in_sinks else None
        if match:
            outputs.append({"id": int(match.group(2)), "name": friendly(match.group(3)),
                            "current": bool(match.group(1)),
                            "volume": round(float(match.group(4)) * 100),
                            "muted": bool(match.group(5))})
    # Two sockets of the same kind get the same name from the driver.
    seen = {}
    for output in outputs:
        seen[output["name"]] = seen.get(output["name"], 0) + 1
        if seen[output["name"]] > 1:
            output["name"] += f" #{seen[output['name']]}"
    return outputs


def friendly(name):
    """Drop the driver's "Digital Stereo" wording; keep what identifies the output."""
    name = re.sub(r"\s+(Analog|Digital) (Stereo|Surround \S+)", "", name)
    return re.sub(r"\s+", " ", name).strip() or "Audio output"


def volume():
    ok, out = _wpctl("get-volume", SINK)
    return parse_volume(out) if ok else (None, False)


def change_volume(steps):
    """Louder (positive) or quieter (negative). Unmutes when turning up."""
    if steps > 0:
        _wpctl("set-mute", SINK, "0")
        _wpctl("set-volume", "-l", "1.0", SINK, f"{STEP * steps}%+")
    elif steps < 0:
        _wpctl("set-volume", SINK, f"{STEP * -steps}%-")
    return volume()


def toggle_mute():
    _wpctl("set-mute", SINK, "toggle")
    return volume()


def outputs():
    ok, out = _wpctl("status")
    return parse_outputs(out) if ok else []


def set_output(output_id):
    """Make this the output everything plays through."""
    return _wpctl("set-default", str(int(output_id)))[0]
