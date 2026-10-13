"""Gateway client credentials: generation, verification, rotation.

Proxy passwords are hashed (PBKDF2) for verification. Because Xray SOCKS/HTTP
inbounds require the cleartext password in their (0600, runtime-only) config,
we ALSO store a Fernet-encrypted copy (key derived from PROXYHUB_SECRET_KEY)
so the control plane can regenerate core configs. The cleartext password is
returned ONLY at creation/rotation time; the UI shows it once.
"""
from __future__ import annotations

import base64
import hashlib
import re
import secrets
import string
from datetime import datetime, timezone

from .auth import hash_password, verify_password

try:
    from cryptography.fernet import Fernet, InvalidToken

    _HAS_CRYPTO = True
except ImportError:  # pragma: no cover
    _HAS_CRYPTO = False

USERNAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,62}$")
_PW_ALPHABET = "".join(
    c for c in (string.ascii_letters + string.digits) if c not in "0O1l"
)


def generate_username(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()) or "device"
    base = base.strip("_")[:40] or "device"
    return f"{base}_{secrets.token_hex(2)}"


def generate_password(length: int = 20) -> str:
    return "".join(secrets.choice(_PW_ALPHABET) for _ in range(length))


def validate_username(username: str) -> str:
    u = username.strip().lower()
    if not USERNAME_RE.match(u):
        raise ValueError("نام کاربری باید لاتین، کوتاه و بدون فاصله باشد (مثال: laptop_x7).")
    return u


def client_usable(client) -> tuple[bool, str]:
    if client is None:
        return False, "unknown user"
    if not client.enabled:
        return False, "disabled"
    if client.expires_at is not None and client.expires_at <= datetime.now(timezone.utc):
        return False, "expired"
    return True, ""


def verify_client_password(client, password: str) -> bool:
    try:
        return verify_password(password, client.password_hash)
    except Exception:
        return False


def touch_last_used(session, client) -> None:
    client.last_used_at = datetime.now(timezone.utc)
    session.commit()


def make_password_hash(password: str) -> str:
    if len(password) < 12:
        raise ValueError("گذرواژه باید حداقل ۱۲ کاراکتر باشد.")
    return hash_password(password)


def _fernet() -> "Fernet":
    import os as _os

    from ..config import get_settings

    if not _HAS_CRYPTO:
        raise RuntimeError("cryptography package is required for client passwords")
    # Fail fast when no stable key is configured: deriving from an ephemeral
    # process key would seal passwords that no future process can open.
    # The installer provisions PROXYHUB_SECRET_KEY; key rotation (re-encryption
    # of stored passwords) is NOT supported — treat the key as part of backups.
    configured = (_os.getenv("PROXYHUB_SECRET_KEY", "") or "").strip()
    if not configured:
        raise RuntimeError(
            "client credential store unavailable: PROXYHUB_SECRET_KEY is not "
            "configured (see installer; restore it from backup, do not rotate "
            "it without re-creating client passwords)")
    raw = hashlib.sha256(
        b"proxyhub-client-key-v1|" + get_settings().secret_key.encode()
    ).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def seal_password(password: str) -> str:
    return _fernet().encrypt(password.encode()).decode()


def open_password(sealed: str) -> str:
    try:
        return _fernet().decrypt(sealed.encode()).decode()
    except Exception as exc:
        raise RuntimeError("cannot decrypt client password") from exc
