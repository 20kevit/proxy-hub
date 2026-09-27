# Configuration

All runtime secrets live in `/etc/proxy-hub/proxy-hub.env` (0600, root:proxyhub).
The repo only ships `.env.example` with placeholders.

| Variable | Default | Meaning |
|---|---|---|
| `PROXYHUB_SUBSCRIPTION_URL` | (empty) | Private subscription URL. Secret. |
| `PROXYHUB_ADMIN_USER` / `PROXYHUB_ADMIN_PASSWORD` | `admin` / (empty) | Bootstrap panel login. |
| `PROXYHUB_SECRET_KEY` | auto-generated at install | Session signing key. |
| `PROXYHUB_HOST` / `PROXYHUB_PORT` | `127.0.0.1` / `8899` | API bind. Keep localhost. |
| `PROXYHUB_DATA_DIR` / `PROXYHUB_DB_PATH` | `/var/lib/proxy-hub`(+`/proxyhub.db`) | SQLite + runtime state. |
| `PROXYHUB_XRAY_BIN` / `PROXYHUB_XRAY_CONFIG` | `/usr/local/bin/xray`, `…/generated/xray.json` | Managed core. |
| `PROXYHUB_SOCKS_PORT` / `PROXYHUB_HTTP_PORT` | `2081` / `1081` | Managed listeners (localhost). |
| `PROXYHUB_SINGBOX_BIN` / `PROXYHUB_SINGBOX_ENABLED` | `/usr/local/bin/sing-box`, `false` | Hy2 core (optional). |
| `PROXYHUB_SUB_UPDATE_INTERVAL` | `21600` | Subscription refresh, seconds. |
| `PROXYHUB_HEALTH_INTERVAL` | `300` | Health sweep, seconds. |
| `PROXYHUB_HEALTH_URL` / `PROXYHUB_HEALTH_TIMEOUT` | `https://api.ipify.org` / `15` | Egress probe. |
| `PROXYHUB_FAILOVER_MODE` / `_THRESHOLD` / `_COOLDOWN` | `auto` / `3` / `600` | Failover policy. |

Panel-editable settings (stored in DB `settings` table, override env defaults):
sub/health intervals, health URL, failover mode/threshold/cooldown.

## Layout

- Repo: `/opt/projects/proxy-hub/`
- Secrets/config: `/etc/proxy-hub/proxy-hub.env`
- Runtime: `/var/lib/proxy-hub/` (db, `generated/` core configs, backups)
- Logs: journald (`journalctl -u proxy-hub`)
