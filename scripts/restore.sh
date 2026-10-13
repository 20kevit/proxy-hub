#!/usr/bin/env bash
# Restore a backup created by backup.sh.
# Safety: snapshots current state first, validates archive contents and DB
# integrity, restores, fixes ownership, restarts and health-checks the service.
# Usage: restore.sh <backup.tar.gz>
set -euo pipefail
SRC="${1:?usage: restore.sh <backup.tar.gz}"
REPO=/opt/projects/proxy-hub
DATA_DIR=/var/lib/proxy-hub
[ -f "$SRC" ] || { echo "backup not found: $SRC" >&2; exit 1; }

echo "==> pre-restore safety snapshot of current state"
bash "$REPO/scripts/backup.sh" "/var/backups/proxy-hub/pre-restore-$(date +%F-%H%M%S).tar.gz"

echo "==> validating archive contents"
# Allowlist: only var/lib/proxy-hub/** may be inside the archive.
if tar -tzf "$SRC" | grep -vE '^(var/lib/proxy-hub/|var/lib/proxy-hub/proxyhub\.db|var/lib/proxy-hub/generated/)' | grep -q .; then
  echo "refusing restore: archive contains unexpected paths:" >&2
  tar -tzf "$SRC" | grep -vE '^(var/lib/proxy-hub/|var/lib/proxy-hub/proxyhub\.db|var/lib/proxy-hub/generated/)' >&2
  exit 1
fi

echo "==> stopping proxy-hub (xray keeps serving from memory during the switch)"
systemctl stop proxy-hub.service 2>/dev/null || true

echo "==> extracting"
tar -xzf "$SRC" -C /
chown -R proxyhub:proxyhub "$DATA_DIR"
chmod 750 "$DATA_DIR"

echo "==> verifying restored database"
"$REPO/.venv/bin/python" - <<'EOF'
import sqlite3
c = sqlite3.connect("file:/var/lib/proxy-hub/proxyhub.db?mode=ro", uri=True)
row = c.execute("PRAGMA integrity_check").fetchone()
assert row and row[0] == "ok", f"integrity check failed: {row}"
print("integrity ok")
EOF

echo "==> starting proxy-hub"
systemctl start proxy-hub.service
sleep 5
systemctl is-active --quiet proxy-hub.service || { echo "proxy-hub failed to start" >&2; exit 1; }
curl -sf --max-time 10 http://127.0.0.1:8899/api/health >/dev/null \
  && echo "health check ok" || { echo "health check failed" >&2; exit 1; }
echo "restored from $SRC"
