#!/usr/bin/env bash
# Consistent backup of control-plane data (SQLite DB + generated core configs).
# The live database is snapshotted through the SQLite backup API first, so the
# archive is transactionally consistent even while proxy-hub is running.
# Secrets env (/etc/proxy-hub/proxy-hub.env) is intentionally NOT included;
# back it up separately with 0600 perms. Never commit backups to Git.
#
# Layout inside the archive (relative):
#   var/lib/proxy-hub/proxyhub.db
#   var/lib/proxy-hub/generated/...
set -euo pipefail
DATA_DIR=/var/lib/proxy-hub
DEST="${1:-/var/backups/proxy-hub/proxyhub-$(date +%F-%H%M%S).tar.gz}"
REPO=/opt/projects/proxy-hub
mkdir -p "$(dirname "$DEST")"
TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT
mkdir -p "$TMPD/var/lib/proxy-hub"
"$REPO/.venv/bin/python" - "$DATA_DIR/proxyhub.db" "$TMPD/var/lib/proxy-hub/proxyhub.db" <<'EOF'
import sqlite3, sys
src = sqlite3.connect(sys.argv[1])
try:
    dst = sqlite3.connect(sys.argv[2])
    try:
        src.backup(dst)
    finally:
        dst.close()
finally:
    src.close()
print("snapshot ok")
EOF
if [ -d "$DATA_DIR/generated" ]; then
  mkdir -p "$TMPD/var/lib/proxy-hub"
  cp -a "$DATA_DIR/generated" "$TMPD/var/lib/proxy-hub/"
fi
tar -czf "$DEST" -C "$TMPD" var
chmod 600 "$DEST"
echo "backup: $DEST"
