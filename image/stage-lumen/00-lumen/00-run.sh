#!/bin/bash -e
# Runs on the build host: copy the Lumen TV sources into the image.
install -d "${ROOTFS_DIR}/usr/src/lumen-tv"
cp -r files/. "${ROOTFS_DIR}/usr/src/lumen-tv/"
