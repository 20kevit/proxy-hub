# Proxy cores

## Xray (primary) — `XrayAdapter`

- Binary: `/usr/local/bin/xray` (v26.3.27, pre-existing).
- Managed unit: `proxyhub-xray.service`, config
  `/var/lib/proxy-hub/generated/xray.json` (generated, localhost inbounds
  2081 socks / 1081 http, single active outbound + direct + block).
- Handles: `vless` (tcp/reality/tls/ws/grpc), `vmess`, `trojan`, `ss`.
- Every write is validated (`xray run -test`) before restart; the unit also has
  `ExecStartPre` validation. The system `xray.service` is never referenced.
- Connection limits: single active outbound at a time = one upstream connection
  per protocol session. No concurrency tricks; respects provider device limits.

## Managed instance layout (Phase 2)

The generated `/var/lib/proxy-hub/generated/xray.json` (0600) now contains:

- `socks-in`/`http-in` (127.0.0.1:2081/1081, no auth) — local tooling.
- `gw-socks`/`gw-http` (bind + 8388/8389, `auth: password`) — public gateway,
  accounts = currently-usable clients only. Present only when the gateway is
  enabled AND at least one usable client exists (no anonymous listener, ever).
- `gw-<username>` dedicated SOCKS inbounds — only for devices with
  client-scoped rules (per-device port).
- Routing: private ranges DIRECT → user traffic rules (compiled) → catch-all
  = configured DEFAULT route.

Every regeneration is validated (`xray run -test`) before restart. If the
gateway is enabled, activating a Hysteria2 node is refused with a clear error
(the gateway is Xray-based); disable the gateway first or use an
Xray-capable node.

## sing-box (optional, Hysteria2 only) — `SingBoxAdapter`

- Xray cannot reliably terminate Hysteria2, so hy2 nodes need sing-box.
- Disabled by default (`PROXYHUB_SINGBOX_ENABLED=false`,
  `proxyhub-singbox.service` not enabled).
- Install only if you need the hy2 node (official upstream .deb):

```bash
VERSION=1.12.9  # check https://github.com/SagerNet/sing-box/releases
curl -fsSLO "https://github.com/SagerNet/sing-box/releases/download/v${VERSION}/sing-box_${VERSION}_linux_amd64.deb"
dpkg -i "sing-box_${VERSION}_linux_amd64.deb"
cp /opt/projects/proxy-hub/systemd/proxyhub-singbox.service /etc/systemd/system/
systemctl daemon-reload
```

- Activating a `hysteria2` node without sing-box fails with a clear message
  pointing at the System page (no silent fallback).
- Health semantics: Hysteria2 egress is verified through an isolated
  ephemeral sing-box instance (never through the active proxy). While
  sing-box is not installed, hy2 checks report `unverified` — this is not
  evidence of health, so such nodes stay ineligible for failover/rotation
  and their counters are left untouched. Recovery path: install sing-box;
  the next deep cycle verifies the node genuinely and it becomes eligible
  again (no manual reset needed; expired circuit breakers reopen
  automatically).

## Public client listener (laptop/phone — explicit operator step)

Defaults are localhost-only (no open proxy). To expose later:

1. Pick a free port (checked via `ss -tlnp`), e.g. 8388.
2. Add an authenticated inbound (SOCKS user/pass) to the managed config
   template, bound to that port.
3. Open ONLY that port in the firewall, restart the managed core, verify auth
   rejects anonymous clients.
4. Until then: SSH dynamic tunnel (`ssh -p 9011 -D 1080 user@host`) or VPN.
