# Troubleshooting

## Panel unreachable

```bash
systemctl is-active proxy-hub.service || systemctl start proxy-hub.service
journalctl -u proxy-hub -n 50 --no-pager
ss -tlnp | grep 8899   # must show 127.0.0.1:8899
```

## Login fails

- Wrong password → `scripts/set-admin.sh` to reset.
- 429 “too many attempts” → wait 5 min (rate limiter per IP).
- 403 on actions → CSRF cookie missing; hard-refresh the page.

## Subscription update fails

- Check error on Subscription tab (`last_error`).
- Common: provider timeout → retry; interval/backoff is automatic.
- Last-known-good nodes keep working; nothing is deleted on fetch failure.
- Verify manually WITHOUT storing the secret anywhere:
  `curl -s -m 30 -A ProxyHub/1.0 "$URL" | head -c 200` (shell history: prefix with space).

## Active node unhealthy

- Nodes tab shows `last_error` (e.g. timeout → provider-side or filtered).
- Auto failover switches after `threshold` consecutive failures + cooldown.
- If ALL nodes fail: check VPS egress (`curl https://api.ipify.org` direct),
  then check provider status.

## Managed Xray won't start

```bash
/usr/local/bin/xray run -test -config /var/lib/proxy-hub/generated/xray.json
journalctl -u proxyhub-xray -n 50 --no-pager
```

- System `xray.service` is independent; if the managed one breaks, `2080/1080`
  keep working.

## System Xray broken (should not happen via ProxyHub)

```bash
cp /etc/proxy-hub/xray-system-orig.json /usr/local/etc/xray/config.json
/usr/local/bin/xray run -test -config /usr/local/etc/xray/config.json
systemctl restart xray.service
curl --proxy socks5h://127.0.0.1:2080 -4 --connect-timeout 15 https://api.ipify.org
```

## No global proxy leaked?

```bash
iptables -t nat -L -n   # empty
env | grep -i proxy     # empty in your shell unless you opted in
```
