# Installation

## Prerequisites (already present on this VPS)

- Ubuntu 24.04, Python 3.12, Xray at `/usr/local/bin/xray`, systemd, Nginx.

## Install

```bash
sudo bash /opt/projects/proxy-hub/scripts/install.sh
```

What it does (idempotent, safe to re-run):

1. Backs up the working system Xray config to
   `/var/backups/proxy-hub/` and `/etc/proxy-hub/xray-system-orig.json`.
2. Creates the `proxyhub` system user (no login).
3. Creates `/var/lib/proxy-hub/generated`, `/var/log/proxy-hub`.
4. Creates `/opt/projects/proxy-hub/.venv` and installs `requirements.txt`.
5. Creates `/etc/proxy-hub/proxy-hub.env` (0600) from `.env.example` if missing.
6. Installs + enables `proxy-hub.service` and `proxyhub-xray.service`.
   It NEVER modifies `xray.service`.

## Configure

```bash
sudo nano /etc/proxy-hub/proxy-hub.env
# set: PROXYHUB_SUBSCRIPTION_URL, PROXYHUB_ADMIN_PASSWORD
sudo bash /opt/projects/proxy-hub/scripts/set-admin.sh
sudo systemctl restart proxy-hub.service
```

## Verify

```bash
systemctl status proxy-hub.service
curl -s http://127.0.0.1:8899/api/health
# system Xray untouched:
curl --proxy socks5h://127.0.0.1:2080 -4 --connect-timeout 15 https://api.ipify.org
```

## Remote access to the panel

FastAPI binds 127.0.0.1 only. Expose via a NEW Nginx server block with HTTPS
(reverse-proxy to 127.0.0.1:8899). Do not modify the existing microchess blocks.
See SECURITY.md.
