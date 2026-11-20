"""Talk to the lumen-input daemon: `lumenctl osk show|hide|toggle|auto`, `lumenctl home`."""

import os
import socket
import sys

SOCKET_PATH = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "lumen-input.sock")


def send(command):
    """Best effort: the shell must keep working when the daemon is not running."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            sock.connect(SOCKET_PATH)
            sock.sendall(command.encode() + b"\n")
        return True
    except OSError:
        return False


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: lumenctl osk show|hide|toggle|auto | home | close")
    if not send(" ".join(sys.argv[1:])):
        sys.exit("lumenctl: lumen-input is not running")


if __name__ == "__main__":
    main()
