#!/usr/bin/env bash
# ProxyHub installer — idempotent. Safe to re-run. Never touches system xray.service.
set -euo pipefail
REPO=/opt/projects/proxy-hub
ENV_FILE=/etc/proxy-hub/proxy-hub.env
DATA_DIR=/var/lib/proxy-hub

echo "==> backing up existing system Xray config (read-only copy)"
mkdir -p /etc/proxy-hub /var/backups/proxy-hub
if [ -f /usr/local/etc/xray/config.json ]; then
  cp -a /usr/local/etc/xray/config.json "/var/backups/proxy-hub/xray-system-$(date +%F-%H%M%S).json"
  cp -a /usr/local/etc/xray/config.json /etc/proxy-hub/xray-system-orig.json
fi

echo "==> dedicated user"
id proxyhub >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin proxyhub

echo "==> directories"
mkdir -p "$DATA_DIR/generated" "$DATA_DIR/backups" /var/log/proxy-hub
chown -R proxyhub:proxyhub "$DATA_DIR" /var/log/proxy-hub
chmod 750 "$DATA_DIR" /etc/proxy-hub

echo "==> python venv + deps"
[ -d "$REPO/.venv" ] || python3 -m venv "$REPO/.venv"
"$REPO/.venv/bin/pip" install -q --upgrade pip
"$REPO/.venv/bin/pip" install -q -r "$REPO/requirements.txt"

echo "==> env file"
if [ ! -f "$ENV_FILE" ]; then
  SECRET=$(python3 -c "import secrets;print(secrets.token_hex(32))")
  sed "s/^PROXYHUB_SECRET_KEY=$/PROXYHUB_SECRET_KEY=$SECRET/" "$REPO/.env.example" > "$ENV_FILE"
  echo "Created $ENV_FILE — edit PROXYHUB_SUBSCRIPTION_URL and PROXYHUB_ADMIN_PASSWORD."
fi
chmod 600 "$ENV_FILE"
chown root:proxyhub "$ENV_FILE"
chgrp proxyhub /etc/proxy-hub
chmod 750 /etc/proxy-hub
# group must be able to read the env file (service runs as proxyhub)
chmod 640 "$ENV_FILE"

echo "==> systemd units"
cp "$REPO/systemd/proxy-hub.service" "$REPO/systemd/proxyhub-xray.service" /etc/systemd/system/
if command -v sing-box >/dev/null 2>&1 || [ -x /usr/local/bin/sing-box ]; then
  cp "$REPO/systemd/proxyhub-singbox.service" /etc/systemd/system/
fi
systemctl daemon-reload
systemctl enable proxy-hub.service proxyhub-xray.service >/dev/null

echo "==> done. Next:"
echo "  1) edit $ENV_FILE (subscription URL, admin password)"
echo "  2) bash $REPO/scripts/set-admin.sh"
echo "  3) systemctl restart proxy-hub.service && systemctl status proxy-hub.service"
