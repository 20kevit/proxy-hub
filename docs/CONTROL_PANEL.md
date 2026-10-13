# Control Panel — https://vpn.20kevit.ir

## Architecture

Browser → HTTPS (Nginx `vpn.20kevit.ir`) → `http://127.0.0.1:8899` (FastAPI, same origin, no CORS).

- Nginx: `/etc/nginx/sites-available/vpn.20kevit.ir` (enabled), TLS via
  `/etc/letsencrypt/live/vpn.20kevit.ir/`, rate limit zone
  `/etc/nginx/conf.d/proxyhub-ratelimit.conf` (login: 10r/m).
- Backend serves `/` (index.html) and `/assets/*` (frontend/); Nginx also
  serves `/assets/*` directly with 1y immutable caching.
- API stays on loopback (`127.0.0.1:8899`); proxy listeners stay on loopback
  (`1081`/`2081`). Only 80/443 are public via Nginx.

## Pages (all authenticated, real backend data)

| Tab | Data source |
|-----|-------------|
| Dashboard | `GET /api/status` (services, active node, exit IP, failover, resources) |
| Nodes | `GET /api/nodes`, `POST /api/nodes/{id}/activate` (confirm), `POST .../test` |
| Health | `GET /api/health/cycles`, `GET /api/nodes` |
| Lifecycle | `GET /api/lifecycle/status`, `POST /api/lifecycle/rotate` (confirm), audit `node.activate` |
| Telemetry | `GET /api/telemetry/summary`, `GET /api/telemetry/series` (canvas charts, p50/p95/p99 need ≥20 samples) |
| Connections | `GET /api/connections` (journal tail + ss snapshot; no process attribution claimed) |
| Logs | `GET /api/audit` (filters, max 500) |
| System/Settings/Routing/Devices/Sub | pre-existing endpoints, unchanged behavior |

## Manual node switch (safe path)

1. Nodes tab → Activate → confirm dialog (duplicate-submit guarded by confirm).
2. Server validates: configured node only, enabled, not removed, circuit closed.
3. `activate_node()`: generate → `xray -test` validate → atomic write (0600, proxyhub-owned) → `restart` (node change) or SIGHUP reload (same node).
4. Bounded egress test after switch; audit event `node.activate` with actor/source/target/outcome.
5. Failure preserves last known-good config (write-then-validate ordering).

## Node test allowlist

`POST /api/nodes/{id}/test` uses the fixed configured `health_url` only — no
arbitrary URLs, ports, or schemes accepted. Timeouts and response sizes bounded
in `health.py`/`health_v2.py`.

## Auth model

- PBKDF2 sessions (`ph_session`, HttpOnly, SameSite=lax, Secure on HTTPS),
  CSRF token (`ph_csrf` + `x-csrf-token` header on mutating API calls).
- Login rate-limited per IP (10/300s in-app + 10r/m at Nginx).
- Generic `invalid credentials` errors; session TTL 12h; logout clears cookie + audit.

## Telemetry coverage honesty

- `probe_telemetry` accumulates post-fix; 24h/48h views label incomplete windows
  (`sufficient_for_percentiles`, sample counts, `first_record`/`last_record`).
- Percentiles computed only from in-window successful deep (HTTPS egress) probes.
- Zero samples is reported as "no data", never as 100% success.

## Connection visibility limits

- Source: `journalctl -u proxyhub-xray` accepted-lines + `ss -tlnp` snapshot.
- Shows client IP:port, destination host:port, inbound/outbound tags, active outbound node.
- Explicitly states: no OS process or application attribution available.

## Rollback

```bash
# DB (consistent backup via Python sqlite3 backup API)
ls -l /var/backups/proxy-hub/proxyhub-*.db
# restore: stop proxy-hub, copy backup over /var/lib/proxy-hub/proxyhub.db, chown proxyhub:proxyhub, start
# Nginx: /var/backups/proxy-hub/vpn.20kevit.ir.nginx.bak
# App: git stash / git checkout <prev-commit> in /opt/projects/proxy-hub
```

## Troubleshooting

```bash
systemctl status proxy-hub proxyhub-xray
journalctl -u proxy-hub -n 100 --no-pager
journalctl -u proxyhub-xray -n 50 --no-pager
curl -s http://127.0.0.1:8899/api/health
curl -sk https://vpn.20kevit.ir/api/health
nginx -t && systemctl reload nginx
certbot certificates | grep -A3 vpn
```
