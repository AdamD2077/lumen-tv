"""On-screen keyboard: layout and cursor logic (no GTK here, see osk_view.py)."""

from dataclasses import dataclass

COLS = 20  # grid units per row; a normal key is 2 wide


@dataclass(frozen=True)
class Key:
    label: str
    action: str = "char"   # char | shift | layer | space | left | right | backspace | enter | done
    span: int = 2


def _chars(text):
    return [Key(c) for c in text]


_BOTTOM = [
    Key("?123", "layer", 3), Key("space", "space", 6), Key("←", "left"), Key("→", "right"),
    Key("⌫", "backspace"), Key("⏎", "enter"), Key("Done", "done", 3),
]

LAYERS = {
    "letters": [
        _chars("1234567890"),
        _chars("qwertyuiop"),
        _chars("asdfghjkl@"),
        [Key("⇧", "shift")] + _chars("zxcvbnm,."),
        _BOTTOM,
    ],
    "symbols": [
        _chars("1234567890"),
        _chars("@#$%&-+()/"),
        _chars("*\"':;!?_=\\"),
        _chars("~`|<>{}[]^"),
        [Key("abc", "layer", 3)] + _BOTTOM[1:],
    ],
}


class OskModel:
    """Which key is selected, and what pressing it means."""

    def __init__(self):
        self.layer = "letters"
        self.shift = False
        self.row, self.col = 2, 4

    @property
    def rows(self):
        return LAYERS[self.layer]

    @property
    def key(self):
        return self.rows[self.row][self.col]

    def label(self, key):
        if key.action == "char" and self.shift and self.layer == "letters":
            return key.label.upper()
        return key.label

    def _center(self, row, col):
        keys = self.rows[row]
        return sum(k.span for k in keys[:col]) + keys[col].span / 2

    def move(self, direction):
        if direction in ("left", "right"):
            step = 1 if direction == "right" else -1
            self.col = (self.col + step) % len(self.rows[self.row])
            return
        step = 1 if direction == "down" else -1
        x = self._center(self.row, self.col)
        self.row = (self.row + step) % len(self.rows)
        # Land on the key that sits under the one we came from.
        self.col = min(range(len(self.rows[self.row])),
                       key=lambda c: abs(self._center(self.row, c) - x))

    def toggle_shift(self):
        self.shift = not self.shift

    def toggle_layer(self):
        self.layer = "symbols" if self.layer == "letters" else "letters"
        self.shift = False
        self.col = min(self.col, len(self.rows[self.row]) - 1)

    def press(self):
        """Activate the selected key. Returns (action, text)."""
        key = self.key
        if key.action == "shift":
            self.toggle_shift()
        elif key.action == "layer":
            self.toggle_layer()
        elif key.action == "char":
            text = self.label(key)
            self.shift = False  # shift applies to one letter, like a phone keyboard
            return "char", text
        return key.action, ""


# Characters the keyboard can produce -> (key name, needs shift) on a US layout.
def _charmap():
    m = {c: (f"KEY_{c.upper()}", False) for c in "abcdefghijklmnopqrstuvwxyz0123456789"}
    m.update({c.upper(): (f"KEY_{c.upper()}", True) for c in "abcdefghijklmnopqrstuvwxyz"})
    plain = {" ": "SPACE", "-": "MINUS", "=": "EQUAL", "[": "LEFTBRACE", "]": "RIGHTBRACE",
             "\\": "BACKSLASH", ";": "SEMICOLON", "'": "APOSTROPHE", "`": "GRAVE",
             ",": "COMMA", ".": "DOT", "/": "SLASH"}
    shifted = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7",
               "*": "8", "(": "9", ")": "0", "_": "MINUS", "+": "EQUAL", "{": "LEFTBRACE",
               "}": "RIGHTBRACE", "|": "BACKSLASH", ":": "SEMICOLON", '"': "APOSTROPHE",
               "~": "GRAVE", "<": "COMMA", ">": "DOT", "?": "SLASH"}
    m.update({c: (f"KEY_{k}", False) for c, k in plain.items()})
    m.update({c: (f"KEY_{k}", True) for c, k in shifted.items()})
    return m


CHARMAP = _charmap()
