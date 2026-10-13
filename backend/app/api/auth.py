"""Auth endpoints: login/logout/me with rate limiting and audit."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ..config import get_settings
from ..models import AuditLog, User
from ..services.auth import hash_password, rate_limited, verify_password
from .deps import current_user, db_session, set_auth_cookies

router = APIRouter()


def _audit(db, actor: str, action: str, detail: str = "") -> None:
    db.add(AuditLog(actor=actor, action=action, detail=detail))
    db.commit()


@router.post("/api/auth/login")
def login(payload: dict, request: Request, response: Response):
    ip = request.client.host if request.client else "unknown"
    if rate_limited(ip):
        _audit(db_session(), ip, "login.rate_limited")
        from fastapi import HTTPException
        raise HTTPException(status_code=429, detail="too many attempts")
    db = db_session()
    try:
        username = str(payload.get("username", "")).strip()[:64]
        password = str(payload.get("password", ""))[:256]
        user = db.query(User).filter(User.username == username).first()
        if user is None or not verify_password(password, user.password_hash):
            _audit(db, username or ip, "login.failed")
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="invalid credentials")
        fwd = request.headers.get("x-forwarded-proto", "")
        is_https = request.url.scheme == "https" or fwd.split(",")[0].strip().lower() == "https"
        csrf = set_auth_cookies(response, username, secure=is_https)
        _audit(db, username, "login.ok")
        return {"ok": True, "csrf": csrf, "username": username}
    finally:
        db.close()


@router.post("/api/auth/logout")
def logout(response: Response, user: str = Depends(current_user)):
    response.delete_cookie("ph_session", path="/")
    db = db_session()
    try:
        _audit(db, user, "logout")
    finally:
        db.close()
    return {"ok": True}


@router.get("/api/auth/me")
def me(user: str = Depends(current_user)):
    from fastapi import Request as Req

    return {"username": user}
