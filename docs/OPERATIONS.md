# Operations

## Daily commands

```bash
systemctl status proxy-hub.service proxyhub-xray.service
journalctl -u proxy-hub -n 100 --no-pager
journalctl -u proxyhub-xray -n 50 --no-pager
curl -s http://127.0.0.1:8899/api/health
```

## Subscription

Panel → Subscription → “Update now”, or:

```bash
# force via API (needs login session; easiest from the panel)
```

## Switch node / failover

Panel → Nodes → “Activate”. Failover mode (`auto`/`manual`), threshold and
cooldown live in Settings. Failover reason is shown on the dashboard and in
the audit log (System tab).

## Gateway

- Settings → gateway toggle/bind/ports/default route (rebuilds managed Xray).
- Devices & Access → create per-device credentials (password shown once).
- Verify locally before exposing: keep bind `127.0.0.1`, test, then switch
  to `0.0.0.0` with confirmation. No firewall package exists on this VPS, so
  bind IS the exposure control — documented, no firewall change was made.
- If a device misbehaves: disable it (config rebuild drops its account
  immediately) or rotate its password.

## Backup / restore

```bash
bash /opt/projects/proxy-hub/scripts/backup.sh            # -> /var/backups/proxy-hub/*.tar.gz
bash /opt/projects/proxy-hub/scripts/restore.sh <file>    # stops, restores, restarts
```

Backups contain DB + generated configs (0600). The secret env file
(`/etc/proxy-hub/proxy-hub.env`) is backed up separately by the operator.

## Health endpoints

- `GET /api/health` — unauthenticated liveness (for systemd/monitoring).
- `GET /api/status` — full dashboard payload (authenticated).
- `GET /api/logs` — pointer to journalctl (logs never leave the host via API).
