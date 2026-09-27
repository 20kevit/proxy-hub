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


def create_session(username: str) -> str:
    expiry = int(time.time()) + SESSION_TTL
    nonce = secrets.token_hex(12)
    payload = f"{username}.{expiry}.{nonce}"
    return f"{payload}.{_sign(payload)}"


def parse_session(token: str) -> str | None:
    try:
        username, expiry, nonce, sig = token.split(".")
        payload = f"{username}.{expiry}.{nonce}"
        if not hmac.compare_digest(_sign(payload), sig):
            return None
        if int(expiry) < time.time():
            return None
        return username
    except Exception:
        return None


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
