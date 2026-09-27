# Routing engine (Phase 2)

Two SEPARATE systems — do not confuse them:

1. **Traffic rules** (Routing tab): decide DIRECT / PROXY / BLOCK for traffic
   arriving at the public gateway, based on destination. Compiled into the
   managed Xray config. This is the routing engine.
2. **Server-app rules** (Routing tab, lower card): opt-in `direct|proxy` hints
   per VPS service (OpenCode, MicroChess, …). A service applies them to ITSELF
   via proxy env vars. Xray never sees process names — a SOCKS gateway cannot
   identify processes on remote client devices.

## Rule model

`priority` (lower = earlier), `match_type`, `pattern`, optional `port`
constraint (`443` or `80-443`), optional `network` (`tcp`/`udp`/`any`),
`action`, `client_scope` (`all` or a device username), `enabled`.

Evaluation: `(priority ASC, id ASC)`, first match wins. No match → DEFAULT
route (configurable `direct|proxy|block`, current default `proxy`).

## Match semantics

| Type | Example | Matches |
|---|---|---|
| `domain_exact` | `telegram.org` | only `telegram.org` (case/trailing-dot normalized, IDN ok) |
| `domain_suffix` | `*.telegram.org` | `telegram.org` + all subdomains, nothing else |
| `domain_wildcard` | `*.google.*` | anchored glob, `*` = any chars |
| `ip` | `1.2.3.4` | that address exactly |
| `cidr` | `10.0.0.0/8` | prefix membership |
| `port` | `443`, `80-443` | destination port |

No unrestricted regex from users; wildcard compiles to an anchored Xray
`regexp:` rule. Anything else is rejected with a Persian message.

## Client-specific rules

Xray cannot match SOCKS-authenticated users in routing (verified empirically),
so a rule scoped to device `X` compiles with `inboundTag: gw-X`, served by a
dedicated SOCKS inbound (per-device port, same credentials). Same rule table —
no duplication. A device without dedicated rules uses the shared gateway port.

## Seeds (defaults)

Private ranges + localhost → DIRECT; `*.ir` → DIRECT; TCP/22 → DIRECT;
default → PROXY. Adjust in the UI; every change rebuilds + validates the
managed Xray config before restart.

## DNS behavior

- PROXY destinations: `socks5h` semantics — the client sends the hostname, the
  upstream exit resolves it (no VPS DNS leak for proxied domains).
- DIRECT destinations: resolved/exited from the VPS path (direct DNS).
- No VPS-wide DNS change; local/private DNS untouched. DNS routing stays
  consistent with traffic routing by construction (same rule table).
