"""Routing + settings endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..config import get_settings
from ..models import AuditLog, RoutingRule
from ..schemas import RoutingRuleIn, SettingsIn
from ..services.failover import session_get, session_set, get_failover_config, set_failover_config
from ..services.lifecycle import lifecycle_manager
from ..services.routing import ensure_defaults, ordered_rules, proxy_env_snippet
from ..services.health_v2 import get_cycle_status as get_health_cycle_status
from ..services.watchdog import watchdog
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
        fo = get_failover_config(db)
        return {
            "sub_update_interval": int(session_get(db, "sub_update_interval", str(s.sub_update_interval))),
            "health_interval": int(session_get(db, "health_interval", str(s.health_interval))),
            "active_probe_interval": int(session_get(db, "active_probe_interval", "10")),
            "deep_probe_interval": int(session_get(db, "deep_probe_interval", "60")),
            "health_url": session_get(db, "health_url", s.health_url),
            "health_timeout": int(session_get(db, "health_timeout", str(s.health_timeout))),
            "failover_mode": fo["mode"],
            "failover_threshold": fo["threshold"],
            "failover_cooldown": fo["cooldown"],
            "failover_min_healthy": fo["min_healthy"],
            "rotation_enabled": session_get(db, "rotation_enabled", "true").lower() in ("1", "true", "yes", "on"),
            "rotation_max_duration_hours": int(session_get(db, "rotation_max_duration_hours", "6")),
            "rotation_exclusion_hours": int(session_get(db, "rotation_exclusion_hours", "24")),
            "rotation_dry_run": session_get(db, "rotation_dry_run", "false").lower() in ("1", "true", "yes", "on"),
            "rotation_check_interval": int(session_get(db, "rotation_check_interval", "300")),
            "managed_socks_port": s.socks_port,
            "managed_http_port": s.http_port,
            "gateway_enabled": session_get(db, "gateway_enabled", str(s.gateway_enabled)).lower() in ("1", "true", "yes", "on"),
            "gateway_bind": session_get(db, "gateway_bind", s.gateway_bind),
            "public_socks_port": int(session_get(db, "public_socks_port", str(s.public_socks_port))),
            "public_http_port": int(session_get(db, "public_http_port", str(s.public_http_port))),
            "default_route": session_get(db, "default_route", s.default_route),
        }
    finally:
        db.close()


@router.put("/api/settings")
def put_settings(payload: SettingsIn, user: str = Depends(current_user)):
    db = db_session()
    try:
        data = payload.model_dump(exclude_unset=True)
        allowed = {"sub_update_interval", "health_interval", "active_probe_interval", "deep_probe_interval",
                   "health_url", "health_timeout",
                   "failover_mode", "failover_threshold", "failover_cooldown", "failover_min_healthy",
                   "rotation_enabled", "rotation_max_duration_hours", "rotation_exclusion_hours",
                   "rotation_dry_run", "rotation_check_interval",
                   "gateway_enabled", "gateway_bind", "public_socks_port",
                   "public_http_port", "default_route"}
        gw_changed = False
        fo_changed = False
        for key, value in data.items():
            if key in allowed and value is not None:
                if key == "failover_mode" and value not in ("auto", "manual"):
                    raise HTTPException(400, "failover_mode must be auto|manual")
                if key == "gateway_bind" and value not in ("127.0.0.1", "0.0.0.0"):
                    raise HTTPException(400, "gateway_bind must be 127.0.0.1 or 0.0.0.0")
                if key == "default_route" and value not in ("direct", "proxy", "block"):
                    raise HTTPException(400, "default_route must be direct|proxy|block")
                if key in ("public_socks_port", "public_http_port"):
                    value = int(value)
                    if not 1024 <= value <= 65535:
                        raise HTTPException(400, "port out of range 1024-65535")
                if key in ("gateway_enabled", "gateway_bind", "public_socks_port",
                           "public_http_port", "default_route"):
                    gw_changed = True
                if key in ("failover_mode", "failover_threshold", "failover_cooldown", "failover_min_healthy"):
                    fo_changed = True
                session_set(db, key, str(value).lower() if isinstance(value, bool) else str(value))
        _audit(db, user, "settings.update", ",".join(data.keys()))
        if gw_changed:
            from ..services.cores import rebuild_gateway

            try:
                rebuild_gateway(db)
            except Exception as exc:
                raise HTTPException(502, f"تنظیمات ذخیره شد اما بازسازی gateway ناموفق بود: {exc}")
        return {"ok": True, "failover": get_failover_config(db) if fo_changed else None}
    finally:
        db.close()


@router.get("/api/failover/config")
def get_failover_config_api(user: str = Depends(current_user)):
    db = db_session()
    try:
        return get_failover_config(db)
    finally:
        db.close()


@router.put("/api/failover/config")
def put_failover_config_api(payload: dict, user: str = Depends(current_user)):
    db = db_session()
    try:
        return set_failover_config(db, **payload)
    finally:
        db.close()


# ---- lifecycle/rotation ----

@router.get("/api/lifecycle/status")
def get_lifecycle_status(user: str = Depends(current_user)):
    db = db_session()
    try:
        return lifecycle_manager.get_rotation_status(db)
    finally:
        db.close()


@router.post("/api/lifecycle/rotate")
async def trigger_rotation(user: str = Depends(current_user)):
    try:
        result = await lifecycle_manager.force_rotation()
        return result
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ---- watchdog ----

@router.get("/api/watchdog/status")
def get_watchdog_status(user: str = Depends(current_user)):
    return watchdog.get_status()


@router.get("/api/health/cycles")
def get_cycle_status(user: str = Depends(current_user)):
    return get_health_cycle_status()