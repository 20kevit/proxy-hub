#!/usr/bin/env bash
# Backup control-plane data (DB + generated core configs). Secrets env is backed up
# separately with 0600 perms. Never commit backups to Git.
set -euo pipefail
DATA_DIR=/var/lib/proxy-hub
DEST="${1:-/var/backups/proxy-hub/proxyhub-$(date +%F-%H%M%S).tar.gz}"
mkdir -p "$(dirname "$DEST")"
tar -czf "$DEST" -C / var/lib/proxy-hub/proxyhub.db var/lib/proxy-hub/generated 2>/dev/null || \
tar -czf "$DEST" -C "$DATA_DIR" . 
chmod 600 "$DEST"
echo "backup: $DEST"
