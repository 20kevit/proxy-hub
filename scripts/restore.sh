#!/usr/bin/env bash
# Restore a backup created by backup.sh. Stops proxy-hub first, restarts afterwards.
set -euo pipefail
SRC="${1:?usage: restore.sh <backup.tar.gz>}"
systemctl stop proxy-hub.service 2>/dev/null || true
tar -xzf "$SRC" -C /
chown -R proxyhub:proxyhub /var/lib/proxy-hub
systemctl start proxy-hub.service
echo "restored from $SRC"
