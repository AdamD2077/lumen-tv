#!/bin/bash
# Lumen TV installer: turns Raspberry Pi OS Lite (64-bit) into a TV system.
#
#   sudo ./install.sh               install everything
#   sudo ./install.sh --no-android  skip Android (Waydroid) support
#   sudo ./install.sh --uninstall   remove Lumen TV again (packages are kept)
#
# Environment: LUMEN_USER  the account the TV session runs as
#                          (default: the user who ran sudo, else "tv")
#              LUMEN_CHROOT=1  set when building an image: nothing is started
set -euo pipefail

SRC="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
DEST=/opt/lumen
ANDROID=1
UNINSTALL=0
for arg in "$@"; do
    case "$arg" in
        --no-android) ANDROID=0 ;;
        --uninstall)  UNINSTALL=1 ;;
        -h|--help)    sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *)            echo "Unknown option: $arg" >&2; exit 1 ;;
    esac
done

[ "$(id -u)" = 0 ] || { echo "Run as root: sudo $0" >&2; exit 1; }
umask 022       # files we create must be readable by the TV user
UPDATE="${LUMEN_UPDATE:-0}"
# An update keeps the choices made when Lumen TV was first installed.
if [ "$UPDATE" = 1 ] && grep -qx "ANDROID=0" /etc/lumen/install.conf 2>/dev/null; then
    ANDROID=0
fi
command -v apt-get >/dev/null || { echo "This needs a Debian-based system." >&2; exit 1; }

TV_USER="${LUMEN_USER:-${SUDO_USER:-tv}}"
[ "$TV_USER" = root ] && TV_USER=tv
CHROOT="${LUMEN_CHROOT:-0}"
BOOT=/boot/firmware
[ -d "$BOOT" ] || BOOT=/boot

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

GETTY_DROPIN=/etc/systemd/system/getty@tty1.service.d/lumen-autologin.conf
PROFILE_MARK="# lumen-tv session"

if [ "$UNINSTALL" = 1 ]; then
    say "Removing Lumen TV"
    rm -f "$GETTY_DROPIN" /etc/sudoers.d/lumen /etc/udev/rules.d/70-lumen-uinput.rules \
          /etc/modules-load.d/lumen.conf
    rm -rf /etc/lumen /var/lib/lumen
    for bin in "$DEST"/bin/*; do rm -f "/usr/local/bin/$(basename "$bin")"; done
    home="$(getent passwd "$TV_USER" | cut -d: -f6)"
    [ -n "$home" ] && [ -f "$home/.profile" ] && sed -i "/$PROFILE_MARK/d" "$home/.profile"
    rm -rf "$DEST"
    systemctl daemon-reload
    echo "Done. Boot settings in $BOOT (psi=1, kernel=kernel8.img) were left in place."
    exit 0
fi

# The launcher needs GTK 4.12+ and gtk4-layer-shell, which arrived with Debian 13.
. /etc/os-release
case "${VERSION_CODENAME:-}" in
    buster|bullseye|bookworm)
        echo "Lumen TV needs Raspberry Pi OS based on Debian 13 (trixie) or newer;" >&2
        echo "this system is '$VERSION_CODENAME'. Please flash a current Raspberry Pi OS Lite (64-bit)." >&2
        exit 1 ;;
esac
[ "$(dpkg --print-architecture)" = arm64 ] || \
    echo "Warning: this is not a 64-bit ARM system. Lumen TV targets Raspberry Pi 4/5 (64-bit)."

# --- packages -------------------------------------------------------------
say "Installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update
PACKAGES=(
    sway                                    # compositor
    python3 python3-gi python3-evdev        # launcher + input daemon
    gir1.2-gtk-4.0 gir1.2-gtk4layershell-1.0 libgtk4-layer-shell0 librsvg2-common
    fonts-inter fonts-noto-color-emoji
    mpv imv                                 # video and photo viewers
    pipewire pipewire-pulse wireplumber     # sound
    network-manager bluez                   # Wi-Fi and controllers
    udiskie                                 # USB drives mount themselves
    dbus-user-session v4l-utils curl ca-certificates
    git                                     # in-system updates
    flatpak xwayland                        # app store; X11 for apps that need it
)
# The browser's package name differs between Raspberry Pi OS releases.
if apt-cache show chromium >/dev/null 2>&1; then
    PACKAGES+=(chromium)
else
    PACKAGES+=(chromium-browser)
fi
apt-get install -y --no-install-recommends "${PACKAGES[@]}"
# Widevine (needed by Netflix, Disney+ and friends) where the distro ships it.
if apt-cache show libwidevinecdm0 >/dev/null 2>&1; then
    apt-get install -y --no-install-recommends libwidevinecdm0 || true
fi

if [ "$ANDROID" = 1 ] && ! command -v waydroid >/dev/null; then
    say "Installing Waydroid (Android container)"
    # Waydroid's own apt repository; this is their documented install method.
    curl -fsSL https://repo.waydro.id | bash
    apt-get install -y --no-install-recommends waydroid
fi

# --- user -----------------------------------------------------------------
say "Setting up user '$TV_USER'"
if ! id "$TV_USER" >/dev/null 2>&1; then
    useradd -m -s /bin/bash "$TV_USER"
    passwd -d "$TV_USER" >/dev/null    # no password: the account only ever auto-logs in
fi
for group in video audio input render netdev plugdev bluetooth; do
    getent group "$group" >/dev/null && usermod -aG "$group" "$TV_USER"
done
TV_HOME="$(getent passwd "$TV_USER" | cut -d: -f6)"

# --- files ----------------------------------------------------------------
say "Installing Lumen TV to $DEST"
rm -rf "$DEST"
mkdir -p "$DEST"
cp -r "$SRC/lumen" "$SRC/bin" "$SRC/config" "$DEST/"
find "$DEST" -name __pycache__ -prune -exec rm -rf {} +
# Record which version this is, so the updater can tell when a newer one exists.
if [ -e "$SRC/.git" ] && command -v git >/dev/null; then
    git -c safe.directory='*' -C "$SRC" rev-parse HEAD > "$DEST/COMMIT" 2>/dev/null || rm -f "$DEST/COMMIT"
elif [ -f "$SRC/COMMIT" ]; then
    cp "$SRC/COMMIT" "$DEST/COMMIT"
fi
mkdir -p /etc/lumen
[ -f /etc/lumen/update.conf ] || cp "$SRC/config/update.conf" /etc/lumen/update.conf
echo "ANDROID=$ANDROID" > /etc/lumen/install.conf
# The TV user must be able to read everything, whatever the source's
# permissions were.
chown -R root:root "$DEST"
chmod -R u=rwX,go=rX "$DEST" /etc/lumen
chmod 755 "$DEST"/bin/*
chmod 755 "$DEST"/bin/*
for bin in "$DEST"/bin/*; do
    ln -sf "$bin" "/usr/local/bin/$(basename "$bin")"
done

# The input daemon creates a virtual keyboard/mouse through /dev/uinput.
echo 'KERNEL=="uinput", SUBSYSTEM=="misc", GROUP="input", MODE="0660", OPTIONS+="static_node=uinput"' \
    > /etc/udev/rules.d/70-lumen-uinput.rules
echo uinput > /etc/modules-load.d/lumen.conf

# The two root-only steps the launcher may run by itself: installing an
# update and downloading the Android image.
SUDO_CMDS="$DEST/bin/lumen-update apply"
[ "$ANDROID" = 1 ] && SUDO_CMDS="$SUDO_CMDS, $DEST/bin/lumen-android-setup"
echo "$TV_USER ALL=(root) NOPASSWD: $SUDO_CMDS" > /etc/sudoers.d/lumen
chmod 440 /etc/sudoers.d/lumen

# --- boot straight into the TV session ------------------------------------
say "Configuring boot"
mkdir -p "$(dirname "$GETTY_DROPIN")"
cat > "$GETTY_DROPIN" <<EOF
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin $TV_USER --noclear %I \$TERM
EOF
touch "$TV_HOME/.profile"
grep -qF "$PROFILE_MARK" "$TV_HOME/.profile" || \
    echo "[ \"\$(tty)\" = /dev/tty1 ] && [ -z \"\${WAYLAND_DISPLAY:-}\" ] && exec lumen-session  $PROFILE_MARK" \
        >> "$TV_HOME/.profile"
chown "$TV_USER:" "$TV_HOME/.profile"
systemctl set-default multi-user.target >/dev/null
systemctl enable NetworkManager bluetooth >/dev/null 2>&1 || true

CMDLINE="$BOOT/cmdline.txt"
if [ -f "$CMDLINE" ]; then
    add_arg() { grep -qw -- "$1" "$CMDLINE" || sed -i "1 s/\$/ $1/" "$CMDLINE"; }
    add_arg quiet
    add_arg loglevel=3
    add_arg vt.global_cursor_default=0
    [ "$ANDROID" = 1 ] && add_arg psi=1      # Android's memory manager needs PSI
fi
CONFIG="$BOOT/config.txt"
if [ "$ANDROID" = 1 ] && [ -f "$CONFIG" ] && ! grep -q "lumen-tv" "$CONFIG"; then
    # A Pi 5 boots a 16K-page kernel by default; Android needs 4K pages.
    cat >> "$CONFIG" <<EOF

# lumen-tv: 4K-page kernel on the Pi 5, required for Android apps
[pi5]
kernel=kernel8.img
[all]
EOF
fi

if [ "$CHROOT" != 1 ]; then
    systemctl daemon-reload
    udevadm control --reload-rules && udevadm trigger --name-match=uinput || true
    modprobe uinput || true
fi

if [ "$UPDATE" = 1 ]; then
    # Restart the controller daemon on the new code (the session restarts it);
    # the launcher restarts itself once it sees the update succeeded.
    pkill -u "$TV_USER" -f "lumen\.(input|remote)" || true
    say "Lumen TV is updated"
    exit 0
fi

say "Lumen TV is installed"
echo "Reboot to start it:  sudo reboot"
[ "$ANDROID" = 1 ] && echo "Then open Settings > Android > Set up Android (about 1 GB download)."
exit 0
