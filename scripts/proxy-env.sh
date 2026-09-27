#!/usr/bin/env bash
# Opt-in proxy environment for a single application (explicit routing — never global).
# Usage: source scripts/proxy-env.sh   (or copy into a systemd drop-in EnvironmentFile)
echo "export HTTP_PROXY=socks5h://127.0.0.1:2081"
echo "export HTTPS_PROXY=socks5h://127.0.0.1:2081"
echo "export ALL_PROXY=socks5h://127.0.0.1:2081"
echo "export NO_PROXY=127.0.0.1,localhost"
