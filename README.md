# ProxyHub 🛰️

Self-hosted proxy gateway + management panel for this VPS (Persian-first, RTL).

One control plane for proxy configurations: fetch a private subscription
(VLESS/REALITY, Hysteria2, VMess, Trojan, Shadowsocks), health-check nodes,
fail over automatically, route apps explicitly, and manage Xray/sing-box cores
— without touching the working system Xray and without any global transparent
proxy.

## Architecture (short)

```
clients -> gateway *:8388 (SOCKS5, auth) / *:8389 (HTTP, auth)
           -> routing engine (DIRECT/PROXY/BLOCK, default PROXY)
           -> managed Xray -> active upstream node (egress 91.107.187.175)
           -> panel/API 127.0.0.1:8899 (Persian RTL, 9 sections)
           -> system Xray 127.0.0.1:2080/1080 (UNMANAGED, left alone)
```

## Quick start (Phase 2)

1. Panel via SSH tunnel: `ssh -p 9011 -N -L 8899:127.0.0.1:8899 root@194.60.231.172`
   → `http://127.0.0.1:8899`
2. Devices & Access → create one client per device (password shown once).
3. Routing → review rules, use Test (simulator) for any domain/IP/port.
4. On the device: SOCKS5 `194.60.231.172:8388` (or HTTP `:8389`) + credentials.
   Full per-OS guide: `docs/CLIENT_ACCESS.md`.

Details: `docs/ARCHITECTURE.md` · decisions: `docs/DECISIONS.md`.

## Quick start

```bash
sudo bash scripts/install.sh
sudo nano /etc/proxy-hub/proxy-hub.env   # subscription URL + admin password
sudo bash scripts/set-admin.sh
sudo systemctl restart proxy-hub.service
```

Panel: `http://127.0.0.1:8899/` (via SSH tunnel or Nginx reverse-proxy; the API
never listens on a public interface by itself).

## What’s inside

- `backend/app/` — FastAPI control plane (api/services/models, thin routes)
- `frontend/` — static Persian RTL panel (no build step)
- `systemd/` — `proxy-hub.service`, `proxyhub-xray.service`, `proxyhub-singbox.service`
- `scripts/` — install, set-admin, backup, restore, proxy-env
- `tests/` — 24 tests (parsers, sync, failover, routing, auth, API)
- `docs/` — architecture, installation, security, routing, subscriptions, cores,
  operations, troubleshooting, API, development, decisions

## Safety guarantees

- No global proxy, no NAT rules, no gateway change.
- System `xray.service` (2080/1080) is never written by ProxyHub.
- Secrets live in `/etc/proxy-hub/` + `/var/lib/proxy-hub/`, never in Git.
- One active upstream at a time (respects provider connection terms).

## Docs map

Start with `docs/INSTALLATION.md`, then `docs/OPERATIONS.md`.
Security model: `docs/SECURITY.md`. Backup/restore: `docs/OPERATIONS.md`.
