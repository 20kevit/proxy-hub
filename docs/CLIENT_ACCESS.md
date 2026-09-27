# Client access (devices: Windows, Android, iOS, macOS, Linux)

The public gateway speaks standard **SOCKS5** (port 8388) and **HTTP CONNECT**
(port 8389). Any client with standard proxy settings works — no custom app needed.

## Connection parameters

- Server: `194.60.231.172`
- SOCKS5 port: `8388` · HTTP port: `8389`
- Username/password: per-device credentials created in the panel
  (Devices & Access → + دستگاه جدید). The password is shown ONCE — copy it then.
- QR button shows a `socks5://user:pass@host:port` code for clients that import it.

Never share one credential across devices — create one client per device so you
can disable/rotate each independently.

## Windows 11

Settings → Network & internet → Proxy → Manual proxy setup → Use a proxy server
On. Address `194.60.231.172`, port `8389` (HTTP). Windows prompts for username +
password on first use. (For SOCKS-only apps, configure inside the app with port
8388 and SOCKS5 type.)

## Android

Wi-Fi → long-press network → Modify → Advanced → Proxy: Manual.
Hostname `194.60.231.172`, port `8389`. Android 12+ may not ask for proxy auth
on all builds — if auth fails, use the app-level SOCKS5 setting (e.g. in
Telegram: Settings → Data and Storage → Proxy → Add Proxy → SOCKS5,
server/port/username/password).

## iPhone / iPad

Wi-Fi → (i) on the network → Configure Proxy → Manual. Server
`194.60.231.172`, port `8389`, Authentication On with the device credentials.

## macOS

System Settings → Wi-Fi → Details… → Proxies. Enable “SOCKS proxy”
(`194.60.231.172:8388`) and/or “Web proxy (HTTP)” (`:8389`) with username +
password. Or per-app (Telegram, browsers) proxy settings.

## Linux

GNOME: Settings → Network → Proxy → Manual (SOCKS `…:8388`, HTTP `…:8389`).
Terminal-only: `export HTTPS_PROXY=http://USER:PASS@194.60.231.172:8389` etc.
(Per-session only — never system-wide on the VPS itself.)

## Behavior notes

- `*.ir` and private ranges go DIRECT (VPS egress) by default; everything else
  goes through the active upstream node (currently exits `91.107.187.175`).
- Use the panel’s Routing → Test to preview any domain/IP/port decision.
- If a device stops working: check Devices (disabled? expired?) and Health.
