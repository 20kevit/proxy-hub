"""Gateway client management. Cleartext passwords are returned ONLY on create/rotate."""
from __future__ import annotations

import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response

from ..config import get_settings
from ..models import AuditLog, ProxyClient
from ..services.clients import (
    client_usable,
    generate_password,
    generate_username,
    make_password_hash,
    open_password,
    seal_password,
    validate_username,
)
from ..services.cores import rebuild_gateway
from .deps import current_user, db_session

router = APIRouter()


def _audit(db, actor: str, action: str, detail: str = "") -> None:
    db.add(AuditLog(actor=actor, action=action, detail=detail))
    db.commit()


def _public_client(c: ProxyClient) -> dict:
    usable, why = client_usable(c)
    return {
        "id": c.id, "name": c.name, "username": c.username,
        "enabled": c.enabled, "usable": usable, "status_detail": why,
        "expires_at": c.expires_at.isoformat() if c.expires_at else None,
        "dedicated_port": c.dedicated_port,
        "max_connections": c.max_connections,
        # Xray SOCKS inbounds expose no per-user connection cap, so this
        # value is administrative metadata only and is NOT enforced.
        "max_connections_enforced": False,
        "notes": c.notes or "",
        "last_used_at": c.last_used_at.isoformat() if c.last_used_at else None,
        "created_at": c.created_at.isoformat() if c.created_at else None,
    }


def _parse_expiry(value: str | None):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        raise HTTPException(400, "قالب تاریخ انقضا نامعتبر است.")


@router.get("/api/clients")
def list_clients(user: str = Depends(current_user)):
    db = db_session()
    try:
        return [_public_client(c) for c in db.query(ProxyClient).order_by(ProxyClient.id).all()]
    finally:
        db.close()


@router.post("/api/clients")
def create_client(payload: dict, user: str = Depends(current_user)):
    from ..schemas import ClientIn

    db = db_session()
    try:
        try:
            data = ClientIn(**payload)
        except Exception as exc:
            raise HTTPException(400, str(exc)[:200])
        username = validate_username(data.username or generate_username(data.name))
        if db.query(ProxyClient).filter(ProxyClient.username == username).first():
            raise HTTPException(409, "این نام کاربری قبلاً استفاده شده است.")
        password = data.password or generate_password()
        try:
            pwd_hash = make_password_hash(password)
            sealed = seal_password(password)
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(400, str(exc)[:200])
        client = ProxyClient(
            name=data.name[:128], username=username, password_hash=pwd_hash,
            password_enc=sealed, expires_at=_parse_expiry(data.expires_at),
            dedicated_port=data.dedicated_port,
            max_connections=max(1, min(data.max_connections, 100)),
            notes=(data.notes or "")[:512],
        )
        db.add(client)
        db.commit()
        _audit(db, user, "client.create", username)
        try:
            rebuild_gateway(db)
        except Exception as exc:
            raise HTTPException(502, f"کلاینت ساخته شد اما بازسازی gateway ناموفق بود: {exc}")
        out = _public_client(client)
        out["password_once"] = password  # shown exactly once
        return out
    finally:
        db.close()


@router.patch("/api/clients/{client_id}")
def update_client(client_id: int, payload: dict, user: str = Depends(current_user)):
    from ..schemas import ClientUpdate

    db = db_session()
    try:
        client = db.query(ProxyClient).filter(ProxyClient.id == client_id).first()
        if client is None:
            raise HTTPException(404, "کلاینت یافت نشد.")
        try:
            data = ClientUpdate(**payload)
        except Exception as exc:
            raise HTTPException(400, str(exc)[:200])
        if data.name is not None:
            client.name = data.name[:128]
        if data.enabled is not None:
            client.enabled = bool(data.enabled)
        if data.expires_at is not None:
            client.expires_at = _parse_expiry(data.expires_at) if data.expires_at else None
        if data.dedicated_port is not None:
            client.dedicated_port = data.dedicated_port
        if data.max_connections is not None:
            client.max_connections = max(1, min(data.max_connections, 100))
        if data.notes is not None:
            client.notes = data.notes[:512]
        client.updated_at = datetime.now(timezone.utc)
        db.commit()
        _audit(db, user, "client.update", client.username)
        try:
            rebuild_gateway(db)
        except Exception as exc:
            raise HTTPException(502, f"بازسازی gateway ناموفق بود: {exc}")
        return _public_client(client)
    finally:
        db.close()


@router.post("/api/clients/{client_id}/rotate")
def rotate_client(client_id: int, user: str = Depends(current_user)):
    db = db_session()
    try:
        client = db.query(ProxyClient).filter(ProxyClient.id == client_id).first()
        if client is None:
            raise HTTPException(404, "کلاینت یافت نشد.")
        password = generate_password()
        client.password_hash = make_password_hash(password)
        client.password_enc = seal_password(password)
        client.updated_at = datetime.now(timezone.utc)
        db.commit()
        _audit(db, user, "client.rotate", client.username)
        try:
            rebuild_gateway(db)
        except Exception as exc:
            raise HTTPException(502, f"بازسازی gateway ناموفق بود: {exc}")
        out = _public_client(client)
        out["password_once"] = password
        return out
    finally:
        db.close()


@router.delete("/api/clients/{client_id}")
def delete_client(client_id: int, user: str = Depends(current_user)):
    db = db_session()
    try:
        client = db.query(ProxyClient).filter(ProxyClient.id == client_id).first()
        if client is None:
            raise HTTPException(404, "کلاینت یافت نشد.")
        _audit(db, user, "client.delete", client.username)
        db.delete(client)
        db.commit()
        try:
            rebuild_gateway(db)
        except Exception as exc:
            raise HTTPException(502, f"بازسازی gateway ناموفق بود: {exc}")
        return {"ok": True}
    finally:
        db.close()


@router.get("/api/clients/{client_id}/qr.png")
def client_qr(client_id: int, request: Request, user: str = Depends(current_user)):
    """QR of a standard socks5://user:pass@host:port URI (no secrets in logs)."""
    try:
        import qrcode
    except ImportError:
        raise HTTPException(501, "QR پشتیبانی نمی‌شود (qrcode نصب نیست).")
    db = db_session()
    try:
        client = db.query(ProxyClient).filter(ProxyClient.id == client_id).first()
        if client is None:
            raise HTTPException(404, "کلاینت یافت نشد.")
        usable, why = client_usable(client)
        if not usable:
            raise HTTPException(400, f"کلاینت قابل استفاده نیست: {why}")
        from ..services.failover import session_get

        # Derive the public host from the request (works behind the
        # reverse proxy and avoids hardcoding deployment addresses).
        host = (request.headers.get("x-forwarded-host", "") or request.url.hostname or "")
        host = host.split(",")[0].strip().split(":")[0]
        if not host or any(c.isspace() for c in host) or "/" in host:
            raise HTTPException(400, "cannot determine public host for QR")
        port = int(session_get(db, "public_socks_port",
                               str(get_settings().public_socks_port)))
        password = open_password(client.password_enc)
        uri = f"socks5://{client.username}:{password}@{host}:{port}"
        img = qrcode.make(uri)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return Response(content=buf.getvalue(), media_type="image/png")
    finally:
        db.close()
