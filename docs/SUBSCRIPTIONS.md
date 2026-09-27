# Subscriptions

## Pipeline

```
fetch (HTTP, 30s timeout, size cap 5MB)
 -> decode (base64 blob | plain lines | per-line base64 | JSON w/ embedded URIs)
 -> parse (vless/vmess/trojan/ss/hysteria2; one bad line never aborts)
 -> normalize (ParsedNode: protocol/server/port/transport/security/params)
 -> dedupe (SHA-256 fingerprint over protocol+server+port+secret-hash+params)
 -> validate (port range, required fields, supported protocol)
 -> persist (add new, refresh last_seen, mark vanished as removed_upstream)
 -> health-check -> activate/failover
```

## Rules

- A node removed upstream is flagged `removed_upstream`, deactivated, and never
  auto-selected. It stays visible so the operator can inspect it.
- Locally deleted nodes may be re-imported by the next sync if still upstream
  (by design — the subscription is the source of truth; use “disable” for
  permanent local exclusion).
- Failed syncs keep the last-known-good set and record `last_error`.
- The secret URL is stored only in `/etc/proxy-hub/proxy-hub.env`, shown masked
  (`https://host/sub/****`), and never logged.

## Supported formats

- Whole-body base64 (the current provider uses double encoding: outer base64
  document, inner plain `vless://…` / `hysteria2://…` lines).
- Plain URI-per-line.
- Sing-box-style JSON (URIs extracted via regex).
- Clash-style YAML is NOT parsed (out of scope — document if the provider adds it).

## Configure

Panel: Subscription tab → set URL → “Update now”. Or edit
`/etc/proxy-hub/proxy-hub.env` and `systemctl restart proxy-hub`.
