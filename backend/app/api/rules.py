"""Traffic routing rules: CRUD, reorder, simulate, default route."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..config import get_settings
from ..models import AuditLog, ProxyClient, RouteRule
from ..schemas import RouteRuleIn, SimulateIn
from ..services.cores import rebuild_gateway
from ..services.failover import session_get, session_set
from ..services.routing_engine import (
    ACTIONS,
    ensure_seed_rules,
    ordered_rules,
    rule_matches,
    simulate,
    validate_client_scope,
    validate_rule,
)
from .deps import current_user, db_session

router = APIRouter()


def _audit(db, actor: str, action: str, detail: str = "") -> None:
    db.add(AuditLog(actor=actor, action=action, detail=detail))
    db.commit()


def _public(rule: RouteRule) -> dict:
    return {
        "id": rule.id, "priority": rule.priority, "match_type": rule.match_type,
        "pattern": rule.pattern, "port": rule.port or "", "network": rule.network,
        "action": rule.action, "client_scope": rule.client_scope,
        "enabled": rule.enabled, "note": rule.note or "",
    }


@router.get("/api/rules")
def list_rules(user: str = Depends(current_user)):
    db = db_session()
    try:
        ensure_seed_rules(db)
        return {"rules": [_public(r) for r in ordered_rules(db)],
                "default_route": session_get(db, "default_route",
                                             get_settings().default_route)}
    finally:
        db.close()


@router.post("/api/rules")
def create_rule(payload: RouteRuleIn, user: str = Depends(current_user)):
    db = db_session()
    try:
        ensure_seed_rules(db)
        try:
            pattern, port = validate_rule(payload.match_type, payload.pattern,
                                          payload.port, payload.network, payload.action)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        scope = (payload.client_scope or "all").strip().lower()
        try:
            validate_client_scope(db, scope)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        rule = RouteRule(priority=payload.priority, match_type=payload.match_type,
                         pattern=pattern, port=port, network=payload.network,
                         action=payload.action, client_scope=scope,
                         enabled=payload.enabled, note=(payload.note or "")[:512])
        db.add(rule)
        db.commit()
        _audit(db, user, "rule.create", f"#{rule.priority} {pattern} -> {payload.action}")
        try:
            rebuild_gateway(db)
        except Exception as exc:
            raise HTTPException(502, f"قانون ذخیره شد اما بازسازی gateway ناموفق بود: {exc}")
        return _public(rule)
    finally:
        db.close()


@router.put("/api/rules/{rule_id}")
def update_rule(rule_id: int, payload: RouteRuleIn, user: str = Depends(current_user)):
    db = db_session()
    try:
        rule = db.query(RouteRule).filter(RouteRule.id == rule_id).first()
        if rule is None:
            raise HTTPException(404, "قانون یافت نشد.")
        try:
            pattern, port = validate_rule(payload.match_type, payload.pattern,
                                          payload.port, payload.network, payload.action)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        scope = (payload.client_scope or "all").strip().lower()
        try:
            validate_client_scope(db, scope)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        rule.priority = payload.priority
        rule.match_type = payload.match_type
        rule.pattern = pattern
        rule.port = port
        rule.network = payload.network
        rule.action = payload.action
        rule.client_scope = scope
        rule.enabled = payload.enabled
        rule.note = (payload.note or "")[:512]
        db.commit()
        _audit(db, user, "rule.update", f"#{rule.id}")
        try:
            rebuild_gateway(db)
        except Exception as exc:
            raise HTTPException(502, f"بازسازی gateway ناموفق بود: {exc}")
        return _public(rule)
    finally:
        db.close()


@router.delete("/api/rules/{rule_id}")
def delete_rule(rule_id: int, user: str = Depends(current_user)):
    db = db_session()
    try:
        rule = db.query(RouteRule).filter(RouteRule.id == rule_id).first()
        if rule is None:
            raise HTTPException(404, "قانون یافت نشد.")
        _audit(db, user, "rule.delete", f"#{rule.priority} {rule.pattern}")
        db.delete(rule)
        db.commit()
        try:
            rebuild_gateway(db)
        except Exception as exc:
            raise HTTPException(502, f"بازسازی gateway ناموفق بود: {exc}")
        return {"ok": True}
    finally:
        db.close()


@router.post("/api/rules/{rule_id}/duplicate")
def duplicate_rule(rule_id: int, user: str = Depends(current_user)):
    db = db_session()
    try:
        rule = db.query(RouteRule).filter(RouteRule.id == rule_id).first()
        if rule is None:
            raise HTTPException(404, "قانون یافت نشد.")
        copy = RouteRule(priority=rule.priority + 1, match_type=rule.match_type,
                         pattern=rule.pattern, port=rule.port, network=rule.network,
                         action=rule.action, client_scope=rule.client_scope,
                         enabled=False, note=(rule.note or "")[:512])
        db.add(copy)
        db.commit()
        _audit(db, user, "rule.duplicate", f"#{rule.id} -> #{copy.id}")
        return _public(copy)
    finally:
        db.close()


@router.post("/api/rules/{rule_id}/move")
def move_rule(rule_id: int, payload: dict, user: str = Depends(current_user)):
    """Move up/down by swapping priority with the neighbor."""
    direction = (payload or {}).get("direction", "")
    if direction not in ("up", "down"):
        raise HTTPException(400, "direction must be up|down")
    db = db_session()
    try:
        rules = ordered_rules(db)
        idx = next((i for i, r in enumerate(rules) if r.id == rule_id), None)
        if idx is None:
            raise HTTPException(404, "قانون یافت نشد.")
        other = rules[idx - 1] if direction == "up" and idx > 0 else \
            rules[idx + 1] if direction == "down" and idx < len(rules) - 1 else None
        if other is None:
            return {"ok": True, "moved": False}
        rule = rules[idx]
        rule.priority, other.priority = other.priority, rule.priority
        db.commit()
        _audit(db, user, "rule.move", f"#{rule.id} {direction}")
        try:
            rebuild_gateway(db)
        except Exception as exc:
            raise HTTPException(502, f"بازسازی gateway ناموفق بود: {exc}")
        return {"ok": True, "moved": True}
    finally:
        db.close()


@router.post("/api/routing/simulate")
def simulate_route(payload: SimulateIn, user: str = Depends(current_user)):
    db = db_session()
    try:
        from ..models import ProxyNode

        ensure_seed_rules(db)
        active = db.query(ProxyNode).filter(ProxyNode.is_active.is_(True)).first()
        return simulate(
            db, domain=payload.domain.strip(), ip=payload.ip.strip(),
            port=payload.port, network=payload.network or "any",
            client=(payload.client or "all").strip().lower() or "all",
            default_route=session_get(db, "default_route", get_settings().default_route),
            active_node_name=active.name if active else "",
        )
    finally:
        db.close()


@router.put("/api/routing/default")
def set_default(payload: dict, user: str = Depends(current_user)):
    route = (payload or {}).get("default_route", "")
    if route not in ACTIONS:
        raise HTTPException(400, "default_route must be direct|proxy|block")
    db = db_session()
    try:
        session_set(db, "default_route", route)
        _audit(db, user, "rule.default", route)
        try:
            rebuild_gateway(db)
        except Exception as exc:
            raise HTTPException(502, f"بازسازی gateway ناموفق بود: {exc}")
        return {"ok": True, "default_route": route}
    finally:
        db.close()
