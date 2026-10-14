# UX decisions — panel redesign (`feature/ui-ux-redesign`)

Baseline: `51d5939`. Frontend-only redesign; no backend API contract changed.
All 12 functional areas are preserved, regrouped into 4 navigation groups.

## 1. Information architecture

12 equally-weighted tabs → sidebar with 4 labeled groups:

- **عملیات**: داشبورد، نودها، سلامت، چرخه حیات
- **دسترسی**: دستگاه‌ها و دسترسی، اشتراک، مسیریابی
- **دیده‌بانی**: تلمتری، اتصالات، گزارش‌ها
- **سامانه**: سیستم، تنظیمات

Rationale: matches real workflows (operate → grant access → observe → configure).
No section removed. Deep-linkable hash routes (`#/nodes` …) with `aria-current`.
Mobile (<900px): sidebar becomes an RTL drawer + scrim; topbar keeps brand,
core chip (hidden <560px), theme toggle, logout.

## 2. Dashboard: honest ops overview (was: 9 decorative stat cards)

- Service state comes first: managed-core `active/state` drives a topbar chip
  and a warning banner. "Process running" vs "end-to-end egress verified" are
  shown as two separate facts (`egress_ok` from a real HTTPS probe).
- Removed vague cards (CPU/RAM %, bare system-xray dot, «به‌روز/نامشخص»).
  Kept: managed core, active node + protocol badge, egress verification,
  live exit IP, healthy/total, subscription age, gateway.
- New: warning list (managed core down, no active node, egress unverified,
  subscription error, unhealthy count) + recent activity from the previously
  unused `GET /api/report/summary` (transitions + deep success rate).
- Quick actions retained, now busy-guarded; shortcuts are real links.

## 3. Nodes

- Split the ambiguous single filter (protocol values and health values shared
  one `<select>`, so `vless`/`HEALTHY` collided) into protocol + health selects
  covering all protocols (vless/vmess/trojan/ss/hysteria2) and states
  (+DISABLED, +active-only). Result count shown.
- Protocol badges for every protocol; per-node `<details>` with quality,
  probe counters, circuit, rank (progressive disclosure, was: one dense line).
- Hysteria2: warning chip + activation guard when `/api/system` reports
  `singbox.installed === false`. The UI never implies hy2 works without the
  binary (backend parses/generates, but cannot verify/activate — truthful).
- Added the missing delete + restore actions (backend already supported
  `DELETE /api/nodes/{id}` and `POST …/restore`; UI had no buttons).
  Delete is hidden for the active node (server also rejects it).
- Disable-active now confirms with an explicit outage warning (backend sets
  `is_active=False` with no replacement; UI at least makes it deliberate).
- Activate always confirms; test is busy-guarded.

## 4. Routing / devices / subscription / lifecycle

- Routing: default route is now an inline select + save (was: `prompt()` with
  no validation). Rule dialog validates pattern/port inline; server errors
  still surface in-dialog. App-rules kept under progressive disclosure.
- Devices: added edit (name/notes/expiry) via the existing dialog — backend
  `PATCH` already supported it but the UI offered only toggle/delete.
  `max_connections` stays out of create/edit flows with an explanatory note:
  the backend returns `max_connections_enforced: false` (Xray has no per-user
  cap) — presenting it as a limit would be dishonest.
- Subscription: URL field requires `https://` inline; masked URL, attempt
  timestamps, full stats (added updated/unsupported) shown.
- Lifecycle: renders **both** `last_rotation_at` (with relative age) **and**
  `last_rotation_reason` (was: only `at`; `reason` added by the security fix
  was never displayed). Fixed a null-crash (`$("lifecycleRotate")` referenced
  an element that does not exist, throwing inside the try block so rotation
  history never rendered) — history renders again.

## 5. Telemetry / connections / logs

- Telemetry: canvas charts now have axes, max/0 labels, bucket time labels,
  `role="img"` labels, empty states; table capped at 100 rows with a note
  (was: unbounded-feeling render, `drawBars` with no labels).
- Connections: fixed a column mismatch — the header said
  زمان/کلاینت/مقصد/پورت/inbound/outbound but rows rendered
  `dest:port` merged + an `undefined` status cell (API has no `status` field).
  Now 6 aligned columns matching `{at,client,dest,port,inbound,outbound}`;
  gateway listeners (8388/8389) shown in addition to local ones.
- Logs: standardized empty state; filter list unchanged.

## 6. System / settings

- System: new "core capabilities" card renders `sing-box installed/missing`
  with the hy2 consequence spelled out (the canonical truthful-capability
  signal — no new endpoint needed).
- Settings: rotation group added (enabled/max-duration/exclusion/check
  interval/dry-run) — `PUT /api/settings` already accepted these keys, the UI
  just never exposed them. Gateway 0.0.0.0 confirmation retained.

## 7. Design system

- CSS tokens for dark (default) + light (`[data-theme]`), semantic status
  colors always paired with text/dots (never color-alone).
- Reusable `.btn` (primary/ghost/small/danger, busy state), `.badge`
  (per-protocol), `.chip`, `.alert` (info/ok/warn/bad), `.empty`,
  `.skel` skeletons, sticky table headers, scrollable dialogs (88vh).
- Font: `Vazirmatn` first **if installed locally**, else system stack
  (Tahoma/Segoe UI). No webfont fetch — basic readability never depends on
  an external font service (verified: no `@font-face`/`<link>` to a font CDN).
- `prefers-reduced-motion` disables shimmer/smooth scroll. `:focus-visible`
  outlines preserved. Toasts are `aria-live="polite"`; form/dialog errors use
  `role="alert"`. Technical strings keep `dir="ltr"` + `overflow-wrap`.

## 8. Interaction quality

- `refreshAll` is parallel (`Promise.allSettled`) with per-section error +
  retry; previously sequential `await` chain where one failure aborted all
  remaining sections and left them blank.
- All mutating buttons use the `busy()` guard (duplicate-submit prevention);
  success is only shown after server confirmation.
- 401 anywhere → login view + expiry message. No fake/demo data in views.
- Destructive/connectivity-impacting acts (activate, disable-active, delete
  node/client, rotate password, rule/app-route delete, gateway→0.0.0.0,
  manual rotation) confirm via dialog, not bare `confirm()`, except rename
  which keeps `prompt()` (harmless, non-destructive).

## 9. Competitor patterns adopted (interaction patterns, not copies)

Verified `MHSanaei/3x-ui` repo/README availability (48k★): per-client
expiry/QR/share-link management, traffic stats with reset, routing rule
editor with browsable categories, dark/light themes, فارسی localization.
Adopted where they fit this single-admin product: device cards with
expiry/QR/rotate, rule table with order/priority/simulation, capability
honesty (3x-ui documents per-feature limits), theme toggle. Rejected as
out-of-scope: multi-user accounts, inbound cloning, Telegram bots, PWA,
PostgreSQL option, traffic quotas as enforced limits (our backend has no
per-user accounting — same honesty rule as `max_connections`).

NekoRay/Happ/Hiddify-app/Hiddify-Manager/Marzban were considered from the
mission brief for status-at-a-glance, empty/loading states, and safe
destructive-action patterns; no live-app inspection is claimed (no screenshots
taken from them; only this repo's UI was rendered — see verification).

## 10. Known limitations / deliberately excluded backend work

- `max_connections`: still unenforced (backend limitation, documented in UI).
- Hysteria2: still not executable here (no `sing-box` binary; not installed
  per mission rules). UI warns; no backend change.
- Node secrets at rest remain plaintext; ephemeral `secret_key` fallback
  remains (see post-deployment audit) — backend work, out of scope.
- QR load failures are handled via `addEventListener` (no inline handlers,
  CSP-safe under `script-src 'self'`).
- No frontend framework introduced; no new dependencies (zero).
