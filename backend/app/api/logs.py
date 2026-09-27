"""Audit log viewer with filters. Secrets are never stored in audit rows."""
from __future__ import annotations

from fastapi import APIRouter, Depends

from ..models import AuditLog
from .deps import current_user, db_session

router = APIRouter()


@router.get("/api/audit")
def list_audit(action: str = "", actor: str = "", limit: int = 100,
               user: str = Depends(current_user)):
    db = db_session()
    try:
        q = db.query(AuditLog).order_by(AuditLog.id.desc())
        if action:
            q = q.filter(AuditLog.action == action[:64])
        if actor:
            q = q.filter(AuditLog.actor == actor[:64])
        rows = q.limit(max(1, min(limit, 500))).all()
        return [
            {"id": e.id, "actor": e.actor, "action": e.action, "detail": e.detail,
             "at": e.created_at.isoformat() if e.created_at else None}
            for e in rows
        ]
    finally:
        db.close()
