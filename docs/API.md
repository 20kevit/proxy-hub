# API reference

Base: `http://127.0.0.1:8899`. Auth: session cookie + `x-csrf-token` header on
mutating calls. No secrets are ever returned (nodes omit `secret`; subscription
URL is masked).

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Liveness (no auth) |
| POST | `/api/auth/login` | `{username,password}` → sets cookies, returns `csrf` |
| POST | `/api/auth/logout` | Clear session |
| GET | `/api/auth/me` | Current user |
| GET | `/api/status` | Dashboard payload |
| GET | `/api/system` | Services, listeners, paths, recent audit events |
| GET | `/api/logs` | Pointer to journalctl |
| GET | `/api/nodes` | List nodes (sanitized) |
| PATCH | `/api/nodes/{id}` | `{name?,tags?,enabled?}` |
| POST | `/api/nodes/{id}/activate` | Validate + restart managed core |
| POST | `/api/nodes/{id}/test` | Health-check one node (egress test if active) |
| POST | `/api/nodes/{id}/restore` | Re-enable / un-remove |
| DELETE | `/api/nodes/{id}` | Local delete (refuses active node) |
| GET | `/api/subscription` | Masked status + counters |
| PUT | `/api/subscription` | `{url, update_interval?}` → writes env file |
| POST | `/api/subscription/update` | Run full pipeline now |
| GET/POST | `/api/routing` | List / create rules |
| PUT/DELETE | `/api/routing/{id}` | Update / delete rule |
| GET/PUT | `/api/settings` | Scheduler + failover knobs |

Errors: JSON `{detail}` with 400/401/403/404/409/429/502 as appropriate.
