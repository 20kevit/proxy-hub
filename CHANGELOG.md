# Changelog

## 1.0.0 — 2026-09-27

- Initial production release: FastAPI control plane + Persian RTL panel.
- Subscription engine (base64/plain/JSON, vless/vmess/trojan/ss/hysteria2).
- Managed Xray core (separate instance, validated configs); sing-box adapter optional.
- Health checks (TCP latency + active egress probe) and guarded auto-failover.
- Explicit per-app routing (no transparent proxy).
- Session auth + CSRF + rate limiting; audit log; backup/restore scripts.
- systemd units, 24 automated tests, full docs.
