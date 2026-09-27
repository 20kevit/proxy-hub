# Changelog

## 2.0.0 — 2026-09-27 (Phase 2)

- Routing engine: exact/suffix/wildcard/IP/CIDR/port rules, priority order,
  enable/disable, duplicate, move up/down, DEFAULT route (direct|proxy|block),
  client-scoped rules via dedicated inbounds, routing simulator.
- Public authenticated gateway: Xray SOCKS5 :8388 + HTTP :8389, per-device
  credentials (hash + sealed copy, shown once), QR codes, disable/rotate/expire.
- Health states UNKNOWN/HEALTHY/DEGRADED/UNHEALTHY/DISABLED + success counters;
  failover threshold/cooldown unchanged (3/600), previous-node tracking.
- Panel redesign: 9 Persian RTL sections (dashboard, nodes, routing, devices,
  subscription, health, logs, system, settings), toasts, dialogs, responsive.
- Subscription UI: last/next update, imported/removed/duplicate/error stats.
- Audit log viewer with filters. 44 automated tests. New docs: CLIENT_ACCESS.
- Verified live: auth accept/reject, routing BLOCK enforcement, simulator,
  public-IP egress, system Xray untouched.

## 1.0.0 — 2026-09-27

- Initial production release: FastAPI control plane + Persian RTL panel.
- Subscription engine (base64/plain/JSON, vless/vmess/trojan/ss/hysteria2).
- Managed Xray core (separate instance, validated configs); sing-box adapter optional.
- Health checks (TCP latency + active egress probe) and guarded auto-failover.
- Explicit per-app routing (no transparent proxy).
- Session auth + CSRF + rate limiting; audit log; backup/restore scripts.
- systemd units, 24 automated tests, full docs.
