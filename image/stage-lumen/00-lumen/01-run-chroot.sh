#!/bin/bash -e
# Runs inside the image.
LUMEN_USER=tv LUMEN_CHROOT=1 bash /usr/src/lumen-tv/install.sh
rm -rf /usr/src/lumen-tv
