# ProxyHub — Architecture

## Big picture

```
Internet clients (phone/laptop/tablet, per-device auth)
      |  SOCKS5 :8388 / HTTP :8389  (public gateway inbounds, Xray)
      v
ProxyHub control plane (FastAPI, 127.0.0.1:8899)
      |
      +-- managed Xray instance  (proxyhub-xray.service)
      |     127.0.0.1:2081/1081 local + *:8388/8389 gateway (auth)
      |     routing: seeds -> user rules -> DEFAULT (direct|proxy|block)
      |       +-- DIRECT -> freedom (VPS egress)
      |       +-- PROXY  -> active upstream node (VLESS/...)
      |       +-- BLOCK  -> blackhole
      +-- managed sing-box instance (optional, Hysteria2 only, no gateway)
      +-- system Xray service (UNMANAGED — 127.0.0.1:2080/1080)
      v
Upstream provider nodes
```

The VPS itself never enters the client path: no NAT, no gateway change,
no global env proxy. Gateway exposure is controlled solely by its bind
address (no firewall package on this host).

## Why this shape

- **Python is the control plane, never the data plane.** Proxy traffic is handled
  exclusively by mature cores (Xray / sing-box). Python only generates configs,
  restarts services, runs health checks and serves the panel/API.
- **The working system Xray is untouched.** ProxyHub runs its own managed Xray
  instance with a generated config (`/var/lib/proxy-hub/generated/xray.json`).
  The original `/usr/local/etc/xray/config.json` + `xray.service` are never
  written by ProxyHub (a backup is taken at install time).
- **SQLite is enough.** Single VPS, low write volume, no replication need.
- **No Docker/K8s/Redis.** 2 CPU / 5.8 GB RAM: one uvicorn worker + in-process
  schedulers. No frontend build step (static vanilla JS served by FastAPI).

## Request flow (switching the active node)

1. Operator (or failover) picks node N.
2. `adapter_for(N)` selects `XrayAdapter` (vless/vmess/trojan/ss) or
   `SingBoxAdapter` (hysteria2).
3. Adapter writes generated config atomically (tmp + rename), runs
   `xray run -test` / `sing-box check`, then restarts the managed service.
4. DB marks N active; audit log records the switch.

## Schedulers (in-process asyncio)

- Subscription refresh every `sub_update_interval` (default 6h).
- Health sweep every `health_interval` (default 5min, min 60s):
  TCP dial latency for every enabled node + full egress test
  (`health_url`, default `https://api.ipify.org`) for the active node only.

## Routing model

Explicit per-application rules (`direct` | `proxy`), ordered, deterministic.
There is deliberately NO transparent proxy, NO NAT interception, NO gateway
change. Apps opt in via proxy env vars or their own settings (see ROUTING.md).
