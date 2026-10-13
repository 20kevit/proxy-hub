"""Shared API dependencies: DB session, auth, CSRF."""
from __future__ import annotations

from fastapi import Cookie, HTTPException, Request, Response
from sqlalchemy.orm import Session

from ..db import get_session_factory
from ..services.auth import new_csrf, parse_session


def db_session() -> Session:
    return get_session_factory()()


def current_user(request: Request, ph_session: str | None = Cookie(default=None)) -> str:
    from ..services.auth import parse_session_verified

    token = ph_session or ""
    if not token:
        raise HTTPException(status_code=401, detail="unauthenticated")
    db = db_session()
    try:
        user = parse_session_verified(token, db)
    finally:
        db.close()
    if not user:
        raise HTTPException(status_code=401, detail="unauthenticated")
    return user


def check_csrf(request: Request) -> None:
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    token = request.headers.get("x-csrf-token", "")
    expected = request.cookies.get("ph_csrf", "")
    if not token or not expected or token != expected:
        raise HTTPException(status_code=403, detail="bad csrf token")


def set_auth_cookies(response: Response, username: str, secure: bool = False,
                     session_version: int = 0) -> str:
    from ..services.auth import create_session

    token = create_session(username, session_version=session_version)
    response.set_cookie(
        "ph_session", token, httponly=True, samesite="lax", secure=secure,
        path="/", max_age=12 * 3600,
    )
    csrf = new_csrf()
    response.set_cookie("ph_csrf", csrf, samesite="lax", secure=secure, path="/", max_age=12 * 3600)
    return csrf
