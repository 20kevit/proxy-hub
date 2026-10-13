# Security model

## Panel authentication

- Username + password, PBKDF2-HMAC-SHA256 (210k rounds).
- Sessions: HMAC-signed tokens in `httpOnly`, `SameSite=Lax` cookies (12h TTL).
- CSRF: double-submit cookie (`ph_csrf` + `x-csrf-token` header), enforced by
  middleware on all mutating `/api/*` calls except login.
- Login rate limiting: 10 attempts / 5 min per IP (429 afterwards).
- No default weak credentials: install requires setting `PROXYHUB_ADMIN_PASSWORD`;
  without it no user exists and login is impossible.

## Proxy endpoint authentication

- Managed listeners bind **127.0.0.1 only** by default — no open proxy.
- Public client access (laptop/phone) is an explicit operator step:
  choose a free port, bind with Xray user/pass auth, open only that port.
  Until then, remote devices connect via SSH tunnel or VPN, never directly.
- Strong generated credentials required; see PROXY_CORES.md “public listener”.

## Secret handling

- Subscription URL, node UUIDs/passwords/keys live in `/etc/proxy-hub/` and
  `/var/lib/proxy-hub/` — never in Git, never in the frontend bundle,
  never in logs, never in API responses (nodes serialize without `secret`;
  subscription URL is returned masked only).
- `.gitignore` excludes `.env`, `*.db`, backups, venvs.
- Audit log records WHO did WHAT (login, activate, routing, settings,
  failover) without secret values.

## Proxy client credentials (Phase 2)

- Separate from the admin login. One username/password per device.
- Stored as PBKDF2 hash (verification) + Fernet-encrypted copy (required to
  render Xray inbound accounts; key derived from `PROXYHUB_SECRET_KEY`).
- Cleartext is returned by the API exactly once (create/rotate) and never
  listed, logged, or audited.
- Disabled/expired/unknown users are rejected by the core (config contains
  only currently-usable accounts; verified live: wrong password and no-auth
  both fail closed).
- Per-client `max_connections` is stored as policy; hard per-user connection
  caps are NOT natively enforced by Xray — documented limitation (abuse is
  contained via disable/rotate/expire + conservative timeouts).

## Admin sessions and revocation

- Sessions are HMAC-signed tokens (username · expiry · nonce · version) with
  a 12h TTL, `HttpOnly` + `SameSite=lax` cookies (`Secure` on HTTPS), and
  CSRF tokens on all mutating API calls.
- Logout and admin password changes bump a server-side `session_version`,
  so previously issued tokens are rejected even if still unexpired or
  replayed — verified by regression tests (logout, password-change,
  forged-version, expiry, CSRF, rate limiting).
- A process restart with an unchanged `PROXYHUB_SECRET_KEY` keeps existing
  sessions valid; changing the secret key invalidates all sessions
  (fail-closed: re-login required).

## Encryption-key lifecycle (`PROXYHUB_SECRET_KEY`)

- Client passwords are stored PBKDF2-hashed (verification) plus
  Fernet-encrypted (needed to render Xray inbound accounts), with the key
  derived from `PROXYHUB_SECRET_KEY`. The installer provisions a stable
  32-byte key into `/etc/proxy-hub/proxy-hub.env` (0600/640 root:proxyhub).
- The application refuses to seal/open client passwords when no stable key
  is configured instead of silently using an ephemeral process key (which
  would create passwords no future process can open).
- Backup the env file alongside the database (both 0600, never in Git).
  Key loss = stored client passwords become unopenable; recovery is
  re-creating/rotating device passwords, not decryption.
- Key rotation (re-encryption) is NOT implemented: rotating the key without
  re-creating passwords breaks `open_password`. Documented limitation —
  fail safely, do not rotate silently.

## Threat model notes

- The gateway listener (`0.0.0.0:8388/8389`) is authenticated-only; the admin
  API (8899) stays localhost-only and is never served from a proxy listener.
- No firewall package is active on this VPS (no UFW, empty iptables, no nft);
  exposure is controlled SOLELY by the gateway bind address. `127.0.0.1` =
  local verification only; `0.0.0.0` = public. Changing it requires explicit
  confirmation in the Settings UI and is audit-logged.

## Network posture

- No transparent proxy, no NAT rules, no gateway changes (verified: `iptables -t nat -L` empty).
- API never exposed directly; use Nginx reverse-proxy + HTTPS for remote panel access.
- SSH (9011), Nginx, MicroChess, FIDE-Service are out of scope and untouched.
