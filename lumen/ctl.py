"""Control the running session: `lumenctl home | close | quick | osk show|hide|toggle|auto`.

Two processes listen: lumen-input (controller, on-screen keyboard, home/close)
and the launcher (the quick settings panel).
"""

import os
import socket
import sys

_RUNTIME = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
SOCKET_PATH = os.path.join(_RUNTIME, "lumen-input.sock")
SHELL_SOCKET = os.path.join(_RUNTIME, "lumen-shell.sock")
SHELL_COMMANDS = {"quick"}


def send(command, path=None):
    """Best effort: everything must keep working when the other side is not running."""
    if path is None:
        path = SHELL_SOCKET if command in SHELL_COMMANDS else SOCKET_PATH
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            sock.connect(path)
            sock.sendall(command.encode() + b"\n")
        return True
    except OSError:
        return False


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: lumenctl home | close | quick | osk show|hide|toggle|auto")
    if not send(" ".join(sys.argv[1:])):
        sys.exit("lumenctl: Lumen TV is not running")


if __name__ == "__main__":
    main()
