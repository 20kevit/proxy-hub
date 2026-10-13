#!/usr/bin/env bash
# Create/update the ProxyHub admin user. Password via env or prompt (never as CLI arg in history).
set -euo pipefail
REPO=/opt/projects/proxy-hub
cd "$REPO/backend"
USER_NAME="${1:-${PROXYHUB_ADMIN_USER:-admin}}"
if [ -z "${PROXYHUB_ADMIN_PASSWORD:-}" ]; then
  read -rsp "Password for ${USER_NAME}: " PROXYHUB_ADMIN_PASSWORD; echo
fi
PROXYHUB_ADMIN_USER="$USER_NAME" PROXYHUB_ADMIN_PASSWORD="$PROXYHUB_ADMIN_PASSWORD" \
  "$REPO/.venv/bin/python" - <<'EOF'
import os
from app.config import get_settings
from app.db import get_session_factory, init_db
from app.models import User
from app.services.auth import bump_session_version, hash_password

init_db()
db = get_session_factory()()
try:
    u = db.query(User).filter(User.username == os.environ["PROXYHUB_ADMIN_USER"]).first()
    if u is None:
        db.add(User(username=os.environ["PROXYHUB_ADMIN_USER"],
                    password_hash=hash_password(os.environ["PROXYHUB_ADMIN_PASSWORD"])))
        print("admin created")
    else:
        u.password_hash = hash_password(os.environ["PROXYHUB_ADMIN_PASSWORD"])
        # Revoke all existing sessions issued before this password change.
        u.session_version = int(u.session_version or 0) + 1
        print("admin password updated (sessions revoked)")
    db.commit()
finally:
    db.close()
EOF
