"""Subscription endpoints. The secret URL is never returned — only a masked preview."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..config import get_settings
from ..models import AuditLog, SubscriptionState
from ..schemas import SubscriptionIn
from ..services.failover import session_get, session_set
from ..services.subscription import mask_url, update_subscription
from .deps import current_user, db_session

router = APIRouter()


def _audit(db, actor: str, action: str, detail: str = "") -> None:
    db.add(AuditLog(actor=actor, action=action, detail=detail))
    db.commit()


@router.get("/api/subscription")
def get_subscription(user: str = Depends(current_user)):
    import json as _json
    from datetime import timedelta

    db = db_session()
    try:
        sub = db.query(SubscriptionState).first()
        url = get_settings().subscription_url
        interval = int(session_get(db, "sub_update_interval", str(get_settings().sub_update_interval)))
        try:
            stats = _json.loads(session_get(db, "last_sub_stats", "{}"))
        except Exception:
            stats = {}
        next_update = None
        if sub and sub.last_ok_at:
            try:
                next_update = (sub.last_ok_at + timedelta(seconds=interval)).isoformat()
            except Exception:
                next_update = None
        return {
            "configured": bool(url),
            "url_masked": mask_url(url) if url else "",
            "update_interval": sub.update_interval if sub else interval,
            "last_ok_at": sub.last_ok_at.isoformat() if sub and sub.last_ok_at else None,
            "last_attempt_at": sub.last_attempt_at.isoformat() if sub and sub.last_attempt_at else None,
            "next_update_at": next_update,
            "last_error": sub.last_error if sub else "",
            "node_count": sub.node_count if sub else 0,
            "last_stats": stats,
        }
    finally:
        db.close()


@router.put("/api/subscription")
def put_subscription(payload: SubscriptionIn, user: str = Depends(current_user)):
    # Persist URL to /etc/proxy-hub/proxy-hub.env (outside Git) — never in DB/logs.
    from pathlib import Path

    env_path = Path("/etc/proxy-hub/proxy-hub.env")
    try:
        lines = env_path.read_text().splitlines() if env_path.exists() else []
    except OSError as exc:
        raise HTTPException(500, f"cannot read env file: {exc}")
    found = False
    out = []
    for line in lines:
        if line.strip().startswith("PROXYHUB_SUBSCRIPTION_URL="):
            out.append(f"PROXYHUB_SUBSCRIPTION_URL={payload.url}")
            found = True
        else:
            out.append(line)
    if not found:
        out.append(f"PROXYHUB_SUBSCRIPTION_URL={payload.url}")
    try:
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text("\n".join(out) + "\n")
    except OSError as exc:
        raise HTTPException(500, f"cannot write env file: {exc}")
    import os as _os
    _os.environ["PROXYHUB_SUBSCRIPTION_URL"] = payload.url
    from ..config import refresh_settings as _refresh
    _refresh()
    db = db_session()
    try:
        if payload.update_interval:
            session_set(db, "sub_update_interval", str(payload.update_interval))
        _audit(db, user, "subscription.configure", mask_url(payload.url))
    finally:
        db.close()
    return {"ok": True, "url_masked": mask_url(payload.url)}


@router.post("/api/subscription/update")
def run_update(user: str = Depends(current_user)):
    import json as _json

    db = db_session()
    try:
        try:
            result = update_subscription(db)
        except Exception as exc:
            raise HTTPException(502, str(exc)[:300])
        session_set(db, "last_sub_stats", _json.dumps({
            "added": result.get("added", 0), "updated": result.get("updated", 0),
            "removed": result.get("removed", 0), "total": result.get("total", 0),
            "duplicates": result.get("duplicates", 0),
            "unsupported": result.get("unsupported", 0),
            "errors": result.get("errors", 0),
        }))
        _audit(db, user, "subscription.update", f"total={result.get('total')}")
        return {"ok": True, **result}
    finally:
        db.close()
