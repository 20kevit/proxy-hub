# Changelog

## Unreleased — panel redesign (`feature/ui-ux-redesign`, baseline `51d5939`)

Frontend-only; no backend API contract changed. Decisions: `docs/UX_DECISIONS.md`.

- New navigation: 12 flat tabs → sidebar with 4 groups
  (operations / access / observability / system), hash routes (`#/nodes` …),
  mobile drawer, `aria-current`. All sections preserved.
- Dashboard: warning-first ops overview; managed-core state chip; process
  running vs verified egress shown separately; recent activity from
  `GET /api/report/summary`.
- Nodes: separate protocol/health filters, per-protocol badges, per-node
  technical `<details>`, Hysteria2 guard when sing-box is missing, delete +
  restore actions, disable-active confirmation.
- Routing: inline default-route select (was `prompt()`), inline dialog
  validation. Devices: edit (name/notes/expiry, incl. clearing expiry);
  password field disabled in edit mode (rotation is the only change path).
- Lifecycle: renders `last_rotation_at` and `last_rotation_reason`.
  Settings: rotation group exposed (keys the backend already accepted).
  System: core-capabilities card (sing-box/Hysteria2 consequence).
- Interaction: parallel section loading with per-section retry, busy guards
  against double submit, dialog confirms for destructive/connectivity
  actions, light + dark themes (only the theme pref. in localStorage).
- Bug fixes: rotation history never rendered (null reference on a missing
  element); connections table column mismatch (`undefined` status cell);
  ambiguous node filter; mobile horizontal overflow on 3 sections.
- Known limitations (unchanged backend behavior): `max_connections` is not
  enforced (noted in UI); Hysteria2 needs sing-box (warned, not implied);
  node secrets at rest remain plaintext (out of scope for this branch).

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
