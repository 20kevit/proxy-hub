"""Authentication: PBKDF2 password hashing, signed sessions, CSRF, login rate limiting."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from ..config import get_settings

SESSION_TTL = 12 * 3600
PBKDF2_ROUNDS = 210_000

_attempts: dict[str, deque] = defaultdict(deque)


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ROUNDS)
    return f"pbkdf2${PBKDF2_ROUNDS}${salt}${dk.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        _, rounds, salt, digest = encoded.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds))
        return hmac.compare_digest(dk.hex(), digest)
    except Exception:
        return False


def _sign(data: str) -> str:
    key = get_settings().secret_key.encode()
    return hmac.new(key, data.encode(), hashlib.sha256).hexdigest()


def create_session(username: str, session_version: int = 0) -> str:
    expiry = int(time.time()) + SESSION_TTL
    nonce = secrets.token_hex(12)
    payload = f"{username}.{expiry}.{nonce}.{int(session_version)}"
    return f"{payload}.{_sign(payload)}"


def _split_session(token: str) -> tuple[str, int, int] | None:
    """Return (username, expiry, version) after verifying signature and expiry.

    Accepts legacy 4-part tokens (treated as version 0) and current 5-part
    tokens. Returns None on any failure. Never raises.
    """
    try:
        parts = token.split(".")
        if len(parts) == 5:
            username, expiry, nonce, version, sig = parts
            payload = f"{username}.{expiry}.{nonce}.{version}"
            version_i = int(version)
        elif len(parts) == 4:
            username, expiry, nonce, sig = parts
            payload = f"{username}.{expiry}.{nonce}"
            version_i = 0
        else:
            return None
        if not hmac.compare_digest(_sign(payload), sig):
            return None
        if int(expiry) < time.time():
            return None
        return username, int(expiry), version_i
    except Exception:
        return None


def parse_session(token: str) -> str | None:
    """Stateless check (signature + expiry only). Prefer parse_session_verified."""
    parsed = _split_session(token)
    return parsed[0] if parsed else None


def parse_session_verified(token: str, session) -> str | None:
    """Full check: signature + expiry + server-side session version.

    Returns the username only when the token's version matches the user's
    current session_version in the database (logout / password change bump it).
    """
    from ..models import User

    parsed = _split_session(token)
    if not parsed:
        return None
    username, _expiry, version = parsed
    try:
        row = session.query(User).filter(User.username == username).first()
    except Exception:
        return None
    if row is None:
        return None
    if int(row.session_version or 0) != int(version):
        return None
    return username


def bump_session_version(session, username: str) -> int:
    """Revoke all existing tokens for a user. Returns the new version."""
    from ..models import User

    row = session.query(User).filter(User.username == username).first()
    if row is None:
        return 0
    row.session_version = int(row.session_version or 0) + 1
    session.commit()
    return row.session_version


def new_csrf() -> str:
    return secrets.token_hex(16)


def rate_limited(ip: str, limit: int = 10, window: int = 300) -> bool:
    now = time.time()
    dq = _attempts[ip]
    while dq and now - dq[0] > window:
        dq.popleft()
    if len(dq) >= limit:
        return True
    dq.append(now)
    return False
