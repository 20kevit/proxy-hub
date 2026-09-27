# Routing

ProxyHub does **explicit routing only**. There is no TProxy/NAT interception, no
default-route change, no `iptables -t nat` rules. Verified clean:

```bash
iptables -t nat -L -n   # must stay empty
ip route                # default via 194.60.231.1 dev eth0 (unchanged)
```

## Rule model

Ordered `app -> direct | proxy` rows (Routing tab). Deterministic: lowest
`position` first. Defaults:

| App | Route | Rationale |
|---|---|---|
| opencode | proxy | AI assistant needs global egress |
| chrome | proxy | browser via managed proxy |
| telegram | proxy | client via managed proxy |
| microchess | direct | production app, never proxied |
| fide-service | direct | production app, never proxied |
| ssh | direct | never break admin access |
| nginx | direct | never proxy the web server |
| apt | direct | system packages stay direct |
| git | direct | unless explicitly overridden |

## How an app opts in (example: OpenCode)

OpenCode already uses env proxy vars via a systemd override
(`/etc/systemd/system/opencode.service.d/override.conf` pointing at the system
Xray `127.0.0.1:2080`). To move it to the managed core later (safe, reversible):

```ini
[Service]
Environment="HTTP_PROXY=socks5h://127.0.0.1:2081"
Environment="HTTPS_PROXY=socks5h://127.0.0.1:2081"
Environment="ALL_PROXY=socks5h://127.0.0.1:2081"
Environment="NO_PROXY=127.0.0.1,localhost,194.60.231.172"
```

```bash
systemctl daemon-reload && systemctl restart opencode.service
```

The API also exposes `GET /api/routing` → `proxy_env` snippet for tooling.
Changing a rule in the panel never touches the app itself; the operator applies
the snippet per app. Nothing is migrated automatically — preserving working
services wins over elegance.
