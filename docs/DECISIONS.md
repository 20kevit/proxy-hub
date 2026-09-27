# Decisions (ADRs, short form)

1. **Separate managed Xray instance** instead of rewriting the working system
   config. Cost: one extra lightweight process. Benefit: zero risk to the
   proven `2080/1080` path OpenCode relies on.
2. **Xray primary, sing-box only for Hysteria2.** Current subscription:
   22× VLESS, 1× hy2. Installing a second core unconditionally would add
   complexity for one node; sing-box stays optional + documented.
3. **SQLite, no Postgres/Redis.** Single-node control plane, tiny write load.
4. **In-process schedulers** instead of systemd timers for sub/health loops:
   fewer units, shared DB session handling, still visible via journald.
5. **PBKDF2 (stdlib) instead of bcrypt/passlib.** One less native dependency,
   still a modern KDF at 210k rounds.
6. **Vanilla-JS frontend, no build.** Panel must work without Node in
   production; RTL Persian UI in three small files.
7. **TCP-dial health for all + egress probe for active only.** Full proxy
   handshake per node per sweep would be slow and noisy; TCP latency gives
   ranking, egress probe gives truth for the node that matters.
8. **Single active outbound (no multiplexing tricks).** One upstream connection
   at a time — respects the provider's concurrent-connection terms by design.
9. **Panel binds localhost; Nginx termination later.** No direct Internet
   exposure of the FastAPI server, ever.
