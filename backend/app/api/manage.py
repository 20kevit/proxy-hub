"""Routing + settings endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..config import get_settings
from ..models import AuditLog, RoutingRule
from ..schemas import RoutingRuleIn, SettingsIn
from ..services.failover import session_get, session_set
from ..services.routing import ensure_defaults, ordered_rules, proxy_env_snippet
from .deps import current_user, db_session

router = APIRouter()


def _audit(db, actor: str, action: str, detail: str = "") -> None:
    db.add(AuditLog(actor=actor, action=action, detail=detail))
    db.commit()


# ---- routing ----

@router.get("/api/routing")
def list_routing(user: str = Depends(current_user)):
    db = db_session()
    try:
        ensure_defaults(db)
        rules = ordered_rules(db)
        return {
            "rules": [
                {"id": r.id, "app": r.app, "route": r.route,
                 "position": r.position, "note": r.note or ""}
                for r in rules
            ],
            "proxy_env": proxy_env_snippet(),
            "note": "Explicit opt-in routing only. No transparent proxy, no global interception.",
        }
    finally:
        db.close()


@router.post("/api/routing")
def create_routing(payload: RoutingRuleIn, user: str = Depends(current_user)):
    db = db_session()
    try:
        ensure_defaults(db)
        if db.query(RoutingRule).filter(RoutingRule.app == payload.app).first():
            raise HTTPException(409, "rule for this app already exists")
        rule = RoutingRule(app=payload.app, route=payload.route,
                           position=payload.position, note=payload.note[:512])
        db.add(rule)
        db.commit()
        _audit(db, user, "routing.create", f"{payload.app}->{payload.route}")
        return {"id": rule.id, "app": rule.app, "route": rule.route,
                "position": rule.position, "note": rule.note}
    finally:
        db.close()


@router.put("/api/routing/{rule_id}")
def update_routing(rule_id: int, payload: RoutingRuleIn, user: str = Depends(current_user)):
    db = db_session()
    try:
        rule = db.query(RoutingRule).filter(RoutingRule.id == rule_id).first()
        if rule is None:
            raise HTTPException(404, "rule not found")
        rule.app = payload.app
        rule.route = payload.route
        rule.position = payload.position
        rule.note = payload.note[:512]
        db.commit()
        _audit(db, user, "routing.update", f"{payload.app}->{payload.route}")
        return {"id": rule.id, "app": rule.app, "route": rule.route,
                "position": rule.position, "note": rule.note}
    finally:
        db.close()


@router.delete("/api/routing/{rule_id}")
def delete_routing(rule_id: int, user: str = Depends(current_user)):
    db = db_session()
    try:
        rule = db.query(RoutingRule).filter(RoutingRule.id == rule_id).first()
        if rule is None:
            raise HTTPException(404, "rule not found")
        _audit(db, user, "routing.delete", rule.app)
        db.delete(rule)
        db.commit()
        return {"ok": True}
    finally:
        db.close()


# ---- settings ----

@router.get("/api/settings")
def get_settings_api(user: str = Depends(current_user)):
    db = db_session()
    try:
        s = get_settings()
        return {
            "sub_update_interval": int(session_get(db, "sub_update_interval", str(s.sub_update_interval))),
            "health_interval": int(session_get(db, "health_interval", str(s.health_interval))),
            "health_url": session_get(db, "health_url", s.health_url),
            "failover_mode": session_get(db, "failover_mode", s.failover_mode),
            "failover_threshold": int(session_get(db, "failover_threshold", str(s.failover_threshold))),
            "failover_cooldown": int(session_get(db, "failover_cooldown", str(s.failover_cooldown))),
            "managed_socks_port": s.socks_port,
            "managed_http_port": s.http_port,
        }
    finally:
        db.close()


@router.put("/api/settings")
def put_settings(payload: SettingsIn, user: str = Depends(current_user)):
    db = db_session()
    try:
        data = payload.model_dump(exclude_unset=True)
        allowed = {"sub_update_interval", "health_interval", "health_url",
                   "failover_mode", "failover_threshold", "failover_cooldown"}
        for key, value in data.items():
            if key in allowed and value is not None:
                if key == "failover_mode" and value not in ("auto", "manual"):
                    raise HTTPException(400, "failover_mode must be auto|manual")
                session_set(db, key, str(value))
        _audit(db, user, "settings.update", ",".join(data.keys()))
        return {"ok": True}
    finally:
        db.close()
