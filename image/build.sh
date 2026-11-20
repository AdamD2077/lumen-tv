#!/bin/bash
# Builds a flashable Lumen TV image (Raspberry Pi OS Lite 64-bit + Lumen TV)
# with pi-gen. Needs Docker and about 15 GB of disk; takes 30-60 minutes.
#
#   image/build.sh        ->  build/pi-gen/deploy/*lumen*.img.xz
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$REPO/build"
PIGEN="$WORK/pi-gen"

command -v docker >/dev/null || { echo "Docker is required." >&2; exit 1; }
mkdir -p "$WORK"
# pi-gen keeps its 64-bit build on the arm64 branch.
[ -d "$PIGEN" ] || git clone --depth 1 --branch arm64 https://github.com/RPi-Distro/pi-gen.git "$PIGEN"

# Our stage runs after stage2 (= Raspberry Pi OS Lite) and installs Lumen TV.
STAGE="$PIGEN/stage-lumen"
rm -rf "$STAGE"
cp -r "$REPO/image/stage-lumen" "$STAGE"
mkdir -p "$STAGE/00-lumen/files"
cp -r "$REPO/lumen" "$REPO/bin" "$REPO/config" "$REPO/install.sh" "$STAGE/00-lumen/files/"
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +
git -C "$REPO" rev-parse HEAD > "$STAGE/00-lumen/files/COMMIT"   # version, for the updater
chmod +x "$STAGE"/prerun.sh "$STAGE"/00-lumen/*.sh

touch "$PIGEN/stage2/SKIP_IMAGES"      # only export the final image
cp "$REPO/image/config" "$PIGEN/config"

cd "$PIGEN"
./build-docker.sh
ls -lh deploy/
